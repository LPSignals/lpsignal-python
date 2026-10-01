import json
from pathlib import Path

import httpx

VECTORS = json.loads((Path(__file__).resolve().parents[1] / "testdata" / "webhook-vectors.json").read_text("utf8"))["vectors"]


def sig(i: int) -> dict:
    return {"id": str(i), "kind": "depeg", "firedAt": "2026-09-30T00:00:00.000Z"}


def signals_api(table: list[int]):
    """A mock REST API whose signal table is `table` (served newest first, paged by `before`)."""
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        limit = int(req.url.params.get("limit", 50))
        before = req.url.params.get("before")
        rows = [sig(i) for i in sorted(table, reverse=True) if not before or i < int(before)][:limit]
        return httpx.Response(200, json={"signals": rows, "next": rows[-1]["id"] if len(rows) == limit else None})

    return handler, calls
