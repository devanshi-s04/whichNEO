// Interactive zoom/pan for the sky-plane uncertainty plot, as a real chart
// zoom rather than a picture being stretched: the frame, gridlines and tick
// labels stay fixed on screen and legible at a constant size, while the
// *values* those gridlines represent change with the zoom level. Only the
// data layer (dots, nominal ring, field box) actually moves, via an SVG
// transform; the axis layer is a small string of SVG rebuilt from scratch on
// every interaction, mirroring uncertainty.py's own tick-picking logic
// (_ticks / _axis_svg) so the initial server render and every later redraw
// look identical. No library: this is the only interactive chart in the app.

(function () {
  // Exactly uncertainty.py's _ticks: same candidate list, same threshold
  // (half/step <= 4, i.e. span/step <= 8), so the tick set never jumps to a
  // different density the moment a user's first interaction triggers the
  // first JS redraw at an unchanged zoom level.
  var STEP_CANDIDATES = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000,
    10000, 20000, 50000, 100000, 200000, 500000, 1000000, 2000000, 5000000];

  function pickStep(span) {
    for (var i = 0; i < STEP_CANDIDATES.length; i++) {
      if (span / STEP_CANDIDATES[i] <= 8) return STEP_CANDIDATES[i];
    }
    return STEP_CANDIDATES[STEP_CANDIDATES.length - 1];
  }

  // Ticks covering [lo, hi], not assumed centred on zero -- panning can push
  // the visible range off-centre, unlike the server's initial +/-half view.
  // Excludes 0 itself: like _ticks, the crosshair at zero is drawn
  // separately (see redrawAxis) regardless of whether it lands "on step".
  function niceTicks(lo, hi) {
    if (!(hi > lo)) return [];
    var step = pickStep(hi - lo);
    var start = Math.ceil(lo / step) * step;
    var out = [];
    for (var v = start; v <= hi + step * 1e-6; v += step) {
      var t = Math.round(v / step) * step;   // snap off float drift
      if (t !== 0) out.push(t);
    }
    return out;
  }

  function enableUncertaintyZoom(svg) {
    var pad = parseFloat(svg.dataset.pad);
    var inner = parseFloat(svg.dataset.inner);
    var scale = parseFloat(svg.dataset.scale);
    // Two sibling groups share this class -- an unclipped one (field box,
    // label, nominal ring) and a clipped one (the dot cloud) -- and both
    // need the identical transform so they pan/zoom in lockstep.
    var contentGroups = svg.querySelectorAll(".uncContent");
    var axis = svg.querySelector("#uncAxis");
    if (!contentGroups.length || !axis || !isFinite(pad + inner + scale)) return;

    // Same mapping as uncertainty.py's px()/py(): both axes share one
    // formula (RA offset increases to the left, same as Dec upward).
    function basePos(v) {
      return pad + inner / 2 - v * scale;
    }

    var k = 1, tx = 0, ty = 0;
    var MIN_K = 0.03, MAX_K = 40;

    function valueAtScreen(s, t) {
      return (pad + inner / 2 - (s - t) / k) / scale;
    }

    function redrawAxis() {
      var vAtPad = valueAtScreen(pad, tx);
      var vAtEdge = valueAtScreen(pad + inner, tx);
      var loX = Math.min(vAtPad, vAtEdge), hiX = Math.max(vAtPad, vAtEdge);
      var vAtPadY = valueAtScreen(pad, ty);
      var vAtEdgeY = valueAtScreen(pad + inner, ty);
      var loY = Math.min(vAtPadY, vAtEdgeY), hiY = Math.max(vAtPadY, vAtEdgeY);

      var out = [];
      niceTicks(loX, hiX).forEach(function (t) {
        var x = tx + k * basePos(t);
        if (x < pad || x > pad + inner) return;
        out.push('<line x1="' + x.toFixed(1) + '" y1="' + pad + '" x2="' +
          x.toFixed(1) + '" y2="' + (pad + inner) + '" stroke="#171d28"/>');
        out.push('<text x="' + x.toFixed(1) + '" y="' + (pad + inner + 13) +
          '" fill="#556074" font-size="9" text-anchor="middle" ' +
          'font-family="monospace">' + t + "</text>");
      });
      niceTicks(loY, hiY).forEach(function (t) {
        var y = ty + k * basePos(t);
        if (y < pad || y > pad + inner) return;
        out.push('<line x1="' + pad + '" y1="' + y.toFixed(1) + '" x2="' +
          (pad + inner) + '" y2="' + y.toFixed(1) + '" stroke="#171d28"/>');
        out.push('<text x="' + (pad - 5) + '" y="' + (y + 3).toFixed(1) +
          '" fill="#556074" font-size="9" text-anchor="end" ' +
          'font-family="monospace">' + t + "</text>");
      });

      // Zero crosshair, drawn unconditionally when in range -- like
      // uncertainty.py's _axis_svg, independent of the tick set above.
      var cx = tx + k * basePos(0), cy = ty + k * basePos(0);
      if (cx >= pad && cx <= pad + inner) {
        out.push('<line x1="' + cx.toFixed(1) + '" y1="' + pad + '" x2="' +
          cx.toFixed(1) + '" y2="' + (pad + inner) + '" stroke="#2a3a52"/>');
      }
      if (cy >= pad && cy <= pad + inner) {
        out.push('<line x1="' + pad + '" y1="' + cy.toFixed(1) + '" x2="' +
          (pad + inner) + '" y2="' + cy.toFixed(1) + '" stroke="#2a3a52"/>');
      }
      axis.innerHTML = out.join("");
    }

    function apply() {
      var t = "translate(" + tx + "," + ty + ") scale(" + k + ")";
      contentGroups.forEach(function (g) { g.setAttribute("transform", t); });
      redrawAxis();
    }

    function toScreenPoint(evt) {
      var ctm = svg.getScreenCTM();
      if (!ctm) return null;
      var pt = svg.createSVGPoint();
      pt.x = evt.clientX;
      pt.y = evt.clientY;
      return pt.matrixTransform(ctm.inverse());
    }

    svg.addEventListener("wheel", function (evt) {
      evt.preventDefault();
      var p = toScreenPoint(evt);
      if (!p) return;
      var factor = evt.deltaY < 0 ? 1 / 0.88 : 0.88;
      var newK = Math.min(MAX_K, Math.max(MIN_K, k * factor));
      // Keep the point under the cursor fixed on screen as k changes.
      tx = p.x - (p.x - tx) * (newK / k);
      ty = p.y - (p.y - ty) * (newK / k);
      k = newK;
      apply();
    }, {passive: false});

    var dragging = false, lastX = 0, lastY = 0;
    svg.addEventListener("mousedown", function (evt) {
      dragging = true;
      lastX = evt.clientX;
      lastY = evt.clientY;
      svg.style.cursor = "grabbing";
    });
    window.addEventListener("mousemove", function (evt) {
      if (!dragging) return;
      var rect = svg.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      // Screen-space deltas, rescaled from CSS pixels to the SVG's own user
      // units (the two differ whenever the element is drawn narrower than
      // its viewBox, e.g. a responsive width:100% below max-width).
      var svgW = pad * 2 + inner;
      var uScale = svgW / rect.width;
      tx += (evt.clientX - lastX) * uScale;
      ty += (evt.clientY - lastY) * uScale;
      lastX = evt.clientX;
      lastY = evt.clientY;
      apply();
    });
    window.addEventListener("mouseup", function () {
      if (dragging) {
        dragging = false;
        svg.style.cursor = "grab";
      }
    });

    svg.addEventListener("dblclick", function () {
      k = 1; tx = 0; ty = 0;
      apply();
    });
  }

  document.querySelectorAll("svg.zoomable[data-pad]").forEach(enableUncertaintyZoom);
})();
