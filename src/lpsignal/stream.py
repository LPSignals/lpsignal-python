from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Optional, Protocol, Union

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from .client import AsyncLPSignal, LPSignalError
from .types import Signal

Source = Literal["rest", "replay", "live"]


class LastIdStore(Protocol):
    """Where the stream is: the id of the last signal handled. Persist it to resume after a restart.

    To make a crash unable to repeat a signal's side effects, keep the id in the same database transaction as those
    effects (write it inside on_signal) and pass a store over that same row. Its save() must only move forward
    (e.g. `SET last_id = GREATEST(last_id, %s)`): the stream also saves the starting point with it.
    """

    def load(self) -> Optional[str]: ...
    def save(self, last_id: str) -> None: ...


class MemoryLastIdStore:
    def __init__(self) -> None:
        self._id: Optional[str] = None

    def load(self) -> Optional[str]:
        return self._id

    def save(self, last_id: str) -> None:
        self._id = last_id


class FileLastIdStore:
    """A small JSON file, replaced atomically (temp file + os.replace). Same format as the Node SDK's."""

    def __init__(self, path: Union[str, Path]) -> None:
        self.path = Path(path)

    def load(self) -> Optional[str]:
        try:
            data = json.loads(self.path.read_text("utf8"))
        except FileNotFoundError:
            return None
        last = data.get("lastId") if isinstance(data, dict) else None
        if not isinstance(last, str) or not last.isdigit():
            raise ValueError(f"{self.path}: no valid lastId")
        return last

    def save(self, last_id: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"lastId": last_id}), "utf8")
        os.replace(tmp, self.path)


OnSignal = Callable[[Signal, Source], Union[None, Awaitable[None]]]
OnEvent = Callable[[dict[str, Any]], None]


