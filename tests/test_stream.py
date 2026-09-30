"""SignalStream against a real local WebSocket server and a mocked REST API."""

import asyncio
import json
from http import HTTPStatus

import httpx
import pytest
from websockets.asyncio.server import serve

from lpsignal import AsyncLPSignal, FileLastIdStore, MemoryLastIdStore, SignalStream

from conftest import sig, signals_api


class Server:
    """Each connection runs the next scripted behaviour; records paths and headers."""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.paths: list[str] = []
        self.auth: list[str] = []
        self.srv = None

    async def __aenter__(self):
        async def process_request(conn, request):
            self.paths.append(request.path)
            self.auth.append(request.headers.get("authorization", ""))
            script = self.scripts[0] if self.scripts else None
            if isinstance(script, int):  # refuse the upgrade with this HTTP status
                self.scripts.pop(0)
                return conn.respond(HTTPStatus(script), "refused\n")
            return None

        async def handler(ws):
            script = self.scripts.pop(0) if self.scripts else None
            if script:
                await script(ws)
            else:
                await ws.wait_closed()

        self.srv = await serve(handler, "127.0.0.1", 0, process_request=process_request)
        self.port = self.srv.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.srv.close()
        await self.srv.wait_closed()


def frame(i, replay=False):
    return json.dumps({"type": "signal", **({"replay": True} if replay else {}), "signal": sig(i)})


async def until(cond, timeout=3.0):
    async def wait():
        while not cond():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(wait(), timeout)


