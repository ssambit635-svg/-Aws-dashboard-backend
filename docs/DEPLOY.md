# Deploying Cloud Control

There are three ways to look at this application. Pick by what you need.

| | Where | Lifetime | Setup |
| --- | --- | --- | --- |
| **Ephemeral preview** | `https://<port>-<sandbox-id>.e2b.app` | Dies with the sandbox session | none — the sandbox proxy already forwards to `0.0.0.0:8000` |
| **Local** | `http://localhost:8000` | As long as your terminal | `uvicorn app.main:app --reload` |
| **Permanent public URL** | Render / Fly / any Docker host / AWS | Until you delete it | ~5 minutes, recipes below |

Everything below deploys the *same* container image. There is no second build
path to keep in sync.

---

## Before you deploy: decide the mode

Two environment variables decide what a visitor gets. Getting them wrong is the
usual cause of "it works locally but the deployed site is broken".

| Variable | Value | Effect |
| --- | --- | --- |
| `DASH_PROVIDER_MODE` | `auto` *(default)* | Uses real AWS when credentials resolve, otherwise the deterministic demo data. The UI shows a **demo data** badge when it falls back. |
| | `aws` | Requires credentials. Fails loudly at request time if there are none. |
| | `mock` | Always demo data. |
| `DASH_ENVIRONMENT` | `production` | Disables API-key injection into the page. **Refuses to start if `DASH_API_KEY` is still the default.** |
| | `local` | Injects the key into the page so `localhost` needs zero setup. |

So there are exactly two sensible deployment shapes:

**A. Public read-only demo** — a link you can put on a CV. No AWS account, no
secrets, deterministic data.

```env
DASH_ENVIRONMENT=production
DASH_AUTH_ENABLED=false        # nothing secret is exposed; the data is fixtures
DASH_PROVIDER_MODE=auto        # no credentials -> demo data
```

**B. Real account, private** — an internal ops tool. Keep auth on.

```env
DASH_ENVIRONMENT=production
DASH_API_KEY=<openssl rand -base64 32>   # required; the app refuses to boot otherwise
DASH_PROVIDER_MODE=aws
AWS_REGION=us-east-1
```

