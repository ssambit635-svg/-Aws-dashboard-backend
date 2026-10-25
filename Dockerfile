# syntax=docker/dockerfile:1

# ── Stage 1: build wheels ──────────────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt


# ── Stage 2: runtime ───────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# DASH_ENVIRONMENT=production is the right default for a deploy image: it
# disables API-key injection into the page. Because of that, the app refuses to
# start unless DASH_API_KEY is set to something other than the development
# default, or DASH_AUTH_ENABLED=false is set deliberately. Failing at startup
# is the point — see docs/DEPLOY.md.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    DASH_ENVIRONMENT=production \
    DASH_BOOTSTRAP_DASHBOARD=false \
    PORT=8000

# Copy the pre-built virtualenv: no compiler, no build cache in the final image.
COPY --from=builder /opt/venv /opt/venv

# Run as a non-root user. UID 10001 is arbitrary but explicit and stable.
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --shell /usr/sbin/nologin app

WORKDIR /srv
COPY --chown=app:app app ./app

USER app

EXPOSE 8000

# Kubernetes-style probes hit /livez and /readyz; this is for plain Docker.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/livez', timeout=2).status == 200 else 1)"

# One worker per container: scale by adding replicas, not threads. The in-process
# cache is per-worker, which is correct for a read-through cache at this size.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
