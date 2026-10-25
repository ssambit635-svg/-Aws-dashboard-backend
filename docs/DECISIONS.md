# Design decisions (ADRs)

Short records of the choices that shaped this codebase, including the ones that
were wrong at first. Written the way a team would write them.

---

## ADR-001 — Money is `Decimal`, never `float`

**Status:** accepted

**Context.** The first version summed `round(random.uniform(...), 0)` floats and
reported a total rounded to two decimals. That produces totals that do not equal
the sum of their components — the single most corrosive bug a billing dashboard
can ship.

**Decision.** Amounts are `Decimal` internally, quantised to cents with
`ROUND_HALF_UP`, and serialised as fixed-precision *strings* (`"5172.49"`), not
JSON numbers. JSON has no decimal type; a float on the wire reintroduces the
problem in the browser.

**Consequences.** Consumers must parse strings, which is explicit and correct.
`Money` is frozen, so it cannot be mutated in place. Tests assert that
`0.1 + 0.2 == "0.30"`.

---

## ADR-002 — A demo provider ships alongside the real one

**Status:** accepted

**Context.** An AWS dashboard that only runs with the author's credentials
cannot be reviewed. But a dashboard with *only* mock data does not demonstrate
AWS ability — which was the exact flaw in the original version of this project.

**Decision.** Keep both behind a `CloudProvider` ABC, selected at runtime by
`DASH_PROVIDER_MODE`. `auto` prefers real credentials and falls back to fixtures
only when none exist.

**Consequences.** The code path a reviewer reads is the one that runs in
production. The demo path is labelled everywhere (`meta.provider`, a UI badge,
`fallback_reason`), so it can never be mistaken for live billing. The ABC costs
one extra indirection and buys complete test isolation.

---

## ADR-003 — Demo data is deterministic, not random

**Status:** accepted

**Context.** The original `/billing` endpoint returned a different invoice number
for the same service on every refresh. Any user would notice within seconds.

**Decision.** Seed a `random.Random` from a SHA-256 of stable identifiers
(`("trend", service, iso_date)`). Same input → same output, forever.

**Consequences.** Screenshots are reproducible, UI changes can be compared
honestly, and tests can assert on values. Determinism must not collapse into a
flat line, so a test asserts the cost series has more than five distinct values
and that weekends are cheaper than weekdays.

---

## ADR-004 — Boto3 is confined to `providers/aws.py`

**Status:** accepted

**Context.** Importing `boto3` inside routers spreads AWS error handling
everywhere and makes tests require credentials.

**Decision.** Only `providers/aws.py` imports boto3. Everything above it works
with Pydantic models.

**Consequences.** The service layer is trivially testable, and swapping boto3 for
another SDK — or an HTTP client — is a single-file change.

---

## ADR-005 — Partial data beats a 500

**Status:** accepted

**Context.** Bucket sizes are CloudWatch metrics, not S3 metadata. Instances need
a second API call for CPU. Either call can fail with `AccessDenied` on an IAM
policy that only grants `ListAllMyBuckets`.

**Decision.** Enrichment calls are best-effort: they log at INFO and fall back to
a documented default (`0` bytes, `0.0`% CPU) instead of failing the request.
Core listing calls still fail loudly with a typed error.

**Consequences.** A dashboard with 0% CPU columns is better than no dashboard.
The defaults are honest zeros, not invented values.

---

## ADR-006 — Aggregates describe the account, not the current page

**Status:** accepted

**Context.** A test caught `/api/v1/instances?state=running` returning
`by_state: {running: 6}` — computed *after* filtering. The UI renders those
counts as filter chips, so the chips contradicted the data.

**Decision.** Totals (`total_size_bytes`, `total_objects`, `by_state`) are
calculated over the full result set, before filtering and pagination.

**Consequences.** `?page=1&page_size=1` still reports account-wide totals, which
is what a header or a chip needs. Pagination metadata describes the page; the
totals describe the resource collection.

---

