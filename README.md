# Cloud Control — AWS Observability API & Dashboard

[![CI](https://github.com/ssambit635-svg/-Aws-dashboard-backend/actions/workflows/ci.yml/badge.svg)](https://github.com/ssambit635-svg/-Aws-dashboard-backend/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-2dd4bf.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-172%20%2B%2049%20DOM-34d399.svg)](#testing)

A production-shaped, read-only **AWS control-plane API** with a zero-dependency
observability dashboard. It reads real account state through **boto3** —
S3 inventory, EC2 fleet, CloudWatch alarms and Cost Explorer — behind a
validated, cached, authenticated REST interface.

> **Runs anywhere.** With no AWS credentials present it transparently falls back
> to a deterministic demo provider so you can clone, run, and click around in
> under a minute. The live AWS code path is the real implementation and is
> selected automatically the moment credentials exist.

```bash
git clone https://github.com/ssambit635-svg/-Aws-dashboard-backend.git
cd -- -Aws-dashboard-backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
# → http://localhost:8000        dashboard
# → http://localhost:8000/docs   OpenAPI / Swagger UI
```

**Reviewing this in a hurry?** Three things are worth your time, in order:
[what it demonstrates](#what-this-project-demonstrates) (feature → skill map),
[`app/providers/aws.py`](app/providers/aws.py) (the real AWS work), and
[`tests/test_aws_provider.py`](tests/test_aws_provider.py) (how SDK code is
tested without an AWS account).

---

## Contents

- [What this project demonstrates](#what-this-project-demonstrates)
- [Features](#features)
- [Configuration](#configuration) · [AWS permissions](#required-aws-permissions)
- [API examples](#api-examples)
- [Architecture](#architecture)
- [Why the demo provider exists](#why-the-demo-provider-exists)
- [Testing](#testing)
- [Docker](#docker)
- [Known limitations](#known-limitations) · [Roadmap](#roadmap)

---

## What this project demonstrates

A map from feature to the engineering skill it evidences. This is the section to
read if you are reviewing the repository.

| Area | What is implemented | Skill demonstrated |
| --- | --- | --- |
| **AWS integration** | `boto3` clients for S3, EC2, CloudWatch and Cost Explorer with bounded retries, explicit timeouts, paginators, and per-service error translation | Real cloud SDK usage, not a mock-up |
| **Service architecture** | `providers/` (data sources) → `services/` (business logic) → `routers/` (HTTP). Routers contain no logic; providers know nothing about HTTP | Layered design, separation of concerns, dependency inversion |
| **Testability** | The `CloudProvider` ABC lets the service layer run against fixtures, while the boto3 provider is verified with `botocore`'s `Stubber` against recorded AWS payloads. 172 tests, no network, no credentials | Designing for tests instead of bolting them on, and knowing how to test SDK code |
| **API design** | Typed Pydantic v2 models for every request and response; consistent `{data, meta}` envelope; cursor-free offset pagination; HTTP-semantic status codes | Contract-first API design, OpenAPI schema generation |
| **Correctness** | Money is `Decimal`, never `float`; ISO-8601 dates; validated AWS ID formats; aggregates computed over the full set, not the current page | Attention to the details that actually break billing systems |
| **Reliability** | TTL read-through cache, `/livez` + `/readyz` probes, typed error envelopes, graceful degradation on partial AWS failures, structured JSON logs with request IDs | Operability — the difference between a demo and a service |
| **Security** | API-key auth (constant-time compare) enforced by a dependency, restricted CORS, security headers, non-root container, secrets only via env, no key ever echoed in a response | Threat-aware defaults |
| **Frontend engineering** | Hand-built SVG chart engine (stacked area with crosshair, donut, sparkline, cumulative), custom CSS design system, no framework or CDN, and 49 DOM-level tests running the real page in jsdom | Understanding the platform instead of importing it, and testing it |
| **UX polish** | Command palette (⌘K), keyboard shortcuts, skeleton loading, auto-refresh with countdown, sortable/filterable tables, CSV/JSON export, full responsive layout | Product sense |
| **Delivery** | Multi-stage Dockerfile, non-root user, healthcheck, docker-compose, Makefile, GitHub Actions (lint + types + tests on 3 Python versions + container smoke test) | Shipping, not just committing |
| **Documentation** | This README, `docs/ARCHITECTURE.md`, `docs/DECISIONS.md` (ADRs), full OpenAPI docs, docstrings that explain *why* | Communicating decisions |

---

## Features

**API**

- `GET /api/v1/overview` — one aggregated call: KPIs, budget, health score, top services
- `GET /api/v1/buckets` — S3 inventory with size, object count, storage class, encryption/versioning/public-access posture, estimated monthly cost
- `GET /api/v1/instances` — EC2 fleet with state, type, AZ, IPs, CloudWatch CPU and tags
- `GET /api/v1/alarms` — CloudWatch metric alarms with thresholds, current values and severity
- `GET /api/v1/billing/summary` — month-to-date unblended cost by service, projected, budget tracking
- `GET /api/v1/billing/trend?days=30` — daily cost, grouped by service
- `GET /api/v1/billing/forecast` — Cost Explorer month-end forecast vs. budget
- `GET /api/v1/system/info` — runtime config, active provider and cache statistics *(authenticated)*
- `GET /livez`, `GET /readyz` — probes that never require credentials, so an orchestrator can reach them; every `/api/v1/*` route is authenticated

**Dashboard**

- KPI strip with sparklines and budget-burn status
- Stacked area chart of daily spend with a crosshair tooltip and togglable series
- Budget gauge, composite health score with an itemised reason list
- Cost-by-service table and donut breakdown
- Bucket, instance and alarm tables with live text filtering, state chips and column sorting
- Cumulative-spend-vs-budget-pace chart
- Command palette (⌘K), shortcuts (`R`, `Space`, `1`–`5`, `/`, `?`), JSON export, auto-refresh
- Mock/live provider badge so demo data can never be mistaken for real billing

---

## Configuration

All settings are environment variables with the `DASH_` prefix (see
[`.env.example`](.env.example)). Copy it to `.env` to override locally.

| Variable | Default | Purpose |
| --- | --- | --- |
| `DASH_PROVIDER_MODE` | `auto` | `auto` (AWS if credentials resolve, else demo), `aws` (require credentials), `mock` |
| `DASH_AWS_REGION` | `us-east-1` | Region for S3/EC2/CloudWatch calls |
| `DASH_COST_EXPLORER_REGION` | `us-east-1` | Cost Explorer's endpoint is global; kept separate because that asymmetry is a real gotcha |
| `DASH_API_KEY` | `local-dev-key` | Shared key required by data endpoints |
| `DASH_AUTH_ENABLED` | `true` | Disable only behind another auth layer |
| `DASH_BOOTSTRAP_DASHBOARD` | `true` | Inject the dev key into the dashboard HTML. **Ignored outside `local`/`staging`** |
| `DASH_MONTHLY_BUDGET` | `8000` | Budget baseline for gauges and burn rate |
| `DASH_CACHE_TTL_SECONDS` | `60` | Read-through cache TTL |
| `DASH_CORS_ORIGINS` | `http://localhost:8000` | Comma-separated allowlist |
| `DASH_LOG_LEVEL` | `INFO` | Log verbosity; logs are structured JSON |

Generate a real key with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### Required AWS permissions

Read-only. Attach this policy to the principal whose credentials the app uses:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:ListAllMyBuckets",
        "s3:GetBucketLocation",
        "s3:GetBucketVersioning",
        "s3:GetBucketPublicAccessBlock"
      ],
      "Resource": "*"
    },
    { "Effect": "Allow", "Action": ["ec2:DescribeInstances"], "Resource": "*" },
    {
      "Effect": "Allow",
      "Action": ["cloudwatch:DescribeAlarms", "cloudwatch:GetMetricData"],
      "Resource": "*"
    },
    { "Effect": "Allow", "Action": ["ce:GetCostAndUsage", "ce:GetCostForecast"], "Resource": "*" },
    { "Effect": "Allow", "Action": ["sts:GetCallerIdentity"], "Resource": "*" }
  ]
}
```

Cost Explorer must be **activated once** in the account before its API returns
data — a step that trips up most first attempts.

---

## API examples

```bash
KEY=local-dev-key

curl -s -H "X-API-Key: $KEY" localhost:8000/api/v1/overview | jq '.data.kpis'
curl -s -H "X-API-Key: $KEY" "localhost:8000/api/v1/buckets?page_size=2" | jq
curl -s -H "X-API-Key: $KEY" "localhost:8000/api/v1/billing/trend?days=7" | jq '.data.points[0]'
curl -s -H "Authorization: Bearer $KEY" localhost:8000/api/v1/alarms?state=ALARM
```

Response envelope:

```json
{
  "data": { "...": "resource payload" },
  "meta": {
    "generated_at": "2026-10-25T07:04:11.482Z",
    "provider": "mock",
    "cached": false,
    "request_id": "b7f1c0a92d34",
    "pagination": { "page": 1, "page_size": 50, "total": 5, "total_pages": 1, "has_next": false }
  }
}
```

Errors are uniform, machine-readable, and never leak internals:

```json
{
  "error": {
    "code": "unauthorized",
    "message": "Missing API credentials. Send an X-API-Key header or a Bearer token.",
    "request_id": "4c3375446504"
  }
}
```

| Code | Status | Meaning |
| --- | --- | --- |
| `unauthorized` | 401 | Missing or invalid credentials |
| `validation_error` | 422 | Bad query parameters (with per-field detail) |
| `not_found` | 404 | Unknown route |
| `provider_unavailable` | 503 | The data source cannot be reached |
| `upstream_aws_error` | 502 | AWS returned an error (code preserved in `detail`) |
| `internal_error` | 500 | Unexpected failure, deliberately opaque |

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  Browser                                                             │
│  index.html + app.js + charts.js + app.css   (no framework, no CDN)  │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  fetch + X-API-Key
┌───────────────────────────────▼──────────────────────────────────────┐
│  FastAPI application                                                 │
│                                                                      │
│  middleware   request-ID · timing · gzip · CORS · security headers   │
│  security     require_api_key  (constant-time compare)               │
│  routers      /overview · /buckets · /instances · /alarms · /billing │
│  services     CloudService  → aggregation, cache, derived metrics    │
│  providers    CloudProvider (ABC)                                    │
│                 ├── AWSProvider   → boto3                            │
│                 └── MockProvider  → deterministic fixtures           │
│  models       Pydantic v2  → validation + OpenAPI schema             │
└───────────────────────────────┬──────────────────────────────────────┘
                                │
                     AWS APIs: S3 · EC2 · CloudWatch · Cost Explorer · STS
```

Dependencies point **inward only**: `routers → services → providers`. The
service layer never imports boto3, and the providers never import FastAPI. That
is what allows the identical suite to run against fixtures and against AWS.

```
app/
├── main.py               application factory, middleware, dashboard route
├── core/
│   ├── config.py         typed settings (pydantic-settings, DASH_ prefix)
│   ├── errors.py         AppError hierarchy + exception handlers
│   ├── security.py       API-key dependency
│   ├── cache.py          TTL cache with hit/miss instrumentation
│   ├── logging.py        structured JSON logging
│   └── deps.py           shared FastAPI dependencies
├── models/               common (Money, Envelope, Pagination), resources, billing, overview
├── providers/
│   ├── base.py           CloudProvider ABC
│   ├── aws.py            boto3 implementation
│   ├── mock.py           deterministic demo implementation
│   └── registry.py       provider selection + fallback
├── services/
│   ├── cloud.py          read-side aggregation, caching, health score
│   └── container.py      composition root (settings + provider + cache)
├── routers/              health, overview, resources, billing
└── static/               dashboard (index.html, css/app.css, js/*.js)
```

More detail: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) ·
Design rationale: [`docs/DECISIONS.md`](docs/DECISIONS.md).

---

## Why the demo provider exists

A portfolio API that only works with someone else's AWS credentials is a
portfolio API nobody can run. The compromise here is **explicit, deterministic
fixtures**:

- **Deterministic.** Every pseudo-random value is seeded from a SHA-256 of a
  stable key (`resource id`, `service + date`). The same request always returns
  the same numbers, so screenshots, tests and the UI stay stable.
- **Realistic.** Real storage-class price rates, real instance-type prices,
  valid 17-character EC2 IDs, weekday/weekend cost shape, and an S3 service line
  that reconciles with the bucket inventory rather than contradicting it.
- **Labelled.** `meta.provider` is `"mock"`, the UI shows a **demo data** badge,
  and `fallback_reason` explains why. The app never pretends to be live.

Set `DASH_PROVIDER_MODE=aws` to make the absence of credentials a hard failure
rather than a fallback — the right setting for production.

---

## Testing

```bash
make test        # 172 tests, ~2s, no network
make test-ui     # 49 dashboard checks in jsdom
make cov         # coverage report
make check       # lint + format + types + tests (fast, no node)
make check-all   # everything CI runs, including the dashboard tests
```

The suite covers HTTP contracts, authentication and its ordering relative to
provider work, pagination maths, cache expiry and hit-rate accounting, provider
selection and credential fallback, money rounding and serialisation, Pydantic
validation boundaries, aggregate consistency (totals equal the sum of rows), and
provider-failure → typed-error mapping.

`tests/test_aws_provider.py` drives the real boto3 request/response cycle through
`botocore.stub.Stubber` — recorded `DescribeInstances`, `GetMetricData`,
`GetCostAndUsage` and S3 payloads, with no network and no credentials. It exists
because the provider is the only code that can break silently in production, and
it covers the specific failure modes that are easy to get wrong: paginated
responses, `AccessDenied` on optional enrichment, multi-region batching, and
every AWS enum that is not in our models.

### Dashboard tests

The frontend has tests too, because a hand-written chart engine and 1,500 lines
of DOM code are exactly as likely to break as the backend:

```bash
make test-ui     # 49 checks; installs jsdom on first run, starts its own server
```

Requires **Node 22.22+** (or 24.15+): `jsdom@30` declares those engines and its
`undici` dependency throws at import time on Node 20. The harness pins jsdom and
commits a lockfile so CI and local runs install the same tree. The Python suite
needs no Node at all.

`tests/js/dashboard.test.mjs` executes the real `index.html`, CSS and JavaScript
in **jsdom** against a live instance of the API — no Chromium download, no
display server, a few seconds per run — and asserts in four suites:

| Suite | What it proves |
| --- | --- |
| rendering | every panel, table and chart draws; no `NaN`/`undefined` reaches the DOM; the demo badge is honest |
| interactions | sorting, filters, state chips, range switching, legend toggling, command palette, keyboard shortcuts, export, `aria-pressed` correctness |
| security | a hostile resource name from the API (`<img onerror=…>`) is rendered as literal text and never becomes live DOM |
| failure handling | a 401 shows an actionable banner, and a successful retry clears it rather than leaving a stale error |

These tests earned their place: they found four defects that the backend suite
could not see — a focus trap that silently disabled every keyboard shortcut, a
renderer chain that a single malformed response could abort, an unhandled
promise rejection on every failed request, and an infinite "enter API key"
dialog loop. `make check-all` runs both suites.

Tests are written against behaviour that would matter in production — for
example `test_instance_ids_match_aws_format` and
`test_billing_total_equals_sum_of_services` exist because the first draft of
this project got both of those wrong.

---

## Docker

```bash
make docker-build
docker run --rm -p 8000:8000 -e DASH_BOOTSTRAP_DASHBOARD=true cloud-control-api:1.0.0
# or, with your local AWS profile mounted:
docker compose up --build
```

Multi-stage build, no compiler in the final image, non-root (UID 10001),
`HEALTHCHECK` against `/livez`, and one worker per container — scale with
replicas.

---

## Known limitations

Honesty is more useful than a longer feature list:

- **Per-resource cost is estimated**, not billed. Cost Explorer's API aggregates
  by service; per-instance dollars require Cost and Usage Reports (CUR) with
  resource IDs enabled. Instance/bucket figures here use published on-demand
  rates and are labelled *estimated* in the UI.
- **Memory utilisation is only populated by the mock provider.** It requires the
  CloudWatch agent to publish `mem_used_percent`; without it, `0.0` is reported
  rather than a fabricated number.
- **Bucket sizes only count the `StandardStorage` class.** `BucketSizeBytes` is
  published per storage class, so an account full of Glacier objects will look
  smaller than its S3 bill implies. Request metrics are also opt-in, so most
  buckets report `0 B` until CloudWatch metrics are enabled.
- **Encryption is assumed, not verified.** The S3 API exposes no single
  "is this bucket encrypted" flag; SSE-S3 is the default for new buckets, so the
  UI treats buckets as encrypted unless told otherwise. Verify with
  `GetBucketEncryption` if you need certainty.
- **`usage_quantity` is a mixed unit.** Cost Explorer returns GB-months for
  storage, hours for compute and invocations for Lambda in the same field. It is
  labelled "usage" rather than "hours", and should not be summed across services.
- **The cache is per-process.** Correct for a read-through cache at this scale;
  a multi-replica deployment would want Redis and a shared TTL.
- **API-key auth is single-tenant.** Fine for an internal ops tool; multi-user
  deployments should swap the dependency for OIDC/JWT — routers would not change.
- **No WebSocket/SSE push.** The dashboard polls every 60s with a visible
  countdown, which is honest about freshness.
- The demo provider's fixture set is intentionally small (5 buckets, 8 instances,
  6 alarms). It is shaped for UI realism, not load testing.

## Roadmap

- [ ] CUR-based per-resource cost attribution
- [ ] Prometheus `/metrics` export
- [ ] Saved views and budget alerts to SNS
- [ ] Redis-backed cache + rate limiting
- [ ] OIDC authentication for multi-tenant use

---

## License

MIT — see [LICENSE](LICENSE).
