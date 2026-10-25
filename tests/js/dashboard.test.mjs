/**
 * dashboard.test.mjs — DOM-level tests for the Cloud Control dashboard.
 *
 * Runs the real page in jsdom against a live instance of the API and asserts on
 * what a user would actually get: rendered panels, working controls, no leaked
 * `NaN`, no markup injection, and a usable error state.
 *
 * Requires the API to be running (see Makefile target `test-ui`).
 */

import { bootDashboard, createReporter, getText } from "./harness.mjs";

const reporter = createReporter();
const { check } = reporter;

/* ── Suite 1: initial render ───────────────────────────────────────────── */
async function renderingSuite() {
  const errors = [];
  const { document } = await bootDashboard({ errors });

  check("KPI strip renders five cards", document.querySelectorAll("#kpis .kpi").length === 5);
  check(
    "KPI sparklines render where a series exists",
    document.querySelectorAll("#kpis .kpi-spark svg").length >= 3
  );
  check(
    "stacked area chart draws paths",
    document.querySelectorAll("#trend-chart svg path").length >= 8
  );
  check(
    "legend lists every service",
    document.querySelectorAll("#trend-legend .legend-item").length === 8
  );
  check("budget gauge renders an arc", document.querySelectorAll("#budget-ring .gauge-fill").length === 1);
  check("health score lists its reasons", document.querySelectorAll("#health-list li").length === 5);
  check("services table is populated", document.querySelectorAll("#services-table tbody tr").length === 8);
  check("donut has a segment per service group", document.querySelectorAll("#donut .donut-seg").length === 7);
  check("bucket table is populated", document.querySelectorAll("#buckets-table tbody tr").length === 5);
  check("instance table is populated", document.querySelectorAll("#instances-table tbody tr").length === 8);
  check("alarm table is populated", document.querySelectorAll("#alarms-table tbody tr").length === 6);
  check("billing table is populated", document.querySelectorAll("#billing-table tbody tr").length === 8);
  check("velocity chart draws actual + budget lines", document.querySelectorAll("#velocity-chart svg path").length >= 3);
  check("cost-driver bars render", document.querySelectorAll("#service-bars .bar-row").length === 8);

  check(
    "provider badge reports demo mode honestly",
    document.getElementById("provider-label").textContent.trim() === "demo data",
    document.getElementById("provider-label").textContent
  );
  check("cache statistics reach the footer", document.getElementById("foot-cache").textContent.includes("cache"));
  check("version is displayed", document.getElementById("foot-version").textContent.startsWith("v"));
  check("no error banner on a healthy load", document.getElementById("errorbar").hidden);
  check(
    "no NaN or undefined leaks into the DOM",
    !/\bNaN\b/.test(document.body.textContent) && !/undefined/.test(document.body.textContent)
  );
  check("request ID is shown for support", document.getElementById("foot-request").textContent.length > 5);

  return errors;
}

