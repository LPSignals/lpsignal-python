# lpsignal (Python)

Official Python SDK for [LPSignal](https://lpsignal.app): net-of-IL APR signals for concentrated-liquidity pools.
Python ≥ 3.10, built on `httpx` and `websockets`.

[中文说明](README.zh.md) · Node.js SDK: [LPSignals/lpsignal-node](https://github.com/LPSignals/lpsignal-node) · [API docs](https://lpsignal.app/docs)

```bash
pip install lpsignal
```

## REST

```python
from lpsignal import LPSignal, LPSignalError

with LPSignal(api_key="lps_...") as lps:          # key optional for public endpoints
    page = lps.pools(chain="base", window=168, min_tvl_usd=1e6)
    top = page["pools"][0]
    detail = lps.pool("base", top["address"])     # every (window × range) metric
    bt = lps.backtest("base", top["address"], range_pct=5, days=7)
    try:
        lps.wallet_positions("0x...")
    except LPSignalError as e:
        if e.code == "pro_required":
            ...
```

`AsyncLPSignal` has the same methods as coroutines. Responses are the API's JSON (camelCase keys); `lpsignal.types`
has `TypedDict`s for the main shapes.

| Method | Endpoint | Key |
|---|---|---|
| `health()` · `chains()` | `/v1/health` · `/v1/chains` | – |
| `pools(chain, pair_class, window, min_tvl_usd, limit, offset, sort, order)` | `GET /v1/pools` — `sort`: `netApr` (default) · `feeApr` · `ilApr` · `inRange` · `emissionApr` · `tvl` · `fee` · `volume24h` · `fees24h` · `poolApr`; `min_pool_apr` (0.3 = 30%); `order`: `desc` (default) · `asc`; the page carries `total`; each pool carries `volume24hUsd`, `fees24hUsd` (an estimate: volume × the current fee rate) `best.net24h` and `poolApr24h` (pool-level 24h APR, as DEX sites show it) | – |
| `iter_pools(…, sort, order)` | every page, in that order (best effort: none twice; one whose place changes meanwhile may be missed) | – |
| `pool(chain, address)` · `pool_hours(chain, address, hours)` | `GET /v1/pools/:chain/:address[/hours]` | – |
| `backtest(chain, address, range_pct, days)` | `GET …/backtest` | – |
| `signals(kind, limit, before, source, kinds, sort, order, offset)` · `signal(id)` | `GET /v1/signals[/:id]` — newest first by `before`; or `sort`: `return` (APR at firing) · `outcome` (realised 7-day result), paged by `offset` with `total` | optional |
| `signal_stats(days)` | `GET /v1/signals/stats` — the public track record | – |
| `iter_signals(kind)` | every page, newest first | optional |
| `signals_after(id)` | everything newer than `id`, oldest first | optional |
| `smart_lps(window_days, chain, limit, offset, sort, order)` | `GET /v1/smart-lps` — `sort`: `rank` (default, = pnl rank) · `pnl` · `return` · `capital` · `closes` · `wins` · `apr` · `winRate`; `rank` stays the pnl rank whatever the sort; `total`; each wallet carries `aprVsHold` (annualised vs holding), `winRate`, `avgHoldH` | optional |
| `iter_smart_lps(…, sort, order)` | every wallet on the board, in that order | optional |
| `wallet_positions(owner, limit, open_offset, open_sort, open_order, closed_offset, closed_sort, closed_order)` | `GET /v1/smart-lps/:owner/positions` — each list pages and sorts on its own (`open_sort`: `lastEvent` · `openedAt` · `entryUsd`; `closed_sort`: `closedAt` · `openedAt` · `capitalUsd` · `pnlUsd`); `openTotal` / `closedTotal` | Pro |
| `follows()` · `follow(owner)` · `unfollow(owner)` | `/v1/me/follows` | Pro |
| `me()` · `set_webhook(url)` · `delete_webhook()` · `telegram_link()` | `/v1/me…` | yes |
| `create_api_key(replace)` | `POST /v1/me/api-key` — replaces the key (or `replace=False`: only if none), returned once | yes |
| `billing(refresh)` · `checkout(tier)` · `billing_portal()` | `/v1/billing…` | yes |
| `crypto_billing()` · `create_crypto_order(tier, months)` · `crypto_order(id)` · `cancel_crypto_order(id)` | `/v1/billing/crypto…` — prepaid USDT/USDC plans | yes |

Errors are `LPSignalError` with `status`, `code` (the API's `error` field), `body` and `request_id`. A `429` on a
GET, PUT or DELETE is retried after its `Retry-After` (`max_retries`, default 2).

## Stream

```python
import asyncio
from lpsignal import AsyncLPSignal, FileLastIdStore, SignalStream

async def on_signal(signal, source):   # source: "rest" | "replay" | "live"
    ...                                # once per signal, in id order, never concurrently

async def main():
    lps = AsyncLPSignal(api_key="lps_...")   # Basic or Pro
    stream = SignalStream(lps, on_signal, store=FileLastIdStore("lpsignal-state.json"),
                          on_event=lambda e: print(e) if e["type"] == "fatal" else None)
    await stream.run()                        # or: await stream.start() … await stream.stop()

asyncio.run(main())
```

- **First start** with an empty store: begins after the newest signal that exists now (or after `since`).
- **Every (re)connect**: first fetches everything after the saved id over REST, repeating until a pass finds
  nothing new, then connects with `?since=<id>`; anything at or below the saved id is dropped. No gap however long
  you were away, and no duplicates while the process runs.
- **Your handler decides progress**: the id is saved only after `on_signal` returns. If it raises, the connection
  is dropped and the signal is offered again. A crash after the handler but before the save also offers it again
  after the restart: delivery is **at-least-once**, so make the handler idempotent on `signal["id"]`.
- **No repeats even across crashes**: keep the last id in the same database transaction as your side effects
  (write it inside `on_signal`), and pass a `store` that reads and writes that same row; its `save()` must only
  move forward (e.g. `UPDATE … SET last_id = GREATEST(last_id, %s)`), since the stream also saves the starting point.
- `stop()` waits for the signal being handled; nothing new is handed over after it is called. Cancelling `run()`
  stops the stream. Don't await `stop()` from inside `on_signal` (it would wait for itself). After a `fatal` event,
  call `stop()` before `start()` again.
- **Fatal** (the stream stops): invalid key (401), a plan without the stream (402), or the plan expiring (4402).
  Everything else reconnects with backoff.

The state file has the same format as the Node SDK's, so you can switch languages without losing your place.

## Webhooks

```python
from fastapi import FastAPI, Request, Response
from lpsignal import WebhookVerificationError, verify_webhook

app = FastAPI()

@app.post("/lpsignal")
async def lpsignal(request: Request):
    try:
        event = verify_webhook(await request.body(), request.headers, SECRET)
    except WebhookVerificationError as e:
        return Response(e.reason, status_code=400)
    # deliveries are at-least-once: skip event["deliveryId"] if already handled
    return Response(status_code=200)
```

Pass the raw body bytes, never re-serialised JSON. Deliveries older than 5 minutes are rejected (`tolerance_sec`);
every retry is signed afresh.

## Adding and removing liquidity

`lpsignal.liquidity` builds the transactions to add liquidity to a pool (in the range you choose) and to remove it, on
the pools' official position managers — Uniswap v3, PancakeSwap v3, Aerodrome and Velodrome Slipstream. You sign and
send them with your own web3.py: your keys never reach the SDK, the position is always minted to and collected by
your own address, and there is no LPSignal contract or fee in between. Uniswap v4 pools are not supported here.
Install with `pip install "lpsignal[liquidity]"`.

```python
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware, SignAndSendRawMiddlewareBuilder
from lpsignal import LPSignal
from lpsignal import liquidity as lp

w3 = Web3(Web3.HTTPProvider("https://mainnet.base.org"))
# w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)   # BNB Chain and Polygon only
acct = w3.eth.account.from_key(PRIVATE_KEY)
w3.middleware_onion.inject(SignAndSendRawMiddlewareBuilder.build(acct), layer=0)

pool = LPSignal().pool("base", "0x6c561b446416e1a00e8e93e221854d6ea4171372")["pool"]
# ±5% around the current price, 1 WETH in, paid in ETH; the USDC side is computed
plan = lp.plan_add_liquidity(w3, pool, owner=acct.address, amount0=10**18, range_bp=500, native_side=0)
lp.send_plan(w3, plan["approvals"] + [plan["mint"]], sender=acct.address)   # exact approvals, then the mint

# later: your positions, then take half of one out (principal + fees, to you)
pos = next(p for p in lp.positions(w3, "base", acct.address) if p["pool"] == pool["address"])
half = lp.plan_remove_liquidity(w3, pos, owner=acct.address, share_bps=5000)
done = lp.send_plan(w3, [half["call"]], sender=acct.address, finalized=True)
rest = lp.plan_remove_liquidity(w3, pos, owner=acct.address, share_bps=10000, min_block=done[0]["blockNumber"])
lp.send_plan(w3, [rest["call"]], sender=acct.address)
```

- Minimum amounts follow the Uniswap SDK's rule for a price move of up to `slippage_bps` (default 0.5%); transactions
  expire after `deadline_s` (default 20 minutes).
- Every planned call is bound to its chain and owner: `send_plan` refuses to send it from another account or chain.
- `send_plan` waits for each receipt; if one is not seen in time it raises `TxPending` with the hash: **do not send the
  same mint or partial removal again until you know what became of it** — a second one would also go through.
- A send that fails without a hash (the node may have taken it) raises `TxUnknown` with the account and nonce: check
  whether that nonce was used before sending again.
- After a `TxUnknown` or `TxPending`, further sends from that account on that chain raise `AccountBlocked` until you
  have checked the transaction and call `lp.unblock(chain_id, account)`.
- Before another partial removal from the same position, send with `finalized=True` and pass the previous block as
  `min_block`: a lagging RPC or a reorg could otherwise hand the next removal the old liquidity.
- Minimum amounts are what the position manager would take at the edges of the `slippage` band. With a range narrower
  than that band (e.g. ±0.05% on a stable pair at 0.5% slippage) both minimums can be 0: the mint then has no on-chain
  price bound, but a price pushed outside your range only makes the deposit single-sided (a mint never trades), and
  coming back it converts at prices inside your range — the loss is bounded by the range's width.
- Positions staked in an Aerodrome / Velodrome gauge belong to the gauge and are not listed by `positions`.
- Not financial advice: a range that paid well can lose money if the price leaves it.

## Swapping

`plan_swap` swaps one of a pool's tokens for the other (e.g. the side you are short of before adding) through the
[KyberSwap](https://kyberswap.com) aggregator, with **LPSignal's fee: 0.25% of the input (0.05% in stable pools)**,
sent by the aggregator's router to LPSignal's address. The aggregator's answers are checked, never trusted: the quote
must be close to the pool's own on-chain price, and the transaction it builds is decoded — the router, the tokens and
amount, the recipient (your own address), exactly LPSignal's fee and no other, no permit, and a guaranteed minimum out
no lower than your slippage allows — then simulated. The router pays at least `minReturn` or the swap reverts.

```python
swap = lp.plan_swap(w3, pool, owner=acct.address, from_side=0, from_native=True, amount_in=10**17)  # 0.1 ETH for USDC
print(swap["quoteOut"], swap["minReturn"], swap["feeBps"])
lp.send_plan(w3, swap["approvals"] + [swap["swap"]], sender=acct.address)
```

- `min_out`: the least the swap must deliver (e.g. what you are short of); refused (`SwapRefused`, reason `moved`) if
  the quote less the slippage no longer covers it. Other refusals: `impact`, `quote` / `calldata`, `simulation`.
- Send the plan right away (quotes move; it expires after `deadline_s`, default 10 minutes). A swap's deadline sits in
  calldata nobody can check, so after a `TxUnknown` / `TxPending` find out what became of that very transaction before
  swapping again (`send_plan` blocks the account meanwhile).
- Uniswap v4 pools are not supported. The aggregator refuses some addresses (e.g. well-known test keys).

## License

MIT
