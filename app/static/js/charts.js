/* ============================================================================
   charts.js — dependency-free SVG chart engine
   ----------------------------------------------------------------------------
   Why hand-rolled? A dashboard that pulls 300 KB of charting library to draw
   one stacked area chart is a liability: supply-chain surface, a second
   rendering model, and no control over the details that make a chart readable.
   These are ~350 lines, render server-sent numbers directly, and are testable.

   Exports (window.Chart):
     sparkline(values, opts)          -> inline SVG string
     stackedArea(el, model, opts)     -> live stacked area w/ crosshair + legend
     cumulative(el, model, opts)      -> actual vs. budget burn lines
     donut(el, segments)              -> ring chart
   ========================================================================== */
(function () {
  "use strict";

  const PALETTE = [
    "#5b9bff", "#2dd4bf", "#a78bfa", "#fbbf24",
    "#f472b6", "#34d399", "#fb923c", "#60a5fa",
    "#f76a6a", "#94a3b8",
  ];

  const svgNS = "http://www.w3.org/2000/svg";

  function el(tag, attrs) {
    const node = document.createElementNS(svgNS, tag);
    for (const k in attrs || {}) {
      if (attrs[k] !== undefined && attrs[k] !== null) node.setAttribute(k, attrs[k]);
    }
    return node;
  }

  function colorFor(i) {
    return PALETTE[i % PALETTE.length];
  }

  function hexToRgba(hex, alpha) {
    const h = hex.replace("#", "");
    const n = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h, 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
  }

  /* ── Sparkline ────────────────────────────────────────────────────────── */
  function sparkline(values, opts) {
    const o = Object.assign({ width: 84, height: 26, stroke: "#5b9bff", fill: true }, opts);
    const data = (values || []).filter((v) => Number.isFinite(v));
    if (data.length < 2) return "";
    const min = Math.min(...data);
    const max = Math.max(...data);
    const span = max - min || 1;
    const stepX = o.width / (data.length - 1);
    const pts = data.map((v, i) => [
      +(i * stepX).toFixed(2),
      +(o.height - 2 - ((v - min) / span) * (o.height - 4)).toFixed(2),
    ]);
    const line = pts.map((p, i) => (i ? "L" : "M") + p[0] + " " + p[1]).join(" ");
    const area = `${line} L${o.width} ${o.height} L0 ${o.height} Z`;
    const gid = "sg" + Math.random().toString(36).slice(2, 8);
    return `<svg viewBox="0 0 ${o.width} ${o.height}" preserveAspectRatio="none">
      <defs><linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="${o.stroke}" stop-opacity=".28"/>
        <stop offset="1" stop-color="${o.stroke}" stop-opacity="0"/>
      </linearGradient></defs>
      ${o.fill ? `<path d="${area}" fill="url(#${gid})"/>` : ""}
      <path d="${line}" fill="none" stroke="${o.stroke}" stroke-width="1.5"
            stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>
      <circle cx="${pts[pts.length - 1][0]}" cy="${pts[pts.length - 1][1]}" r="1.8" fill="${o.stroke}"/>
    </svg>`;
  }

  /* ── Stacked area ─────────────────────────────────────────────────────── */
  function stackedArea(container, model, opts) {
    const o = Object.assign({ height: 268, formatMoney: (v) => v, hidden: new Set() }, opts);
    const wrap = container;
    let tip = wrap.querySelector(".chart-tip");
    if (!tip) {
      tip = document.createElement("div");
      tip.className = "chart-tip";
      wrap.appendChild(tip);
    }

    function render() {
      const width = Math.max(wrap.clientWidth || 640, 320);
      const pad = { top: 14, right: 12, bottom: 22, left: 54 };
      const innerW = width - pad.left - pad.right;
      const innerH = o.height - pad.top - pad.bottom;
      const points = model.points || [];
      const series = model.series.filter((s) => !o.hidden.has(s.name));

      wrap.querySelectorAll("svg").forEach((n) => n.remove());
      const svg = el("svg", { viewBox: `0 0 ${width} ${o.height}`, height: o.height });
      svg.style.height = o.height + "px";

      if (!points.length) {
        wrap.appendChild(svg);
        return;
      }

      // stack totals (respecting hidden series)
      const totals = points.map((p) =>
        series.reduce((sum, s) => sum + (Number(p.values[s.name]) || 0), 0)
      );
      const rawMax = Math.max(...totals, 1);
      const tickCount = 4;
      const niceMax = Math.ceil(rawMax / tickCount) * tickCount;
      const x = (i) => pad.left + (points.length === 1 ? innerW / 2 : (i / (points.length - 1)) * innerW);
      const y = (v) => pad.top + innerH - (v / niceMax) * innerH;

      // gridlines + y labels
      for (let t = 0; t <= tickCount; t++) {
        const val = (niceMax / tickCount) * t;
        const yy = y(val);
        svg.appendChild(el("line", { x1: pad.left, y1: yy, x2: width - pad.right, y2: yy, class: "chart-grid-line" }));
        const label = el("text", { x: pad.left - 8, y: yy + 3.5, "text-anchor": "end", class: "chart-axis-label" });
        label.textContent = o.formatMoney(val, true);
        svg.appendChild(label);
      }

      // stack baselines
      const baseline = new Array(points.length).fill(0);
      const stackPaths = [];
      series.forEach((s, si) => {
        const topPts = [];
        const botPts = [];
        points.forEach((p, i) => {
          const v = Number(p.values[s.name]) || 0;
          const y0 = y(baseline[i]);
          const y1 = y(baseline[i] + v);
          topPts.push([x(i), y1]);
          botPts.push([x(i), y0]);
          baseline[i] += v;
        });
        const d =
          topPts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(2) + " " + p[1].toFixed(2)).join(" ") +
          " " +
          botPts.reverse().map((p) => "L" + p[0].toFixed(2) + " " + p[1].toFixed(2)).join(" ") +
          " Z";
        stackPaths.push({ d, color: s.color, index: si, name: s.name });
      });

      // draw in reverse so the largest sits at the bottom visually
      stackPaths.forEach((sp) => {
        svg.appendChild(el("path", { d: sp.d, fill: hexToRgba(sp.color, 0.55), stroke: sp.color, "stroke-width": 1, "stroke-opacity": 0.55 }));
      });

      // total line on top for legibility
      const totalPath = points
        .map((p, i) => (i ? "L" : "M") + x(i).toFixed(2) + " " + y(totals[i]).toFixed(2))
        .join(" ");
      svg.appendChild(el("path", { d: totalPath, fill: "none", stroke: "#dbe7f7", "stroke-width": 1.4, "stroke-opacity": 0.55, "stroke-linejoin": "round" }));

      // x labels
      const labelEvery = Math.max(1, Math.ceil(points.length / (width < 620 ? 5 : 8)));
      points.forEach((p, i) => {
        if (i % labelEvery !== 0 && i !== points.length - 1) return;
        const t = el("text", { x: x(i), y: o.height - 6, "text-anchor": i === 0 ? "start" : i === points.length - 1 ? "end" : "middle", class: "chart-axis-label" });
        t.textContent = o.formatDate ? o.formatDate(p.date) : p.date;
        svg.appendChild(t);
      });

      // interaction layer
      const crosshair = el("line", { y1: pad.top, y2: pad.top + innerH, class: "chart-crosshair" });
      crosshair.style.opacity = "0";
      svg.appendChild(crosshair);
      const dot = el("circle", { r: 3.5, fill: "#0c0f15", stroke: "#dbe7f7", "stroke-width": 1.6 });
      dot.style.opacity = "0";
      svg.appendChild(dot);

      const hit = el("rect", { x: pad.left, y: pad.top, width: innerW, height: innerH, fill: "transparent" });
      svg.appendChild(hit);

      function showAt(clientX) {
        const box = svg.getBoundingClientRect();
        const rel = clientX - box.left;
        const ratio = Math.min(1, Math.max(0, (rel - pad.left) / innerW));
        const idx = Math.round(ratio * (points.length - 1));
        const px = x(idx);
        crosshair.setAttribute("x1", px);
        crosshair.setAttribute("x2", px);
        crosshair.style.opacity = "1";
        dot.setAttribute("cx", px);
        dot.setAttribute("cy", y(totals[idx]));
        dot.style.opacity = "1";

        const p = points[idx];
        const rows = series
          .map((s) => ({ name: s.name, color: s.color, value: Number(p.values[s.name]) || 0 }))
          .filter((r) => r.value > 0)
          .sort((a, b) => b.value - a.value)
          .slice(0, 8)
          .map(
            (r) => `<div class="tip-row">
              <span class="swatch" style="background:${r.color}"></span>
              <span class="tip-name">${r.name}</span>
              <span class="tip-val">${o.formatMoney(r.value)}</span>
            </div>`
          )
          .join("");
        tip.innerHTML =
          `<div class="tip-date">${o.formatFullDate ? o.formatFullDate(p.date) : p.date}</div>${rows}
           <div class="tip-total"><span>Total</span><span>${o.formatMoney(totals[idx])}</span></div>`;
        tip.classList.add("is-visible");
        const tipW = tip.offsetWidth || 180;
        const left = Math.min(Math.max(8, px - tipW / 2), width - tipW - 6);
        tip.style.left = left + "px";
        tip.style.top = pad.top + "px";
      }

      function hide() {
        crosshair.style.opacity = "0";
        dot.style.opacity = "0";
        tip.classList.remove("is-visible");
      }

      svg.addEventListener("mousemove", (ev) => showAt(ev.clientX));
      svg.addEventListener("mouseleave", hide);
      svg.addEventListener("touchstart", (ev) => ev.touches[0] && showAt(ev.touches[0].clientX), { passive: true });
      svg.addEventListener("touchmove", (ev) => ev.touches[0] && showAt(ev.touches[0].clientX), { passive: true });

      wrap.appendChild(svg);
    }

    render();
    const ro = new ResizeObserver(() => {
      window.requestAnimationFrame(render);
    });
    ro.observe(wrap);
    return { render, destroy: () => ro.disconnect() };
  }

  /* ── Cumulative spend vs budget burn ──────────────────────────────────── */
  function cumulative(container, model, opts) {
    const o = Object.assign({ height: 220, formatMoney: (v) => v, formatDate: (d) => d }, opts);
    const wrap = container;
    wrap.querySelectorAll(".chart-tip").forEach((n) => n.remove());
    const tip = document.createElement("div");
    tip.className = "chart-tip";
    wrap.appendChild(tip);

    function render() {
      wrap.querySelectorAll("svg").forEach((n) => n.remove());
      const width = Math.max(wrap.clientWidth || 520, 300);
      const pad = { top: 12, right: 14, bottom: 22, left: 52 };
      const innerW = width - pad.left - pad.right;
      const innerH = o.height - pad.top - pad.bottom;
      const pts = model.points || [];
      const svg = el("svg", { viewBox: `0 0 ${width} ${o.height}`, height: o.height });
      if (pts.length < 2) { wrap.appendChild(svg); return; }

      let running = 0;
      const actual = pts.map((p) => (running += Number(p.value) || 0));
      const budgetLine = pts.map((p) => (model.budget / Math.max(pts.length - 1, 1)) * p.i);
      const max = Math.max(...actual, ...budgetLine) * 1.08 || 1;
      const x = (i) => pad.left + (i / (pts.length - 1)) * innerW;
      const y = (v) => pad.top + innerH - (v / max) * innerH;

      for (let t = 0; t <= 3; t++) {
        const v = (max / 3) * t;
        svg.appendChild(el("line", { x1: pad.left, y1: y(v), x2: width - pad.right, y2: y(v), class: "chart-grid-line" }));
        const lab = el("text", { x: pad.left - 8, y: y(v) + 3.5, "text-anchor": "end", class: "chart-axis-label" });
        lab.textContent = o.formatMoney(v, true);
        svg.appendChild(lab);
      }

      const actualPath = actual.map((v, i) => (i ? "L" : "M") + x(i) + " " + y(v)).join(" ");
      const area = `${actualPath} L${x(pts.length - 1)} ${pad.top + innerH} L${pad.left} ${pad.top + innerH} Z`;
      const gid = "cg" + Math.random().toString(36).slice(2, 8);

      svg.appendChild(el("path", { d: area, fill: `url(#${gid})` }));
      const defs = el("defs");
      const grad = el("linearGradient", { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 });
      grad.appendChild(el("stop", { offset: "0", "stop-color": "#5b9bff", "stop-opacity": "0.35" }));
      grad.appendChild(el("stop", { offset: "1", "stop-color": "#5b9bff", "stop-opacity": "0" }));
      defs.appendChild(grad);
      svg.insertBefore(defs, svg.firstChild);

      const budgetPath = budgetLine.map((v, i) => (i ? "L" : "M") + x(i) + " " + y(v)).join(" ");
      svg.appendChild(el("path", { d: budgetPath, fill: "none", stroke: "#fbbf24", "stroke-width": 1.4, "stroke-dasharray": "5 4", "stroke-opacity": .85 }));
      svg.appendChild(el("path", { d: actualPath, fill: "none", stroke: "#5b9bff", "stroke-width": 2, "stroke-linejoin": "round" }));

      const lastX = x(pts.length - 1);
      svg.appendChild(el("circle", { cx: lastX, cy: y(actual[actual.length - 1]), r: 3.2, fill: "#0c0f15", stroke: "#5b9bff", "stroke-width": 2 }));

      const labelEvery = Math.max(1, Math.ceil(pts.length / 5));
      pts.forEach((p, i) => {
        if (i % labelEvery !== 0 && i !== pts.length - 1) return;
        const t = el("text", { x: x(i), y: o.height - 6, "text-anchor": i === pts.length - 1 ? "end" : i === 0 ? "start" : "middle", class: "chart-axis-label" });
        t.textContent = o.formatDate(p.date);
        svg.appendChild(t);
      });

      const cross = el("line", { y1: pad.top, y2: pad.top + innerH, class: "chart-crosshair" });
      cross.style.opacity = "0";
      svg.appendChild(cross);
      const hit = el("rect", { x: pad.left, y: pad.top, width: innerW, height: innerH, fill: "transparent" });
      svg.appendChild(hit);

      svg.addEventListener("mousemove", (ev) => {
        const box = svg.getBoundingClientRect();
        const ratio = Math.min(1, Math.max(0, (ev.clientX - box.left - pad.left) / innerW));
        const i = Math.round(ratio * (pts.length - 1));
        cross.setAttribute("x1", x(i));
        cross.setAttribute("x2", x(i));
        cross.style.opacity = "1";
        const over = actual[i] > budgetLine[i];
        tip.innerHTML = `<div class="tip-date">${o.formatFullDate ? o.formatFullDate(pts[i].date) : pts[i].date}</div>
          <div class="tip-row"><span class="swatch" style="background:#5b9bff"></span>
            <span class="tip-name">Cumulative</span><span class="tip-val">${o.formatMoney(actual[i])}</span></div>
          <div class="tip-row"><span class="swatch" style="background:#fbbf24"></span>
            <span class="tip-name">Budget pace</span><span class="tip-val">${o.formatMoney(budgetLine[i])}</span></div>
          <div class="tip-total"><span>${over ? "Over pace" : "Under pace"}</span>
            <span style="color:${over ? "#f76a6a" : "#34d399"}">${o.formatMoney(Math.abs(actual[i] - budgetLine[i]))}</span></div>`;
        tip.classList.add("is-visible");
        const tipW = tip.offsetWidth || 190;
        tip.style.left = Math.min(Math.max(8, x(i) - tipW / 2), width - tipW - 6) + "px";
        tip.style.top = pad.top + "px";
      });
      svg.addEventListener("mouseleave", () => {
        cross.style.opacity = "0";
        tip.classList.remove("is-visible");
      });

      wrap.appendChild(svg);
    }

    render();
    const ro = new ResizeObserver(() => window.requestAnimationFrame(render));
    ro.observe(wrap);
    return { render, destroy: () => ro.disconnect() };
  }

  /* ── Donut ────────────────────────────────────────────────────────────── */
  function donut(container, segments, opts) {
    const o = Object.assign({ size: 150, stroke: 17, formatMoney: (v) => v }, opts);
    const total = segments.reduce((s, x) => s + x.value, 0) || 1;
    const r = (o.size - o.stroke) / 2;
    const circumference = 2 * Math.PI * r;
    let offset = 0;

    container.querySelectorAll("svg").forEach((n) => n.remove());
    const svg = el("svg", {
      viewBox: `0 0 ${o.size} ${o.size}`,
      width: o.size,
      height: o.size,
    });
    svg.style.width = o.size + "px";
    svg.style.height = o.size + "px";
    svg.style.flex = "none";
    svg.appendChild(el("circle", { cx: o.size / 2, cy: o.size / 2, r, fill: "none", stroke: "#161c27", "stroke-width": o.stroke }));

    segments.forEach((seg) => {
      const len = (seg.value / total) * circumference;
      const circle = el("circle", {
        cx: o.size / 2,
        cy: o.size / 2,
        r,
        fill: "none",
        stroke: seg.color,
        "stroke-width": o.stroke,
        "stroke-dasharray": `${Math.max(len - 2, 0)} ${circumference - Math.max(len - 2, 0)}`,
        "stroke-dashoffset": -offset,
        class: "donut-seg",
      });
      circle.appendChild(el("title")).textContent = `${seg.name}: ${o.formatMoney(seg.value)} (${((seg.value / total) * 100).toFixed(1)}%)`;
      svg.appendChild(circle);
      offset += len;
    });
    container.appendChild(svg);

    if (!container.querySelector(".donut-center")) {
      const center = document.createElement("div");
      center.className = "donut-center";
      container.appendChild(center);
    }
    const center = container.querySelector(".donut-center");
    center.innerHTML = `<div class="donut-total">${o.formatMoney(total)}</div><div class="donut-cap">total MTD</div>`;
  }

  window.Chart = { sparkline, stackedArea, cumulative, donut, PALETTE, colorFor, hexToRgba };
})();
