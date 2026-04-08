// uPlot hydration for the trade detail page.
// Re-initialises on htmx:afterSwap so that HTMX-driven navigation does not
// leak uPlot instances. Only static text is written to the DOM (textContent).
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
    const realHigh = sc.real_high || data.real_high_curve || [];
    const realLow = sc.real_low || data.real_low_curve || [];
    const intervalSec = sc.interval_sec || data.candle_interval_sec || 900;
    const k = Math.max(realHigh.length, realLow.length);

    if (k === 0) {
      showMessage(container, "No real candle curve recorded for this trade.", "text-yellow-400");
      return;
    }

    const tp = sc.predicted_tp != null ? sc.predicted_tp : data.predicted_tp_price;
    const sl = sc.predicted_sl != null ? sc.predicted_sl : data.predicted_sl_price;
    const entry = sc.entry != null ? sc.entry : data.entry_price;
    const exitPrice = sc.exit != null ? sc.exit : data.exit_price;

    const xs = [];
    for (let i = 0; i < k; i++) xs.push(i * intervalSec);

    const seriesData = [xs];
    const seriesCfg = [{}];
    if (tp != null) {
      seriesData.push(new Array(k).fill(tp));
      seriesCfg.push({ label: "Predicted TP", stroke: "#22c55e", width: 2, dash: [6, 4] });
    }
    if (sl != null) {
      seriesData.push(new Array(k).fill(sl));
      seriesCfg.push({ label: "Predicted SL", stroke: "#ef4444", width: 2, dash: [6, 4] });
    }
    seriesData.push(realHigh);
    seriesCfg.push({ label: "Real high", stroke: "#60a5fa", width: 1.5 });
    seriesData.push(realLow);
    seriesCfg.push({ label: "Real low", stroke: "#a78bfa", width: 1.5 });

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
    };

    clearChildren(container);
    currentPlot = new uPlot(opts, seriesData, container);

    const caption = document.createElement("div");
    caption.className = "mt-2 text-xs text-gray-400";
    const reason = data.exit_reason || "—";
    caption.textContent = "Entry: " + (entry != null ? entry : "—") +
      "   Exit: " + (exitPrice != null ? exitPrice : "—") +
      "   Reason: " + reason +
      "   K observed: " + k;
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
