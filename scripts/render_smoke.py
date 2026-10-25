#!/usr/bin/env python3
"""Prove the app works in the shape a PaaS runs it, not the shape we develop in.

Render (and Fly, Cloud Run, ECS) do three things a `uvicorn --reload` session
does not:

* assign the port through ``$PORT`` instead of 8000,
* run with ``DASH_ENVIRONMENT=production``, which changes what is injected into
  the page and which startup checks apply,
* put a TLS-terminating proxy in front and forward ``X-Forwarded-*`` headers.

A deployment that only ever ran on :8000 with the development defaults can still
fail all three. This script boots the server exactly the way the container CMD
does, then checks every route the OpenAPI schema advertises, every asset
``index.html`` asks for, and that no secret reaches the served page.

    python scripts/render_smoke.py --boot            # boots, checks, exits
    python scripts/render_smoke.py --url http://...  # checks an existing one

Exits non-zero and prints the failures. Standard library only, so it runs in the
runtime image with no extra install and in CI without a venv.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

TIMEOUT = 10
DEV_KEY = "local-dev-key"  # the value committed in .env.example


@dataclass
class Result:
    name: str
    ok: bool
    detail: str = ""


def get(url: str) -> tuple[int, bytes, dict[str, str]]:
    request = urllib.request.Request(url, headers={"X-Forwarded-Proto": "https"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)
    except urllib.error.URLError as exc:
        return 0, str(exc).encode(), {}


def check_status(
    results: list[Result], base: str, path: str, expected: int = 200
) -> tuple[int, bytes, dict[str, str]]:
    status, body, headers = get(base + path)
    results.append(
        Result(
            f"GET {path}",
            status == expected,
            "" if status == expected else f"expected {expected}, got {status}",
        )
    )
    return status, body, headers


def smoke(base: str, expect_key: str | None) -> list[Result]:
    results: list[Result] = []
    base = base.rstrip("/")

    # ── liveness and readiness ───────────────────────────────────────────
    status, body, _ = check_status(results, base, "/livez")
    if status == 200:
        try:
            results.append(Result("/livez payload", json.loads(body)["status"] == "alive"))
        except (ValueError, KeyError):
            results.append(Result("/livez payload", False, "not the expected JSON"))

    status, body, _ = check_status(results, base, "/readyz")
    if status == 200:
        try:
            payload = json.loads(body)
            # "degraded" is legitimate in a deployment with no AWS credentials:
            # the process is healthy, the upstream is not configured.
            results.append(
                Result(
                    "/readyz reports a known state",
                    payload.get("status") in {"ready", "degraded"},
                    f"status={payload.get('status')!r}",
                )
            )
        except ValueError:
            results.append(Result("/readyz is JSON", False))

    # ── the page and its assets ──────────────────────────────────────────
    status, html, headers = check_status(results, base, "/")
    page = html.decode("utf-8", "replace") if status == 200 else ""
    if status == 200:
        results.append(Result("page is HTML", "text/html" in headers.get("content-type", "")))
        results.append(Result("page has the dashboard title", "Cloud Control" in page))
        leaks_key = DEV_KEY in page
        results.append(
            Result(
                "page does not leak the development key",
                not leaks_key,
                "the committed development key is in the served HTML" if leaks_key else "",
            )
        )

        csp = headers.get("content-security-policy", "")
        if csp:
            results.append(Result("CSP present", True, csp[:60]))

        assets = sorted(set(re.findall(r'(?:src|href)="(/static/[^"]+)"', page)))
        results.append(Result("page references static assets", bool(assets)))
        for asset in assets:
            asset_status, _, asset_headers = get(base + asset)
            ok = asset_status == 200 and len(asset_headers.get("content-type", "")) > 0
            results.append(Result(f"asset {asset}", ok, f"status {asset_status}"))

    check_status(results, base, "/docs")
    status, schema_body, _ = check_status(results, base, "/openapi.json")

    # ── every advertised route actually answers ──────────────────────────
    api_paths: list[str] = []
    if status == 200:
        try:
            schema = json.loads(schema_body)
        except ValueError:
            results.append(Result("openapi.json is JSON", False))
            schema = {}

        # FastAPI emits parameterised paths like /api/v1/buckets/{name}; those
        # need a real resource name, so only probe the literal ones.
        for path, operations in schema.get("paths", {}).items():
            if "get" not in operations or "{" in path:
                continue
            if path in {"/livez", "/readyz", "/", "/health", "/docs", "/openapi.json"}:
                continue
            api_paths.append(path)

        results.append(Result("schema advertises API routes", bool(api_paths)))

        for path in api_paths:
            request = urllib.request.Request(
                base + path,
                headers={"X-Forwarded-Proto": "https"},
            )
            if expect_key:
                request.add_header("X-API-Key", expect_key)
            try:
                with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                    code, payload = response.status, response.read()
                    content_type = response.headers.get("content-type", "")
            except urllib.error.HTTPError as exc:
                code, payload, content_type = exc.code, exc.read(), ""

            if code == 401 and not expect_key:
                # Auth is on and we were not given a key: the route is meant to
                # be closed, which is itself the correct answer.
                results.append(Result(f"GET {path}", True, "401 as expected (auth on)"))
                continue

            ok = code == 200 and "application/json" in content_type
            detail = "" if ok else f"status {code}, content-type {content_type!r}"
            if ok and DEV_KEY.encode() in payload:
                ok, detail = False, "response body contains the development key"
            results.append(Result(f"GET {path}", ok, detail))

    return results


def wait_for_livez(base: str, seconds: float = 40.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        status, _, _ = get(base + "/livez")
        if status == 200:
            return True
        time.sleep(0.5)
    return False


def boot(port: int) -> subprocess.Popen[bytes]:
    """Run the container's CMD, with the platform's environment."""
    env = dict(os.environ)
    env.update(
        {
            "PORT": str(port),
            "DASH_ENVIRONMENT": "production",
            "DASH_AUTH_ENABLED": "false",
            "DASH_PROVIDER_MODE": "auto",
            # A deployed container has no AWS credentials unless a role is
            # attached; make that explicit so the run cannot silently use ours.
            "AWS_ACCESS_KEY_ID": "",
            "AWS_SECRET_ACCESS_KEY": "",
            "AWS_PROFILE": "",
        }
    )
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        str(port),
        "--proxy-headers",
        "--forwarded-allow-ips=*",
    ]
    return subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="check an already-running deployment")
    parser.add_argument("--boot", action="store_true", help="start the app first")
    parser.add_argument(
        "--port", type=int, default=10000, help="port when booting (Render uses 10000)"
    )
    parser.add_argument("--key", help="API key to use for protected routes")
    args = parser.parse_args()

    process: subprocess.Popen[bytes] | None = None
    if args.boot:
        port = args.port
        base = f"http://127.0.0.1:{port}"
        print(f"booting like a PaaS: PORT={port}, DASH_ENVIRONMENT=production, auth off")
        process = boot(port)
    elif args.url:
        base = args.url
    else:
        parser.error("pass --boot or --url")

    try:
        if not wait_for_livez(base):
            print(f"FAIL: {base}/livez never became ready", file=sys.stderr)
            if process and process.stderr:
                tail = process.stderr.read().decode("utf-8", "replace")[-2000:]
                print(tail, file=sys.stderr)
            return 1

        results = smoke(base, args.key)
    finally:
        if process:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()

    failures = [r for r in results if not r.ok]
    for result in results:
        mark = "PASS" if result.ok else "FAIL"
        suffix = f"  ({result.detail})" if result.detail else ""
        print(f"{mark}  {result.name}{suffix}")

    print(f"\n{len(results) - len(failures)}/{len(results)} checks passed")
    if failures:
        print("This shape is NOT deploy-ready.", file=sys.stderr)
        return 1
    print("Deploy shape verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
