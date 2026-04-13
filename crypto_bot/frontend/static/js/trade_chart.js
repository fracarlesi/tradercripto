// Trade detail chart — clean, readable layout.
// Candlesticks + TP/SL lines + entry/exit markers.
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

  // --- Plugins ---

  // OHLC candlesticks drawn directly on canvas.
  // preN: number of context candles before entry (rendered dimmed).
  // postStart: index from which post-exit context candles begin (rendered dimmed).
  function candlestickPlugin(openIdx, highIdx, lowIdx, closeIdx, preN, postStart) {
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
          let step = 1;
          if (n >= 2) step = xs[1] - xs[0];
          const pxPerUnit = (u.valToPos(xs[0] + step, "x", true) - u.valToPos(xs[0], "x", true));
          const bodyW = Math.max(3, Math.floor(Math.abs(pxPerUnit) * 0.6));

          ctx.save();
          for (let i = 0; i < n; i++) {
            const o = os[i], h = hs[i], l = ls[i], c = cs[i];
            if (o == null || h == null || l == null || c == null) continue;
            const isContext = (preN > 0 && i < preN) || (postStart != null && i >= postStart);
            const alpha = isContext ? 0.35 : 1.0;
            const xPx = u.valToPos(xs[i], "x", true);
            const oPx = u.valToPos(o, "y", true);
            const hPx = u.valToPos(h, "y", true);
            const lPx = u.valToPos(l, "y", true);
            const cPx = u.valToPos(c, "y", true);
            const bull = c >= o;
            const color = bull ? `rgba(34,197,94,${alpha})` : `rgba(239,68,68,${alpha})`;
            ctx.strokeStyle = color;
            ctx.fillStyle = color;
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(xPx, hPx);
            ctx.lineTo(xPx, lPx);
            ctx.stroke();
            const top = Math.min(oPx, cPx);
            const h2 = Math.max(1, Math.abs(cPx - oPx));
            ctx.fillRect(xPx - bodyW / 2, top, bodyW, h2);
          }
          ctx.restore();
        },
      },
    };
  }

  // Horizontal price line with right-edge label badge.
  function hLine(yVal, color, label, dashPattern) {
    return {
      hooks: {
        draw: function (u) {
          if (yVal == null) return;
          var ctx = u.ctx;
          var left = u.bbox.left;
          var right = left + u.bbox.width;
          var yPx = u.valToPos(yVal, "y", true);

          ctx.save();
          ctx.strokeStyle = color;
          ctx.lineWidth = 1.5;
          ctx.setLineDash(dashPattern || []);
          ctx.beginPath();
          ctx.moveTo(left, yPx);
          ctx.lineTo(right, yPx);
          ctx.stroke();
          ctx.setLineDash([]);

          if (label) {
            ctx.font = "bold 10px ui-sans-serif, system-ui, sans-serif";
            var tw = ctx.measureText(label).width;
            var pad = 4;
            var bw = tw + pad * 2;
            var bh = 16;
            ctx.fillStyle = color;
            ctx.beginPath();
            ctx.roundRect(right + 3, yPx - bh / 2, bw, bh, 3);
            ctx.fill();
            ctx.fillStyle = "#111827";
            ctx.fillText(label, right + 3 + pad, yPx + 4);
          }
          ctx.restore();
        },
      },
    };
  }

  // Subtle vertical line marking the expected expiry deadline.
  function expiryLine(xVal) {
    return {
      hooks: {
        draw: function (u) {
          if (xVal == null) return;
          var ctx = u.ctx;
          var xPx = u.valToPos(xVal, "x", true);
          var top = u.bbox.top;
          var bot = top + u.bbox.height;
          ctx.save();
          ctx.strokeStyle = "rgba(156,163,175,0.4)";
          ctx.lineWidth = 1;
          ctx.setLineDash([3, 4]);
          ctx.beginPath();
          ctx.moveTo(xPx, top);
          ctx.lineTo(xPx, bot);
          ctx.stroke();
          ctx.setLineDash([]);
          // Small label at top
          ctx.font = "9px ui-sans-serif, system-ui, sans-serif";
          ctx.fillStyle = "rgba(156,163,175,0.7)";
          ctx.fillText("EXPIRY", xPx + 3, top + 12);
          ctx.restore();
        },
      },
    };
  }

  // Vertical line spanning the full chart height at a given x.
  // Label badge positioned at the yVal price level so it visually matches the price.
  function verticalLine(xVal, yVal, label, color) {
    return {
      hooks: {
        draw: function (u) {
          if (xVal == null) return;
          var ctx = u.ctx;
          var xPx = u.valToPos(xVal, "x", true);
          var top = u.bbox.top;
          var bot = top + u.bbox.height;

          ctx.save();
          // Vertical line
          ctx.strokeStyle = color;
          ctx.lineWidth = 1.5;
          ctx.setLineDash([5, 4]);
          ctx.beginPath();
          ctx.moveTo(xPx, top);
          ctx.lineTo(xPx, bot);
          ctx.stroke();
          ctx.setLineDash([]);

          // Label badge at the price level
          if (label && yVal != null) {
            var yPx = u.valToPos(yVal, "y", true);
            ctx.font = "bold 10px ui-sans-serif, system-ui, sans-serif";
            var tw = ctx.measureText(label).width;
            var badgeW = tw + 10;
            var badgeH = 16;
            // Position badge to the right of the vertical line
            ctx.fillStyle = color;
            ctx.beginPath();
            ctx.roundRect(xPx + 4, yPx - badgeH / 2, badgeW, badgeH, 3);
            ctx.fill();
            ctx.fillStyle = "#111827";
            ctx.fillText(label, xPx + 9, yPx + 4);

            // Small dot on the price level
            ctx.fillStyle = color;
            ctx.beginPath();
            ctx.arc(xPx, yPx, 4, 0, Math.PI * 2);
            ctx.fill();
          }
          ctx.restore();
        },
      },
    };
  }

  async function renderChart(container) {
    destroyCurrent();
    var tradeId = container.getAttribute("data-trade-id");
    if (!tradeId) return;

    var data;
    try {
      var res = await fetch("/api/trades/" + encodeURIComponent(tradeId) + "/curves");
      data = await res.json();
    } catch (err) {
      showMessage(container, "Failed to load curve data.", "text-red-400");
      return;
    }

    var sc = data.sidecar || {};
    var chart = data.chart;  // New: optimal-resolution chart data

    var realHigh, realLow, realOpen, realClose, hasOHLC;
    var intervalSec, preCandlesRaw, postCandlesRaw;

    if (chart && chart.trade_candles && chart.trade_candles.length > 0) {
      // --- New path: use HL API candles at optimal resolution ---
      var tc = chart.trade_candles;
      realOpen = tc.map(function(c) { return c.o; });
      realHigh = tc.map(function(c) { return c.h; });
      realLow = tc.map(function(c) { return c.l; });
      realClose = tc.map(function(c) { return c.c; });
      hasOHLC = true;
      intervalSec = chart.interval_sec || 60;
      preCandlesRaw = chart.pre_candles || [];
      postCandlesRaw = chart.post_candles || [];
    } else {
      // --- Legacy path: sidecar curves + separate context candles ---
      realHigh = sc.real_high_curve || sc.real_high || data.real_high_curve || [];
      realLow = sc.real_low_curve || sc.real_low || data.real_low_curve || [];
      realOpen = sc.real_open_curve || data.real_open_curve || null;
      realClose = sc.real_close_curve || data.real_close_curve || null;
      hasOHLC = Array.isArray(realOpen) && Array.isArray(realClose)
        && realOpen.length > 0 && realClose.length > 0;
      intervalSec = sc.candle_interval_sec || sc.interval_sec || data.candle_interval_sec || 900;
      preCandlesRaw = data.pre_candles || [];
      postCandlesRaw = data.post_candles || [];
    }

    var k = Math.max(realHigh.length, realLow.length, hasOHLC ? realOpen.length : 0);

    if (k === 0) {
      showMessage(container, "No real candle curve recorded for this trade.", "text-yellow-400");
      return;
    }

    // Save original trade candle count before prepending/appending context
    var tradeK = k;

    var preN = preCandlesRaw.length;
    var postN = postCandlesRaw.length;

    // Build full OHLC arrays: [pre... | trade... | post...]
    if (hasOHLC && preN > 0 || postN > 0) {
      var preO = preCandlesRaw.map(function(c) { return c.o; });
      var preH = preCandlesRaw.map(function(c) { return c.h; });
      var preL = preCandlesRaw.map(function(c) { return c.l; });
      var preC = preCandlesRaw.map(function(c) { return c.c; });
      var postO = postCandlesRaw.map(function(c) { return c.o; });
      var postH = postCandlesRaw.map(function(c) { return c.h; });
      var postL = postCandlesRaw.map(function(c) { return c.l; });
      var postC = postCandlesRaw.map(function(c) { return c.c; });

      if (hasOHLC) {
        realOpen = preO.concat(realOpen, postO);
        realHigh = preH.concat(realHigh, postH);
        realLow = preL.concat(realLow, postL);
        realClose = preC.concat(realClose, postC);
      } else {
        realHigh = preH.concat(realHigh, postH);
        realLow = preL.concat(realLow, postL);
      }
      k = Math.max(realHigh.length, hasOHLC ? realOpen.length : 0);
    }

    var tp = sc.predicted_tp_price != null ? sc.predicted_tp_price
      : (sc.predicted_tp != null ? sc.predicted_tp : data.predicted_tp_price);
    var sl = sc.predicted_sl_price != null ? sc.predicted_sl_price
      : (sc.predicted_sl != null ? sc.predicted_sl : data.predicted_sl_price);
    var entry = sc.entry_price != null ? sc.entry_price
      : (sc.entry != null ? sc.entry : data.entry_price);
    var exitPrice = sc.exit_price != null ? sc.exit_price
      : (sc.exit != null ? sc.exit : data.exit_price);
    var tpPct = sc.predicted_tp_pct != null ? sc.predicted_tp_pct : data.predicted_tp_pct;
    var slPct = sc.predicted_sl_pct != null ? sc.predicted_sl_pct : data.predicted_sl_pct;

    // x-axis: pre-candles at negative offsets, entry=0, post-candles positive
    var xs = [];
    for (var i = 0; i < k; i++) xs.push((i - preN) * intervalSec);

    // Only OHLC series (invisible, for y-scale) or legacy high/low lines.
    var seriesData = [xs];
    var seriesCfg = [{}];

    var openIdx = -1, highIdx = -1, lowIdx = -1, closeIdx = -1;
    if (hasOHLC) {
      // Series must be visible (show:true) for uPlot to include them in y-scale,
      // but we draw candles ourselves so use transparent stroke + no points.
      var invisible = { stroke: "rgba(0,0,0,0)", width: 0, points: { show: false } };
      openIdx = seriesData.length;
      seriesData.push(realOpen);
      seriesCfg.push(Object.assign({ label: "O" }, invisible));
      highIdx = seriesData.length;
      seriesData.push(realHigh);
      seriesCfg.push(Object.assign({ label: "H" }, invisible));
      lowIdx = seriesData.length;
      seriesData.push(realLow);
      seriesCfg.push(Object.assign({ label: "L" }, invisible));
      closeIdx = seriesData.length;
      seriesData.push(realClose);
      seriesCfg.push(Object.assign({ label: "C" }, invisible));
    } else {
      seriesData.push(realHigh);
      seriesCfg.push({ label: "High", stroke: "#60a5fa", width: 1.5 });
      seriesData.push(realLow);
      seriesCfg.push({ label: "Low", stroke: "#a78bfa", width: 1.5 });
    }

    // --- Plugins (background → foreground) ---
    var plugins = [];

    if (hasOHLC) {
      var postStart = preN + (k - preN - postN);  // index where post-context candles begin
      plugins.push(candlestickPlugin(openIdx, highIdx, lowIdx, closeIdx, preN, postStart));
    }

    // TP line (green dashed) with label
    if (tp != null) {
      var tpLabel = "TP" + (tpPct != null ? " +" + Number(tpPct).toFixed(1) + "%" : "");
      plugins.push(hLine(tp, "#22c55e", tpLabel, [6, 3]));
    }
    // SL line (red dashed) with label
    if (sl != null) {
      var slLabel = "SL" + (slPct != null ? " -" + Number(slPct).toFixed(1) + "%" : "");
      plugins.push(hLine(sl, "#ef4444", slLabel, [6, 3]));
    }
    // Entry line (yellow, subtle dotted)
    if (entry != null) {
      plugins.push(hLine(entry, "rgba(251,191,36,0.4)", null, [2, 4]));
    }

    // Entry vertical line at first candle, badge at entry price level
    if (entry != null) {
      plugins.push(verticalLine(0, entry, "ENTRY $" + Number(entry).toFixed(4), "#fbbf24"));
    }
    // Exit vertical line at last trade candle (not post-context), badge at exit price level
    if (exitPrice != null) {
      var exitX = Math.max(0, tradeK - 1) * intervalSec;
      // When entry and exit overlap (1 candle), offset exit slightly right
      if (tradeK <= 1) exitX = 0.3 * intervalSec;
      plugins.push(verticalLine(exitX, exitPrice, "EXIT $" + Number(exitPrice).toFixed(4), "#f472b6"));
    }
    // Expiry deadline vertical line (gray, subtle) — x is relative to entry (x=0)
    var expiryK = sc.k_candles ?? data.k_candles;
    if (expiryK != null && expiryK > 0 && expiryK <= tradeK) {
      plugins.push(expiryLine(expiryK * intervalSec));
    }

    var rect = container.getBoundingClientRect();
    var opts = {
      width: Math.max(320, Math.floor(rect.width || 800)),
      height: 400,
      padding: [30, 70, 0, 0],
      scales: { x: { time: false } },
      axes: [
        {
          stroke: "#6b7280",
          grid: { stroke: "rgba(55,65,81,0.5)" },
          values: function (_u, vals) {
            var entryMs = data.timestamp ? new Date(data.timestamp).getTime() : null;
            return vals.map(function (v) {
              if (entryMs != null) {
                var d = new Date(entryMs + v * 1000);
                var hh = d.getUTCHours().toString().padStart(2, "0");
                var mm = d.getUTCMinutes().toString().padStart(2, "0");
                return hh + ":" + mm;
              }
              // Fallback: relative time
              var abs = Math.abs(v);
              var h = Math.floor(abs / 3600);
              var m = Math.round((abs % 3600) / 60);
              var s = h > 0 ? h + "h" + (m > 0 ? m + "m" : "") : m + "m";
              return v < 0 ? "-" + s : s;
            });
          },
        },
        {
          stroke: "#6b7280",
          grid: { stroke: "rgba(55,65,81,0.5)" },
          size: 60,
        },
      ],
      legend: { show: false },
      cursor: { show: true },
      series: seriesCfg,
      plugins: plugins,
    };

    clearChildren(container);
    currentPlot = new uPlot(opts, seriesData, container);

    // Summary line below chart using safe DOM methods
    var reason = data.exit_reason_v2 || data.exit_reason || "\u2014";
    var pnl = data.pnl_usd;
    var pnlStr = pnl != null ? (pnl >= 0 ? "+" : "") + Number(pnl).toFixed(2) : "\u2014";
    var pnlColor = pnl != null && pnl >= 0 ? "text-green-400" : "text-red-400";

    // Duration: actual hold vs expected expiry
    var holdMin = sc.hold_duration_minutes ?? data.hold_duration_minutes;
    var kCandles = sc.k_candles ?? data.k_candles;
    var candleIntervalSec = sc.candle_interval_sec ?? data.candle_interval_sec ?? intervalSec;
    var holdStr = "\u2014";
    if (holdMin != null) {
      var hh = Math.floor(holdMin / 60);
      var mm = Math.round(holdMin % 60);
      holdStr = hh > 0 ? hh + "h" + (mm > 0 ? mm + "m" : "") : mm + "m";
    }
    var expiryStr = "\u2014";
    if (kCandles != null && candleIntervalSec > 0) {
      var expiryMin = kCandles * candleIntervalSec / 60;
      var eh = Math.floor(expiryMin / 60);
      var em = Math.round(expiryMin % 60);
      expiryStr = eh > 0 ? eh + "h" + (em > 0 ? em + "m" : "") : em + "m";
    }
    var durationColor = "text-gray-400";
    if (holdMin != null && kCandles != null && candleIntervalSec > 0) {
      var expiryMinutes = kCandles * candleIntervalSec / 60;
      if (holdMin >= expiryMinutes * 0.95) durationColor = "text-yellow-400";  // near/at expiry
      if (holdMin < expiryMinutes * 0.3) durationColor = "text-blue-400";     // early exit
    }

    var caption = document.createElement("div");
    caption.className = "mt-3 flex flex-wrap gap-x-6 gap-y-1 text-xs text-gray-500";

    var items = [
      ["Entry ", "text-yellow-400", entry != null ? "$" + Number(entry).toFixed(4) : "\u2014"],
      ["Exit ", "text-pink-400", exitPrice != null ? "$" + Number(exitPrice).toFixed(4) : "\u2014"],
      ["PnL ", pnlColor, pnlStr],
      ["Reason ", "text-white", reason],
      ["Hold ", durationColor, holdStr + " / " + expiryStr],
      ["", "text-gray-500", (k - preN - postN) + " candles" + (preN > 0 ? " +" + preN + " pre" : "") + (postN > 0 ? " +" + postN + " post" : "")],
    ];

    items.forEach(function (item) {
      var wrap = document.createElement("span");
      if (item[0]) {
        wrap.appendChild(document.createTextNode(item[0]));
      }
      var val = document.createElement("span");
      val.className = item[1];
      val.textContent = item[2];
      wrap.appendChild(val);
      caption.appendChild(wrap);
    });

    container.appendChild(caption);
  }

  function init() {
    var container = document.getElementById("chart");
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