## ADR-007 — Offset pagination, not cursors

**Status:** accepted

**Context.** Cursor pagination is better for large, mutating collections.

**Decision.** Use `page`/`page_size` with a bounded `page_size` (max 100).

**Consequences.** Simple, cacheable, and the UI can jump to a page. Wrong choice
above roughly 100k mutable rows; at that point move to cursors and accept the
loss of random access. Documented rather than silently assumed.

---

## ADR-008 — API-key auth, with an explicit upgrade path

**Status:** accepted

**Context.** A cost and inventory API should not be anonymous, but a full OIDC
integration would dwarf the rest of the project.

**Decision.** Shared key via `X-API-Key` or `Bearer`, compared with
`hmac.compare_digest`, enforced by one dependency. `/docs`, `/livez` and
`/readyz` stay public.

**Consequences.** The key is injected into the dashboard HTML *only* in `local`
and `staging`. In production, `DASH_BOOTSTRAP_DASHBOARD` is ignored, so the key
can never leak through page source. Tests assert that no response body contains
the key and that auth is checked *before* provider work happens.

---

## ADR-009 — Hand-written SVG charts

**Status:** accepted

**Context.** Charting libraries are 150–400 KB, add a supply-chain surface, and
impose their own rendering model on the few charts needed here.

**Decision.** Write the four primitives needed — sparkline, stacked area,
cumulative line, donut — in ~350 lines of dependency-free SVG.

**Consequences.** Full control over the details that make a chart readable:
fixed gridlines, compact axis labels, a crosshair that snaps to the nearest day,
a tooltip that never overflows the panel, and a legend that re-stacks the areas
when a series is hidden. Copy-paste is thinner but the result is smaller,
faster and inspectable.

---

## ADR-010 — One image, one worker

**Status:** accepted

**Context.** Multiple uvicorn workers inside one container multiply memory and
fragment the in-process cache.

**Decision.** One worker per container; scale horizontally with replicas. The
Dockerfile uses a multi-stage build, drops to UID 10001, and health-checks
`/livez`.

**Consequences.** Cache hit rate per replica is lower than with a shared cache;
that is the trigger for moving to Redis (see ARCHITECTURE → Scaling notes).

---

## ADR-011 — AWS enums are coerced, never trusted

**Status:** accepted

**Context.** The instance model accepted five states. EC2 returns six. Any
instance in `shutting-down` therefore failed validation and took the entire
`/instances` endpoint down with a 500 — a bug that only appears on a live
account, at the exact moment someone is terminating something.

Two more of the same shape existed: `ComparisonOperator` has seven values, and
treating anything other than `LessThanThreshold` as "above" silently inverted
`LessThanOrEqualToThreshold` alarms; and `AlarmState` would raise on any state a
future API version adds.

**Decision.** Map AWS values onto our enums explicitly (`_coerce_instance_state`,
`_normalise_comparison`, `_coerce_alarm_state`), degrading to a documented
default instead of raising. Tests pin every branch against real payloads.

**Consequences.** A dashboard degrades on unexpected input rather than 500ing,
which is the correct behaviour for a monitoring tool: an unrecognised value
should be visible, not fatal. The trade-off is that genuinely new AWS states
render as `unknown` until someone adds them — visible, and easy to spot.

---

## ADR-012 — The dashboard renders each panel independently

**Status:** accepted

**Context.** `renderAll()` called twelve renderers in sequence. A 200 response
with an unexpected shape — a proxy error page, a version skew, a truncated body
— threw inside `renderKPIs` and aborted the remaining eleven, producing a page
that was half-rendered with no explanation.

**Decision.** Run each renderer in its own try/catch, collect failures, and show
one error bar naming the panels that failed. Each renderer also validates the
shape it depends on before indexing into it.

**Consequences.** One malformed endpoint costs one panel. The user is told which
panel is missing instead of staring at a broken grid. The cost is a small amount
of defensive code that the type system would otherwise provide — acceptable for
a dependency-free frontend.
