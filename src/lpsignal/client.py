from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator, Iterator, Literal, Optional
from urllib.parse import quote

import httpx

from .types import (
    Backtest, BillingStatus, ChainStatus, Follow, Leaderboard, Me, PoolDetail, PoolHour, PoolsPage, Signal,
    SignalsPage, TelegramLink, WalletPositions, WebhookRegistration,
)

DEFAULT_BASE_URL = "https://api.lpsignal.app"
_IDEMPOTENT = {"GET", "PUT", "DELETE"}
_MAX_RETRY_WAIT = 60.0


class LPSignalError(Exception):
    """A non-2xx answer from the API. `code` is the API's stable `error` field (e.g. `rate_limited`)."""

    def __init__(self, status: int, body: Any, request_id: Optional[str] = None):
        self.status = status
        self.body = body
        self.request_id = request_id
        self.code: Optional[str] = body.get("error") if isinstance(body, dict) and isinstance(body.get("error"), str) else None
        detail = body if isinstance(body, str) else json.dumps(body)
        super().__init__(f"LPSignal API {status}{' ' + self.code if self.code else ''}"
                         f"{f' (request {request_id})' if request_id else ''}: {detail}")


def _q(v: str) -> str:
    return quote(str(v), safe="")


class _Base:
    def __init__(self, api_key: Optional[str], base_url: str, timeout: float, max_retries: int):
        if api_key is not None and not api_key.startswith("lps_"):
            raise ValueError('api_key must start with "lps_"')
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries

    @property
    def stream_url(self) -> str:
        """The stream URL derived from the base URL (https → wss)."""
        return ("ws" + self.base_url[4:] if self.base_url.startswith("http") else self.base_url) + "/v1/stream"

    @property
    def _headers(self) -> dict[str, str]:
        h = {"accept": "application/json"}
        if self.api_key:
            h["authorization"] = f"Bearer {self.api_key}"
        return h

    @staticmethod
    def _params(query: Optional[dict[str, Any]]) -> dict[str, Any]:
        return {k: v for k, v in (query or {}).items() if v is not None}

    @staticmethod
    def _body(res: httpx.Response) -> Any:
        text = res.text
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return text

    def _retry_wait(self, method: str, res: httpx.Response, body: Any, attempt: int) -> Optional[float]:
        """Seconds to wait before retrying, or None to raise."""
        if res.status_code != 429 or method not in _IDEMPOTENT or attempt >= self.max_retries:
            return None
        raw = res.headers.get("retry-after") or (body.get("retryAfterSec") if isinstance(body, dict) else None)
        try:
            after = float(raw) if raw is not None else 1.0
        except (TypeError, ValueError):
            after = 1.0
        return min(_MAX_RETRY_WAIT, after if after > 0 else 1.0)

    @staticmethod
    def _pools_query(chain, pair_class, window, min_tvl_usd, limit, offset) -> dict[str, Any]:
        return {"chain": chain, "class": pair_class, "window": window, "minTvlUsd": min_tvl_usd, "limit": limit, "offset": offset}


