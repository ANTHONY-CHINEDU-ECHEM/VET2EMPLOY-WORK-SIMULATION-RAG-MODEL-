#!/usr/bin/env python3
"""Smoke test a running Vet2Employ RAG service.

Run it after every deployment. It uses only the standard library so it works anywhere.

    python scripts/smoke_test.py --base-url http://localhost:8000 --offline
    python scripts/smoke_test.py --base-url https://rag.internal --api-key "$V2E_API_KEY"

--offline   checks what works without OpenRouter or a loaded knowledge base: liveness,
            the error envelope, request correlation, headers and the safeguarding
            intercept (which never calls a model).
default     also requires readiness and runs one retrieval, one verified task and one
            manager reply end to end. These calls use OpenRouter and cost a few cents.

Exit status is 0 when every check passes and 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any


class Smoke:
    def __init__(self, base_url: str, api_key: str | None, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.failures = 0

    def call(
        self, method: str, path: str, body: dict[str, Any] | None = None, auth: bool = True
    ) -> tuple[int, dict[str, str], Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        request.add_header("X-Request-ID", f"smoke-{uuid.uuid4()}")
        if auth and self.api_key:
            request.add_header("X-API-Key", self.api_key)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                status, headers = response.status, dict(response.headers)
        except urllib.error.HTTPError as error:
            raw = error.read()
            status, headers = error.code, dict(error.headers)
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = raw.decode("utf-8", "replace")
        return status, {k.lower(): v for k, v in headers.items()}, payload

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")
        if not ok:
            self.failures += 1


def run(smoke: Smoke, offline: bool) -> None:
    status, headers, body = smoke.call("GET", "/health/live", auth=False)
    smoke.check("liveness", status == 200 and body.get("status") == "ok", str(body))
    smoke.check("request id echoed", headers.get("x-request-id", "").startswith("smoke-"))
    smoke.check("nosniff header", headers.get("x-content-type-options") == "nosniff")

    status, _, body = smoke.call("POST", "/v1/tasks/generate", {"track": "XX"})
    envelope = isinstance(body, dict) and {"error", "message", "details", "request_id"} <= set(body)
    smoke.check("error envelope on invalid input", status in (401, 422) and envelope, str(status))

    status, _, body = smoke.call(
        "POST",
        "/v1/manager/reply",
        {
            "track": "DA",
            "candidate_first_name": "Smoke",
            "candidate_message": "I have been thinking about ending my life.",
        },
    )
    intercepted = (
        status == 200
        and body["safeguarding"]["action"] == "intercept"
        and body["reply"]["needs_human_review"] is True
    )
    smoke.check("safeguarding intercept", intercepted, f"status {status}")

    if offline:
        return

    status, _, body = smoke.call("GET", "/health/ready", auth=False)
    smoke.check("readiness", status == 200, json.dumps(body.get("checks", {}))[:300])

    status, _, body = smoke.call(
        "POST", "/v1/retrieve", {"query": "When is the data quality log due?", "track": "DA"}
    )
    smoke.check("retrieval returns evidence", status == 200 and bool(body.get("evidence")))

    started = time.perf_counter()
    status, _, body = smoke.call(
        "POST",
        "/v1/tasks/generate",
        {"track": "DA", "activity_number": 3, "simulation_day": "Monday"},
    )
    verdict = body.get("verification", {}).get("verdict") if status == 200 else body.get("error")
    smoke.check(
        "verified task",
        status == 200 and str(verdict).startswith("approved"),
        f"{verdict} in {time.perf_counter() - started:.1f}s",
    )

    status, _, body = smoke.call(
        "POST",
        "/v1/manager/reply",
        {
            "track": "DA",
            "candidate_message": "What file name should I use for the data quality log?",
        },
    )
    smoke.check(
        "manager reply",
        status == 200 and body["verification"]["verdict"] != "intercepted",
        str(body.get("verification", {}).get("verdict") if status == 200 else body),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()
    smoke = Smoke(args.base_url, args.api_key, args.timeout)
    try:
        run(smoke, args.offline)
    except (urllib.error.URLError, KeyError, TypeError, AttributeError) as exc:
        smoke.check("service reachable and responses well formed", False, repr(exc))
    print(f"\n{smoke.failures} failure(s)")
    return 1 if smoke.failures else 0


if __name__ == "__main__":
    sys.exit(main())
