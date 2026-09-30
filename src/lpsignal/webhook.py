from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from typing import Any, Mapping, Optional, Union


class WebhookVerificationError(Exception):
    """`reason` is one of: missing_headers, bad_timestamp, expired, bad_signature, bad_body."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"LPSignal webhook rejected: {reason}")


def _header(headers: Mapping[str, Any], name: str) -> Optional[str]:
    for k, v in headers.items():
        if k.lower() == name:
            return v[0] if isinstance(v, (list, tuple)) else v
    return None


def verify_webhook(raw_body: Union[bytes, str], headers: Mapping[str, Any], secret: str,
                   tolerance_sec: int = 300, now: Optional[float] = None) -> dict[str, Any]:
    """Verify a webhook delivery and return its parsed body.

    `x-lpsignal-signature` is `sha256=` + hex HMAC-SHA256 of `"<x-lpsignal-timestamp>.<raw body>"`, keyed with the
    secret returned when the webhook was set. Pass the raw request bytes, before any JSON parsing. Deliveries older
    than `tolerance_sec` are rejected as replays; each retry is signed afresh.

    Deliveries are at-least-once: skip `deliveryId`s you have already handled.
    """
    ts = _header(headers, "x-lpsignal-timestamp")
    sig = _header(headers, "x-lpsignal-signature")
    if not ts or not sig or not secret:
        raise WebhookVerificationError("missing_headers")
    if not re.fullmatch(r"\d{1,12}", ts):
        raise WebhookVerificationError("bad_timestamp")
    if abs((time.time() if now is None else now) - int(ts)) > tolerance_sec:
        raise WebhookVerificationError("expired")
    body = raw_body.encode("utf8") if isinstance(raw_body, str) else raw_body
    want = "sha256=" + hmac.new(secret.encode("utf8"), ts.encode("ascii") + b"." + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig.encode("utf8"), want.encode("ascii")):
        raise WebhookVerificationError("bad_signature")
    try:
        return json.loads(body)
    except ValueError:
        raise WebhookVerificationError("bad_body") from None