class SignalStream:
    """Live signals in id order, with no gaps across disconnects, outages and restarts.

    The server replays the last 24 hours on `?since=<id>`. Before every connection this stream first fetches
    everything newer than its position over REST, repeating until a pass finds nothing new, so an outage of any length
    is filled and the socket opens seconds after the last fetch. Anything at or below the position is dropped, so
    within one running process each signal reaches the handler once; across a crash the last one may be offered again.

    `on_signal(signal, source)` is called in ascending id order, never concurrently; `source` is "rest", "replay" or
    "live". The position is saved only after the handler returns: if it raises, the connection is dropped and the
    signal is offered again. Delivery is at-least-once across crashes, so make the handler idempotent on
    `signal["id"]` (or keep the position in your own database, see LastIdStore).

    `on_event(event)` receives dicts with a `type`: anchored, caught_up, connecting, connected, live, disconnected,
    error, fatal (the stream has stopped: bad key, or the plan does not include the stream).
    """

    def __init__(self, client: AsyncLPSignal, on_signal: OnSignal, on_event: Optional[OnEvent] = None,
                 store: Optional[LastIdStore] = None, since: Optional[str] = None, ping_interval: float = 30.0,
                 min_backoff: float = 1.0, max_backoff: float = 30.0):
        if not client.api_key:
            raise ValueError("SignalStream: the client needs an API key (the stream is for paid plans)")
        if since is not None and not str(since).isdigit():
            raise ValueError("SignalStream: since must be a signal id")
        self._client = client
        self._on_signal = on_signal
        self._on_event = on_event
        self._store = store or MemoryLastIdStore()
        self._since = since
        self._ping = ping_interval
        self._min_backoff = min_backoff
        self._max_backoff = max_backoff
        self._last_id: Optional[str] = None
        self._running = False
        self._task: Optional[asyncio.Task[None]] = None
        self._ws: Optional[ClientConnection] = None
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()  # set while no handler is running
        self._idle.set()
        self._done = asyncio.Event()
        self._stopping: Optional[asyncio.Task[None]] = None
        # the first-run starting point, chosen once and kept until it is saved (a retry must not pick a newer one)
        self._anchor: Optional[str] = None

    @property
    def position(self) -> Optional[str]:
        """Id of the last signal handed to on_signal (None before the first connection)."""
        return self._last_id

    async def start(self) -> None:
        """Load the saved position and start in the background. Call stop() to end.

        A no-op while running; during a stop() it starts once the stop has finished. After a `fatal` event, call
        stop() before starting again.
        """
        while self._stopping is not None:
            await asyncio.shield(self._stopping)
        if self._task is not None:
            return
        self._last_id = self._store.load() or self._since
        self._running = True
        self._wake.clear()
        self._done = asyncio.Event()  # each run has its own: run() must not return when an older run ends
        self._task = asyncio.create_task(self._loop(self._done))

    async def run(self) -> None:
        """Start and run until stop() or a fatal error. Cancelling run() stops the stream."""
        await self.start()
        done = self._done
        try:
            await done.wait()
        except asyncio.CancelledError:
            await self.stop()
            raise

    async def stop(self) -> None:
        """Wait for the signal being handled (if any), then close the connection. The stream can be started again.

        Don't await it from inside on_signal: it would wait for itself.
        """
        if self._stopping is None:
            if self._task is None:
                return
            self._stopping = asyncio.create_task(self._shutdown())
        # every caller waits for the same, complete shutdown; a cancelled caller does not cancel it
        await asyncio.shield(self._stopping)

    async def _shutdown(self) -> None:
        try:
            self._running = False
            self._wake.set()
            task = self._task
            assert task is not None
            # never interrupt a handler; anywhere else (REST, handshake, waiting for frames) the loop can be cancelled
            await self._idle.wait()
            if self._ws is not None:
                with contextlib.suppress(Exception):
                    await self._ws.close()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        finally:
            self._task = None
            self._stopping = None

    def _emit(self, event: dict[str, Any]) -> None:
        if self._on_event:
            with contextlib.suppress(Exception):  # a listener must not break the stream
                self._on_event(event)

    def _fatal(self, error: Exception) -> None:
        self._running = False
        self._emit({"type": "fatal", "error": error})

    async def _loop(self, done: asyncio.Event) -> None:
        try:
            await self._loop_body()
        finally:
            done.set()

    async def _loop_body(self) -> None:
        backoff = self._min_backoff
        while self._running:
            healthy = False
            try:
                await self._catch_up()
                if self._running:
                    healthy = await self._connect_once()
            except asyncio.CancelledError:
                raise
            except LPSignalError as e:
                if e.status == 401:
                    self._fatal(e)
                else:
                    self._emit({"type": "error", "error": e})
            except Exception as e:  # network errors, timeouts: retry
                self._emit({"type": "error", "error": e})
            if not self._running:
                break
            backoff = self._min_backoff if healthy else min(backoff * 2, self._max_backoff)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=backoff)

    async def _catch_up(self) -> None:
        """Establish a position (first run) or fetch everything after it over REST."""
        if self._last_id is None:
            # nothing is consumed until the starting point is saved: a restart must resume from this same point
            if self._anchor is None:
                page = await self._client.signals(limit=1)
                self._anchor = page["signals"][0]["id"] if page["signals"] else "0"
            self._store.save(self._anchor)
            self._last_id = self._anchor
            self._emit({"type": "anchored", "last_id": self._last_id})
        # repeat until a pass finds nothing: a long backlog can take longer than the server's 24h replay window, and
        # whatever was created meanwhile must come from REST too, or the socket would skip it
        delivered = 0
        while self._running:
            batch = await self._client.signals_after(self._last_id)
            if not batch:
                break
            for s in batch:
                if not self._running:
                    return
                if await self._deliver(s, "rest"):
                    delivered += 1
        if delivered:
            self._emit({"type": "caught_up", "delivered": delivered})

    async def _connect_once(self) -> bool:
        """One connection's life. Returns True when it went live."""
        url = f"{self._client.stream_url}?since={self._last_id}"
        self._emit({"type": "connecting", "url": url})
        live = failed = False
        try:
            async with connect(url, additional_headers={"authorization": f"Bearer {self._client.api_key}"},
                               ping_interval=self._ping, ping_timeout=self._ping, open_timeout=15) as ws:
                self._ws = ws
                if not self._running:
                    return False
                self._emit({"type": "connected"})
                try:
                    async for raw in ws:
                        try:
                            frame = json.loads(raw)
                        except ValueError:
                            continue
                        if not isinstance(frame, dict):
                            continue
                        if frame.get("type") == "signal" and isinstance(frame.get("signal"), dict):
                            try:
                                await self._deliver(frame["signal"], "replay" if frame.get("replay") else "live")
                            except Exception as e:  # keep the position: the signal is offered again
                                self._emit({"type": "error", "error": e})
                                failed = True
                                break
                        elif frame.get("type") == "ready":
                            live = True
                            self._emit({"type": "live"})
                        # replay_truncated / replay_failed: the server closes with 4409; we resume from the position
                except ConnectionClosed:
                    pass
            code, reason = ws.close_code or 1006, ws.close_reason or ""
        except InvalidStatus as e:
            status = e.response.status_code
            if status == 401:
                self._fatal(Exception("stream refused (401): the API key is invalid or was replaced"))
            elif status == 402:
                self._fatal(Exception("stream refused (402): the plan does not include the stream"))
            else:
                self._emit({"type": "error", "error": Exception(
                    f"stream refused ({status}){': too many connections for this key' if status == 429 else ''}")})
            return False
        finally:
            self._ws = None
        self._emit({"type": "disconnected", "code": code, "reason": reason})
        if code == 4402:
            self._fatal(Exception("stream closed (4402): the plan no longer includes the stream"))
        return live and not failed

    async def _deliver(self, s: Signal, source: Source) -> bool:
        """True when the signal was new and handed to on_signal."""
        if not self._running:  # after stop(), nothing new reaches the handler
            return False
        if self._last_id is not None and int(s["id"]) <= int(self._last_id):
            return False
        self._idle.clear()
        try:
            r = self._on_signal(s, source)
            if inspect.isawaitable(r):
                await r
        finally:
            self._idle.set()
        # the in-memory position follows the saved one: a failed save offers the signal again
        self._store.save(str(s["id"]))
        self._last_id = str(s["id"])
        return True