def make(port, table, store=None, since=None, on_signal=None, calls_out=None):
    handler, calls = signals_api(table)
    if calls_out is not None:
        calls_out.append(calls)
    client = AsyncLPSignal(api_key="lps_test", base_url=f"http://127.0.0.1:{port}",
                           http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    got, events = [], []

    async def on(s, source):
        if on_signal:
            r = on_signal(s, source)
            if asyncio.iscoroutine(r):
                await r
        got.append((s["id"], source))

    stream = SignalStream(client, on, on_event=events.append, store=store or MemoryLastIdStore(), since=since,
                          min_backoff=0.01, max_backoff=0.05)
    return stream, got, events


async def test_first_run_anchors_then_delivers_live():
    async def script(ws):
        await ws.send(json.dumps({"type": "ready"}))
        for i in (3, 4, 5):
            await ws.send(frame(i))
        await ws.wait_closed()

    async with Server([script]) as srv:
        stream, got, events = make(srv.port, [1, 2, 3])
        await stream.start()
        await until(lambda: len(got) == 2)
        assert got == [("4", "live"), ("5", "live")]
        assert srv.paths[0] == "/v1/stream?since=3"
        assert srv.auth[0] == "Bearer lps_test"
        assert {"type": "anchored", "last_id": "3"} in events
        assert any(e["type"] == "live" for e in events)
        await stream.stop()


async def test_restart_catches_up_over_rest_then_dedupes_replay():
    async def script(ws):
        for i in (179, 180, 181):
            await ws.send(frame(i, replay=True))
        await ws.send(json.dumps({"type": "ready"}))
        await ws.send(frame(182))
        await ws.wait_closed()

    store = MemoryLastIdStore()
    store.save("5")
    async with Server([script]) as srv:
        stream, got, _ = make(srv.port, list(range(1, 181)), store=store)
        await stream.start()
        await until(lambda: len(got) == 177)
        assert [g[0] for g in got[:175]] == [str(i) for i in range(6, 181)]
        assert all(g[1] == "rest" for g in got[:175])
        assert got[-2:] == [("181", "replay"), ("182", "live")]
        assert srv.paths[0] == "/v1/stream?since=180"
        assert store.load() == "182"
        await stream.stop()


async def test_failing_handler_keeps_position():
    state = {"fail": True}

    def on(s, _source):
        if s["id"] == "3" and state["fail"]:
            state["fail"] = False
            raise RuntimeError("db down")

    table = [1]

    async def first(ws):
        await ws.send(json.dumps({"type": "ready"}))
        for i in (2, 3, 4):
            table.append(i)
            await ws.send(frame(i))
        await ws.wait_closed()

    async with Server([first]) as srv:
        stream, got, events = make(srv.port, table, on_signal=on)
        await stream.start()
        await until(lambda: len(srv.paths) == 2)
        await until(lambda: len(got) == 3)
        assert got == [("2", "live"), ("3", "rest"), ("4", "rest")]
        assert srv.paths[1] == "/v1/stream?since=4"
        assert any(e["type"] == "error" and str(e["error"]) == "db down" for e in events)
        await stream.stop()


async def test_reconnects_after_4409_with_last_id():
    async def truncated(ws):
        await ws.send(frame(11, replay=True))
        await ws.send(json.dumps({"type": "replay_truncated", "lastId": "11"}))
        await ws.close(4409, "replay incomplete")

    async with Server([truncated]) as srv:
        stream, got, _ = make(srv.port, [10])
        await stream.start()
        await until(lambda: len(srv.paths) == 2)
        assert srv.paths[1] == "/v1/stream?since=11"
        assert got == [("11", "replay")]
        await stream.stop()


@pytest.mark.parametrize("kill", ["4402", 401, 402])
async def test_fatal_stops_for_good(kill):
    async def expire(ws):
        await ws.close(4402, "plan expired")

    async with Server([expire if kill == "4402" else kill]) as srv:
        stream, _, events = make(srv.port, [1])
        await stream.start()
        await until(lambda: any(e["type"] == "fatal" for e in events))
        await asyncio.sleep(0.1)
        assert len(srv.paths) == 1
        await stream.stop()


async def test_retries_on_429():
    async with Server([429]) as srv:
        stream, _, events = make(srv.port, [1])
        await stream.start()
        await until(lambda: len(srv.paths) == 2)
        assert not any(e["type"] == "fatal" for e in events)
        await stream.stop()


async def test_since_only_when_store_empty():
    async with Server([]) as srv:
        stream, got, _ = make(srv.port, [1, 2, 3, 4], since="2")
        await stream.start()
        await until(lambda: len(srv.paths) == 1)
        assert got == [("3", "rest"), ("4", "rest")]
        await stream.stop()


async def test_stop_waits_for_handler():
    release = asyncio.Event()
    started = asyncio.Event()

    async def on(_s, _src):
        started.set()
        await release.wait()

    async def script(ws):
        await ws.send(frame(2))
        await ws.wait_closed()

    async with Server([script]) as srv:
        stream, got, _ = make(srv.port, [1], on_signal=on)
        await stream.start()
        await asyncio.wait_for(started.wait(), 3)
        stopper = asyncio.create_task(stream.stop())
        await asyncio.sleep(0.05)
        assert not stopper.done()
        release.set()
        await asyncio.wait_for(stopper, 3)
        assert got == [("2", "live")]


def test_requires_key():
    with pytest.raises(ValueError):
        SignalStream(AsyncLPSignal(), lambda s, src: None)


def test_file_store_roundtrip(tmp_path):
    p = tmp_path / "sub" / "state.json"
    s = FileLastIdStore(p)
    assert s.load() is None
    s.save("42")
    assert s.load() == "42"
    assert json.loads(p.read_text()) == {"lastId": "42"}


async def test_catch_up_repeats_until_nothing_new():
    table = [1, 2, 3]
    store = MemoryLastIdStore()
    store.save("1")

    def on(s, _src):
        if s["id"] == "3" and 5 not in table:  # more signals appear while the backlog is handled
            table.extend([4, 5])

    async with Server([]) as srv:
        stream, got, _ = make(srv.port, table, store=store, on_signal=on)
        await stream.start()
        await until(lambda: len(srv.paths) == 1)
        assert got == [("2", "rest"), ("3", "rest"), ("4", "rest"), ("5", "rest")]
        assert srv.paths[0] == "/v1/stream?since=5"
        await stream.stop()


async def test_stop_during_slow_handshake_returns_promptly():
    class SlowServer(Server):
        async def __aenter__(self):
            async def process_request(conn, request):
                self.paths.append(request.path)
                await asyncio.sleep(2)  # hold the handshake open
                return None

            async def handler(ws):
                await ws.send(frame(9))
                await ws.wait_closed()

            self.srv = await serve(handler, "127.0.0.1", 0, process_request=process_request)
            self.port = self.srv.sockets[0].getsockname()[1]
            return self

    async with SlowServer([]) as srv:
        stream, got, _ = make(srv.port, [1])
        await stream.start()
        await until(lambda: len(srv.paths) == 1)
        t0 = asyncio.get_running_loop().time()
        await asyncio.wait_for(stream.stop(), 1)
        assert asyncio.get_running_loop().time() - t0 < 0.5
        await asyncio.sleep(2.2)  # the handshake would have completed by now
        assert got == []


async def test_failed_save_offers_the_signal_again():
    class FlakyStore(MemoryLastIdStore):
        fail = True

        def save(self, last_id):
            if last_id == "2" and self.fail:
                self.fail = False
                raise OSError("disk full")
            super().save(last_id)

    table = [1]

    async def first(ws):
        table.append(2)
        await ws.send(frame(2))
        await ws.wait_closed()

    store = FlakyStore()
    async with Server([first]) as srv:
        stream, got, _ = make(srv.port, table, store=store)
        await stream.start()
        await until(lambda: len(srv.paths) == 2)
        await until(lambda: len(got) == 2)
        assert got == [("2", "live"), ("2", "rest")]
        assert store.load() == "2"
        await stream.stop()


async def test_frames_after_stop_are_not_delivered():
    release = asyncio.Event()

    async def on(s, _src):
        if s["id"] == "2":
            await release.wait()

    async def script(ws):
        await ws.send(frame(2))
        await ws.send(frame(3))
        await ws.wait_closed()

    async with Server([script]) as srv:
        stream, got, _ = make(srv.port, [1], on_signal=on)
        await stream.start()
        await until(lambda: stream._idle.is_set() is False)
        stopper = asyncio.create_task(stream.stop())
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.wait_for(stopper, 3)
        assert got == [("2", "live")]
        assert stream.position == "2"


async def test_cancelling_run_stops_the_stream():
    async with Server([]) as srv:
        stream, _, _ = make(srv.port, [1])
        runner = asyncio.create_task(stream.run())
        await until(lambda: len(srv.paths) == 1)
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner
        await asyncio.sleep(0.1)
        assert len(srv.paths) == 1


async def test_anchor_is_saved_before_anything_is_consumed_and_never_repicked():
    class FlakyStore(MemoryLastIdStore):
        fail = True

        def save(self, last_id):
            if self.fail:
                self.fail = False
                raise OSError("disk full")
            super().save(last_id)

    table = [1, 2, 3]
    store = FlakyStore()
    async with Server([]) as srv:
        stream, got, events = make(srv.port, table, store=store)
        await stream.start()
        await until(lambda: any(e["type"] == "error" and str(e["error"]) == "disk full" for e in events))
        assert srv.paths == []
        table.append(4)  # a newer signal appears before the retry
        await until(lambda: len(srv.paths) == 1)
        assert {"type": "anchored", "last_id": "3"} in events
        assert got == [("4", "rest")]
        assert srv.paths[0] == "/v1/stream?since=4"
        await stream.stop()


async def test_start_during_stop_waits_and_handlers_never_overlap():
    release = asyncio.Event()
    state = {"in": 0, "max": 0}

    async def on(s, _src):
        state["in"] += 1
        state["max"] = max(state["max"], state["in"])
        if s["id"] == "2":
            await release.wait()
        state["in"] -= 1

    table = [1]

    async def first(ws):
        table.append(2)
        await ws.send(frame(2))
        await ws.wait_closed()

    async with Server([first]) as srv:
        stream, got, _ = make(srv.port, table, on_signal=on)
        runner = asyncio.create_task(stream.run())
        await until(lambda: state["in"] == 1)
        stop1 = asyncio.create_task(stream.stop())
        stop2 = asyncio.create_task(stream.stop())
        restart = asyncio.create_task(stream.start())
        await asyncio.sleep(0.05)
        assert len(srv.paths) == 1 and not restart.done()
        release.set()
        await asyncio.wait_for(asyncio.gather(stop1, stop2, restart), 3)
        await asyncio.wait_for(runner, 1)  # the first run() returns once its own run has ended
        await until(lambda: len(srv.paths) == 2)
        assert srv.paths[1] == "/v1/stream?since=2"
        assert state["max"] == 1
        assert got == [("2", "live")]
        await stream.stop()


async def test_rest_anchor_and_catch_up_ask_for_what_the_socket_delivers():
    async def script(ws):
        await ws.send(json.dumps({"type": "ready"}))
        await ws.wait_closed()

    store = MemoryLastIdStore()
    store.save("2")
    async with Server([script]) as srv:
        out = []
        stream, got, _ = make(srv.port, [1, 2, 3, 4], store=store, calls_out=out)
        await stream.start()
        await until(lambda: len(got) == 2)
        assert out[0] and all(r.url.params.get("source") == "subscribed" for r in out[0])
        await stream.stop()