class LPSignal(_Base):
    """Synchronous client for the LPSignal API. `api_key` is optional: public endpoints work without one
    (opportunity signals then arrive 24h late)."""

    def __init__(self, api_key: Optional[str] = None, base_url: str = DEFAULT_BASE_URL, timeout: float = 15.0,
                 max_retries: int = 2, http: Optional[httpx.Client] = None):
        super().__init__(api_key, base_url, timeout, max_retries)
        self._http = http or httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "LPSignal":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def request(self, method: str, path: str, query: Optional[dict[str, Any]] = None, body: Any = None) -> Any:
        attempt = 0
        while True:
            res = self._http.request(method, self.base_url + path, params=self._params(query), headers=self._headers,
                                     json=body)
            parsed = self._body(res)
            if res.is_success:
                return parsed
            wait = self._retry_wait(method, res, parsed, attempt)
            if wait is None:
                raise LPSignalError(res.status_code, parsed, res.headers.get("x-request-id"))
            time.sleep(wait)
            attempt += 1

    # ── status
    def health(self) -> dict[str, Any]:
        return self.request("GET", "/v1/health")

    def chains(self) -> list[ChainStatus]:
        """Scan progress per chain."""
        return self.request("GET", "/v1/chains")["chains"]

    # ── pools
    def pools(self, chain: Optional[str] = None, pair_class: Optional[str] = None, window: Optional[int] = None,
              min_tvl_usd: Optional[float] = None, limit: Optional[int] = None, offset: Optional[int] = None) -> PoolsPage:
        """Active pools ranked by their best range's net APR over `window` hours (24, 168 or 720)."""
        return self.request("GET", "/v1/pools", self._pools_query(chain, pair_class, window, min_tvl_usd, limit, offset))

    def pool(self, chain: str, address: str) -> PoolDetail:
        """One pool with every (window, range) metric. `address` is the pool id for Uniswap v4."""
        return self.request("GET", f"/v1/pools/{_q(chain)}/{_q(address)}")

    def pool_hours(self, chain: str, address: str, hours: Optional[int] = None) -> list[PoolHour]:
        """Hourly aggregates, oldest first (at most 720 hours)."""
        return self.request("GET", f"/v1/pools/{_q(chain)}/{_q(address)}/hours", {"hours": hours})["hours"]

    def backtest(self, chain: str, address: str, range_pct: float, days: Optional[int] = None) -> Backtest:
        """Backtest a symmetric ±`range_pct`% range (0 = full range) over the last `days` (1..30, default 7)."""
        return self.request("GET", f"/v1/pools/{_q(chain)}/{_q(address)}/backtest", {"rangePct": range_pct, "days": days})

    # ── signals
    def signals(self, kind: Optional[str] = None, limit: Optional[int] = None, before: Optional[str] = None) -> SignalsPage:
        """One page of signals, newest first."""
        return self.request("GET", "/v1/signals", {"kind": kind, "limit": limit, "before": before})

    def signal_stats(self, days: Optional[int] = None) -> dict[str, Any]:
        """The public track record over the last `days` (7..365, default 30)."""
        return self.request("GET", "/v1/signals/stats", {"days": days})

    def signal(self, signal_id: str) -> Signal:
        return self.request("GET", f"/v1/signals/{_q(signal_id)}")

    def iter_signals(self, kind: Optional[str] = None, limit: int = 100) -> Iterator[Signal]:
        """Every signal matching `kind`, newest first, following the `next` cursor page by page."""
        before: Optional[str] = None
        while True:
            page = self.signals(kind=kind, limit=limit, before=before)
            yield from page["signals"]
            if not page["next"]:
                return
            before = page["next"]

    def signals_after(self, after_id: str) -> list[Signal]:
        """Every signal visible to this key with an id greater than `after_id`, oldest first."""
        newer: list[Signal] = []
        for s in self.iter_signals():
            if int(s["id"]) <= int(after_id):
                break
            newer.append(s)
        newer.reverse()
        return newer

    # ── smart LPs
    def smart_lps(self, window_days: Optional[int] = None, chain: Optional[str] = None, limit: Optional[int] = None) -> Leaderboard:
        """Wallets ranked by LP pnl versus holding. Without Pro: the top 10 with masked addresses."""
        return self.request("GET", "/v1/smart-lps", {"windowDays": window_days, "chain": chain, "limit": limit})

    def wallet_positions(self, owner: str, limit: Optional[int] = None) -> WalletPositions:
        """A wallet's open and closed positions (Pro)."""
        return self.request("GET", f"/v1/smart-lps/{_q(owner)}/positions", {"limit": limit})

    def follows(self) -> list[Follow]:
        """Wallets you follow (Pro)."""
        return self.request("GET", "/v1/me/follows")["follows"]

    def follow(self, owner: str) -> dict[str, Any]:
        """Follow a wallet: its new positions of $10k+ become smart_lp signals for you (Pro, up to 50)."""
        return self.request("PUT", f"/v1/me/follows/{_q(owner)}")

    def unfollow(self, owner: str) -> None:
        self.request("DELETE", f"/v1/me/follows/{_q(owner)}")

    # ── account
    def me(self) -> Me:
        return self.request("GET", "/v1/me")

    def set_webhook(self, url: str) -> WebhookRegistration:
        """Set or replace the webhook. The signing secret is returned only here."""
        return self.request("PUT", "/v1/me/webhook", body={"url": url})

    def delete_webhook(self) -> None:
        self.request("DELETE", "/v1/me/webhook")

    def create_api_key(self) -> dict[str, str]:
        """Create a new API key and return it (shown only here). The key used for this call stops working."""
        return self.request("POST", "/v1/me/api-key")

    def telegram_link(self) -> TelegramLink:
        """A one-time code: send `/start <code>` to the LPSignal Telegram bot."""
        return self.request("POST", "/v1/me/telegram-link")

    # ── billing
    def billing(self, refresh: bool = False) -> BillingStatus:
        """Current plan. `refresh=True` re-reads Stripe first (use it right after a checkout)."""
        return self.request("GET", "/v1/billing", {"refresh": 1 if refresh else None})

    def checkout(self, tier: Literal["basic", "pro"]) -> dict[str, str]:
        """A Stripe Checkout URL for a monthly plan."""
        return self.request("POST", "/v1/billing/checkout", body={"tier": tier})

    def billing_portal(self) -> dict[str, str]:
        """A Stripe Customer Portal URL (change plan, cancel, invoices)."""
        return self.request("POST", "/v1/billing/portal")


