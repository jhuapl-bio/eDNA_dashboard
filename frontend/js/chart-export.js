// ##############################################################################################
// # Copyright 2026 The Johns Hopkins University Applied Physics Laboratory LLC
// # All rights reserved.
// # Permission is hereby granted, free of charge, to any person obtaining a copy of this
// # software and associated documentation files (the "Software"), to deal in the Software
// # without restriction, including without limitation the rights to use, copy, modify,
// # merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
// # permit persons to whom the Software is furnished to do so.
// #
// # THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
// # INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR
// # PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
// # LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
// # TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE
// # OR OTHER DEALINGS IN THE SOFTWARE.
// #
/* ─────────────────────────────────────────────────────────────────────────────
   chart-export.js — export controls for the MBON dashboard and the standalone
   station-map report.

   Adds a small download button to every chart and every data table:
     • charts — top-right of the plot (in the card's header row when there is
       one, otherwise on its own reserved strip above the plot, so the button
       never covers the drawing).
     • tables — same placement, above the table.

   Clicking opens an overlay:
     • charts — output width/height, legend/title toggles, background,
       resolution, live preview, and SVG / PNG / JPEG / PDF download.
     • tables — a row/column summary and CSV / XLSX / HTML download, honouring
       hidden columns and the current sort order.

   Requires nothing at load time. jsPDF (window.jspdf) is used for chart PDFs
   and SheetJS (window.XLSX) for .xlsx; both are already loaded by the pages
   that include this file, and each degrades with a clear message if absent.
   ──────────────────────────────────────────────────────────────────────────── */
