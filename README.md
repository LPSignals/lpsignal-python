# lpsignal (Python)

Official Python SDK for [LPSignal](https://lpsignal.app): net-of-IL APR signals for concentrated-liquidity pools.
Python ≥ 3.10, built on `httpx` and `websockets`.

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
| `pools(chain, pair_class, window, min_tvl_usd, limit, offset)` | `GET /v1/pools` | – |
| `pool(chain, address)` · `pool_hours(chain, address, hours)` | `GET /v1/pools/:chain/:address[/hours]` | – |
| `backtest(chain, address, range_pct, days)` | `GET …/backtest` | – |
| `signals(kind, limit, before)` · `signal(id)` | `GET /v1/signals[/:id]` | optional |
| `signal_stats(days)` | `GET /v1/signals/stats` — the public track record | – |
| `iter_signals(kind)` | every page, newest first | optional |
| `signals_after(id)` | everything newer than `id`, oldest first | optional |
| `smart_lps(window_days, chain, limit)` | `GET /v1/smart-lps` | optional |
| `wallet_positions(owner)` | `GET /v1/smart-lps/:owner/positions` | Pro |
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

## License

MIT
