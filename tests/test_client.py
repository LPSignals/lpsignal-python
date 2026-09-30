import json
import httpx
import pytest

from lpsignal import AsyncLPSignal, LPSignal, LPSignalError

from conftest import signals_api


def mock(handler):
    calls = []

    def wrapped(req):
        calls.append(req)
        return handler(req)

    return httpx.Client(transport=httpx.MockTransport(wrapped)), calls


def test_bearer_key_and_query_without_nones():
    http, calls = mock(lambda r: httpx.Response(200, json={"pools": [], "limit": 5, "offset": 0}))
    c = LPSignal(api_key="lps_abc", base_url="http://api.test/", http=http)
    c.pools(chain="base", pair_class="stable", window=24, limit=5)
    assert str(calls[0].url) == "http://api.test/v1/pools?chain=base&class=stable&window=24&limit=5"
    assert calls[0].headers["authorization"] == "Bearer lps_abc"


def test_anonymous_has_no_authorization():
    http, calls = mock(lambda r: httpx.Response(200, json={"signals": [], "next": None}))
    LPSignal(base_url="http://api.test", http=http).signals(kind="depeg")
    assert "authorization" not in calls[0].headers
    assert calls[0].url.query == b"kind=depeg"


def test_rejects_foreign_key():
    with pytest.raises(ValueError):
        LPSignal(api_key="sk_live_x")


def test_path_parts_are_encoded():
    http, calls = mock(lambda r: httpx.Response(200, json={"hours": []}))
    LPSignal(base_url="http://api.test", http=http).pool_hours("ethereum", "0xab/../cd", hours=24)
    assert calls[0].url.raw_path.startswith(b"/v1/pools/ethereum/0xab%2F..%2Fcd/hours")


def test_error_carries_code_and_request_id():
    http, _ = mock(lambda r: httpx.Response(403, json={"error": "pro_required"}, headers={"x-request-id": "req-1"}))
    with pytest.raises(LPSignalError) as e:
        LPSignal(api_key="lps_k", base_url="http://api.test", http=http).wallet_positions("0x1")
    assert (e.value.status, e.value.code, e.value.request_id) == (403, "pro_required", "req-1")


def test_429_retried_on_get_not_on_post():
    n = {"i": 0}

    def h(r):
        n["i"] += 1
        if n["i"] == 1:
            return httpx.Response(429, json={"error": "rate_limited"}, headers={"retry-after": "0.01"})
        return httpx.Response(200, json={"ok": True})

    http, calls = mock(h)
    assert LPSignal(base_url="http://api.test", http=http).health() == {"ok": True}
    assert len(calls) == 2

    http, calls = mock(lambda r: httpx.Response(429, json={"error": "rate_limited"}, headers={"retry-after": "0.01"}))
    with pytest.raises(LPSignalError) as e:
        LPSignal(api_key="lps_k", base_url="http://api.test", http=http).telegram_link()
    assert e.value.code == "rate_limited" and len(calls) == 1


def test_gives_up_after_max_retries():
    http, calls = mock(lambda r: httpx.Response(429, json={"error": "rate_limited"}, headers={"retry-after": "0.01"}))
    with pytest.raises(LPSignalError):
        LPSignal(base_url="http://api.test", http=http, max_retries=1).chains()
    assert len(calls) == 2


def test_billing_refresh_and_checkout_body():
    http, calls = mock(lambda r: httpx.Response(200, json={"url": "https://checkout"}))
    c = LPSignal(api_key="lps_k", base_url="http://api.test", http=http)
    c.billing(refresh=True)
    c.checkout("pro")
    assert calls[0].url.query == b"refresh=1"
    assert calls[1].method == "POST" and calls[1].content == b'{"tier":"pro"}'


def test_iter_and_signals_after():
    handler, calls = signals_api(list(range(1, 251)))
    c = LPSignal(api_key="lps_k", base_url="http://api.test", http=httpx.Client(transport=httpx.MockTransport(handler)))
    ids = [s["id"] for s in c.iter_signals()]
    assert len(ids) == 250 and ids[0] == "250" and ids[-1] == "1"
    calls.clear()
    got = [s["id"] for s in c.signals_after("120")]
    assert got == [str(i) for i in range(121, 251)]
    assert len(calls) == 2
    assert c.signals_after("250") == []


def test_signal_stats_and_create_api_key():
    http, calls = mock(lambda r: httpx.Response(200, json={"days": 90, "scored": 3} if r.url.path.endswith("/stats") else {"apiKey": "lps_new"}))
    c = LPSignal(api_key="lps_k", base_url="http://api.test", http=http)
    assert c.signal_stats(days=90)["scored"] == 3
    assert c.create_api_key() == {"apiKey": "lps_new"}
    assert str(calls[0].url) == "http://api.test/v1/signals/stats?days=90"
    assert calls[1].method == "POST" and calls[1].url.path == "/v1/me/api-key"
    assert calls[1].content == b""
    c.create_api_key(replace=False)
    assert calls[2].content == b'{"replace":false}'


def test_crypto_billing_calls():
    http, calls = mock(lambda r: httpx.Response(200, json={"id": "9", "status": "pending"}))
    c = LPSignal(api_key="lps_k", base_url="http://api.test", http=http)
    c.crypto_billing()
    c.create_crypto_order("pro", 12)
    c.crypto_order("9")
    c.cancel_crypto_order("9")
    assert [f"{x.method} {x.url.path}" for x in calls] == ["GET /v1/billing/crypto", "POST /v1/billing/crypto/orders", "GET /v1/billing/crypto/orders/9", "POST /v1/billing/crypto/orders/9/cancel"]
    assert calls[1].content == b'{"tier":"pro","months":12}'


def test_rules_calls():
    http, calls = mock(lambda r: httpx.Response(204) if r.method == "DELETE" else httpx.Response(200, json={"id": "7"}))
    c = LPSignal(api_key="lps_k", base_url="http://api.test", http=http)
    c.rules()
    c.create_rule({"kind": "net_apr", "name": "wide", "minNet7d": 0.15, "chains": ["base"]})
    c.update_rule("7", {"kind": "depeg", "name": "peg", "minDeviation": 0.003})
    c.delete_rule("7")
    c.set_default_signals(False)
    c.signals(source="rules", limit=5)
    assert [f"{x.method} {x.url.path}" for x in calls] == [
        "GET /v1/me/rules", "POST /v1/me/rules", "PUT /v1/me/rules/7", "DELETE /v1/me/rules/7", "PUT /v1/me/default-signals", "GET /v1/signals",
    ]
    assert json.loads(calls[1].content) == {"kind": "net_apr", "name": "wide", "minNet7d": 0.15, "chains": ["base"]}
    assert calls[4].content == b'{"enabled":false}'
    assert dict(calls[5].url.params) == {"limit": "5", "source": "rules"}


def test_stream_url():
    assert LPSignal(base_url="https://api.lpsignal.app").stream_url == "wss://api.lpsignal.app/v1/stream"
    assert LPSignal(base_url="http://127.0.0.1:8080/").stream_url == "ws://127.0.0.1:8080/v1/stream"


async def test_async_client_and_signals_after():
    handler, _ = signals_api(list(range(1, 11)))
    c = AsyncLPSignal(api_key="lps_k", base_url="http://api.test", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert [s["id"] for s in await c.signals_after("7")] == ["8", "9", "10"]
    await c.aclose()
