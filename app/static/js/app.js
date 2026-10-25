/* ============================================================================
   app.js — Cloud Control dashboard
   ----------------------------------------------------------------------------
   Structure:
     1. formatting helpers
     2. state
     3. data loading
     4. renderers (one per panel)
     5. interactions (filters, sorting, ranges, palette, keyboard)
     6. boot
   ========================================================================== */
(function () {
  "use strict";

  const { sparkline, stackedArea, cumulative, donut, PALETTE } = window.Chart;
  const API = window.API;

  /* ── 1. Formatting ───────────────────────────────────────────────────── */

  const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 });
  const usd0 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
  const int = new Intl.NumberFormat("en-US");

  const num = (v) => (v == null || Number.isNaN(Number(v)) ? 0 : Number(v));
  const money = (m) => (m && typeof m === "object" ? num(m.amount) : num(m));

  function fmtMoney(v, compact) {
    const n = num(v);
    if (compact) {
      if (Math.abs(n) >= 1e6) return "$" + (n / 1e6).toFixed(1) + "M";
      if (Math.abs(n) >= 1e3) return "$" + (n / 1e3).toFixed(1) + "K";
      return "$" + n.toFixed(0);
    }
    return Math.abs(n) >= 1000 ? usd.format(n) : usd.format(n);
  }

  function fmtMoney0(v) { return usd0.format(num(v)); }

  // Thresholds like 0.4 or 2431 need more care than a plain integer format.
  function fmtNum(v) {
    const n = num(v);
    if (Math.abs(n) >= 1000) return int.format(n);
    if (Number.isInteger(n)) return String(n);
    return n.toFixed(Math.abs(n) < 10 ? 2 : 1).replace(/\.?0+$/, "");
  }

  function fmtBytes(bytes) {
    const b = num(bytes);
    if (b === 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB", "PB"];
    const i = Math.min(Math.floor(Math.log(b) / Math.log(1024)), units.length - 1);
    const val = b / Math.pow(1024, i);
    return `${val.toFixed(val >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
  }

  function fmtDelta(v) {
    const n = num(v);
    if (Math.abs(n) < 0.05) return { cls: "flat", text: "—" };
    return { cls: n > 0 ? "up" : "down", text: `${n > 0 ? "▲" : "▼"} ${Math.abs(n).toFixed(1)}%` };
  }

  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function parseDate(iso) {
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(Date.UTC(y, m - 1, d));
  }
  function fmtDateShort(iso) {
    const d = parseDate(iso);
    return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}`;
  }
  function fmtDateFull(iso) {
    const d = parseDate(iso);
    const wd = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][d.getUTCDay()];
    return `${wd}, ${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}, ${d.getUTCFullYear()}`;
  }
  function fmtDateTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "—";
    return `${MONTHS[d.getMonth()]} ${d.getDate()}, ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  }
  function ago(iso) {
    if (!iso) return "—";
    const secs = Math.max(1, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
    if (secs < 60) return `${secs}s ago`;
    if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
    if (secs < 86400) return `${Math.round(secs / 3600)}h ago`;
    return `${Math.round(secs / 86400)}d ago`;
  }

  // Escapes every character that can break out of text or an attribute value.
  // Applied to all API-derived strings before they reach innerHTML.
  const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;", "`": "&#96;" };
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"'`]/g, (c) => ESCAPES[c]);
  }

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const el = (id) => document.getElementById(id);

  /* ── 2. State ────────────────────────────────────────────────────────── */

  const state = {
    overview: null,
    buckets: null,
    instances: null,
    alarms: null,
    billing: null,
    trend: null,
    info: null,
    days: 30,
    hiddenSeries: new Set(),
    loading: false,
    lastUpdated: null,
    renderFailures: [],
    keyPrompts: 0,
    autoRefresh: true,
    countdown: 60,
    filters: { bucket: "", instance: "", instanceState: "all", alarmState: "all" },
    sort: {
      buckets: { key: "size_bytes", dir: -1 },
      instances: { key: "cpu", dir: -1 },
      alarms: { key: "state", dir: 1 },
      services: { key: "cost", dir: -1 },
    },
  };

  const REFRESH_SECONDS = 60;

  /* ── 3. Toasts ───────────────────────────────────────────────────────── */

  function toast(message, kind) {
    const wrap = el("toasts");
    const node = document.createElement("div");
    node.className = "toast " + (kind || "");
    const tick = kind === "err" ? "!" : "✓";
    node.innerHTML = `<span class="tick ${kind === "err" ? "bad" : "ok"}">${tick}</span><span>${esc(message)}</span>`;
    wrap.appendChild(node);
    setTimeout(() => {
      node.style.opacity = "0";
      node.style.transform = "translateY(6px)";
      node.style.transition = "all .2s";
      setTimeout(() => node.remove(), 220);
    }, kind === "err" ? 6000 : 2600);
  }

  function showError(message) {
    const bar = el("errorbar");
    el("errorbar-text").textContent = message;
    bar.hidden = false;
  }
  function clearError() {
    el("errorbar").hidden = true;
  }

  /* ── 4. Load ─────────────────────────────────────────────────────────── */

  async function loadAll({ silent = false } = {}) {
    if (state.loading) return;
    state.loading = true;
    if (!silent) el("btn-refresh").classList.add("is-loading");

    const results = await Promise.allSettled([
      API.endpoints.overview(),
      API.endpoints.buckets(),
      API.endpoints.instances(),
      API.endpoints.alarms(),
      API.endpoints.billing(),
      API.endpoints.trend(state.days),
      API.endpoints.systemInfo(),
    ]);

    const [ov, bk, inst, al, bill, trend, info] = results;
    let failures = 0;

    if (ov.status === "fulfilled") {
      state.overview = ov.value.data;
      state.lastRequestId = (ov.value.meta && ov.value.meta.request_id) || null;
    } else failures++;
    if (bk.status === "fulfilled") state.buckets = bk.value.data;
    else failures++;
    if (inst.status === "fulfilled") state.instances = inst.value.data;
    else failures++;
    if (al.status === "fulfilled") state.alarms = al.value.data;
    else failures++;
    if (bill.status === "fulfilled") state.billing = bill.value.data;
    else failures++;
    if (trend.status === "fulfilled") state.trend = trend.value.data;
    else failures++;
    if (info.status === "fulfilled") state.info = info.value;
    else failures++;

    state.loading = false;
    el("btn-refresh").classList.remove("is-loading");

    const firstError = [ov, bk, inst, al, bill, trend, info].find((r) => r.status === "rejected");
    if (firstError) {
      const err = firstError.reason;
      const suffix = err && err.code === "unauthorized" ? " — API key required or invalid." : "";
      showError(`${err.message}${suffix}`);
      if (err && err.code === "unauthorized") offerKey();
    } else {
      clearError();
    }

    if (failures === 7) {
      renderFailure();
      return;
    }

    state.lastUpdated = new Date();
    renderAll();
    resetCountdown();
  }

  function renderFailure() {
    el("kpis").innerHTML = `<div class="panel" style="grid-column:1/-1;padding:24px;text-align:center">
      <p style="margin:0 0 4px;font-weight:600">Dashboard unavailable</p>
      <p class="muted" style="margin:0;font-size:12.5px">The API did not return data. Check the backend logs, then retry.</p>
    </div>`;
    el("trend-chart").innerHTML = `<div class="empty">
      <svg viewBox="0 0 24 24"><path d="M12 2 1 21h22zM11 9h2v6h-2zm0 8h2v2h-2z"/></svg>
      <p>No data to plot</p><small>Waiting for a successful response</small></div>`;
  }

  // Ask for a key at most twice per page load. Without this cap a wrong key
  // produces an infinite prompt -> retry -> 401 -> prompt loop: a dialog storm
  // the user cannot escape from.
  const KEY_PROMPT_LIMIT = 2;

  function offerKey() {
    if (state.keyPrompts >= KEY_PROMPT_LIMIT) {
      showError(
        "Authentication keeps failing. Verify DASH_API_KEY on the server, then reload."
      );
      return;
    }
    state.keyPrompts += 1;

    const prompt = API.hasKey()
      ? "Authentication failed. Enter a different API key:"
      : "This API requires a key. Paste the value of DASH_API_KEY:";
    const entered = window.prompt(prompt);
    if (!entered || !entered.trim()) {
      showError("No API key provided. Reload with ?key=YOUR_KEY or press ⌘K → Set API key.");
      return;
    }

    const candidate = entered.trim();
    if (candidate === API.getKey()) {
      // Same key, same outcome. Do not spin.
      showError("That key was rejected. Check DASH_API_KEY on the server.");
      return;
    }

    API.setKey(candidate);
    loadAll();
  }

  /* ── 5. Renderers ────────────────────────────────────────────────────── */

  function renderAll() {
    // Each panel renders independently. A malformed payload — a proxy error
    // page, a version skew, a truncated body — must cost one panel, not the
    // whole dashboard. Failures are collected and surfaced once.
    const steps = [
      renderHeader, renderKPIs, renderTrend, renderBudget, renderHealth,
      renderServices, renderDonut, renderBuckets, renderInstances,
      renderAlarms, renderBilling, renderBars, renderFooter,
    ];
    const failed = [];
    for (const step of steps) {
      try {
        step();
      } catch (err) {
        failed.push(step.name);
      }
    }
    // Replaced, not appended: a panel that recovers must stop being reported.
    state.renderFailures = failed;
    if (failed.length) {
      showError(
        `Unexpected response shape — could not render ${failed.join(", ")}. ` +
          "Check /api/v1/system/info."
      );
    }
    el("last-updated").textContent = state.lastUpdated
      ? "updated " + state.lastUpdated.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })
      : "—";
  }

  function renderHeader() {
    const info = state.info;
    const provider = info && info.provider ? info.provider : { mode: "unknown", live: false, region: "—" };
    const dot = el("provider-dot");
    dot.className = "dot " + (provider.live ? "dot-live" : provider.mode === "mock" ? "dot-mock" : "dot-down");
    el("provider-label").textContent = provider.live ? "live AWS" : provider.mode === "mock" ? "demo data" : provider.mode;
    el("region-label").textContent = provider.region || "—";
    el("env-label").textContent = (info && info.settings && info.settings.environment) || "—";
    if (info && info.settings) {
      el("foot-version").textContent = "v" + info.settings.version;
      const mode = provider.mode === "mock" ? "mock" : "aws";
      el("foot-provider").textContent = `provider ${mode}${provider.fallback_reason ? " (fallback)" : ""}`;
    }
    if (provider.fallback_reason) {
      el("provider-label").title = provider.fallback_reason;
    }
  }

  function renderKPIs() {
    const o = state.overview;
    if (!o || !Array.isArray(o.kpis) || !o.kpis.length) return;
    const grid = el("kpis");
    grid.innerHTML = "";

    const iconFor = {
      spend_mtd: '<path d="M4 4h16v16H4zm2 3v3h12V7zm0 5v2h3v-2zm5 0v2h3v-2zm5 0v2h2v-2zm-10 4v2h3v-2zm5 0v2h3v-2zm5 0v2h2v-2z"/>',
      forecast: '<path d="M3 17l6-6 4 4 8-8v5h-2V9.8l-6 6-4-4-4 4z"/>',
      instances: '<path d="M4 4h16v4H4zm0 6h16v4H4zm0 6h16v4H4z"/>',
      buckets: '<path d="M12 3c4.4 0 8 1.1 8 2.5S16.4 8 12 8 4 6.9 4 5.5 7.6 3 12 3z"/>',
      alarms: '<path d="M12 2a6 6 0 0 0-6 6v4l-2 3v1h16v-1l-2-3V8a6 6 0 0 0-6-6z"/>',
    };
    const sparkColor = { ok: "#5b9bff", warn: "#fbbf24", critical: "#f76a6a" };

    o.kpis.forEach((k) => {
      const d = fmtDelta(k.delta_pct);
      const card = document.createElement("article");
      card.className = "kpi" + (k.status === "critical" ? " is-critical" : k.status === "warn" ? " is-warn" : "");
      card.innerHTML = `
        <div class="kpi-label">
          <svg viewBox="0 0 24 24" width="12" height="12" fill="currentColor" style="opacity:.7">${iconFor[k.key] || ""}</svg>
          ${esc(k.label)}
        </div>
        <div class="kpi-value">${esc(k.value)}<span class="kpi-unit">${esc(k.unit || "")}</span></div>
        <div class="kpi-foot">
          <span class="kpi-delta ${d.cls}">${d.text}</span>
          <div class="kpi-spark">${sparkline(k.spark, { stroke: sparkColor[k.status] || "#5b9bff" })}</div>
        </div>`;
      grid.appendChild(card);
    });
  }

  function trendModel() {
    const t = state.trend;
    if (!t) return { points: [], series: [] };
    const names = t.services || [];
    const points = t.points.map((p) => ({ date: p.date, values: p.by_service }));
    const series = names.map((name, i) => ({ name, color: PALETTE[i % PALETTE.length] }));
    // Largest total first so stacking reads top-down by importance.
    series.sort((a, b) => {
      const sum = (n) => points.reduce((s, p) => s + num(p.values[n.name]), 0);
      return sum(b) - sum(a);
    });
    return { points, series };
  }

  let trendChart = null;
  function renderTrend() {
    const model = trendModel();
    const wrap = el("trend-chart");
    if (!model.points.length) {
      wrap.innerHTML = `<div class="empty"><p>No trend data</p></div>`;
      return;
    }
    if (trendChart && trendChart.destroy) trendChart.destroy();
    wrap.querySelectorAll("svg").forEach((n) => n.remove());
    trendChart = stackedArea(wrap, model, {
      height: 268,
      hidden: state.hiddenSeries,
      formatMoney: fmtMoney,
      formatDate: fmtDateShort,
      formatFullDate: fmtDateFull,
    });
    renderLegend(model);
  }

  function renderLegend(model) {
    const wrap = el("trend-legend");
    wrap.innerHTML = "";
    model.series.forEach((s) => {
      const b = document.createElement("button");
      b.className = "legend-item" + (state.hiddenSeries.has(s.name) ? " is-off" : "");
      b.innerHTML = `<span class="swatch" style="background:${s.color}"></span>${esc(s.name)}`;
      b.addEventListener("click", () => {
        if (state.hiddenSeries.has(s.name)) state.hiddenSeries.delete(s.name);
        else if (state.hiddenSeries.size < model.series.length - 1) state.hiddenSeries.add(s.name);
        renderTrend();
      });
      wrap.appendChild(b);
    });
  }

  function renderBudget() {
    const o = state.overview;
    const b = state.billing;
    if (!o || !b || !b.total || !b.budget) return;
    const used = num(o.budget_used_pct);
    const pct = Math.min(used, 100);
    const color = used > 100 ? "#f76a6a" : used > 85 ? "#fbbf24" : "#34d399";
    const r = 45;
    const circ = 2 * Math.PI * r;
    const ring = el("budget-ring");
    ring.innerHTML = `
      <svg viewBox="0 0 104 104">
        <circle class="gauge-track" cx="52" cy="52" r="${r}"/>
        <circle class="gauge-fill" cx="52" cy="52" r="${r}"
          stroke="${color}" stroke-dasharray="${circ}" stroke-dashoffset="${circ - (circ * pct) / 100}"/>
      </svg>
      <div class="gauge-center">
        <div class="gauge-pct" style="color:${color}">${used.toFixed(0)}%</div>
        <div class="gauge-cap">of budget</div>
      </div>`;

    const remaining = num(money(b.budget)) - num(money(b.total));
    el("budget-legend").innerHTML = `
      <div class="gauge-row is-strong"><span class="k">Spent</span><span class="v">${fmtMoney0(money(b.total))}</span></div>
      <div class="gauge-row"><span class="k">Budget</span><span class="v">${fmtMoney0(money(b.budget))}</span></div>
      <div class="gauge-row"><span class="k">Forecast</span><span class="v" style="color:${num(money(b.forecast)) > num(money(b.budget)) ? "#f76a6a" : "#34d399"}">${fmtMoney0(money(b.forecast))}</span></div>
      <div class="gauge-row"><span class="k">${remaining >= 0 ? "Remaining" : "Overrun"}</span><span class="v">${fmtMoney0(Math.abs(remaining))}</span></div>`;
  }

  function renderHealth() {
    const o = state.overview;
    const buckets = (state.buckets && Array.isArray(state.buckets.items) && state.buckets.items) || [];
    const alarms = (state.alarms && Array.isArray(state.alarms.items) && state.alarms.items) || [];
    if (!o || typeof o.health_score !== "number") return;

    const score = o.health_score;
    const cls = score >= 85 ? "" : score >= 65 ? "warn" : "critical";
    const node = el("health-score");
    node.className = "score " + cls;
    node.textContent = score;

    const firing = alarms.filter((a) => a.state === "ALARM");
    const unencrypted = buckets.filter((b) => !b.encrypted);
    const publicish = buckets.filter((b) => !b.public_access_blocked);
    const idle = buckets.filter((b) => b.object_count === 0);

    el("health-pill").textContent = cls === "" ? "healthy" : cls === "warn" ? "degraded" : "at risk";

    const rows = [
      firing.length
        ? { c: "bad", t: `${firing.length} alarm${firing.length > 1 ? "s" : ""} firing` }
        : { c: "ok", t: "No alarms firing" },
      publicish.length
        ? { c: "bad", t: `${publicish.length} bucket${publicish.length > 1 ? "s" : ""} without public-access block` }
        : { c: "ok", t: "All buckets block public access" },
      unencrypted.length
        ? { c: "warn", t: `${unencrypted.length} bucket${unencrypted.length > 1 ? "s" : ""} unencrypted` }
        : { c: "ok", t: "Encryption at rest enabled" },
      { c: o.budget_used_pct > 100 ? "bad" : o.budget_used_pct > 85 ? "warn" : "ok",
        t: `Budget at ${num(o.budget_used_pct).toFixed(0)}% of monthly cap` },
      idle.length
        ? { c: "warn", t: `${idle.length} empty bucket${idle.length > 1 ? "s" : ""}` }
        : { c: "ok", t: "No empty buckets" },
    ];
    el("health-list").innerHTML = rows
      .map((r) => `<li><span class="tick ${r.c}">${r.c === "ok" ? "✓" : "!"}</span>${esc(r.t)}</li>`)
      .join("");
  }

  function sortRows(rows, sort, accessors) {
    const get = accessors[sort.key] || accessors[Object.keys(accessors)[0]];
    return rows.slice().sort((a, b) => {
      const va = get(a);
      const vb = get(b);
      if (typeof va === "string" || typeof vb === "string") {
        return String(va).localeCompare(String(vb)) * sort.dir;
      }
      return (va - vb) * sort.dir;
    });
  }

  function sortHeader(key, label, sort, cls) {
    const active = sort.key === key;
    return `<th class="is-sortable ${active ? "is-sorted" : ""} ${cls || ""}" data-sort="${key}">
      ${label}<span class="sort-arrow">${active ? (sort.dir > 0 ? "▲" : "▼") : "▾"}</span></th>`;
  }

  function renderServices() {
    const b = state.billing;
    if (!b || !Array.isArray(b.services)) return;
    const rows = b.services;
    const wrap = el("services-table");
    const max = Math.max(...rows.map((r) => num(money(r.cost))), 1);
    wrap.innerHTML = `<table>
      <thead><tr>
        ${sortHeader("service", "Service", state.sort.services)}
        <th>Trend</th>
        ${sortHeader("cost", "MTD", state.sort.services, "num")}
        ${sortHeader("share", "Share", state.sort.services, "num")}
      </tr></thead>
      <tbody>${sortRows(rows, state.sort.services, {
        service: (r) => r.service,
        cost: (r) => num(money(r.cost)),
        share: (r) => num(r.share_pct),
      }).map((r) => {
        const d = fmtDelta(r.delta_pct);
        const pct = (num(money(r.cost)) / max) * 100;
        return `<tr>
          <td class="strong">${esc(r.service)}</td>
          <td><span class="kpi-delta ${d.cls}" style="font-size:10.5px">${d.text}</span></td>
          <td class="num">${fmtMoney(money(r.cost))}</td>
          <td class="num"><div class="meter-cell" style="justify-content:flex-end">
            <div class="meter"><i style="width:${pct}%"></i></div><span>${num(r.share_pct).toFixed(1)}%</span>
          </div></td>
        </tr>`;
      }).join("")}</tbody>
    </table>`;
    bindSort(wrap, "services", renderServices);
  }

  function renderDonut() {
    const b = state.billing;
    if (!b || !Array.isArray(b.services) || !b.services.length) return;
    const wrap = el("donut");
    wrap.innerHTML = "";
    const top = b.services.slice(0, 6);
    const restTotal = b.services.slice(6).reduce((s, r) => s + num(money(r.cost)), 0);
    const segments = top.map((r, i) => ({ name: r.service, value: num(money(r.cost)), color: PALETTE[i % PALETTE.length] }));
    if (restTotal > 0) segments.push({ name: "Other services", value: restTotal, color: "#475569" });

    const ring = document.createElement("div");
    ring.className = "donut";
    wrap.appendChild(ring);
    Chart.donut(ring, segments, { formatMoney: (v) => fmtMoney0(v) });

    const legend = document.createElement("div");
    legend.className = "donut-legend";
    const total = segments.reduce((s, x) => s + x.value, 0) || 1;
    legend.innerHTML = segments.map((s) => `<div class="row">
      <span class="swatch" style="background:${s.color}"></span>
      <span class="name" title="${esc(s.name)}">${esc(s.name)}</span>
      <span class="val">${((s.value / total) * 100).toFixed(1)}%</span>
    </div>`).join("");
    wrap.appendChild(legend);
  }

  function renderBuckets() {
    const data = state.buckets;
    if (!data || !Array.isArray(data.items)) return;
    const wrap = el("buckets-table");
    const q = state.filters.bucket.toLowerCase();
    let rows = data.items.filter((b) => !q || b.name.toLowerCase().includes(q) || b.region.includes(q));
    rows = sortRows(rows, state.sort.buckets, {
      name: (b) => b.name,
      size_bytes: (b) => b.size_bytes,
      object_count: (b) => b.object_count,
      cost: (b) => num(money(b.monthly_cost)),
    });

    el("storage-meta").innerHTML = `
      <span class="pill">${data.items.length} buckets</span>
      <span class="pill">${esc(fmtBytes(data.total_size_bytes))} stored</span>
      <span class="pill">${int.format(data.total_objects)} objects</span>
      <span class="pill pill-quiet">${fmtMoney0(money(data.total_monthly_cost))}/mo</span>`;

    if (!rows.length) {
      wrap.innerHTML = `<div class="empty"><p>No buckets match “${esc(state.filters.bucket)}”</p><small>Clear the filter to see all buckets</small></div>`;
      return;
    }

    const maxSize = Math.max(...data.items.map((b) => b.size_bytes), 1);
    wrap.innerHTML = `<table>
      <thead><tr>
        ${sortHeader("name", "Bucket", state.sort.buckets)}
        <th>Class</th>
        ${sortHeader("size_bytes", "Size", state.sort.buckets, "num")}
        ${sortHeader("object_count", "Objects", state.sort.buckets, "num")}
        <th>Posture</th>
        <th>Created</th>
        ${sortHeader("cost", "Est. / mo", state.sort.buckets, "num")}
      </tr></thead>
      <tbody>${rows.map((b) => {
        const pct = (b.size_bytes / maxSize) * 100;
        const posture = [
          b.encrypted ? `<span class="badge ok"><span class="badge-dot"></span>SSE</span>` : `<span class="badge bad"><span class="badge-dot"></span>No SSE</span>`,
          b.public_access_blocked ? `<span class="badge muted">private</span>` : `<span class="badge bad"><span class="badge-dot"></span>public</span>`,
          b.versioning ? `<span class="badge info">versioned</span>` : "",
        ].filter(Boolean).join(" ");
        const classBadge = b.storage_class === "STANDARD" ? "muted"
          : b.storage_class === "GLACIER" ? "violet" : "info";
        return `<tr>
          <td>
            <div class="cell-main">
              <svg viewBox="0 0 24 24" width="14" height="14" fill="#5b9bff" style="opacity:.8;flex:none"><path d="M12 3c4.4 0 8 1.1 8 2.5S16.4 8 12 8 4 6.9 4 5.5 7.6 3 12 3zm8 6.2v3.3c0 1.4-3.6 2.5-8 2.5s-8-1.1-8-2.5V9.2C5.3 10.3 8.5 11 12 11s6.7-.7 8-1.8zm0 6v3.3c0 1.4-3.6 2.5-8 2.5s-8-1.1-8-2.5v-3.3c1.3 1.1 4.5 1.8 8 1.8s6.7-.7 8-1.8z"/></svg>
              <div class="cell-stack"><span class="truncate strong">${esc(b.name)}</span><small>${esc(b.region)}</small></div>
            </div>
          </td>
          <td><span class="badge ${classBadge}">${esc(b.storage_class.replace(/_/g, " "))}</span></td>
          <td class="num"><div class="meter-cell" style="justify-content:flex-end">
            <div class="meter"><i style="width:${pct}%"></i></div><span>${fmtBytes(b.size_bytes)}</span>
          </div></td>
          <td class="num">${int.format(b.object_count)}</td>
          <td><div style="display:flex;gap:3px;flex-wrap:wrap">${posture}</div></td>
          <td class="mono nowrap">${fmtDateTime(b.created_at)}</td>
          <td class="num strong">${fmtMoney(money(b.monthly_cost))}</td>
        </tr>`;
      }).join("")}</tbody></table>`;
    bindSort(wrap, "buckets", renderBuckets);
  }

  function renderInstances() {
    const data = state.instances;
    if (!data || !Array.isArray(data.items)) return;
    const wrap = el("instances-table");
    const { instance, instanceState } = state.filters;

    // filter chips
    const counts = data.by_state || {};
    const states = ["all", ...Object.keys(counts)];
    el("instance-filters").innerHTML = states.map((s) => {
      const n = s === "all" ? data.items.length : counts[s];
      return `<button class="chip ${instanceState === s ? "is-active" : ""}" data-state="${s}">${s}${n != null ? ` <span style="opacity:.55">${n}</span>` : ""}</button>`;
    }).join("");
    $$("#instance-filters .chip").forEach((c) =>
      c.addEventListener("click", () => {
        state.filters.instanceState = c.dataset.state;
        renderInstances();
      })
    );

    let rows = data.items.filter((i) => {
      if (instanceState !== "all" && i.state !== instanceState) return false;
      const q = instance.toLowerCase();
      return !q || [i.name, i.id, i.type, i.availability_zone].some((v) => String(v).toLowerCase().includes(q));
    });
    rows = sortRows(rows, state.sort.instances, {
      name: (i) => (i.tags && i.tags.Name) || i.id,
      cpu: (i) => i.cpu_utilization_pct,
      memory: (i) => i.memory_used_pct,
      cost: (i) => num(money(i.monthly_cost)),
      state: (i) => i.state,
    });

    const running = data.items.filter((i) => i.state === "running").length;
    el("compute-meta").innerHTML = `
      <span class="pill">${data.items.length} instances</span>
      <span class="pill" style="color:var(--green)">${running} running</span>
      <span class="pill pill-quiet">${data.items.length - running} stopped</span>`;

    if (!rows.length) {
      wrap.innerHTML = `<div class="empty"><p>No instances match the current filters</p><small>Clear the filter chips or search box</small></div>`;
      return;
    }

    const stateBadge = (s) =>
      s === "running" ? `<span class="badge ok"><span class="badge-dot"></span>running</span>`
      : s === "stopped" ? `<span class="badge muted"><span class="badge-dot"></span>stopped</span>`
      : s === "pending" ? `<span class="badge warn"><span class="badge-dot"></span>pending</span>`
      : `<span class="badge bad"><span class="badge-dot"></span>${esc(s)}</span>`;

    wrap.innerHTML = `<table>
      <thead><tr>
        ${sortHeader("name", "Instance", state.sort.instances)}
        ${sortHeader("state", "State", state.sort.instances)}
        <th>Type</th>
        <th>Zone</th>
        ${sortHeader("cpu", "CPU", state.sort.instances, "num")}
        ${sortHeader("memory", "Memory", state.sort.instances, "num")}
        <th>IP</th>
        <th>Launched</th>
        ${sortHeader("cost", "Est. / mo", state.sort.instances, "num")}
      </tr></thead>
      <tbody>${rows.map((i) => {
        const cpu = num(i.cpu_utilization_pct);
        const mem = num(i.memory_used_pct);
        const meterCls = (v) => (v >= 85 ? "bad" : v >= 65 ? "warn" : "ok");
        const name = (i.tags && i.tags.Name) || i.id;
        return `<tr>
          <td><div class="cell-stack">
            <span class="truncate strong">${esc(name)}</span>
            <small class="mono">${esc(i.id)}</small>
          </div></td>
          <td>${stateBadge(i.state)}</td>
          <td class="mono">${esc(i.type)}</td>
          <td class="mono">${esc(i.availability_zone)}</td>
          <td class="num"><div class="meter-cell" style="justify-content:flex-end">
            <div class="meter ${meterCls(cpu)}"><i style="width:${cpu}%"></i></div><span>${cpu.toFixed(0)}%</span>
          </div></td>
          <td class="num"><div class="meter-cell" style="justify-content:flex-end">
            <div class="meter ${meterCls(mem)}"><i style="width:${mem}%"></i></div><span>${mem.toFixed(0)}%</span>
          </div></td>
          <td class="mono">${esc(i.public_ip || i.private_ip)}</td>
          <td class="mono nowrap">${fmtDateTime(i.launch_time)}</td>
          <td class="num">${fmtMoney(money(i.monthly_cost))}</td>
        </tr>`;
      }).join("")}</tbody></table>`;
    bindSort(wrap, "instances", renderInstances);
  }

  function renderAlarms() {
    const data = state.alarms;
    if (!data || !Array.isArray(data.items)) return;
    const wrap = el("alarms-table");
    const counts = data.by_state || {};
    const states = ["all", "ALARM", "OK", "INSUFFICIENT_DATA"];

    el("alarm-filters").innerHTML = states.map((s) => {
      const n = s === "all" ? data.items.length : counts[s] || 0;
      return `<button class="chip ${state.filters.alarmState === s ? "is-active" : ""}" data-state="${s}">
        ${s === "INSUFFICIENT_DATA" ? "NO DATA" : s} <span style="opacity:.55">${n}</span></button>`;
    }).join("");
    $$("#alarm-filters .chip").forEach((c) =>
      c.addEventListener("click", () => {
        state.filters.alarmState = c.dataset.state;
        renderAlarms();
      })
    );

    const firing = data.items.filter((a) => a.state === "ALARM").length;
    el("alerts-meta").innerHTML = `
      <span class="pill" style="color:${firing ? "var(--red)" : "var(--green)"}">${firing} firing</span>
      <span class="pill pill-quiet">${data.items.length} total</span>`;
    el("rail-alert-count").hidden = firing === 0;
    el("rail-alert-count").textContent = firing;

    let rows = data.items.filter((a) => state.filters.alarmState === "all" || a.state === state.filters.alarmState);
    rows = sortRows(rows, state.sort.alarms, {
      state: (a) => ["ALARM", "INSUFFICIENT_DATA", "OK"].indexOf(a.state),
      name: (a) => a.name,
      current: (a) => a.current_value,
      updated: (a) => new Date(a.updated_at).getTime(),
    });

    if (!rows.length) {
      wrap.innerHTML = `<div class="empty"><p>No alarms in this state</p></div>`;
      return;
    }

    const stateBadge = (s) =>
      s === "ALARM" ? `<span class="badge bad"><span class="badge-dot"></span>ALARM</span>`
      : s === "OK" ? `<span class="badge ok"><span class="badge-dot"></span>OK</span>`
      : `<span class="badge warn"><span class="badge-dot"></span>NO DATA</span>`;

    wrap.innerHTML = `<table>
      <thead><tr>
        ${sortHeader("state", "State", state.sort.alarms)}
        ${sortHeader("name", "Alarm", state.sort.alarms)}
        <th>Namespace</th>
        <th>Metric</th>
        <th class="num">Threshold</th>
        ${sortHeader("current", "Current", state.sort.alarms, "num")}
        <th>Severity</th>
        ${sortHeader("updated", "Updated", state.sort.alarms)}
      </tr></thead>
      <tbody>${rows.map((a) => {
        const over = a.comparison === "GreaterThanThreshold" ? a.current_value > a.threshold : a.current_value < a.threshold;
        const sev = a.severity === "critical" ? "bad" : a.severity === "warning" ? "warn" : "muted";
        return `<tr>
          <td>${stateBadge(a.state)}</td>
          <td class="strong">${esc(a.name)}</td>
          <td class="mono">${esc(a.namespace)}</td>
          <td class="mono">${esc(a.metric)}</td>
          <td class="num">${fmtNum(a.threshold)}</td>
          <td class="num" style="color:${over ? "var(--red)" : "var(--text)"}">${fmtNum(a.current_value)}</td>
          <td><span class="badge ${sev}">${esc(a.severity)}</span></td>
          <td class="mono nowrap" title="${esc(a.updated_at)}">${ago(a.updated_at)}</td>
        </tr>`;
      }).join("")}</tbody></table>`;
    bindSort(wrap, "alarms", renderAlarms);
  }

  function renderBilling() {
    const b = state.billing;
    if (!b || !Array.isArray(b.services)) return;
    el("billing-meta").innerHTML = `
      <span class="pill">${fmtDateShort(b.period_start)} – ${fmtDateShort(b.period_end)}</span>
      <span class="pill pill-quiet">${esc(b.currency)}</span>`;

    const wrap = el("billing-table");
    wrap.innerHTML = `<table>
      <thead><tr>
        <th>Service</th>
        <th class="num">Actual</th>
        <th class="num">Projected</th>
        <th class="num">Δ</th>
        <th class="num" title="Mixed units — GB-months for storage, hours for compute, invocations for Lambda">Usage</th>
      </tr></thead>
      <tbody>${b.services.map((r) => {
        const d = fmtDelta(r.delta_pct);
        const proj = num(money(r.projected));
        const cost = num(money(r.cost));
        const over = proj > cost * 1.2;
        return `<tr>
          <td class="strong">${esc(r.service)}</td>
          <td class="num">${fmtMoney(cost)}</td>
          <td class="num" style="color:${over ? "var(--amber)" : "var(--text-2)"}">${fmtMoney(proj)}</td>
          <td class="num"><span class="kpi-delta ${d.cls}" style="font-size:10.5px">${d.text}</span></td>
          <td class="num muted">${fmtNum(r.usage_quantity)}</td>
        </tr>`;
      }).join("")}</tbody></table>`;

    // Velocity chart: cumulative spend vs. budget pace for the same number of
    // days. The budget is monthly, so the pace line is prorated by how much of
    // the month the visible window covers — otherwise a 7-day view draws a
    // staggeringly steep budget line and implies runaway spend.
    const t = state.trend;
    const monthlyBudget = num(money(b.budget));
    if (t && t.points.length) {
      const [year, month] = parseDate(t.points[0].date).toISOString().slice(0, 7).split("-").map(Number);
      const daysInMonth = new Date(Date.UTC(year, month, 0)).getUTCDate();
      const windowBudget = monthlyBudget * (t.points.length / daysInMonth);
      const points = t.points.map((p, i) => ({ date: p.date, value: num(money(p.total)), i }));
      cumulative(el("velocity-chart"), { points, budget: windowBudget }, {
        height: 220,
        formatMoney: fmtMoney,
        formatDate: fmtDateShort,
        formatFullDate: fmtDateFull,
      });
    }
  }

  function renderBars() {
    const b = state.billing;
    if (!b || !Array.isArray(b.services)) return;
    const rows = b.services.slice(0, 8);
    const max = Math.max(...rows.map((r) => num(money(r.cost))), 1);
    el("service-bars").innerHTML = rows.map((r, i) => {
      const v = num(money(r.cost));
      const pct = (v / max) * 100;
      return `<div class="bar-row">
        <span class="bar-name" title="${esc(r.service)}">${esc(r.service)}</span>
        <div class="bar-track"><div class="bar-fill" style="width:${pct}%;background:${PALETTE[i % PALETTE.length]}"></div></div>
        <span class="bar-val">${fmtMoney0(v)}</span>
        <span class="bar-pct">${num(r.share_pct).toFixed(1)}%</span>
      </div>`;
    }).join("");
  }

  function renderFooter() {
    const info = state.info;
    if (!info) return;
    const c = info.cache || {};
    el("foot-cache").textContent = `cache ${(((c.hit_rate || 0) * 100)).toFixed(0)}% hit (${c.hits}/${c.hits + c.misses})`;
    el("foot-request").textContent = "req " + (state.lastRequestId || "—");
  }

  function bindSort(wrap, key, rerender) {
    $$("th.is-sortable", wrap).forEach((th) => {
      th.addEventListener("click", () => {
        const s = state.sort[key];
        const k = th.dataset.sort;
        if (s.key === k) s.dir *= -1;
        else { s.key = k; s.dir = -1; }
        rerender();
      });
    });
  }

  /* ── 6. Interactions ─────────────────────────────────────────────────── */

  function resetCountdown() {
    state.countdown = REFRESH_SECONDS;
  }

  function startClock() {
    const tick = () => {
      const now = new Date();
      $(".clock-time").textContent = now.toLocaleTimeString([], { hour12: false });
      if (state.autoRefresh && !state.loading) {
        state.countdown -= 1;
        if (state.countdown <= 0) loadAll({ silent: true });
      }
      const ratio = state.autoRefresh ? Math.max(0, state.countdown / REFRESH_SECONDS) : 0;
      const circ = 94.2;
      el("ring-fg").style.strokeDashoffset = String(circ * (1 - ratio));
      el("ring-fg").style.stroke = state.autoRefresh ? "#2dd4bf" : "#45505d";
    };
    tick();
    setInterval(tick, 1000);
  }

  function bindNav() {
    const sections = $$(".section");
    $$(".rail-item[data-target]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const target = el(btn.dataset.target);
        if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    });

    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((e) => e.isIntersecting)
          .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
        if (!visible) return;
        const id = visible.target.id;
        $$(".rail-item[data-target]").forEach((b) =>
          b.classList.toggle("is-active", b.dataset.target === id)
        );
        el("crumb-section").textContent = id.replace("sec-", "");
      },
      { rootMargin: "-20% 0px -60% 0px", threshold: [0.05, 0.25, 0.5] }
    );
    sections.forEach((s) => observer.observe(s));
  }

  function bindToolbar() {
    el("btn-refresh").addEventListener("click", () => loadAll().then(() => toast("Data refreshed")));

    el("btn-toggle-live").addEventListener("click", () => toggleAutoRefresh());

    el("btn-export").addEventListener("click", exportSnapshot);
    el("btn-retry").addEventListener("click", () => loadAll());

    $$("#trend-range button").forEach((b) =>
      b.addEventListener("click", async () => {
        $$("#trend-range button").forEach((x) => x.classList.remove("is-active"));
        b.classList.add("is-active");
        state.days = Number(b.dataset.days);
        el("range-pill").textContent = `last ${state.days} days`;
        try {
          const res = await API.endpoints.trend(state.days);
          state.trend = res.data;
          state.hiddenSeries.clear();
          renderTrend();
        } catch (e) {
          toast(e.message, "err");
        }
      })
    );

    el("bucket-search").addEventListener("input", (e) => {
      state.filters.bucket = e.target.value;
      renderBuckets();
    });
    el("instance-search").addEventListener("input", (e) => {
      state.filters.instance = e.target.value;
      renderInstances();
    });
  }

  function toggleAutoRefresh() {
    state.autoRefresh = !state.autoRefresh;
    // Set the attribute on the button itself, not on whatever triggered this,
    // so programmatic and keyboard paths keep aria-pressed accurate.
    el("btn-toggle-live").setAttribute("aria-pressed", String(state.autoRefresh));
    resetCountdown();
    toast(state.autoRefresh ? "Auto-refresh on (60s)" : "Auto-refresh paused");
  }

  function exportSnapshot() {
    const snapshot = {
      exported_at: new Date().toISOString(),
      provider: state.info && state.info.provider,
      overview: state.overview,
      billing: state.billing,
      buckets: state.buckets,
      instances: state.instances,
      alarms: state.alarms,
      trend_days: state.days,
    };
    const blob = new Blob([JSON.stringify(snapshot, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `cloud-control-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
    toast("Snapshot exported");
  }

  /* ── Command palette ─────────────────────────────────────────────────── */

  const COMMANDS = [
    { id: "overview", label: "Go to Overview", kind: "section", target: "sec-overview", icon: "M3 13h8V3H3zM13 21h8V11h-8zM3 21h8v-6H3zM13 9h8V3h-8z" },
    { id: "storage", label: "Go to Storage", kind: "section", target: "sec-storage", icon: "M12 3c4.4 0 8 1.1 8 2.5S16.4 8 12 8 4 6.9 4 5.5 7.6 3 12 3z" },
    { id: "compute", label: "Go to Compute", kind: "section", target: "sec-compute", icon: "M4 4h16v4H4zm0 6h16v4H4z" },
    { id: "alerts", label: "Go to Alerts", kind: "section", target: "sec-alerts", icon: "M12 2a6 6 0 0 0-6 6v4l-2 3v1h16v-1l-2-3V8a6 6 0 0 0-6-6z" },
    { id: "billing", label: "Go to Billing", kind: "section", target: "sec-billing", icon: "M4 4h16v16H4z" },
    { id: "refresh", label: "Refresh data", kind: "action", run: () => loadAll().then(() => toast("Data refreshed")), icon: "M12 5V2L7 6l5 4V7a5 5 0 1 1-5 5H5a7 7 0 1 0 7-7z" },
    { id: "export", label: "Export JSON snapshot", kind: "action", run: exportSnapshot, icon: "M12 3v10l3-3 1.4 1.4L12 16l-4.4-4.6L9 10l3 3V3zM5 18h14v3H5z" },
    { id: "toggle", label: "Toggle auto-refresh", kind: "action", run: () => el("btn-toggle-live").click(), icon: "M12 5V2L7 6l5 4V7a5 5 0 1 1-5 5H5a7 7 0 1 0 7-7z" },
    { id: "docs", label: "Open API documentation", kind: "action", run: () => window.open("/docs", "_blank"), icon: "M5 3h9l5 5v13H5z" },
    { id: "key", label: "Set API key", kind: "action", run: offerKey, icon: "M12 2a5 5 0 1 1-2 9.6L8 14H6v2H4v2H2v-4l6.4-6.4A5 5 0 0 1 12 2z" },
  ];

  let paletteIndex = 0;
  let paletteResults = COMMANDS;

  function openPalette() {
    el("palette").hidden = false;
    const input = el("palette-input");
    input.value = "";
    paletteIndex = 0;
    renderPalette("");
    setTimeout(() => input.focus(), 30);
  }
  function closePalette() {
    // Blur *before* hiding. An element inside a hidden ancestor is no longer
    // focusable, so blurring afterwards is a no-op and `document.activeElement`
    // stays on the invisible input — which keeps the "user is typing" guard
    // armed and silently swallows every keyboard shortcut.
    const active = document.activeElement;
    if (active && active !== document.body && typeof active.blur === "function") {
      active.blur();
    }
    el("palette").hidden = true;
  }

  function renderPalette(q) {
    const query = q.toLowerCase().trim();
    paletteResults = COMMANDS.filter((c) => !query || c.label.toLowerCase().includes(query));
    paletteIndex = Math.min(paletteIndex, Math.max(0, paletteResults.length - 1));
    el("palette-list").innerHTML = paletteResults.length
      ? paletteResults.map((c, i) => `<li data-idx="${i}" class="${i === paletteIndex ? "is-selected" : ""}">
          <svg viewBox="0 0 24 24"><path d="${c.icon}"/></svg>
          <span>${esc(c.label)}</span><span class="kind">${c.kind}</span></li>`).join("")
      : `<li class="muted" style="cursor:default">No matching command</li>`;
    $$("#palette-list li[data-idx]").forEach((li) =>
      li.addEventListener("click", () => runCommand(Number(li.dataset.idx)))
    );
  }

  function runCommand(i) {
    const cmd = paletteResults[i];
    if (!cmd) return;
    closePalette();
    if (cmd.kind === "section") {
      const t = el(cmd.target);
      if (t) t.scrollIntoView({ behavior: "smooth", block: "start" });
    } else if (cmd.run) {
      cmd.run();
    }
  }

  function bindPalette() {
    el("btn-palette").addEventListener("click", openPalette);
    el("palette").addEventListener("click", (e) => {
      if (e.target === el("palette")) closePalette();
    });
    el("palette-input").addEventListener("input", (e) => renderPalette(e.target.value));
    el("palette-input").addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const delta = e.key === "ArrowDown" ? 1 : -1;
        paletteIndex = (paletteIndex + delta + paletteResults.length) % paletteResults.length;
        renderPalette(el("palette-input").value);
      } else if (e.key === "Enter") {
        e.preventDefault();
        runCommand(paletteIndex);
      }
    });
  }

  function bindShortcuts() {
    el("btn-shortcuts").addEventListener("click", () => (el("shortcuts").hidden = false));
    el("btn-shortcuts-close").addEventListener("click", () => (el("shortcuts").hidden = true));
    el("shortcuts").addEventListener("click", (e) => {
      if (e.target === el("shortcuts")) el("shortcuts").hidden = true;
    });

    document.addEventListener("keydown", (e) => {
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);

      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        openPalette();
        return;
      }
      if (e.key === "Escape") {
        closePalette();
        el("shortcuts").hidden = true;
        if (typing) document.activeElement.blur();
        return;
      }
      if (el("palette").hidden === false) return;
      if (typing) return;

      if (e.key === "?") { el("shortcuts").hidden = false; return; }
      if (e.key.toLowerCase() === "r") { loadAll().then(() => toast("Data refreshed")); return; }
      if (e.key.toLowerCase() === "e") { exportSnapshot(); return; }
      if (e.key === " ") {
        e.preventDefault();
        toggleAutoRefresh();
        return;
      }
      if (e.key === "/") {
        e.preventDefault();
        const target = el("bucket-search");
        target.scrollIntoView({ behavior: "smooth", block: "center" });
        setTimeout(() => target.focus(), 300);
        return;
      }
      if (["1", "2", "3", "4", "5"].includes(e.key)) {
        const map = ["sec-overview", "sec-storage", "sec-compute", "sec-alerts", "sec-billing"];
        const t = el(map[Number(e.key) - 1]);
        if (t) t.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    });
  }

  /* ── 7. Boot ─────────────────────────────────────────────────────────── */

  function paintSkeletons() {
    el("kpis").innerHTML = Array.from({ length: 5 })
      .map(() => `<div class="skeleton skeleton-kpi"></div>`)
      .join("");
    el("trend-chart").innerHTML = `<div class="skeleton skeleton-chart"></div>`;
    ["buckets-table", "instances-table", "alarms-table", "services-table", "billing-table"].forEach((id) => {
      const node = el(id);
      if (node) node.innerHTML = Array.from({ length: 4 }).map(() => `<div class="skeleton skeleton-row"></div>`).join("");
    });
  }

  function boot() {
    paintSkeletons();
    startClock();
    bindNav();
    bindToolbar();
    bindPalette();
    bindShortcuts();

    // No pre-emptive "missing key" banner: on a deployment with auth disabled
    // there is no key and nothing is wrong, so the message would flash for a
    // moment and then be cleared. The 401 path below is the honest place to
    // surface it, because that is when a key is actually required.
    loadAll();
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