/* ── Suite 2: interactions ─────────────────────────────────────────────── */
async function interactionSuite() {
  const errors = [];
  const { window, document, sleep } = await bootDashboard({ errors });

  const rows = (selector) => Array.from(document.querySelectorAll(selector));
  const cpuColumn = () =>
    rows("#instances-table tbody tr").map((row) => row.children[4].textContent.trim());

  // sorting
  const beforeSort = cpuColumn();
  rows("#instances-table th").find((th) => th.dataset.sort === "cpu").click();
  await sleep(60);
  check("sorting toggles direction", JSON.stringify(beforeSort) !== JSON.stringify(cpuColumn()));

  // text filter
  const search = document.getElementById("bucket-search");
  search.value = "backup";
  search.dispatchEvent(new window.Event("input", { bubbles: true }));
  await sleep(60);
  check("bucket filter narrows the table", rows("#buckets-table tbody tr").length === 1);
  search.value = "";
  search.dispatchEvent(new window.Event("input", { bubbles: true }));
  await sleep(60);
  check("clearing the filter restores rows", rows("#buckets-table tbody tr").length === 5);

  // state chips
  rows("#instance-filters .chip").find((chip) => chip.dataset.state === "stopped").click();
  await sleep(60);
  const stopped = rows("#instances-table tbody tr");
  check(
    "state chip filters and stays consistent",
    stopped.length === 2 && stopped.every((row) => row.textContent.includes("stopped"))
  );
  rows("#instance-filters .chip").find((chip) => chip.dataset.state === "all").click();
  await sleep(60);

  // alarm chips
  rows("#alarm-filters .chip").find((chip) => chip.dataset.state === "ALARM").click();
  await sleep(60);
  check("alarm chip filters to firing alarms", rows("#alarms-table tbody tr").length === 3);
  rows("#alarm-filters .chip").find((chip) => chip.dataset.state === "all").click();
  await sleep(60);

  // trend range
  rows("#trend-range button").find((button) => button.dataset.days === "7").click();
  await sleep(800);
  check(
    "7D range re-renders without losing the legend",
    rows("#trend-legend .legend-item").length === 8 &&
      document.getElementById("range-pill").textContent.includes("7")
  );

  // legend toggling
  document.querySelector("#trend-legend .legend-item").click();
  await sleep(120);
  check(
    "legend toggle marks a series off",
    document.querySelector("#trend-legend .legend-item").classList.contains("is-off")
  );
  document.querySelector("#trend-legend .legend-item").click();
  await sleep(120);

  // command palette
  document.getElementById("btn-palette").click();
  await sleep(60);
  const allCommands = rows("#palette-list li").length;
  const input = document.getElementById("palette-input");
  input.value = "billing";
  input.dispatchEvent(new window.Event("input", { bubbles: true }));
  await sleep(60);
  check("palette lists commands and filters them", allCommands === 10 && rows("#palette-list li").length === 1);
  document.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  await sleep(60);
  check("Escape closes the palette", document.getElementById("palette").hidden);
  check(
    "focus returns to the page after closing the palette",
    document.activeElement === document.body,
    document.activeElement.tagName
  );

  // a shortcut must work immediately after using the palette (regression)
  document.getElementById("btn-palette").click();
  await sleep(60);
  rows("#palette-list li")[0].click();
  await sleep(60);
  document.dispatchEvent(new window.KeyboardEvent("keydown", { key: "?", bubbles: true }));
  await sleep(60);
  check("shortcuts work after palette use", !document.getElementById("shortcuts").hidden);
  document.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  await sleep(60);
  check("Escape closes the shortcuts dialog", document.getElementById("shortcuts").hidden);

  // Full-screen overlays must never be open together: stacking them leaves the
  // user inside a modal they cannot dismiss, which is exactly what a CSS cascade
  // bug produced in the browser while these DOM tests happily passed.
  document.getElementById("btn-shortcuts").click();
  check("shortcuts button opens the dialog", !document.getElementById("shortcuts").hidden);
  document.getElementById("btn-palette").click();
  check(
    "opening the palette closes the shortcuts dialog",
    document.getElementById("shortcuts").hidden
  );
  document.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  document.getElementById("btn-shortcuts").click();
  check(
    "opening the shortcuts dialog closes the palette",
    document.getElementById("palette").hidden && !document.getElementById("shortcuts").hidden
  );
  document.getElementById("btn-shortcuts-close").click();
  check("the close button dismisses the dialog", document.getElementById("shortcuts").hidden);
  check(
    "the backdrop closes the dialog",
    (() => {
      document.getElementById("btn-shortcuts").click();
      const overlay = document.getElementById("shortcuts");
      overlay.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
      return overlay.hidden;
    })()
  );

  // auto-refresh toggle keeps aria-pressed truthful through the palette
  const liveButton = document.getElementById("btn-toggle-live");
  const initial = liveButton.getAttribute("aria-pressed");
  liveButton.click();
  await sleep(40);
  const toggled = liveButton.getAttribute("aria-pressed");
  document.getElementById("btn-palette").click();
  await sleep(60);
  const toggleInput = document.getElementById("palette-input");
  toggleInput.value = "toggle";
  toggleInput.dispatchEvent(new window.Event("input", { bubbles: true }));
  await sleep(60);
  rows("#palette-list li")[0].click();
  await sleep(60);
  check(
    "aria-pressed stays accurate when driven programmatically",
    initial === "true" && toggled === "false" && liveButton.getAttribute("aria-pressed") === "true",
    `${initial} -> ${toggled} -> ${liveButton.getAttribute("aria-pressed")}`
  );

  // keyboard refresh
  let requests = 0;
  const realFetch = window.fetch;
  window.fetch = (url, options) => {
    requests += 1;
    return realFetch(url, options);
  };
  document.dispatchEvent(new window.KeyboardEvent("keydown", { key: "r", bubbles: true }));
  await sleep(1000);
  check("keyboard 'R' refreshes all panels", requests >= 7, `${requests} requests`);

  // export
  let downloaded = false;
  const realCreateElement = document.createElement.bind(document);
  document.createElement = (tag) => {
    const node = realCreateElement(tag);
    if (tag === "a") node.click = () => (downloaded = true);
    return node;
  };
  document.dispatchEvent(new window.KeyboardEvent("keydown", { key: "e", bubbles: true }));
  await sleep(150);
  check("keyboard 'E' exports a snapshot", downloaded);
  document.createElement = realCreateElement;

  return errors;
}

