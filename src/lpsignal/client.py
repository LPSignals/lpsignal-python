from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator, Iterator, Literal, Optional
from urllib.parse import quote

import httpx

from .types import (
    Backtest, BillingStatus, BoardSort, ChainStatus, ClosedSort, Follow, Leaderboard, LeaderboardWallet, Me, OpenSort, Order,
    PoolDetail, PoolHour, PoolSort, PoolsPage, RankedPool, Rule, RulesPage, Signal, SignalsPage, TelegramLink, WalletPositions,
    WebhookRegistration,
)



def _kinds(kinds: Optional[list[str]]) -> Optional[str]:
    """a `kinds` list as the API's comma-separated parameter (None when absent or empty)"""
    return ",".join(kinds) if kinds else None

# "default" = global signals only, "rules" = your custom-rule matches only, "subscribed" = what push delivers
SignalSource = Literal["default", "rules", "subscribed"]
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
    def _pools_query(chain, pair_class, window, min_tvl_usd, limit, offset, sort=None, order=None) -> dict[str, Any]:
        return {"chain": chain, "class": pair_class, "window": window, "minTvlUsd": min_tvl_usd, "limit": limit, "offset": offset, "sort": sort, "order": order}

    @staticmethod
    def _board_query(window_days, chain, limit, offset, sort, order) -> dict[str, Any]:
        return {"windowDays": window_days, "chain": chain, "limit": limit, "offset": offset, "sort": sort, "order": order}

    @staticmethod
    def _positions_query(limit, open_offset, open_sort, open_order, closed_offset, closed_sort, closed_order) -> dict[str, Any]:
        return {"limit": limit, "openOffset": open_offset, "openSort": open_sort, "openOrder": open_order,
                "closedOffset": closed_offset, "closedSort": closed_sort, "closedOrder": closed_order}


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
              min_tvl_usd: Optional[float] = None, limit: Optional[int] = None, offset: Optional[int] = None,
              sort: Optional[PoolSort] = None, order: Optional[Order] = None) -> PoolsPage:
        """Active pools with their best range over `window` hours (1, 24, 168 or 720); by net APR unless `sort`
        says otherwise (netApr, feeApr, ilApr, inRange, emissionApr, tvl, fee; `order` asc/desc, default desc)."""
        return self.request("GET", "/v1/pools", self._pools_query(chain, pair_class, window, min_tvl_usd, limit, offset, sort, order))

    def iter_pools(self, chain: Optional[str] = None, pair_class: Optional[str] = None, window: Optional[int] = None,
                   min_tvl_usd: Optional[float] = None, limit: int = 100, sort: Optional[PoolSort] = None, order: Optional[Order] = None) -> Iterator[RankedPool]:
        """The pools matching the filters, in the asked order, page by page (`limit` = page size). Best effort: pages
        are read one after another, so a pool whose place changes meanwhile (metrics update hourly) may be missed;
        none is yielded twice."""
        offset, seen = 0, set()
        while True:
            page = self.pools(chain, pair_class, window, min_tvl_usd, limit, offset, sort, order)
            for p in page["pools"]:
                if (p["chain"], p["address"]) not in seen:
                    seen.add((p["chain"], p["address"]))
                    yield p
            offset += len(page["pools"])
            if not page["pools"] or offset >= page["total"]:
                return

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
    def signals(self, kind: Optional[str] = None, limit: Optional[int] = None, before: Optional[str] = None, source: Optional[SignalSource] = None, kinds: Optional[list[str]] = None) -> SignalsPage:
        """One page of signals, newest first. `source`: "default" = global signals only, "rules" = your custom-rule
        matches only, "subscribed" = exactly what your push channels deliver; None = global + your matches.
        `kinds` = several kinds at once, e.g. ["net_apr", "burst"]."""
        return self.request("GET", "/v1/signals", {"kind": kind, "limit": limit, "before": before, "source": source, "kinds": _kinds(kinds)})

    def signal_stats(self, days: Optional[int] = None, kind: Optional[Literal["net_apr", "burst"]] = None) -> dict[str, Any]:
        """The public track record of `kind` (net_apr by default, or burst) over the last `days` (7..365, default 30)."""
        return self.request("GET", "/v1/signals/stats", {"days": days, "kind": kind})

    def signal(self, signal_id: str) -> Signal:
        return self.request("GET", f"/v1/signals/{_q(signal_id)}")

    def iter_signals(self, kind: Optional[str] = None, limit: int = 100, source: Optional[SignalSource] = None, kinds: Optional[list[str]] = None) -> Iterator[Signal]:
        """Every signal matching `kind`, newest first, following the `next` cursor page by page."""
        before: Optional[str] = None
        while True:
            page = self.signals(kind=kind, limit=limit, before=before, source=source, kinds=kinds)
            yield from page["signals"]
            if not page["next"]:
                return
            before = page["next"]

    def signals_after(self, after_id: str, source: Optional[SignalSource] = None, kinds: Optional[list[str]] = None) -> list[Signal]:
        """Every signal visible to this key with an id greater than `after_id`, oldest first."""
        newer: list[Signal] = []
        for s in self.iter_signals(source=source, kinds=kinds):
            if int(s["id"]) <= int(after_id):
                break
            newer.append(s)
        newer.reverse()
        return newer

    # ── smart LPs
    def smart_lps(self, window_days: Optional[int] = None, chain: Optional[str] = None, limit: Optional[int] = None,
                  offset: Optional[int] = None, sort: Optional[BoardSort] = None, order: Optional[Order] = None) -> Leaderboard:
        """Wallets ranked by LP pnl versus holding. `sort`: rank (default), pnl, return, capital, closes, wins — `rank`
        stays the pnl rank whatever the sort. Without Pro: the top 10 with masked addresses."""
        return self.request("GET", "/v1/smart-lps", self._board_query(window_days, chain, limit, offset, sort, order))

    def iter_smart_lps(self, window_days: Optional[int] = None, chain: Optional[str] = None, limit: int = 100,
                       sort: Optional[BoardSort] = None, order: Optional[Order] = None) -> Iterator[LeaderboardWallet]:
        """The wallets on the board, in the asked order, page by page. Best effort like `iter_pools`."""
        offset, seen = 0, set()
        while True:
            page = self.smart_lps(window_days, chain, limit, offset, sort, order)
            for w in page["wallets"]:
                if w["owner"] not in seen:
                    seen.add(w["owner"])
                    yield w
            offset += len(page["wallets"])
            if not page["wallets"] or offset >= page["total"]:
                return

    def wallet_positions(self, owner: str, limit: Optional[int] = None, open_offset: Optional[int] = None,
                         open_sort: Optional[OpenSort] = None, open_order: Optional[Order] = None, closed_offset: Optional[int] = None,
                         closed_sort: Optional[ClosedSort] = None, closed_order: Optional[Order] = None) -> WalletPositions:
        """A wallet's open and closed positions (Pro); each list pages and sorts on its own, `limit` is shared."""
        return self.request("GET", f"/v1/smart-lps/{_q(owner)}/positions",
                            self._positions_query(limit, open_offset, open_sort, open_order, closed_offset, closed_sort, closed_order))

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

    def rules(self) -> RulesPage:
        """Your custom alert rules, the plan's limit and today's match count."""
        return self.request("GET", "/v1/me/rules")

    def create_rule(self, rule: dict[str, Any]) -> Rule:
        """Create a rule, e.g. {"kind": "net_apr", "name": "wide", "minNet7d": 0.15, "chains": ["base"]}
        (paid plans; 403 `paid_plan_required`, 400 `rule_limit` when the plan's limit is reached)."""
        return self.request("POST", "/v1/me/rules", body=rule)

    def update_rule(self, rule_id: str, rule: dict[str, Any]) -> Rule:
        """Replace a rule (its kind may change too)."""
        return self.request("PUT", f"/v1/me/rules/{_q(rule_id)}", body=rule)

    def delete_rule(self, rule_id: str) -> None:
        self.request("DELETE", f"/v1/me/rules/{_q(rule_id)}")

    def set_subscriptions(self, kinds: list[str]) -> dict[str, list[str]]:
        """The kinds of global signal pushed to you on Telegram, webhook and WebSocket (core events by default; add
        "burst" for short-term opportunities). Custom-rule matches always arrive."""
        return self.request("PUT", "/v1/me/subscriptions", body={"kinds": kinds})

    def set_webhook(self, url: str) -> WebhookRegistration:
        """Set or replace the webhook. The signing secret is returned only here."""
        return self.request("PUT", "/v1/me/webhook", body={"url": url})

    def delete_webhook(self) -> None:
        self.request("DELETE", "/v1/me/webhook")

    def create_api_key(self, replace: Optional[bool] = None) -> dict[str, str]:
        """Create a new API key and return it (shown only here). By default it replaces the current key, which stops
        working at once; `replace=False` only creates one when the account has none (else 409 `api_key_exists`)."""
        return self.request("POST", "/v1/me/api-key", body=None if replace is None else {"replace": replace})

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

    def crypto_billing(self) -> dict[str, Any]:
        """Prepaid USDT/USDC plans: prices for this account (upgrade credit applied), networks, the open order."""
        return self.request("GET", "/v1/billing/crypto")

    def create_crypto_order(self, tier: Literal["basic", "pro"], months: Literal[1, 3, 12]) -> dict[str, Any]:
        """Start (or get back the open) crypto order; send exactly `amount` before `expiresAt`."""
        return self.request("POST", "/v1/billing/crypto/orders", body={"tier": tier, "months": months})

    def crypto_order(self, order_id: str) -> dict[str, Any]:
        return self.request("GET", f"/v1/billing/crypto/orders/{_q(order_id)}")

    def cancel_crypto_order(self, order_id: str) -> dict[str, Any]:
        """Withdraw an open order (a payment already sent before its deadline still pays it)."""
        return self.request("POST", f"/v1/billing/crypto/orders/{_q(order_id)}/cancel")


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
                    min_tvl_usd: Optional[float] = None, limit: Optional[int] = None, offset: Optional[int] = None,
                    sort: Optional[PoolSort] = None, order: Optional[Order] = None) -> PoolsPage:
        return await self.request("GET", "/v1/pools", self._pools_query(chain, pair_class, window, min_tvl_usd, limit, offset, sort, order))

    async def iter_pools(self, chain: Optional[str] = None, pair_class: Optional[str] = None, window: Optional[int] = None,
                         min_tvl_usd: Optional[float] = None, limit: int = 100, sort: Optional[PoolSort] = None, order: Optional[Order] = None) -> AsyncIterator[RankedPool]:
        offset, seen = 0, set()
        while True:
            page = await self.pools(chain, pair_class, window, min_tvl_usd, limit, offset, sort, order)
            for p in page["pools"]:
                if (p["chain"], p["address"]) not in seen:
                    seen.add((p["chain"], p["address"]))
                    yield p
            offset += len(page["pools"])
            if not page["pools"] or offset >= page["total"]:
                return

    async def pool(self, chain: str, address: str) -> PoolDetail:
        return await self.request("GET", f"/v1/pools/{_q(chain)}/{_q(address)}")

    async def pool_hours(self, chain: str, address: str, hours: Optional[int] = None) -> list[PoolHour]:
        return (await self.request("GET", f"/v1/pools/{_q(chain)}/{_q(address)}/hours", {"hours": hours}))["hours"]

    async def backtest(self, chain: str, address: str, range_pct: float, days: Optional[int] = None) -> Backtest:
        return await self.request("GET", f"/v1/pools/{_q(chain)}/{_q(address)}/backtest", {"rangePct": range_pct, "days": days})

    async def signals(self, kind: Optional[str] = None, limit: Optional[int] = None, before: Optional[str] = None, source: Optional[SignalSource] = None, kinds: Optional[list[str]] = None) -> SignalsPage:
        return await self.request("GET", "/v1/signals", {"kind": kind, "limit": limit, "before": before, "source": source, "kinds": _kinds(kinds)})

    async def signal_stats(self, days: Optional[int] = None, kind: Optional[Literal["net_apr", "burst"]] = None) -> dict[str, Any]:
        return await self.request("GET", "/v1/signals/stats", {"days": days, "kind": kind})

    async def signal(self, signal_id: str) -> Signal:
        return await self.request("GET", f"/v1/signals/{_q(signal_id)}")

    async def iter_signals(self, kind: Optional[str] = None, limit: int = 100, source: Optional[SignalSource] = None, kinds: Optional[list[str]] = None) -> AsyncIterator[Signal]:
        before: Optional[str] = None
        while True:
            page = await self.signals(kind=kind, limit=limit, before=before, source=source, kinds=kinds)
            for s in page["signals"]:
                yield s
            if not page["next"]:
                return
            before = page["next"]

    async def signals_after(self, after_id: str, source: Optional[SignalSource] = None, kinds: Optional[list[str]] = None) -> list[Signal]:
        newer: list[Signal] = []
        async for s in self.iter_signals(source=source, kinds=kinds):
            if int(s["id"]) <= int(after_id):
                break
            newer.append(s)
        newer.reverse()
        return newer

    async def smart_lps(self, window_days: Optional[int] = None, chain: Optional[str] = None, limit: Optional[int] = None,
                        offset: Optional[int] = None, sort: Optional[BoardSort] = None, order: Optional[Order] = None) -> Leaderboard:
        return await self.request("GET", "/v1/smart-lps", self._board_query(window_days, chain, limit, offset, sort, order))

    async def iter_smart_lps(self, window_days: Optional[int] = None, chain: Optional[str] = None, limit: int = 100,
                             sort: Optional[BoardSort] = None, order: Optional[Order] = None) -> AsyncIterator[LeaderboardWallet]:
        offset, seen = 0, set()
        while True:
            page = await self.smart_lps(window_days, chain, limit, offset, sort, order)
            for w in page["wallets"]:
                if w["owner"] not in seen:
                    seen.add(w["owner"])
                    yield w
            offset += len(page["wallets"])
            if not page["wallets"] or offset >= page["total"]:
                return

    async def wallet_positions(self, owner: str, limit: Optional[int] = None, open_offset: Optional[int] = None,
                               open_sort: Optional[OpenSort] = None, open_order: Optional[Order] = None, closed_offset: Optional[int] = None,
                               closed_sort: Optional[ClosedSort] = None, closed_order: Optional[Order] = None) -> WalletPositions:
        return await self.request("GET", f"/v1/smart-lps/{_q(owner)}/positions",
                                  self._positions_query(limit, open_offset, open_sort, open_order, closed_offset, closed_sort, closed_order))

    async def follows(self) -> list[Follow]:
        return (await self.request("GET", "/v1/me/follows"))["follows"]

    async def follow(self, owner: str) -> dict[str, Any]:
        return await self.request("PUT", f"/v1/me/follows/{_q(owner)}")

    async def unfollow(self, owner: str) -> None:
        await self.request("DELETE", f"/v1/me/follows/{_q(owner)}")

    async def me(self) -> Me:
        return await self.request("GET", "/v1/me")

    async def rules(self) -> RulesPage:
        return await self.request("GET", "/v1/me/rules")

    async def create_rule(self, rule: dict[str, Any]) -> Rule:
        return await self.request("POST", "/v1/me/rules", body=rule)

    async def update_rule(self, rule_id: str, rule: dict[str, Any]) -> Rule:
        return await self.request("PUT", f"/v1/me/rules/{_q(rule_id)}", body=rule)

    async def delete_rule(self, rule_id: str) -> None:
        await self.request("DELETE", f"/v1/me/rules/{_q(rule_id)}")

    async def set_subscriptions(self, kinds: list[str]) -> dict[str, list[str]]:
        return await self.request("PUT", "/v1/me/subscriptions", body={"kinds": kinds})

    async def set_webhook(self, url: str) -> WebhookRegistration:
        return await self.request("PUT", "/v1/me/webhook", body={"url": url})

    async def delete_webhook(self) -> None:
        await self.request("DELETE", "/v1/me/webhook")

    async def create_api_key(self, replace: Optional[bool] = None) -> dict[str, str]:
        return await self.request("POST", "/v1/me/api-key", body=None if replace is None else {"replace": replace})

    async def telegram_link(self) -> TelegramLink:
        return await self.request("POST", "/v1/me/telegram-link")

    async def billing(self, refresh: bool = False) -> BillingStatus:
        return await self.request("GET", "/v1/billing", {"refresh": 1 if refresh else None})

    async def checkout(self, tier: Literal["basic", "pro"]) -> dict[str, str]:
        return await self.request("POST", "/v1/billing/checkout", body={"tier": tier})

    async def billing_portal(self) -> dict[str, str]:
        return await self.request("POST", "/v1/billing/portal")

    async def crypto_billing(self) -> dict[str, Any]:
        return await self.request("GET", "/v1/billing/crypto")

    async def create_crypto_order(self, tier: Literal["basic", "pro"], months: Literal[1, 3, 12]) -> dict[str, Any]:
        return await self.request("POST", "/v1/billing/crypto/orders", body={"tier": tier, "months": months})

    async def crypto_order(self, order_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/v1/billing/crypto/orders/{_q(order_id)}")

    async def cancel_crypto_order(self, order_id: str) -> dict[str, Any]:
        return await self.request("POST", f"/v1/billing/crypto/orders/{_q(order_id)}/cancel")