(function () {
  'use strict';

  /* ── Tunables ──────────────────────────────────────────────────────────── */
  const PAD          = 24;   // outer padding around the whole composition
  const TITLE_SIZE   = 16;
  const TITLE_GAP    = 16;
  const LEGEND_GAP   = 18;   // space between chart and legend block
  const LEGEND_SIZE  = 12;   // legend font size
  const LEGEND_ROW_H = 20;
  const SWATCH       = 12;
  const SWATCH_GAP   = 6;
  const ITEM_GAP     = 18;
  const MIN_CHART_H  = 110;  // below this a chart is a sparkline, not a plot
  const MIN_TBL_ROWS = 2;    // don't clutter tiny key/value tables
  const FONT = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';

  // SVG presentation properties worth carrying over from the stylesheet.
  const STYLE_PROPS = [
    'fill', 'fill-opacity', 'fill-rule',
    'stroke', 'stroke-width', 'stroke-opacity', 'stroke-dasharray',
    'stroke-linecap', 'stroke-linejoin', 'stroke-dashoffset',
    'opacity', 'font-family', 'font-size', 'font-weight', 'font-style',
    'text-anchor', 'dominant-baseline', 'letter-spacing', 'paint-order',
    'shape-rendering', 'text-rendering', 'mix-blend-mode', 'visibility',
  ];

  /* Selector config. Defaults suit the Vue dashboard; the standalone station
     map report overrides these via window.MBON_CHART_EXPORT_CONFIG before this
     script runs, because its charts and tables use different class names.    */
  const CFG = Object.assign({
    // Elements holding a chart <svg> as a direct child.
    containers: '.d3-wrap, .d3-exportable',
    // Elements holding a data <table> to offer for export.
    tableContainers: '.table-wrap, .map-popup-table-wrap',
    // Never attach anything inside these.
    excludes: '.import-panel, #import-panel, .mbon-pop, .mbon-backdrop, .chart-export-overlay',
    // Legend block: looked for among the container's next few siblings.
    legendSelector: '.legend',
    // Entries inside a legend block (falls back to direct children).
    legendItemSelector: '.legend-item',
    // The colour chip inside a legend entry.
    swatchSelector: '.legend-swatch, i, .swatch',
    // Where to look for the chart's heading, and how far up to look for it.
    cardSelector: '.card',
    titleSelector: '.chart-header h3, .card-title, h3',
    // A flex toolbar row above the chart/table; the button is placed here when
    // present, so it sits beside the other controls instead of over the plot.
    headerSelector: '.chart-header',
  }, window.MBON_CHART_EXPORT_CONFIG || {});

  /* ── Small helpers ─────────────────────────────────────────────────────── */
  function slug(s) {
    return String(s || 'export').toLowerCase()
      .replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 60) || 'export';
  }

  function todayStamp() {
    const d = new Date();
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}`;
  }

  function svgEl(name, attrs) {
    const el = document.createElementNS('http://www.w3.org/2000/svg', name);
    for (const k in (attrs || {})) {
      if (attrs[k] != null) el.setAttribute(k, attrs[k]);
    }
    return el;
  }

  function escHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }

  // Rough text width estimate — good enough to lay legend items out without
  // forcing a synchronous measure of every string.
  function textWidth(str, size) {
    let w = 0;
    for (const ch of String(str)) {
      w += /[iIl.,:;'|! ]/.test(ch) ? 0.30 : /[mwMW@]/.test(ch) ? 0.92 : 0.55;
    }
    return w * size;
  }

  function download(blobOrUrl, filename) {
    const url = typeof blobOrUrl === 'string' ? blobOrUrl : URL.createObjectURL(blobOrUrl);
    const a = document.createElement('a');
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click(); a.remove();
    if (typeof blobOrUrl !== 'string') setTimeout(() => URL.revokeObjectURL(url), 4000);
  }

  /* ── Locating a chart's title and legend ───────────────────────────────── */

  // The legend lives just after the chart wrapper in the markup, e.g.
  //   <div class="d3-wrap"><svg/></div><div class="legend">…</div>
  function findLegend(wrap) {
    let n = wrap.nextElementSibling;
    let hops = 0;
    while (n && hops++ < 3) {
      if (n.matches && n.matches(CFG.legendSelector)) {
        return readLegend(n).length ? n : null;
      }
      // Skip small utility nodes, but stop once we reach the next chart/card.
      if (n.matches && n.matches(CFG.containers + ',' + CFG.cardSelector)) break;
      n = n.nextElementSibling;
    }
    return null;
  }

  function findCard(host) {
    return (host.closest && host.closest(CFG.cardSelector)) || host.parentElement;
  }

  function findTitle(host) {
    const card = findCard(host);
    const h = card && card.querySelector(CFG.titleSelector);
    const t = h ? h.textContent.replace(/\s+/g, ' ').trim() : '';
    // Untitled panels still deserve a meaningful download name.
    return t || (document.title || '').replace(/\s+/g, ' ').trim();
  }

  // Reads the visible legend entries: swatch colour + label. Pagination
  // controls and hidden (toggled-off) series are left out.
  function readLegend(legendEl) {
    if (!legendEl) return [];
    let items = Array.from(legendEl.querySelectorAll(CFG.legendItemSelector));
    // Some legends have no per-item class — treat direct children as entries.
    if (!items.length) items = Array.from(legendEl.children);
    const out = [];
    items.forEach((item) => {
      const cls = typeof item.className === 'string' ? item.className : '';
      if (/--hidden|legend-pagination|legend-nav|mbon-legend-nav/.test(cls)) return;
      if (item.tagName === 'BUTTON' || item.querySelector('button')) return;
      const sw = item.querySelector(CFG.swatchSelector);
      const cs = sw ? getComputedStyle(sw) : null;
      let color = cs ? cs.backgroundColor : '';
      if (!color || color === 'rgba(0, 0, 0, 0)') color = cs ? cs.borderTopColor : '';
      const label = item.textContent.replace(/\s+/g, ' ').trim();
      if (label) out.push({ label, color: color || '#adb5bd' });
    });
    return out;
  }

  /* ── Reading a table out of the DOM ────────────────────────────────────── */

  // Returns { headers, rows } for the table as it currently appears: current
  // sort order, CSS-hidden columns dropped, placeholder rows skipped.
  function readTable(table) {
    const headRow = table.querySelector('thead tr:last-child');
    const headCells = headRow ? Array.from(headRow.children) : [];
    const hidden = headCells.map((th) => getComputedStyle(th).display === 'none');
    const clean = (n) => (n.textContent || '').replace(/[↕↓↑]/g, '').replace(/\s+/g, ' ').trim();

    const headers = headCells.filter((_, i) => !hidden[i]).map(clean);
    const rows = [];
    table.querySelectorAll('tbody tr').forEach((tr) => {
      if (tr.classList.contains('tt-hidden') || getComputedStyle(tr).display === 'none') {
        // A row hidden by search/pagination is still part of the data set, so
        // it is exported — only skip it if the app itself removed it.
      }
      const tds = Array.from(tr.children);
      // "No data" placeholder rows span every column and carry no values.
      if (tds.length === 1 && tds[0].hasAttribute('colspan')) return;
      rows.push(tds.filter((_, i) => !hidden[i]).map(clean));
    });
    return { headers, rows };
  }

  function tableToAOA(table) {
    const m = readTable(table);
    return [m.headers].concat(m.rows);
  }

  function exportTableCSV(table, name) {
    const csv = tableToAOA(table).map((r) => r.map((c) => {
      c = c == null ? '' : String(c);
      return /[",\n\r]/.test(c) ? '"' + c.replace(/"/g, '""') + '"' : c;
    }).join(',')).join('\r\n');
    // BOM so Excel opens UTF-8 correctly.
    download(new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8;' }), name + '.csv');
  }

  function exportTableXLSX(table, name) {
    if (typeof XLSX === 'undefined') {
      throw new Error('XLSX library did not load — use CSV instead.');
    }
    const data = tableToAOA(table);
    // Turn numeric-looking cells into real numbers so Excel can sum them.
    const typed = data.map((r, ri) => ri === 0 ? r : r.map((c) => {
      const s = String(c == null ? '' : c).trim();
      if (!s || !/\d/.test(s)) return c;
      const bare = s.replace(/[,\s]/g, '').replace(/^[$€£]/, '').replace(/%$/, '');
      return /^-?\d+(\.\d+)?([eE][-+]?\d+)?$/.test(bare) ? parseFloat(bare) : c;
    }));
    const ws = XLSX.utils.aoa_to_sheet(typed);
    // Reasonable column widths based on content length.
    ws['!cols'] = (data[0] || []).map((_, ci) => ({
      wch: Math.min(48, Math.max(10, ...data.map((r) => String(r[ci] == null ? '' : r[ci]).length + 2))),
    }));
    const wb = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb, ws, 'Data');
    XLSX.writeFile(wb, name + '.xlsx');
  }

  function exportTableHTML(table, name) {
    const m = readTable(table);
    let h = '<table border="1" cellspacing="0" cellpadding="6" ' +
      'style="border-collapse:collapse;font-family:system-ui,sans-serif;font-size:13px">' +
      '<thead><tr style="background:#e0f7fa">' +
      m.headers.map((c) => '<th align="left">' + escHtml(c) + '</th>').join('') +
      '</tr></thead><tbody>' +
      m.rows.map((r) => '<tr>' + r.map((c) => '<td>' + escHtml(c) + '</td>').join('') + '</tr>').join('') +
      '</tbody></table>';
    const doc = '<!doctype html><meta charset="utf-8"><title>' + escHtml(name) + '</title>' + h;
    download(new Blob([doc], { type: 'text/html;charset=utf-8;' }), name + '.html');
  }

  /* ── Style inlining ────────────────────────────────────────────────────── */

  // Walks source and clone in lockstep, copying computed presentation styles
  // onto the clone. Without this the export loses everything the stylesheet
  // provided (axis colours, font sizes, grid strokes…).
  function inlineStyles(srcRoot, cloneRoot) {
    const srcNodes = [srcRoot, ...srcRoot.querySelectorAll('*')];
    const clNodes = [cloneRoot, ...cloneRoot.querySelectorAll('*')];
    const n = Math.min(srcNodes.length, clNodes.length);
    for (let i = 0; i < n; i++) {
      const s = srcNodes[i], c = clNodes[i];
      if (!(c instanceof SVGElement)) continue;
      const cs = getComputedStyle(s);
      let decl = '';
      for (const prop of STYLE_PROPS) {
        const v = cs.getPropertyValue(prop);
        if (!v || v === 'auto' || v === 'normal') continue;
        // `fill:none` / `stroke:none` are meaningful and must be kept.
        decl += `${prop}:${v};`;
      }
      if (decl) c.setAttribute('style', decl);
      c.removeAttribute('class');
    }
  }

  /* ── Composition ───────────────────────────────────────────────────────── */

  // Natural pixel size of the live chart SVG.
  function sourceSize(svg) {
    const w = parseFloat(svg.getAttribute('width')) || svg.getBoundingClientRect().width || 700;
    const h = parseFloat(svg.getAttribute('height')) || svg.getBoundingClientRect().height || 400;
    return { w: Math.max(1, w), h: Math.max(1, h) };
  }

  // Lays legend entries out into wrapped rows within `maxW`.
  function layoutLegend(items, maxW) {
    const rows = [];
    let row = [], x = 0;
    for (const it of items) {
      const w = SWATCH + SWATCH_GAP + textWidth(it.label, LEGEND_SIZE) + ITEM_GAP;
      if (row.length && x + w > maxW) { rows.push(row); row = []; x = 0; }
      row.push({ ...it, x, w });
      x += w;
    }
    if (row.length) rows.push(row);
    return { rows, height: rows.length ? rows.length * LEGEND_ROW_H : 0 };
  }

  // Composes the export SVG. Returns { svg, width, height }.
  function compose(chart, opts) {
    const { width, height, includeLegend, includeTitle, background } = opts;
    const src = chart.svg;
    const size = sourceSize(src);

    const innerW = Math.max(40, width - PAD * 2);
    const titleH = includeTitle && chart.title ? TITLE_SIZE + TITLE_GAP : 0;
    const items = includeLegend ? readLegend(chart.legendEl) : [];
    const legend = layoutLegend(items, innerW);
    const legendH = legend.height ? legend.height + LEGEND_GAP : 0;

    const availW = innerW;
    const availH = Math.max(40, height - PAD * 2 - titleH - legendH);
    const scale = Math.min(availW / size.w, availH / size.h);
    const chartW = size.w * scale, chartH = size.h * scale;
    const chartX = PAD + (availW - chartW) / 2;
    const chartY = PAD + titleH + (availH - chartH) / 2;

    const root = svgEl('svg', {
      xmlns: 'http://www.w3.org/2000/svg',
      'xmlns:xlink': 'http://www.w3.org/1999/xlink',
      width, height, viewBox: `0 0 ${width} ${height}`,
      'font-family': FONT,
    });

    if (background && background !== 'transparent') {
      root.appendChild(svgEl('rect', { x: 0, y: 0, width, height, fill: background }));
    }

    if (titleH) {
      const t = svgEl('text', {
        x: PAD, y: PAD + TITLE_SIZE, 'font-size': TITLE_SIZE,
        'font-weight': '700', fill: '#1b2631', 'font-family': FONT,
      });
      t.textContent = chart.title;
      root.appendChild(t);
    }

    // Chart: clone, inline styles, drop into a scaled group.
    const clone = src.cloneNode(true);
    inlineStyles(src, clone);
    clone.removeAttribute('style');
    clone.setAttribute('width', size.w);
    clone.setAttribute('height', size.h);
    clone.setAttribute('viewBox', `0 0 ${size.w} ${size.h}`);
    clone.setAttribute('overflow', 'visible');
    const g = svgEl('g', { transform: `translate(${chartX} ${chartY}) scale(${scale})` });
    g.appendChild(clone);
    root.appendChild(g);

    // Legend, redrawn as native SVG so it survives rasterisation.
    if (legend.rows.length) {
      const ly = PAD + titleH + availH + LEGEND_GAP;
      const lg = svgEl('g', { transform: `translate(${PAD} ${ly})` });
      legend.rows.forEach((row, ri) => {
        const y = ri * LEGEND_ROW_H;
        row.forEach((it) => {
          lg.appendChild(svgEl('rect', {
            x: it.x, y: y + (LEGEND_ROW_H - SWATCH) / 2 - 1,
            width: SWATCH, height: SWATCH, rx: 3, fill: it.color,
          }));
          const t = svgEl('text', {
            x: it.x + SWATCH + SWATCH_GAP, y: y + LEGEND_ROW_H / 2 + 3,
            'font-size': LEGEND_SIZE, fill: '#495057', 'font-family': FONT,
          });
          t.textContent = it.label;
          lg.appendChild(t);
        });
      });
      root.appendChild(lg);
    }

    return { svg: root, width, height };
  }

  // Natural composed height for a given width — used to seed the dialog and to
  // keep the aspect ratio locked while the user changes the width.
  function naturalHeight(chart, width, includeLegend, includeTitle) {
    const size = sourceSize(chart.svg);
    const innerW = Math.max(40, width - PAD * 2);
    const titleH = includeTitle && chart.title ? TITLE_SIZE + TITLE_GAP : 0;
    const items = includeLegend ? readLegend(chart.legendEl) : [];
    const legend = layoutLegend(items, innerW);
    const legendH = legend.height ? legend.height + LEGEND_GAP : 0;
    return Math.round(PAD * 2 + titleH + legendH + innerW * (size.h / size.w));
  }

  /* ── Serialisation / rasterisation ─────────────────────────────────────── */

  function serialize(svg) {
    const s = new XMLSerializer().serializeToString(svg);
    return s.startsWith('<?xml') ? s : '<?xml version="1.0" encoding="UTF-8"?>\n' + s;
  }

  function svgDataUrl(svg) {
    const s = new XMLSerializer().serializeToString(svg);
    return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(s);
  }

  // Renders the composed SVG onto a canvas at `scale`× the nominal size.
  function rasterize(svg, width, height, scale, background) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => {
        const canvas = document.createElement('canvas');
        canvas.width = Math.max(1, Math.round(width * scale));
        canvas.height = Math.max(1, Math.round(height * scale));
        const ctx = canvas.getContext('2d');
        if (background && background !== 'transparent') {
          ctx.fillStyle = background;
          ctx.fillRect(0, 0, canvas.width, canvas.height);
        }
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        resolve(canvas);
      };
      img.onerror = () => reject(new Error('Could not render the chart image.'));
      img.src = svgDataUrl(svg);
    });
  }

  async function exportChart(chart, opts) {
    const { format, scale } = opts;
    const bg = (format === 'jpeg' || format === 'pdf') && opts.background === 'transparent'
      ? '#ffffff' : opts.background;
    const { svg, width, height } = compose(chart, { ...opts, background: bg });
    const name = `${slug(chart.title || 'chart')}-${todayStamp()}`;

    if (format === 'svg') {
      download(new Blob([serialize(svg)], { type: 'image/svg+xml;charset=utf-8' }), `${name}.svg`);
      return;
    }

    const canvas = await rasterize(svg, width, height, scale, bg);

    if (format === 'png' || format === 'jpeg') {
      const mime = format === 'png' ? 'image/png' : 'image/jpeg';
      const dataUrl = canvas.toDataURL(mime, format === 'jpeg' ? 0.95 : undefined);
      download(dataUrl, `${name}.${format === 'jpeg' ? 'jpg' : 'png'}`);
      return;
    }

    if (format === 'pdf') {
      const jsPDFCtor = (window.jspdf && window.jspdf.jsPDF) || window.jsPDF;
      if (!jsPDFCtor) throw new Error('PDF export needs jsPDF, which did not load.');
      const pdf = new jsPDFCtor({
        orientation: width >= height ? 'landscape' : 'portrait',
        unit: 'pt',
        format: [width, height],
      });
      pdf.addImage(canvas.toDataURL('image/png'), 'PNG', 0, 0, width, height, undefined, 'FAST');
      pdf.save(`${name}.pdf`);
    }
  }

  function exportTable(target, format) {
    const name = `${slug(target.title || 'table')}-${todayStamp()}`;
    if (format === 'csv') return exportTableCSV(target.table, name);
    if (format === 'xlsx') return exportTableXLSX(target.table, name);
    if (format === 'html') return exportTableHTML(target.table, name);
  }

  /* ── Overlay dialog ────────────────────────────────────────────────────── */

  let dlg = null;   // the single reusable dialog element
  let current = null;

  function buildDialog() {
    const el = document.createElement('div');
    el.className = 'chart-export-overlay';
    el.innerHTML = `
      <div class="chart-export-modal" role="dialog" aria-modal="true" aria-label="Export">
        <div class="chart-export-head">
          <h3 data-role="heading">Export chart</h3>
          <button type="button" class="chart-export-close" title="Close" aria-label="Close">×</button>
        </div>
        <p class="chart-export-sub" data-role="subtitle"></p>

        <div class="chart-export-body" data-role="chartBody">
          <div class="chart-export-controls">
            <div class="chart-export-row">
              <label>Width <input type="number" min="120" max="8000" step="10" data-role="w"><span>px</span></label>
              <label>Height <input type="number" min="120" max="8000" step="10" data-role="h"><span>px</span></label>
            </div>
            <label class="chart-export-check">
              <input type="checkbox" data-role="lock" checked> Lock aspect ratio
            </label>
            <div class="chart-export-row chart-export-presets" data-role="presets">
              <span class="chart-export-label">Size</span>
              <button type="button" data-w="800">800</button>
              <button type="button" data-w="1200">1200</button>
              <button type="button" data-w="1600">1600</button>
              <button type="button" data-w="2400">2400</button>
              <button type="button" data-role="fit">Fit to screen</button>
            </div>

            <div class="chart-export-row">
              <span class="chart-export-label">Include</span>
              <label class="chart-export-check"><input type="checkbox" data-role="legend" checked> Legend</label>
              <label class="chart-export-check"><input type="checkbox" data-role="title" checked> Title</label>
            </div>

            <div class="chart-export-row">
              <span class="chart-export-label">Background</span>
              <label class="chart-export-check"><input type="radio" name="ce-bg" value="#ffffff" checked> White</label>
              <label class="chart-export-check"><input type="radio" name="ce-bg" value="transparent"> Transparent</label>
            </div>

            <div class="chart-export-row">
              <span class="chart-export-label">Resolution</span>
              <label class="chart-export-check"><input type="radio" name="ce-scale" value="1"> 1×</label>
              <label class="chart-export-check"><input type="radio" name="ce-scale" value="2" checked> 2×</label>
              <label class="chart-export-check"><input type="radio" name="ce-scale" value="3"> 3×</label>
              <span class="chart-export-hint" data-role="pxout"></span>
            </div>
          </div>

          <div class="chart-export-preview">
            <span class="chart-export-label">Preview</span>
            <div class="chart-export-preview-box" data-role="preview"></div>
          </div>
        </div>

        <div class="chart-export-tablebody" data-role="tableBody">
          <p class="chart-export-hint" data-role="tableSummary"></p>
          <div class="chart-export-tablepreview" data-role="tablePreview"></div>
          <p class="chart-export-hint">Exports what you see: current sort order, hidden columns left out.
             CSV and XLSX open in Excel, Numbers, R or pandas.</p>
        </div>

        <div class="chart-export-foot">
          <span class="chart-export-status" data-role="status"></span>
          <div class="chart-export-actions" data-role="chartActions">
            <button type="button" class="ce-btn" data-fmt="svg">SVG</button>
            <button type="button" class="ce-btn" data-fmt="png">PNG</button>
            <button type="button" class="ce-btn" data-fmt="jpeg">JPEG</button>
            <button type="button" class="ce-btn ce-btn-primary" data-fmt="pdf">PDF</button>
          </div>
          <div class="chart-export-actions" data-role="tableActions">
            <button type="button" class="ce-btn" data-tfmt="html">HTML</button>
            <button type="button" class="ce-btn" data-tfmt="csv">CSV</button>
            <button type="button" class="ce-btn ce-btn-primary" data-tfmt="xlsx">XLSX</button>
          </div>
        </div>
      </div>`;
    document.body.appendChild(el);

    const q = (r) => el.querySelector(`[data-role="${r}"]`);
    const wIn = q('w'), hIn = q('h');

    const opts = () => ({
      width: Math.max(120, Math.round(+wIn.value || 1200)),
      height: Math.max(120, Math.round(+hIn.value || 800)),
      includeLegend: q('legend').checked,
      includeTitle: q('title').checked,
      background: el.querySelector('input[name="ce-bg"]:checked').value,
      scale: +el.querySelector('input[name="ce-scale"]:checked').value,
    });

    function syncHeight() {
      if (!current || current.kind !== 'chart' || !q('lock').checked) return;
      hIn.value = naturalHeight(current, Math.max(120, +wIn.value || 1200),
        q('legend').checked, q('title').checked);
    }

    function refresh() {
      if (!current || current.kind !== 'chart') return;
      const o = opts();
      q('pxout').textContent =
        `output ${Math.round(o.width * o.scale)} × ${Math.round(o.height * o.scale)} px`;
      try {
        const { svg } = compose(current, o);
        svg.setAttribute('width', '100%');
        svg.setAttribute('height', '100%');
        svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
        const box = q('preview');
        box.innerHTML = '';
        box.style.background = o.background === 'transparent' ? '' : o.background;
        box.classList.toggle('is-transparent', o.background === 'transparent');
        box.appendChild(svg);
      } catch (err) {
        q('preview').textContent = 'Preview unavailable.';
      }
    }

    // Re-composing a dense chart is not free, so coalesce rapid input.
    let refreshTimer = null;
    function refreshSoon() {
      clearTimeout(refreshTimer);
      refreshTimer = setTimeout(refresh, 130);
    }

    wIn.addEventListener('input', () => { syncHeight(); refreshSoon(); });
    hIn.addEventListener('input', refreshSoon);
    el.querySelectorAll('input[type="checkbox"], input[type="radio"]').forEach((i) => {
      i.addEventListener('change', () => { syncHeight(); refreshSoon(); });
    });
    q('presets').addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (!b) return;
      if (b.dataset.role === 'fit') {
        wIn.value = Math.min(2400, Math.max(600, Math.round(window.innerWidth * 0.9)));
      } else if (b.dataset.w) {
        wIn.value = b.dataset.w;
      }
      syncHeight(); refreshSoon();
    });

    // Export buttons (charts and tables share the status line / busy state).
    async function run(label, fn) {
      const status = q('status');
      const buttons = el.querySelectorAll('button[data-fmt], button[data-tfmt]');
      buttons.forEach((x) => { x.disabled = true; });
      status.textContent = `Preparing ${label}…`;
      try {
        await fn();
        status.textContent = 'Downloaded.';
        setTimeout(() => { if (status.textContent === 'Downloaded.') status.textContent = ''; }, 2500);
      } catch (err) {
        status.textContent = (err && err.message) || 'Export failed.';
      } finally {
        buttons.forEach((x) => { x.disabled = false; });
      }
    }

    q('chartActions').addEventListener('click', (e) => {
      const b = e.target.closest('button[data-fmt]');
      if (!b || !current) return;
      run(b.dataset.fmt.toUpperCase(), () => exportChart(current, { ...opts(), format: b.dataset.fmt }));
    });
    q('tableActions').addEventListener('click', (e) => {
      const b = e.target.closest('button[data-tfmt]');
      if (!b || !current) return;
      run(b.dataset.tfmt.toUpperCase(), () => exportTable(current, b.dataset.tfmt));
    });

    // Dismissal
    el.querySelector('.chart-export-close').addEventListener('click', closeDialog);
    el.addEventListener('mousedown', (e) => { if (e.target === el) closeDialog(); });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && el.classList.contains('is-open')) closeDialog();
    });

    el._api = { q, wIn, hIn, refresh };
    return el;
  }

  function openDialog(target) {
    if (!dlg) dlg = buildDialog();
    current = target;
    const { q, wIn, hIn, refresh } = dlg._api;
    const isChart = target.kind === 'chart';

    q('heading').textContent = isChart ? 'Export chart' : 'Export table';
    q('subtitle').textContent = target.title || (isChart ? 'Untitled chart' : 'Untitled table');
    q('chartBody').hidden = !isChart;
    q('tableBody').hidden = isChart;
    q('chartActions').hidden = !isChart;
    q('tableActions').hidden = isChart;
    q('status').textContent = '';

    if (isChart) {
      const hasLegend = !!(target.legendEl && readLegend(target.legendEl).length);
      q('legend').checked = hasLegend;
      q('legend').disabled = !hasLegend;
      q('legend').closest('label').classList.toggle('is-disabled', !hasLegend);
      q('title').checked = !!target.title;
      q('title').disabled = !target.title;

      const natural = Math.min(2000, Math.max(700, Math.round(sourceSize(target.svg).w * 1.6)));
      wIn.value = natural;
      hIn.value = naturalHeight(target, natural, q('legend').checked, q('title').checked);
      dlg.classList.add('is-open');
      refresh();
      return;
    }

    // Table mode: summary + a short preview of the first rows.
    const m = readTable(target.table);
    q('tableSummary').textContent =
      `${m.rows.length.toLocaleString()} row${m.rows.length === 1 ? '' : 's'} × ` +
      `${m.headers.length} column${m.headers.length === 1 ? '' : 's'}`;
    const head = '<tr>' + m.headers.map((h) => '<th>' + escHtml(h) + '</th>').join('') + '</tr>';
    const body = m.rows.slice(0, 6).map((r) =>
      '<tr>' + r.map((c) => '<td>' + escHtml(c) + '</td>').join('') + '</tr>').join('');
    const more = m.rows.length > 6
      ? `<tr><td colspan="${m.headers.length}">… ${(m.rows.length - 6).toLocaleString()} more rows</td></tr>`
      : '';
    q('tablePreview').innerHTML =
      '<table><thead>' + head + '</thead><tbody>' + body + more + '</tbody></table>';
    // XLSX needs SheetJS; say so up front rather than after a click.
    const xbtn = dlg.querySelector('button[data-tfmt="xlsx"]');
    xbtn.disabled = typeof XLSX === 'undefined';
    xbtn.title = xbtn.disabled ? 'SheetJS (XLSX) did not load on this page' : '';

    dlg.classList.add('is-open');
  }

  function closeDialog() {
    if (dlg) dlg.classList.remove('is-open');
    current = null;
  }

  /* ── Button injection ──────────────────────────────────────────────────── */

  const ICON =
    '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M12 3v12"/><path d="M7 11l5 5 5-5"/><path d="M4 20h16"/></svg>';

  function makeButton(label, onClick) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'chart-export-btn';
    btn.title = label;
    btn.setAttribute('aria-label', label);
    btn.innerHTML = ICON + '<span class="chart-export-btn-txt">Export</span>';
    btn.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); onClick(); });
    return btn;
  }

  // A host keeps a direct reference to its own button, so a Vue re-render that
  // swaps the element out simply means "not attached yet" on the next scan.
  function alreadyAttached(host) {
    return !!(host.__ceBtn && host.__ceBtn.isConnected && host.isConnected);
  }

  // Places the button either in the card's existing toolbar row (preferred —
  // it sits beside the other controls and covers nothing) or on its own
  // reserved strip directly above the chart/table. Never overlays the content.
  function placeButton(host, btn) {
    host.__ceBtn = btn;
    btn.__ceHost = host;

    const card = findCard(host);
    const header = card && CFG.headerSelector ? card.querySelector(CFG.headerSelector) : null;
    if (header) {
      // Drop buttons left behind by charts that have since been re-rendered.
      header.querySelectorAll(':scope > .chart-export-btn').forEach((old) => {
        if (!old.__ceHost || !old.__ceHost.isConnected) old.remove();
      });
      const headerFree = !header.querySelector(':scope > .chart-export-btn');
      const headerIsAbove = !!(header.compareDocumentPosition(host) & Node.DOCUMENT_POSITION_FOLLOWING);
      // One header, one button: a card with two charts gives the second its
      // own strip rather than two ambiguous buttons in the same toolbar.
      if (headerFree && headerIsAbove) {
        btn.classList.add('chart-export-btn--inline');
        header.appendChild(btn);
        return btn;
      }
    }
    const slot = document.createElement('div');
    slot.className = 'chart-export-slot';
    slot.appendChild(btn);
    host.classList.add('ce-host');
    host.insertBefore(slot, host.firstChild);
    return btn;
  }

  function attachChart(wrap) {
    if (alreadyAttached(wrap)) return;
    if (wrap.closest(CFG.excludes)) return;
    const svg = wrap.querySelector(':scope > svg');
    if (!svg) return;
    // Wait until the chart has actually been drawn — height is set by D3. Not
    // latching here means a chart that starts small (or starts empty) still
    // gets its button once it is redrawn at full size.
    const h = parseFloat(svg.getAttribute('height')) || svg.getBoundingClientRect().height;
    if (!h || h < MIN_CHART_H) return;
    placeButton(wrap, makeButton('Export this chart (SVG / PNG / JPEG / PDF)', () => {
      openDialog({
        kind: 'chart', wrap, svg,
        title: findTitle(wrap), legendEl: findLegend(wrap),
      });
    }));
  }

  function attachTable(host) {
    if (alreadyAttached(host)) return;
    if (host.closest(CFG.excludes)) return;
    const table = host.querySelector('table');
    // Cheap row count first — readTable() walks every cell and is too costly
    // to run on every DOM mutation for a table with thousands of rows.
    if (!table || !table.tBodies[0] || table.tBodies[0].rows.length < MIN_TBL_ROWS) return;
    if (!table.querySelector('thead th')) return;
    placeButton(host, makeButton('Export this table (CSV / XLSX / HTML)', () => {
      openDialog({ kind: 'table', host, table, title: findTitle(host) });
    }));
  }

  function scan() {
    document.querySelectorAll(CFG.containers).forEach(attachChart);
    if (CFG.tableContainers) {
      document.querySelectorAll(CFG.tableContainers).forEach(attachTable);
    }
  }

  function start() {
    scan();
    // Charts and tables appear, redraw and disappear as the user moves around,
    // so keep watching the DOM (debounced) rather than scanning only once.
    let pending = null;
    const obs = new MutationObserver(() => {
      if (pending) return;
      pending = requestAnimationFrame(() => { pending = null; try { scan(); } catch (e) {} });
    });
    obs.observe(document.body, { childList: true, subtree: true });
    // Charts resize with the window; a redraw may be the moment one first
    // becomes tall enough to deserve a button.
    window.addEventListener('resize', () => { setTimeout(scan, 300); });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }

  // Exposed for debugging / manual use from the console.
  window.MBONChartExport = { scan, open: openDialog, exportChart, exportTable, readTable };
})();