/* ── Suite 3: hostile data and failure states ──────────────────────────── */
async function securitySuite() {
  const errors = [];
  const hostile = '<img src=x onerror="window.__pwned=1"><svg id="xss-svg" onload="window.__pwned=2"></svg>';
  const meta = { generated_at: "2026-10-25T00:00:00Z", provider: "mock", cached: false, request_id: "t" };
  const money = { amount: "0.01", currency: "USD" };

  const payloads = {
    "/api/v1/buckets": {
      data: {
        items: [
          {
            name: hostile,
            region: "us-east-1",
            size_bytes: 1024,
            object_count: 1,
            storage_class: "STANDARD",
            created_at: "2026-01-01T00:00:00Z",
            versioning: true,
            encrypted: true,
            public_access_blocked: true,
            monthly_cost: money,
          },
        ],
        total_size_bytes: 1024,
        total_objects: 1,
        total_monthly_cost: money,
      },
      meta,
    },
    "/api/v1/instances": {
      data: {
        items: [
          {
            id: "i-0123456789abcdef0",
            type: "t3.micro",
            state: "running",
            availability_zone: "us-east-1a",
            private_ip: "10.0.0.1",
            public_ip: null,
            launch_time: "2026-01-01T00:00:00Z",
            cpu_utilization_pct: 10,
            memory_used_pct: 20,
            tags: { Name: hostile },
            monthly_cost: money,
          },
        ],
        by_state: { running: 1 },
      },
      meta,
    },
    "/api/v1/alarms": {
      data: {
        items: [
          {
            name: hostile,
            state: "ALARM",
            severity: "critical",
            namespace: hostile,
            metric: "CPUUtilization",
            threshold: 80,
            current_value: 91,
            comparison: "GreaterThanThreshold",
            updated_at: "2026-10-25T00:00:00Z",
          },
        ],
        by_state: { ALARM: 1 },
      },
      meta,
    },
  };

  const { window, document } = await bootDashboard({
    errors,
    intercept: async (url) => {
      const path = url.split("?")[0];
      const body = payloads[path] || { data: {}, meta };
      return { ok: true, status: 200, json: async () => body };
    },
  });

  check("hostile API strings do not execute", window.__pwned === undefined, String(window.__pwned ?? "none"));
  check("no injected element is created", document.getElementById("xss-svg") === null);
  check("no injected image reaches the table", document.querySelector("#buckets-table img") === null);
  check(
    "payload is rendered as literal text",
    document.getElementById("buckets-table").textContent.includes("onerror")
  );
  check(
    "malformed overview degrades instead of blanking the page",
    document.getElementById("kpis").querySelectorAll(".kpi").length === 0 ||
      document.getElementById("errorbar").hidden === false
  );
  return errors;
}

/* ── Suite 4: unauthenticated / failing API ────────────────────────────── */
async function failureSuite() {
  const errors = [];
  const { document, sleep, restoreFetch } = await bootDashboard({
    errors,
    intercept: async () => ({
      ok: false,
      status: 401,
      json: async () => ({
        error: { code: "unauthorized", message: "Missing API credentials.", request_id: "abc123" },
      }),
    }),
  });

  const banner = document.getElementById("errorbar");
  check("401 raises a visible error banner", !banner.hidden);
  const bannerText = document.getElementById("errorbar-text").textContent;
  check(
    "banner explains what to do",
    /api[_ ]?key/i.test(bannerText),
    bannerText
  );
  check(
    "dashboard shows an explicit failure state",
    document.getElementById("kpis").textContent.includes("Dashboard unavailable")
  );

  // A retry that succeeds must clear the banner (no stale errors).
  restoreFetch();
  document.getElementById("btn-retry").click();
  await sleep(1600);
  check("a successful retry clears the banner", document.getElementById("errorbar").hidden);
  check("a successful retry repopulates the panels", document.querySelectorAll("#kpis .kpi").length === 5);

  return errors;
}

/* ── Runner ────────────────────────────────────────────────────────────── */
const suites = [
  ["rendering", renderingSuite],
  ["interactions", interactionSuite],
  ["security", securitySuite],
  ["failure handling", failureSuite],
];

const allErrors = [];
for (const [name, suite] of suites) {
  console.log(`\n── ${name} ──`);
  const before = allErrors.length;
  const errors = await suite();
  allErrors.push(...errors);
  check(`${name}: no JavaScript errors`, errors.length === 0, `${errors.length} errors (${allErrors.length - before} new)`);
}

reporter.finish("dashboard", allErrors);