For shape B on AWS, do **not** put access keys in environment variables. Attach
the read-only IAM policy from the [README](../README.md#required-aws-permissions)
to the ECS task role or App Runner instance role and let the SDK's default
credential chain find them.

For shape B, the dashboard also needs the key in the browser. Options, in order
of preference: put it behind your own SSO/reverse proxy; or share
`https://your-host/?key=<key>` (the page stores it in `localStorage`).

---

## Option 1 — Render (fastest, free tier)

Short answer: **yes, this deploys on Render with no edits.** Here is exactly what
happens, and the three details that make it work.

1. Render dashboard → **New** → **Blueprint** → select this repository.
2. Render finds [`render.yaml`](../render.yaml), reads `dockerfilePath: ./Dockerfile`,
   and shows one service on the **free** plan.
3. **Apply.** First build is 2–4 minutes (dependency install dominates; the
   layers cache afterwards, so later deploys are ~40 seconds).

| What Render needs | Where it is handled |
| --- | --- |
| A `$PORT`-aware server | `CMD` runs `uvicorn … --port ${PORT:-8000}`; Render sets `PORT=10000` |
| A health check | `render.yaml` → `healthCheckPath: /livez`, a cheap route with no AWS calls |
| Container health that doesn't contradict it | The image's own `HEALTHCHECK` reads `$PORT` too |
| TLS-terminating proxy awareness | `--proxy-headers --forwarded-allow-ips='*'` |
| Readiness at boot | The app has no migrations and no database; `/livez` answers as soon as uvicorn binds |

Verify it is genuinely up (not just "deployed"):

```bash
HOST=https://cloud-control.onrender.com       # your service URL
curl -s $HOST/livez                            # {"status":"alive"}
curl -s $HOST/readyz                           # {"status":"ready", "provider": {"mode": "mock"}}
curl -s $HOST/api/v1/overview | head -c 120    # works: auth is off in the demo shape
open $HOST                                     # dashboard, with the demo-data badge
```

The same check, run against any URL, is in the repository:

```bash
python scripts/render_smoke.py --url $HOST
```

It walks every route in the OpenAPI schema and every asset the page requests,
and fails if the development key reaches the served HTML. `make render-check`
does the same against a locally booted server using Render's port and
environment, so a breaking change is caught before you push, not after.

Notes:

- The free plan **sleeps after ~15 minutes of inactivity**; the next request
  takes ~30 seconds to wake. Fine for a portfolio link, not for a talk.
  Paid instance types remove this; so does a cron job that pings `/livez`.
- Free instances get 512 MB. This app uses well under 100 MB.
- Custom domain and HTTPS are handled by Render.
- `autoDeploy: true` means every push to `main` redeploys. That is what you want
  for a demo; remove the line if you would rather deploy by hand.
- Blueprints are applied from the **default branch**, so `render.yaml` must be
  on `main` (it is).

## Option 2 — Fly.io

[`fly.toml`](../fly.toml) is included.

```bash
fly launch --no-deploy --copy-config --name cloud-control-<yourname>
fly secrets set DASH_API_KEY="$(openssl rand -base64 32)"   # shape B only
fly deploy
fly open
```

For the public demo shape, remove `DASH_AUTH_ENABLED` from `[env]` or leave it
as `"false"` — the config in this repository already does the demo shape.

`min_machines_running = 0` lets the machine scale to zero when idle; set it to
`1` if you want no cold start.

## Option 3 — Any Docker host or VPS

```bash
docker build -t cloud-control:1.0.0 .

docker run -d --name cloud-control \
  -p 8000:8000 \
  -e DASH_ENVIRONMENT=production \
  -e DASH_AUTH_ENABLED=false \
  -e DASH_PROVIDER_MODE=auto \
  --restart unless-stopped \
  cloud-control:1.0.0
```

Put Caddy, nginx or Traefik in front for TLS. With Caddy it is two lines:

```
your.domain {
    reverse_proxy 127.0.0.1:8000
}
```

> Running the image *without* `DASH_API_KEY` or `DASH_AUTH_ENABLED=false` exits
> immediately with an explanation. That is deliberate: the image defaults to
> `DASH_ENVIRONMENT=production`, and starting a public API with the repository's
> development key is exactly the mistake the guard exists to prevent.

## Option 4 — AWS (this is an AWS project, after all)

### ECS Fargate + task role

The shipped [`deploy/aws/task-definition.json`](../deploy/aws/task-definition.json)
runs one container behind an ALB. The important part is `taskRoleArn`: the task
reads S3/EC2/CloudWatch/Cost Explorer with a role, never with static keys.

```bash
AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
AWS_REGION=us-east-1
REPO="$AWS_ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/cloud-control"

# 1. build and push
aws ecr create-repository --repository-name cloud-control --region "$AWS_REGION"
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$AWS_ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com"
docker build -t "$REPO:1.0.0" .
docker push "$REPO:1.0.0"

# 2. register the task definition (edit the file: account id, execution role)
aws ecs register-task-definition --cli-input-json file://deploy/aws/task-definition.json

# 3. cluster + service behind an ALB
aws ecs create-cluster --cluster-name cloud-control
aws ecs create-service --cluster cloud-control --service-name cloud-control \
  --task-definition cloud-control --desired-count 1 --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-xxxx],securityGroups=[sg-xxxx],assignPublicIp=ENABLED}"
```

Target group health check: **path `/livez`, port 8000, protocol HTTP**. Use
`/readyz` instead if you want unhealthy tasks replaced when AWS calls start
failing.

### AWS App Runner (simpler alternative)

App Runner can build straight from the repository and takes one JSON file; point
its health check at `/livez`, set the instance role to the read-only policy, and
pass the same environment variables.

### Cost warning

Fargate + ALB is roughly $25–35/month if left running. For a portfolio link,
Render or Fly is the pragmatic choice; use Fargate if the deploy itself is part
of what you are demonstrating (it is a genuinely useful thing to show).

---

## Publishing the image

[`release.yml`](../.github/workflows/release.yml) builds a multi-arch image and
pushes it to GitHub Container Registry when you push a tag:

```bash
git tag v1.0.0 && git push origin v1.0.0
# -> ghcr.io/ssambit635-svg/cloud-control:1.0.0
```

No secrets to configure: it authenticates with the workflow's `GITHUB_TOKEN`.
Then any host can run it without building:

```bash
docker run -p 8000:8000 \
  -e DASH_ENVIRONMENT=production -e DASH_AUTH_ENABLED=false \
  ghcr.io/ssambit635-svg/cloud-control:1.0.0
```

---

## Post-deploy checklist

The one-command version, which covers every item below except the AWS-specific
warnings:

```bash
python scripts/render_smoke.py --url https://your-deployment
```


```bash
HOST=https://your-deployment
curl -s $HOST/livez                      # {"status":"alive"}
curl -s $HOST/readyz                     # {"status":"ready","provider":"mock",...}
curl -s -H "X-API-Key: $KEY" $HOST/api/v1/overview | head -c 200
open $HOST/docs                          # Swagger UI
open $HOST                               # dashboard
```

- **The dashboard shows a `demo data` badge.** Expected with no AWS credentials;
  `provider.fallback_reason` on `/api/v1/system/info` says why.
- **`/readyz` returns `degraded`.** The process is healthy but `sts:GetCallerIdentity`
  failed — usually a missing task role.
- **`UpstreamAWSError: ce:GetCostAndUsage AccessDenied`.** Cost Explorer has not
  been activated in the account, or the role lacks `ce:GetCostAndUsage`.
  Activation takes a few hours to backfill the first time.
- **Billing shows zeros.** Cost Explorer only returns data after activation and
  after at least one day of usage.
- **Bucket sizes show `0 B`.** `BucketSizeBytes` needs CloudWatch request metrics
  enabled on the bucket, and only counts `StandardStorage`.

---

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Container exits immediately, log says `DASH_API_KEY is still the development default` | Image defaults to `DASH_ENVIRONMENT=production`; no key supplied | Set `DASH_API_KEY`, or `DASH_AUTH_ENABLED=false` for a public demo |
| Deployed page asks for an API key and the visitor has none | Auth is on and the key cannot be injected outside `local` | Run the demo shape, or share `?key=…`, or put the app behind SSO |
| `401` from the browser console but `curl` works | You passed the key to curl, not to the page | Open `https://host/?key=KEY` once; it is stored in `localStorage` |
| Page loads, every panel is empty | A reverse proxy is returning HTML for `/api/*` | Ensure `/api/`, `/static/` and `/docs` all reach the app |
| CORS error in the console | The dashboard is served from a different origin than the API | Serve both from this app (default), or add the origin to `DASH_CORS_ORIGINS` |
| Cold start of ~30 s | Render free plan sleep, or `min_machines_running = 0` on Fly | Expected; keep one instance warm if you are demoing |
