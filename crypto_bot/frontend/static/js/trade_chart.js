// uPlot hydration for the trade detail page.
// Renders OHLC candlesticks (when open/close curves are present), TP/SL
// predicted levels, and entry/exit vertical markers. Re-initialises on
// htmx:afterSwap so HTMX navigation does not leak uPlot instances.
(function () {
  let currentPlot = null;

  function destroyCurrent() {
    if (currentPlot) {
      try { currentPlot.destroy(); } catch (e) {}
      currentPlot = null;
    }
  }

  function clearChildren(el) {
    while (el.firstChild) el.removeChild(el.firstChild);
  }

  function showMessage(container, text, color) {
    clearChildren(container);
    const div = document.createElement("div");
    div.className = "text-sm " + (color || "text-gray-400");
    div.textContent = text;
    container.appendChild(div);
  }

  // Plugin: draws OHLC candlesticks for the given series indices (o/h/l/c).
  // Uses u.ctx directly in a draw hook so we don't need an external lib.
  function candlestickPlugin(openIdx, highIdx, lowIdx, closeIdx) {
    return {
      hooks: {
        draw: function (u) {
          const ctx = u.ctx;
          const xs = u.data[0];
          const os = u.data[openIdx];
          const hs = u.data[highIdx];
          const ls = u.data[lowIdx];
          const cs = u.data[closeIdx];
          if (!os || !cs) return;
          const n = xs.length;
          // Estimate pixel width per candle from x-scale distance.
          let step = 1;
          if (n >= 2) step = xs[1] - xs[0];
          const pxPerUnit = (u.valToPos(xs[0] + step, "x", true) - u.valToPos(xs[0], "x", true));
          const bodyW = Math.max(2, Math.floor(Math.abs(pxPerUnit) * 0.6));

          ctx.save();
          for (let i = 0; i < n; i++) {
            const o = os[i], h = hs[i], l = ls[i], c = cs[i];
            if (o == null || h == null || l == null || c == null) continue;
            const xPx = u.valToPos(xs[i], "x", true);
            const oPx = u.valToPos(o, "y", true);
            const hPx = u.valToPos(h, "y", true);
            const lPx = u.valToPos(l, "y", true);
            const cPx = u.valToPos(c, "y", true);
            const bull = c >= o;
            const color = bull ? "#22c55e" : "#ef4444";
            ctx.strokeStyle = color;
            ctx.fillStyle = color;
            ctx.lineWidth = 1;
            // Wick
            ctx.beginPath();
            ctx.moveTo(xPx, hPx);
            ctx.lineTo(xPx, lPx);
            ctx.stroke();
            // Body
            const top = Math.min(oPx, cPx);
            const h2 = Math.max(1, Math.abs(cPx - oPx));
            ctx.fillRect(xPx - bodyW / 2, top, bodyW, h2);
          }
          ctx.restore();
        },
      },
    };
  }

  // Plugin: vertical marker at a given x value with a text label on top.
  function verticalMarker(xVal, label, color) {
    return {
      hooks: {
        draw: function (u) {
          if (xVal == null) return;
          const ctx = u.ctx;
          const xPx = u.valToPos(xVal, "x", true);
          const top = u.bbox.top;
          const bot = u.bbox.top + u.bbox.height;
          ctx.save();
          ctx.strokeStyle = color;
          ctx.fillStyle = color;
          ctx.lineWidth = 1.5;
          ctx.setLineDash([4, 3]);
          ctx.beginPath();
          ctx.moveTo(xPx, top);
          ctx.lineTo(xPx, bot);
          ctx.stroke();
          ctx.setLineDash([]);
          ctx.font = "11px ui-sans-serif, system-ui, sans-serif";
          const tw = ctx.measureText(label).width;
          ctx.fillStyle = "rgba(17,24,39,0.85)";
          ctx.fillRect(xPx - tw / 2 - 4, top + 2, tw + 8, 14);
          ctx.fillStyle = color;
          ctx.fillText(label, xPx - tw / 2, top + 13);
          ctx.restore();
        },
      },
    };
  }

  async function renderChart(container) {
    destroyCurrent();
    const tradeId = container.getAttribute("data-trade-id");
    if (!tradeId) return;

    let data;
    try {
      const res = await fetch("/api/trades/" + encodeURIComponent(tradeId) + "/curves");
      data = await res.json();
    } catch (err) {
      showMessage(container, "Failed to load curve data.", "text-red-400");
      return;
    }

    const sc = data.sidecar || {};
    const realHigh = sc.real_high_curve || sc.real_high || data.real_high_curve || [];
    const realLow = sc.real_low_curve || sc.real_low || data.real_low_curve || [];
    // Legacy sidecars (FET, ZRO) have no open/close — degrade gracefully
    // by reusing high/low as approximation so line rendering still works.
    const realOpen = sc.real_open_curve || data.real_open_curve || null;
    const realClose = sc.real_close_curve || data.real_close_curve || null;
    const hasOHLC = Array.isArray(realOpen) && Array.isArray(realClose)
      && realOpen.length > 0 && realClose.length > 0;
    const intervalSec = sc.candle_interval_sec || sc.interval_sec || data.candle_interval_sec || 900;
    const k = Math.max(realHigh.length, realLow.length, hasOHLC ? realOpen.length : 0);

    if (k === 0) {
      showMessage(container, "No real candle curve recorded for this trade.", "text-yellow-400");
      return;
    }

    const tp = sc.predicted_tp_price != null ? sc.predicted_tp_price
      : (sc.predicted_tp != null ? sc.predicted_tp : data.predicted_tp_price);
    const sl = sc.predicted_sl_price != null ? sc.predicted_sl_price
      : (sc.predicted_sl != null ? sc.predicted_sl : data.predicted_sl_price);
    const entry = sc.entry_price != null ? sc.entry_price
      : (sc.entry != null ? sc.entry : data.entry_price);
    const exitPrice = sc.exit_price != null ? sc.exit_price
      : (sc.exit != null ? sc.exit : data.exit_price);

    const xs = [];
    for (let i = 0; i < k; i++) xs.push(i * intervalSec);

    const seriesData = [xs];
    const seriesCfg = [{}];

    // Predicted TP/SL horizontal reference lines with pct in the legend.
    const tpPct = sc.predicted_tp_pct != null ? sc.predicted_tp_pct : data.predicted_tp_pct;
    const slPct = sc.predicted_sl_pct != null ? sc.predicted_sl_pct : data.predicted_sl_pct;
    if (tp != null) {
      seriesData.push(new Array(k).fill(tp));
      const lbl = tpPct != null ? ("TP predicted " + Number(tpPct).toFixed(2) + "%") : "Predicted TP";
      seriesCfg.push({ label: lbl, stroke: "#22c55e", width: 2, dash: [6, 4] });
    }
    if (sl != null) {
      seriesData.push(new Array(k).fill(sl));
      const lbl = slPct != null ? ("SL predicted " + Number(slPct).toFixed(2) + "%") : "Predicted SL";
      seriesCfg.push({ label: lbl, stroke: "#ef4444", width: 2, dash: [6, 4] });
    }
    if (entry != null) {
      seriesData.push(new Array(k).fill(entry));
      seriesCfg.push({ label: "Entry", stroke: "#60a5fa", width: 1, dash: [2, 3] });
    }

    let openIdx = -1, highIdx = -1, lowIdx = -1, closeIdx = -1;
    if (hasOHLC) {
      // Invisible series so uPlot scales y to OHLC range but we custom-draw candles.
      openIdx = seriesData.length;
      seriesData.push(realOpen);
      seriesCfg.push({ label: "Open", stroke: "rgba(0,0,0,0)", width: 0, points: { show: false } });
      highIdx = seriesData.length;
      seriesData.push(realHigh);
      seriesCfg.push({ label: "High", stroke: "rgba(0,0,0,0)", width: 0, points: { show: false } });
      lowIdx = seriesData.length;
      seriesData.push(realLow);
      seriesCfg.push({ label: "Low", stroke: "rgba(0,0,0,0)", width: 0, points: { show: false } });
      closeIdx = seriesData.length;
      seriesData.push(realClose);
      seriesCfg.push({ label: "Close", stroke: "rgba(0,0,0,0)", width: 0, points: { show: false } });
    } else {
      // Legacy fallback: plot high/low as lines.
      seriesData.push(realHigh);
      seriesCfg.push({ label: "Real high", stroke: "#60a5fa", width: 1.5 });
      seriesData.push(realLow);
      seriesCfg.push({ label: "Real low", stroke: "#a78bfa", width: 1.5 });
    }

    const plugins = [];
    if (hasOHLC) {
      plugins.push(candlestickPlugin(openIdx, highIdx, lowIdx, closeIdx));
    }
    // Entry marker (always at x=0, first candle).
    plugins.push(verticalMarker(0, "ENTRY", "#fbbf24"));
    // Exit marker at the last observed candle when we know there was an exit.
    if (exitPrice != null && k > 1) {
      plugins.push(verticalMarker((k - 1) * intervalSec, "EXIT", "#f472b6"));
    }

    const rect = container.getBoundingClientRect();
    const opts = {
      width: Math.max(320, Math.floor(rect.width || 800)),
      height: 360,
      scales: { x: { time: false } },
      axes: [
        {
          stroke: "#9ca3af",
          grid: { stroke: "#1f2937" },
          values: function (u, vals) { return vals.map(function (v) { return Math.round(v / 60) + "m"; }); },
        },
        { stroke: "#9ca3af", grid: { stroke: "#1f2937" } },
      ],
      legend: { show: true },
      series: seriesCfg,
      plugins: plugins,
    };

    clearChildren(container);
    currentPlot = new uPlot(opts, seriesData, container);

    const caption = document.createElement("div");
    caption.className = "mt-2 text-xs text-gray-400";
    const reason = data.exit_reason || "—";
    const mode = hasOHLC ? "candles" : "lines (legacy)";
    caption.textContent = "Entry: " + (entry != null ? entry : "—") +
      "   Exit: " + (exitPrice != null ? exitPrice : "—") +
      "   Reason: " + reason +
      "   K observed: " + k +
      "   Mode: " + mode;
    container.appendChild(caption);
  }

  function init() {
    const container = document.getElementById("chart");
    if (container) renderChart(container);
  }

  document.addEventListener("DOMContentLoaded", init);
  if (window.htmx) {
    window.htmx.on("htmx:afterSwap", function () {
      destroyCurrent();
      init();
    });
  }
  window.addEventListener("beforeunload", destroyCurrent);
})();
