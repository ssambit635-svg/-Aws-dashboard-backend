# Architecture

## The one rule

Dependencies point inward:

```
routers  →  services  →  providers
   ↓           ↓            ↓
 models      models       models
```

- **Routers** parse HTTP, call one service method, wrap the result in an envelope.
  They contain no aggregation, no caching and no AWS knowledge.
- **Services** own business logic: aggregation, cache policy, derived metrics
  (health score, budget burn), pagination.
- **Providers** own the outside world: boto3 calls, paginators, error translation.
  They know nothing about FastAPI.

The payoff is concrete: `tests/test_services.py` exercises the full aggregation
and caching path with no HTTP layer, and `tests/test_api.py` exercises the full
HTTP contract with no AWS. Neither test file knows how the other half works.

## Request lifecycle

```
client
  │  GET /api/v1/instances?state=running
  ▼
middleware        assigns X-Request-ID, starts a timer
  ▼
dependency        require_api_key  ──fail──▶ 401 {error:{code:"unauthorized"}}
  ▼
dependency        cloud_service    ──▶ CloudService(provider, settings, cache)
  ▼
router            filters, paginates, builds InstanceList
  ▼
service           cache hit?  ──no──▶ provider.list_instances() ──▶ boto3
  │                        └──yes──▶ cached list
  ▼
router            Envelope[InstanceList] + Meta(provider, cached, request_id)
  ▼
middleware        X-Request-ID, X-Response-Time-ms, security headers, JSON log line
  ▼
client
```

## Provider selection

`app/providers/registry.py` resolves `DASH_PROVIDER_MODE`:

| Mode | Behaviour |
| --- | --- |
| `mock` | Always fixtures. Used by the test suite and CI. |
| `aws` | Always boto3. Raises `ProviderUnavailableError` (→ 503) if no credentials resolve. |
| `auto` | boto3 if `boto3.Session().get_credentials()` returns a principal, else mock. |

Credential detection is a *local* check — it does not make a network call, so
startup stays fast. Fallback is never silent: `ProviderInfo.fallback_reason` is
returned by `/api/v1/system/info` and rendered in the UI.

## Caching

`TTLCache` is a bounded, thread-safe, in-process read-through cache. Keys are
derived from the call (`buckets`, `trend:30`, `costs:2026-10-01:2026-10-25`).

Why it exists: Cost Explorer is limited to roughly one request per second per
account. Without a cache, a dashboard refreshing every 60 seconds from several
tabs will start collecting `ThrottlingException`. Caching here is a quota
protection mechanism, not a micro-optimisation.

Hit/miss/eviction counters are exposed on `/api/v1/system/info` and shown in the
dashboard footer, so cache behaviour is observable rather than assumed.

## Error handling

`AppError` subclasses carry both an HTTP status and a stable machine-readable
`code`. Handlers in `core/errors.py` render them into one envelope shape. The
catch-all `Exception` handler returns an opaque 500 — internal details are
logged, never returned.

`AWSProvider._raise_upstream` converts `ClientError` into `UpstreamAWSError`
preserving the AWS error code in `detail`, so `AccessDenied` on Cost Explorer
surfaces as an actionable message instead of a generic 500.

## Frontend

The dashboard is three plain scripts and one stylesheet, served as static files:

| File | Responsibility |
| --- | --- |
| `api.js` | fetch wrapper: auth header, 15s timeout, in-flight de-duplication, uniform `ApiError` |
| `charts.js` | SVG chart primitives: sparkline, stacked area (crosshair + tooltip + legend), cumulative line, donut |
| `app.js` | state, one renderer per panel, interactions, polling, palette, shortcuts |

Deliberate choices:

- **No framework, no CDN, no build step.** The page works offline, has no
  supply-chain surface, and there is nothing to compile in CI.
- **Server-rendered numbers, client-rendered layout.** All arithmetic that
  matters (money, totals, percentages) happens in Python with `Decimal`. The
  browser only formats.
- **Explicit loading and error states.** Skeletons on first paint, a retry bar
  on failure, and an auth prompt when a 401 is returned.
- **Progressive degradation.** Charts fall back to empty states; the layout
  collapses to a single column with a bottom tab bar on mobile; `prefers-reduced-motion`
  disables animation.

## Scaling notes

The current design is a single stateless process. To run it at larger scale:

1. Move the cache to Redis (one shared TTL, warm across replicas).
2. Add rate limiting per API key (`slowapi` or an API gateway in front).
3. Replace API keys with OIDC/JWT; the `require_api_key` dependency is the only
   thing that changes.
4. Split cost aggregation into a scheduled job writing to a store, so the UI
   reads pre-computed rollups instead of calling Cost Explorer per view.

Nothing above requires touching the router contract, which is the point of
keeping AWS behind an interface.
