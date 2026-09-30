import pytest

from lpsignal import WebhookVerificationError, verify_webhook

from conftest import VECTORS


def hdr(v):
    return {"x-lpsignal-timestamp": v["timestamp"], "x-lpsignal-signature": v["signature"]}


def reason(fn):
    try:
        fn()
        return "ok"
    except WebhookVerificationError as e:
        return e.reason


def test_accepts_server_signatures():
    for v in VECTORS:
        now = int(v["timestamp"])
        assert verify_webhook(v["body"], hdr(v), v["secret"], now=now)["type"] == "signal"
        assert verify_webhook(v["body"].encode(), hdr(v), v["secret"], now=now)["type"] == "signal"
        mixed = {"X-LPSignal-Timestamp": v["timestamp"], "X-LPSignal-Signature": v["signature"]}
        assert verify_webhook(v["body"], mixed, v["secret"], now=now)


def test_parsed_body():
    v = VECTORS[0]
    e = verify_webhook(v["body"], hdr(v), v["secret"], now=int(v["timestamp"]))
    assert e["deliveryId"] == "9001" and e["signal"]["id"] == "4821"


def test_rejects_tampering():
    v = VECTORS[0]
    now = int(v["timestamp"])
    assert reason(lambda: verify_webhook(v["body"].replace("4821", "4822"), hdr(v), v["secret"], now=now)) == "bad_signature"
    assert reason(lambda: verify_webhook(v["body"], hdr(v), "whsec_wrong", now=now)) == "bad_signature"
    assert reason(lambda: verify_webhook(v["body"], {**hdr(v), "x-lpsignal-timestamp": str(now + 1)}, v["secret"], now=now)) == "bad_signature"


def test_tolerance():
    v = VECTORS[0]
    ts = int(v["timestamp"])
    assert reason(lambda: verify_webhook(v["body"], hdr(v), v["secret"], now=ts + 300)) == "ok"
    assert reason(lambda: verify_webhook(v["body"], hdr(v), v["secret"], now=ts + 301)) == "expired"
    assert reason(lambda: verify_webhook(v["body"], hdr(v), v["secret"], now=ts - 301)) == "expired"


def test_missing_and_malformed():
    v = VECTORS[0]
    assert reason(lambda: verify_webhook(v["body"], {}, v["secret"])) == "missing_headers"
    assert reason(lambda: verify_webhook(v["body"], hdr(v), "")) == "missing_headers"
    assert reason(lambda: verify_webhook(v["body"], {**hdr(v), "x-lpsignal-timestamp": "1e9"}, v["secret"])) == "bad_timestamp"
