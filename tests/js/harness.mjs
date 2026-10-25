/**
 * harness.mjs — boots the real dashboard inside jsdom.
 *
 * The point of testing here rather than in a browser-automation tool is speed
 * and portability: no Chromium download, no display server, a few seconds of
 * runtime, and it runs inside the same CI job as the Python suite.
 *
 * The dashboard is served by the FastAPI app, so the tests exercise the actual
 * HTML, CSS and JavaScript together with the real API responses — not a
 * hand-written fixture of what we think the API returns.
 */

import { JSDOM, VirtualConsole } from "jsdom";

export const BASE = process.env.DASHBOARD_URL || "http://localhost:8000";
export const API_KEY = process.env.DASH_API_KEY || "local-dev-key";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Fetch a page from the running app and fail loudly if it is not reachable. */
export async function getText(path) {
  const response = await fetch(BASE + path);
  if (!response.ok) {
    throw new Error(`GET ${path} -> ${response.status}. Is the API running at ${BASE}?`);
  }
  return response.text();
}

/**
 * Load the dashboard and wait for its initial data fetch to settle.
 *
 * @param {object} [options]
 * @param {(path: string) => Promise<Response>} [options.intercept] - replace the
 *   network layer, used by the security test to feed hostile payloads.
 * @param {string[]} [options.errors] - collect console/page errors into this array.
 */
export async function bootDashboard({ intercept, errors = [] } = {}) {
  const virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", (error) => errors.push(`jsdomError: ${error.message}`));
  virtualConsole.on("error", (...args) => errors.push(`console.error: ${args.join(" ")}`));

  const html = await getText("/");
  const dom = new JSDOM(html, {
    url: BASE + "/",
    runScripts: "dangerously",
    pretendToBeVisual: true,
    virtualConsole,
  });
  const { window } = dom;
  const document = window.document;

  // jsdom implements neither of these, and the dashboard legitimately uses both.
  window.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
  window.IntersectionObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
  window.Element.prototype.scrollIntoView = function scrollIntoView() {};
  window.HTMLElement.prototype.scrollTo = function scrollTo() {};

  // Downloads cannot happen in jsdom; record the intent instead.
  window.URL.createObjectURL = () => "blob:test";
  window.URL.revokeObjectURL = () => {};

  window.alert = () => {};
  window.prompt = () => API_KEY;

  window.fetch = intercept
    ? async (url, options) => intercept(String(url), options)
    : (url, options) => fetch(String(url).startsWith("http") ? url : BASE + url, options);

  for (const script of ["/static/js/api.js", "/static/js/charts.js", "/static/js/app.js"]) {
    window.eval(await getText(script));
  }

  document.dispatchEvent(new window.Event("DOMContentLoaded", { bubbles: true }));
  await sleep(2200);

  return { window, document, dom, sleep };
}

/** Minimal assertion recorder that prints a readable report and sets exit code. */
export function createReporter() {
  const results = [];
  return {
    check(name, condition, detail = "") {
      const pass = Boolean(condition);
      results.push({ name, pass, detail });
      // Stream results rather than buffering: if a suite hangs, the last line
      // printed tells you exactly where.
      const suffix = detail ? `  (${detail})` : "";
      console.log(`${pass ? "PASS" : "FAIL"}  ${name}${suffix}`);
    },
    finish(label, errors = []) {
      const failed = results.filter((result) => !result.pass).length;
      if (errors.length) {
        console.log("\nJavaScript errors:");
        for (const error of errors) console.log(`  ${error}`);
      }
      console.log(`\n${label}: ${results.length - failed}/${results.length} checks passed`);
      const exitCode = failed || errors.length ? 1 : 0;
      console.log(exitCode ? "FAILED" : "OK");
      // The page keeps timers alive, so node would otherwise never exit.
      process.exit(exitCode);
    },
  };
}

export { sleep };
