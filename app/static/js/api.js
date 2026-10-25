/* ============================================================================
   api.js — typed-ish HTTP client for the Cloud Control API
   ----------------------------------------------------------------------------
   Responsibilities kept deliberately narrow: auth header injection, timeouts,
   uniform error extraction, and request de-duplication. Everything about how a
   response is *presented* lives in app.js.
   ========================================================================== */
(function () {
  "use strict";

  const BASE = "";
  const TIMEOUT_MS = 15000;
  const inflight = new Map();

  const boot = window.__BOOTSTRAP__ || {};
  const KEY_STORAGE = "cc.apiKey";

  function resolveKey() {
    // Order: local dev bootstrap → previously saved key → query string.
    if (boot.apiKey) return boot.apiKey;
    const saved = localStorage.getItem(KEY_STORAGE);
    if (saved) return saved;
    const params = new URLSearchParams(location.search);
    const fromQuery = params.get("key");
    if (fromQuery) {
      localStorage.setItem(KEY_STORAGE, fromQuery);
      return fromQuery;
    }
    return null;
  }

  let apiKey = resolveKey();

  function setKey(value) {
    apiKey = value;
    if (value) localStorage.setItem(KEY_STORAGE, value);
    else localStorage.removeItem(KEY_STORAGE);
  }

  class ApiError extends Error {
    constructor(message, { status = 0, code = "network_error", requestId = null, detail = null } = {}) {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.code = code;
      this.requestId = requestId;
      this.detail = detail;
    }
  }

  async function request(path, { signal, dedupe = true } = {}) {
    const key = path;
    if (dedupe && inflight.has(key)) return inflight.get(key);

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
    if (signal) signal.addEventListener("abort", () => controller.abort());

    const headers = { Accept: "application/json" };
    if (apiKey) headers["X-API-Key"] = apiKey;

    const promise = (async () => {
      let response;
      try {
        response = await fetch(BASE + path, { headers, signal: controller.signal });
      } catch (err) {
        clearTimeout(timer);
        if (err.name === "AbortError") {
          throw new ApiError("Request timed out after 15s", { code: "timeout" });
        }
        throw new ApiError("Cannot reach the API", { code: "network_error" });
      } finally {
        clearTimeout(timer);
      }

      const payload = await response.json().catch(() => null);

      if (!response.ok) {
        const e = (payload && payload.error) || {};
        throw new ApiError(e.message || `Request failed (${response.status})`, {
          status: response.status,
          code: e.code || `http_${response.status}`,
          requestId: e.request_id || null,
          detail: e.detail || null,
        });
      }
      return payload;
    })();

    if (dedupe) {
      inflight.set(key, promise);
      promise.finally(() => inflight.delete(key));
    }
    return promise;
  }

  function withTimeoutSignal() {
    return new AbortController();
  }

  window.API = {
    base: BASE,
    getKey: () => apiKey,
    setKey,
    request,
    withTimeoutSignal,
    ApiError,
    hasKey: () => Boolean(apiKey),
    endpoints: {
      overview: () => request("/api/v1/overview"),
      systemInfo: () => request("/api/v1/system/info"),
      buckets: () => request("/api/v1/buckets?page_size=100"),
      instances: () => request("/api/v1/instances?page_size=100"),
      alarms: () => request("/api/v1/alarms"),
      billing: () => request("/api/v1/billing/summary"),
      trend: (days) => request(`/api/v1/billing/trend?days=${days}`),
    },
  };
})();