class AsyncLPSignal(_Base):
    """Asynchronous client; same methods as `LPSignal`, awaitable. Needed by `SignalStream`."""

    def __init__(self, api_key: Optional[str] = None, base_url: str = DEFAULT_BASE_URL, timeout: float = 15.0,
                 max_retries: int = 2, http: Optional[httpx.AsyncClient] = None):
        super().__init__(api_key, base_url, timeout, max_retries)
        self._http = http or httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "AsyncLPSignal":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def request(self, method: str, path: str, query: Optional[dict[str, Any]] = None, body: Any = None) -> Any:
        attempt = 0
        while True:
            res = await self._http.request(method, self.base_url + path, params=self._params(query), headers=self._headers,
                                           json=body)
            parsed = self._body(res)
            if res.is_success:
                return parsed
            wait = self._retry_wait(method, res, parsed, attempt)
            if wait is None:
                raise LPSignalError(res.status_code, parsed, res.headers.get("x-request-id"))
            await asyncio.sleep(wait)
            attempt += 1

    async def health(self) -> dict[str, Any]:
        return await self.request("GET", "/v1/health")

    async def chains(self) -> list[ChainStatus]:
        return (await self.request("GET", "/v1/chains"))["chains"]

    async def pools(self, chain: Optional[str] = None, pair_class: Optional[str] = None, window: Optional[int] = None,
                    min_tvl_usd: Optional[float] = None, limit: Optional[int] = None, offset: Optional[int] = None) -> PoolsPage:
        return await self.request("GET", "/v1/pools", self._pools_query(chain, pair_class, window, min_tvl_usd, limit, offset))

    async def pool(self, chain: str, address: str) -> PoolDetail:
        return await self.request("GET", f"/v1/pools/{_q(chain)}/{_q(address)}")

    async def pool_hours(self, chain: str, address: str, hours: Optional[int] = None) -> list[PoolHour]:
        return (await self.request("GET", f"/v1/pools/{_q(chain)}/{_q(address)}/hours", {"hours": hours}))["hours"]

    async def backtest(self, chain: str, address: str, range_pct: float, days: Optional[int] = None) -> Backtest:
        return await self.request("GET", f"/v1/pools/{_q(chain)}/{_q(address)}/backtest", {"rangePct": range_pct, "days": days})

    async def signals(self, kind: Optional[str] = None, limit: Optional[int] = None, before: Optional[str] = None) -> SignalsPage:
        return await self.request("GET", "/v1/signals", {"kind": kind, "limit": limit, "before": before})

    async def signal_stats(self, days: Optional[int] = None) -> dict[str, Any]:
        return await self.request("GET", "/v1/signals/stats", {"days": days})

    async def signal(self, signal_id: str) -> Signal:
        return await self.request("GET", f"/v1/signals/{_q(signal_id)}")

    async def iter_signals(self, kind: Optional[str] = None, limit: int = 100) -> AsyncIterator[Signal]:
        before: Optional[str] = None
        while True:
            page = await self.signals(kind=kind, limit=limit, before=before)
            for s in page["signals"]:
                yield s
            if not page["next"]:
                return
            before = page["next"]

    async def signals_after(self, after_id: str) -> list[Signal]:
        newer: list[Signal] = []
        async for s in self.iter_signals():
            if int(s["id"]) <= int(after_id):
                break
            newer.append(s)
        newer.reverse()
        return newer

    async def smart_lps(self, window_days: Optional[int] = None, chain: Optional[str] = None, limit: Optional[int] = None) -> Leaderboard:
        return await self.request("GET", "/v1/smart-lps", {"windowDays": window_days, "chain": chain, "limit": limit})

    async def wallet_positions(self, owner: str, limit: Optional[int] = None) -> WalletPositions:
        return await self.request("GET", f"/v1/smart-lps/{_q(owner)}/positions", {"limit": limit})

    async def follows(self) -> list[Follow]:
        return (await self.request("GET", "/v1/me/follows"))["follows"]

    async def follow(self, owner: str) -> dict[str, Any]:
        return await self.request("PUT", f"/v1/me/follows/{_q(owner)}")

    async def unfollow(self, owner: str) -> None:
        await self.request("DELETE", f"/v1/me/follows/{_q(owner)}")

    async def me(self) -> Me:
        return await self.request("GET", "/v1/me")

    async def set_webhook(self, url: str) -> WebhookRegistration:
        return await self.request("PUT", "/v1/me/webhook", body={"url": url})

    async def delete_webhook(self) -> None:
        await self.request("DELETE", "/v1/me/webhook")

    async def create_api_key(self) -> dict[str, str]:
        return await self.request("POST", "/v1/me/api-key")

    async def telegram_link(self) -> TelegramLink:
        return await self.request("POST", "/v1/me/telegram-link")

    async def billing(self, refresh: bool = False) -> BillingStatus:
        return await self.request("GET", "/v1/billing", {"refresh": 1 if refresh else None})

    async def checkout(self, tier: Literal["basic", "pro"]) -> dict[str, str]:
        return await self.request("POST", "/v1/billing/checkout", body={"tier": tier})

    async def billing_portal(self) -> dict[str, str]:
        return await self.request("POST", "/v1/billing/portal")
