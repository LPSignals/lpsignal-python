#!/usr/bin/env python3
"""End-to-end check of the Python SDK against a running LPSignal API. Read-only by default (safe against production):

    LPSIGNAL_BASE_URL=https://api.lpsignal.app [LPSIGNAL_API_KEY=lps_...] python scripts/e2e.py

Optional:
    E2E_WRITE=1               also exercise write endpoints (webhook, Telegram link, follows, rules) — test/PPE accounts only
    E2E_EXPECT_SIGNAL_SEC=60  wait this long for one live signal on the stream (something must fire meanwhile)
    LPSIGNAL_FREE_API_KEY=... a free-plan key: the stream must refuse it
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time

from lpsignal import AsyncLPSignal, LPSignal, LPSignalError, SignalStream

BASE = os.environ.get("LPSIGNAL_BASE_URL")
if not BASE:
    sys.exit("set LPSIGNAL_BASE_URL")
KEY = os.environ.get("LPSIGNAL_API_KEY") or None
FREE_KEY = os.environ.get("LPSIGNAL_FREE_API_KEY") or None
WRITE = os.environ.get("E2E_WRITE") == "1"
EXPECT_SIGNAL_SEC = float(os.environ.get("E2E_EXPECT_SIGNAL_SEC", "0"))

anon = LPSignal(base_url=BASE)
authed = LPSignal(base_url=BASE, api_key=KEY) if KEY else None
reader = authed or anon
stats = {"passed": 0, "failed": 0, "skipped": 0}


class Skip(Exception):
    pass


def check(name, fn):
    try:
        note = fn()
        stats["passed"] += 1
        print(f"ok   {name}{f' — {note}' if note else ''}")
    except Skip:
        stats["skipped"] += 1
        print(f"skip {name}")
    except Exception as e:  # noqa: BLE001 - report every failure and keep going
        stats["failed"] += 1
        print(f"FAIL {name}: {e!r}")


def expect(cond, msg):
    if not cond:
        raise AssertionError(msg)


def expect_error(fn, status, code):
    try:
        fn()
    except LPSignalError as e:
        expect(e.status == status and e.code == code, f"got {e}")
        return
    raise AssertionError(f"expected {status} {code}")


print(f"SDK E2E (python) against {BASE} ({'with key' if KEY else 'anonymous'}{', writes on' if WRITE else ', read-only'})")

check("health", lambda: expect(anon.health()["ok"] is True, "not ok"))


def chains():
    cs = anon.chains()
    for c in cs:
        expect(isinstance(c["block"], str) and isinstance(c["lagSec"], int), f"bad row {c}")
    return ", ".join(f"{c['chain']} lag {c['lagSec']}s" for c in cs) or "no chains yet"


check("chains", chains)

top: dict = {}


def pools():
    page = anon.pools(limit=5)
    if not page["pools"]:  # a young deployment has no 7-day metrics yet
        page = anon.pools(limit=5, window=24)
    nets = [p["best"]["netApr"] for p in page["pools"]]
    expect(len(nets) <= 5 and nets == sorted(nets, reverse=True), "not ranked/bounded")
    if page["pools"]:
        top.update(page["pools"][0])
    return f"{len(nets)} pools"


check("pools: ranked by net APR, bounded", pools)


def pools_sorted():
    win = 168 if anon.pools(limit=1)["total"] else 24
    asc = anon.pools(window=win, sort="tvl", order="asc", limit=10)
    expect(isinstance(asc["total"], int) and asc["sort"] == "tvl" and asc["order"] == "asc", "bad page meta")
    tvls = [p["tvlUsd"] for p in asc["pools"]]
    expect(tvls == sorted(tvls), "tvl asc not sorted")
    if asc["total"] > 10:
        nxt = anon.pools(window=win, sort="tvl", order="asc", limit=10, offset=10)
        seen = {(p["chain"], p["address"]) for p in asc["pools"]}
        expect(not any((p["chain"], p["address"]) in seen for p in nxt["pools"]), "pages overlap")
    # (metrics may update meanwhile: none twice, none far beyond the total)
    ids = [(p["chain"], p["address"]) for p in anon.iter_pools(window=win)]
    n = len(ids)
    expect(len(set(ids)) == n and n <= asc["total"] + 100, f"iter_pools: {n} pools, {len(set(ids))} distinct, total {asc['total']}")
    try:
        anon.pools(sort="owner")
        raise AssertionError("unknown sort accepted")
    except LPSignalError as e:
        expect(e.status == 400, f"got {e.status}")
    return f"{asc['total']} pools, walked {n}"


check("pools: sorted by any column either way, offset pages, total", pools_sorted)


def detail():
    if not top:
        raise Skip
    d = anon.pool(top["chain"], top["address"])
    expect(d["pool"]["address"] == top["address"] and d["metrics"], "detail mismatch")
    hours = anon.pool_hours(top["chain"], top["address"], hours=24)
    expect([h["hour"] for h in hours] == sorted(h["hour"] for h in hours), "hours not ascending")
    try:
        bt = anon.backtest(top["chain"], top["address"], range_pct=5, days=1)
    except LPSignalError as e:
        if e.code in ("window_not_covered", "no_price_data"):
            return f"backtest: {e.code}"
        raise
    expect(abs(bt["netApr"] - (bt["feeApr"] + bt["ilApr"])) < 1e-9 and bt["ilApr"] <= 1e-12, "APR identity")
    return f"{len(d['metrics'])} metrics, {len(hours)} hours, backtest ok"


check("pool detail, hours, backtest", detail)
check("unknown pool → 404 pool_not_found", lambda: expect_error(lambda: anon.pool("base", "0x" + "0" * 40), 404, "pool_not_found"))
check("invalid query → 400 invalid_request", lambda: expect_error(lambda: anon.pools(limit=1000), 400, "invalid_request"))

newest: list = []


def signals():
    page = reader.signals(limit=3)
    newest.extend(page["signals"])
    if not newest:
        return "no signals yet"
    ids = [int(s["id"]) for s in newest]
    expect(ids == sorted(ids, reverse=True), "not newest first")
    expect(reader.signal(newest[0]["id"])["id"] == newest[0]["id"], "signal(id) mismatch")
    seen = 0
    for _ in reader.iter_signals(limit=2):
        seen += 1
        if seen >= 6:
            break
    if len(newest) >= 2:
        after = reader.signals_after(newest[1]["id"])
        expect(after and int(after[0]["id"]) > ids[1], "signals_after wrong")
    return f"newest #{newest[0]['id']}"


check("signals: page, by id, paging, signals_after", signals)
def check_signal_stats():
    st = anon.signal_stats(days=30)
    expect(isinstance(st["scored"], int) and st["positive"] <= st["scored"] and isinstance(st["points"], list), "bad shape")
    return f"{st['scored']} scored, {st['inexact']} inexact"


def signals_sorted():
    r = anon.signals(sort="return", limit=5)
    expect(isinstance(r["total"], int) and r["next"] is None and r["sort"] == "return", "bad page meta")
    v = [s.get("netApr") if s["kind"] == "burst" else s.get("net7d") for s in r["signals"] if s["kind"] in ("burst", "net_apr")]
    v = [x for x in v if isinstance(x, (int, float))]
    expect(v == sorted(v, reverse=True), "return desc not sorted")
    try:
        anon.signals(sort="return", before="5")
        raise AssertionError("before with a sort accepted")
    except LPSignalError as e:
        expect(e.status == 400, f"got {e.status}")
    return f"{r['total']} signals"


check("signals sorted by return / outcome (offset pages, total)", signals_sorted)
check("signal stats (public track record)", check_signal_stats)
def leaderboard():
    lb = reader.smart_lps(window_days=30)
    expect(isinstance(lb["wallets"], list) and isinstance(lb["total"], int), "bad shape")
    expect(lb["full"] or lb["total"] <= 10, "non-Pro sees more than the top 10")
    ranks = [w["rank"] for w in lb["wallets"]]
    expect(ranks == sorted(ranks), "default order is not by rank")
    caps = [w["capitalUsd"] for w in reader.smart_lps(window_days=30, sort="capital", limit=10)["wallets"]]
    expect(caps == sorted(caps, reverse=True), "capital desc not sorted")
    return f"{lb['total']} wallets, full={lb['full']}"


check("smart LP leaderboard", leaderboard)

me: dict = {}
if authed:
    def me_billing():
        me.update(authed.me())
        expect(me["tier"] == authed.billing()["tier"], "tier mismatch")
        return f"tier {me['tier']}, paid {me['paid']}"

    check("me + billing", me_billing)

    def pro_gate():
        if me.get("tier") == "pro":
            return f"{len(authed.follows())} follows"
        expect_error(lambda: authed.wallet_positions("0x" + "1" * 40), 403, "pro_required")

    check("Pro gate", pro_gate)

    async def stream_check():
        if not me.get("paid") or len(newest) < 3:
            raise Skip
        got, events = [], []
        client = AsyncLPSignal(base_url=BASE, api_key=KEY)
        stream = SignalStream(client, lambda s, src: got.append((s["id"], src)), on_event=events.append,
                              since=newest[2]["id"])
        await stream.start()
        t0 = time.monotonic()
        while not any(e["type"] in ("live", "fatal") for e in events) and time.monotonic() - t0 < 15:
            await asyncio.sleep(0.05)
        live = any(e["type"] == "live" for e in events)
        note = ""
        if live and EXPECT_SIGNAL_SEC > 0:
            before = len(got)
            while not any(g[1] == "live" for g in got[before:]) and time.monotonic() - t0 < EXPECT_SIGNAL_SEC:
                await asyncio.sleep(0.1)
            lives = [g for g in got[before:] if g[1] == "live"]
            expect(lives, f"no live signal within {EXPECT_SIGNAL_SEC}s")
            note = f", live #{lives[0][0]}"
        await stream.stop()
        await client.aclose()
        expect(live, f"never went live: {[e['type'] for e in events]}")
        rest = [g[0] for g in got if g[1] == "rest"]
        expect(newest[0]["id"] in rest and newest[1]["id"] in rest, f"catch-up missed signals: {got}")
        ids = [int(g[0]) for g in got]
        expect(ids == sorted(set(ids)), "not strictly ascending")
        return f"{len(rest)} via REST{note}"

    check("stream: catches up over REST from since, then goes live", lambda: asyncio.run(stream_check()))

if FREE_KEY:
    async def free_refused():
        events = []
        client = AsyncLPSignal(base_url=BASE, api_key=FREE_KEY)
        s = SignalStream(client, lambda *_: None, on_event=events.append, since="0")
        await s.start()
        t0 = time.monotonic()
        while not any(e["type"] == "fatal" for e in events) and time.monotonic() - t0 < 10:
            await asyncio.sleep(0.05)
        await s.stop()
        await client.aclose()
        fatal = [e for e in events if e["type"] == "fatal"]
        expect(fatal and "402" in str(fatal[0]["error"]), f"expected 402 fatal, got {[e['type'] for e in events]}")
        return str(fatal[0]["error"])

    check("stream refuses a free key", lambda: asyncio.run(free_refused()))

if authed and WRITE:
    def webhook():
        reg = authed.set_webhook("https://example.com/lpsignal-sdk-e2e")
        expect(reg["secret"].startswith("whsec_"), "no secret")
        expect(authed.me()["webhookUrl"] == reg["url"], "not stored")
        authed.delete_webhook()
        expect(authed.me()["webhookUrl"] is None, "not deleted")

    check("write: webhook set / read back / delete", webhook)
    check("write: webhook rejects a private address",
          lambda: expect_error(lambda: authed.set_webhook("https://127.0.0.1/x"), 400, "invalid_webhook_url"))
    check("write: telegram link code", lambda: expect(len(authed.telegram_link()["code"]) >= 12, "no code"))

    def follows():
        if authed.me()["tier"] != "pro":
            raise Skip
        owner = "0x" + "e2" * 20
        expect(authed.follow(owner)["following"] is True, "follow")
        expect(any(f["owner"] == owner for f in authed.follows()), "not listed")
        authed.unfollow(owner)
        expect(not any(f["owner"] == owner for f in authed.follows()), "still listed")

    check("write: follow / unfollow (Pro)", follows)

    def rules():
        m = authed.me()
        if not m["paid"]:
            raise Skip
        page = authed.rules()
        if len(page["rules"]) >= page["limit"]:
            raise Skip
        r = authed.create_rule({"kind": "net_apr", "name": "sdk e2e", "minNet7d": 0.15, "chains": ["base"]})
        try:
            expect(r["minNet24h"] == 0.15 and r["cooldownHours"] == 24 and r["active"] is True, f"create {r}")
            u = authed.update_rule(r["id"], {"kind": "depeg", "name": "sdk e2e peg", "minDeviation": 0.003, "enabled": False})
            expect(u["kind"] == "depeg" and u["active"] is False, "update")
        finally:
            # never leave a test rule behind (it would use up the plan's slots on the next run)
            try:
                authed.delete_rule(r["id"])
            except LPSignalError as e:
                if e.status != 404:
                    raise RuntimeError(f"cleanup: rule {r['id']} not deleted: {e}") from e
        expect(not any(x["id"] == r["id"] for x in authed.rules()["rules"]), "still listed")
        try:
            accepted = authed.create_rule({"kind": "net_apr", "name": "x", "minNet7d": 0.1, "minTvlUsd": 10})
        except LPSignalError as e:
            expect(e.status == 400 and e.code == "invalid_request", f"got {e}")
        else:
            # a regression that accepts it must not leave the rule behind either
            with contextlib.suppress(LPSignalError):
                authed.delete_rule(accepted["id"])
            raise AssertionError("TVL floor not enforced")
        before = m["subscriptions"]
        try:
            got = authed.set_subscriptions(["depeg", "burst"])["subscriptions"]
            expect(got == ["burst", "depeg"], f"set {got}")
        finally:
            # restore even when the response was lost or wrong
            authed.set_subscriptions(before)
        expect(authed.me()["subscriptions"] == before, "subscriptions not restored")
        expect(authed.signal_stats(kind="burst")["kind"] == "burst", "burst stats")
        expect(all(s["rule"] is not None for s in authed.signals(source="rules", limit=20)["signals"]), "source=rules returned a global signal")

    check("write: custom rules create / update / delete, subscriptions (paid)", rules)

print(f"\n{stats['passed']} passed, {stats['failed']} failed, {stats['skipped']} skipped")
sys.exit(1 if stats["failed"] else 0)
