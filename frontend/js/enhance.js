/* ════════════════════════════════════════════════════════════════════════
   MBON Dashboard — universal table & chart tools
   ------------------------------------------------------------------------
   Adds, to every chart/table card, a corner toolbar (revealed on hover) with:
     • Export   — charts: PNG / JPEG / SVG / PDF / HTML  (with DPI / width)
                  tables: CSV / XLSX / HTML  (respects hidden cols, sort, derived)
     • Columns  — hide/show columns + add derived (computed) columns   [tables]
     • Sort     — click any header to sort asc/desc (tables not already
                  wired to the app's own Vue sort are made sortable here)
   Per-table settings (hidden columns, derived columns) persist in localStorage.
   Self-contained: no framework hooks; survives Vue re-renders via a guarded
   MutationObserver.  Loaded AFTER app.js.
   ════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  /* ── localStorage (namespaced, fail-safe) ───────────────────────────── */
  var NS = 'mbon.tools.';
  var LS = {
    get: function (k, d) {
      try { var v = localStorage.getItem(NS + k); return v == null ? d : JSON.parse(v); }
      catch (e) { return d; }
    },
    set: function (k, v) { try { localStorage.setItem(NS + k, JSON.stringify(v)); } catch (e) {} }
  };

  /* ── small helpers ──────────────────────────────────────────────────── */
  function el(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  }
  function txt(node) { return (node && node.textContent || '').trim(); }
  function slug(s) {
    return String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '') || 'col';
  }
  // parse a number out of a cell ("1,234", "12.3%", "#42", "$5") -> number|NaN
  function num(s) {
    if (s == null) return NaN;
    var m = String(s).replace(/[, %$#]/g, '').match(/-?\d+(\.\d+)?(e-?\d+)?/i);
    return m ? parseFloat(m[0]) : NaN;
  }
  function download(filename, blob) {
    var url = URL.createObjectURL(blob);
    var a = el('a');
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click();
    setTimeout(function () { document.body.removeChild(a); URL.revokeObjectURL(url); }, 200);
  }
  function ts() {
    var d = new Date();
    var p = function (n) { return (n < 10 ? '0' : '') + n; };
    return d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) + '_' + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds());
  }
  // resolve CSS custom properties / color keywords to a concrete color
  var _probe = null;
  function resolveColor(c) {
    if (!c || c === 'none' || c === 'transparent') return c;
    if (c.indexOf('var(') === -1 && c.indexOf('currentColor') === -1) return c;
    if (!_probe) { _probe = el('span'); _probe.style.display = 'none'; document.body.appendChild(_probe); }
    _probe.style.color = ''; _probe.style.color = c;
    return getComputedStyle(_probe).color || c;
  }

  /* ── stable signature for a table (header text + position) ───────────── */
  function tableSig(table) {
    if (table.dataset.mbonTid) return table.dataset.mbonTid;
    var heads = [];
    table.querySelectorAll('thead th').forEach(function (th) { heads.push(txt(th).replace(/[↕↓↑]/g, '').trim()); });
    // find which view this table is under (nearest [v-if] sibling region is hard;
    // use the active nav heading to disambiguate identical headers across tabs)
    var sect = '';
    var h2 = document.querySelector('main .section-header h2');
    if (h2) sect = txt(h2);
    var sig = slug(sect) + '__' + slug(heads.join('|')).slice(0, 60);
    // de-dupe if two identical tables coexist
    var n = 0, base = sig;
    while (document.querySelector('table[data-mbon-tid="' + sig + '"]')) { sig = base + '_' + (++n); }
    table.dataset.mbonTid = sig;
    return sig;
  }

  /* ── derived-column expression compiler (safe-ish, local app) ───────── */
  function compileExpr(expr, colNames) {
    // expose each column as a sanitized identifier + a col("Name") accessor
    var idents = colNames.map(slug);
    var args = idents.concat(['col', 'row']);
    var fn;
    try {
      fn = new Function(args.join(','), 'try{return (' + expr + ');}catch(e){return null;}');
    } catch (e) { return null; }
    return function (cellNums, cellText) {
      var vals = idents.map(function (id, i) { return cellNums[i]; });
      var colAccessor = function (name) {
        var i = colNames.indexOf(name);
        if (i < 0) i = idents.indexOf(slug(name));
        return i >= 0 ? cellNums[i] : undefined;
      };
      vals.push(colAccessor);
      vals.push(cellText);
      try { return fn.apply(null, vals); } catch (e) { return null; }
    };
  }

  /* ── read a table's logical model from the live DOM ─────────────────── */
  function readTable(table, opts) {
    opts = opts || {};
    var headCells = Array.prototype.slice.call(table.querySelectorAll('thead tr:last-child th'));
    var headers = headCells.map(function (th) { return txt(th).replace(/[↕↓↑]/g, '').trim(); });
    var hiddenIdx = {};
    headCells.forEach(function (th, i) {
      if (!opts.includeHidden && getComputedStyle(th).display === 'none') hiddenIdx[i] = true;
    });
    var rows = [];
    table.querySelectorAll('tbody tr').forEach(function (tr) {
      var tds = Array.prototype.slice.call(tr.children);
      // skip "no data" placeholder rows (single cell spanning all)
      if (tds.length === 1 && tds[0].hasAttribute('colspan')) return;
      var rec = [];
      tds.forEach(function (td, i) { if (!hiddenIdx[i]) rec.push(txt(td)); });
      rows.push(rec);
    });
    var keptHeaders = headers.filter(function (_, i) { return !hiddenIdx[i]; });
    return { headers: keptHeaders, rows: rows };
  }

  /* ── table exporters ────────────────────────────────────────────────── */
  function aoa(table) {
    var m = readTable(table);
    return [m.headers].concat(m.rows);
  }
  function exportCSV(table, name) {
    var data = aoa(table);
    var csv = data.map(function (r) {
      return r.map(function (c) {
        c = c == null ? '' : String(c);
        return /[",\n]/.test(c) ? '"' + c.replace(/"/g, '""') + '"' : c;
      }).join(',');
    }).join('\r\n');
    download(name + '.csv', new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8;' }));
  }
  function exportXLSX(table, name) {
    if (typeof XLSX === 'undefined') { alert('XLSX library not loaded — falling back to CSV.'); return exportCSV(table, name); }
    var data = aoa(table);
    // coerce numeric-looking cells to real numbers
    var typed = data.map(function (r, ri) {
      if (ri === 0) return r;
      return r.map(function (c) { var n = num(c); return (!isNaN(n) && String(c).trim() !== '' && /\d/.test(c) && !/[a-zA-Z]/.test(String(c).replace(/[eE]/, ''))) ? n : c; });
    });
    var ws = XLSX.utils.aoa_to_sheet(typed);
    var wb = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb, ws, 'Data');
    XLSX.writeFile(wb, name + '.xlsx');
  }
  function exportTableHTML(table, name) {
    var m = readTable(table);
    var h = '<table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse;font-family:system-ui,sans-serif;font-size:13px">';
    h += '<thead><tr style="background:#e0f7fa">' + m.headers.map(function (c) { return '<th align="left">' + esc(c) + '</th>'; }).join('') + '</tr></thead><tbody>';
    h += m.rows.map(function (r) { return '<tr>' + r.map(function (c) { return '<td>' + esc(c) + '</td>'; }).join('') + '</tr>'; }).join('');
    h += '</tbody></table>';
    var doc = '<!doctype html><meta charset="utf-8"><title>' + esc(name) + '</title>' + h;
    download(name + '.html', new Blob([doc], { type: 'text/html;charset=utf-8;' }));
  }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]; }); }

  /* ── chart (svg) exporters ──────────────────────────────────────────── */
  // build a standalone, fully-styled clone of the svg
  function serializeSVG(svg) {
    var bbW = svg.clientWidth || svg.getBoundingClientRect().width || +svg.getAttribute('width') || 700;
    var bbH = svg.clientHeight || svg.getBoundingClientRect().height || +svg.getAttribute('height') || 400;
    var clone = svg.cloneNode(true);
    // inline the style-critical computed properties for every element
    var srcNodes = svg.querySelectorAll('*');
    var dstNodes = clone.querySelectorAll('*');
    var props = ['fill', 'fill-opacity', 'stroke', 'stroke-width', 'stroke-dasharray',
      'stroke-opacity', 'opacity', 'font-size', 'font-family', 'font-weight',
      'font-style', 'text-anchor', 'color'];
    for (var i = 0; i < srcNodes.length; i++) {
      var cs = getComputedStyle(srcNodes[i]);
      var s = '';
      for (var p = 0; p < props.length; p++) {
        var v = cs.getPropertyValue(props[p]);
        if (v) s += props[p] + ':' + resolveColor(v) + ';';
      }
      dstNodes[i].setAttribute('style', s);
    }
    clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
    clone.setAttribute('width', bbW);
    clone.setAttribute('height', bbH);
    clone.setAttribute('viewBox', '0 0 ' + bbW + ' ' + bbH);
    // white backdrop
    var bg = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    bg.setAttribute('x', 0); bg.setAttribute('y', 0);
    bg.setAttribute('width', bbW); bg.setAttribute('height', bbH);
    bg.setAttribute('fill', '#ffffff');
    clone.insertBefore(bg, clone.firstChild);
    var str = new XMLSerializer().serializeToString(clone);
    return { str: '<?xml version="1.0" standalone="no"?>\r\n' + str, w: bbW, h: bbH };
  }
  function svgToCanvas(svg, scale, cb) {
    var s = serializeSVG(svg);
    var img = new Image();
    var svg64 = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(s.str);
    img.onload = function () {
      var cw = Math.round(s.w * scale), ch = Math.round(s.h * scale);
      var canvas = el('canvas'); canvas.width = cw; canvas.height = ch;
      var ctx = canvas.getContext('2d');
      ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, cw, ch);
      ctx.drawImage(img, 0, 0, cw, ch);
      cb(canvas, s);
    };
    img.onerror = function () { alert('Could not rasterize chart for export.'); };
    img.src = svg64;
  }
  function exportSVGfile(svg, name) {
    var s = serializeSVG(svg);
    download(name + '.svg', new Blob([s.str], { type: 'image/svg+xml;charset=utf-8;' }));
  }
  function exportRaster(svg, name, type, scale) {
    svgToCanvas(svg, scale, function (canvas) {
      canvas.toBlob(function (blob) { download(name + (type === 'image/jpeg' ? '.jpg' : '.png'), blob); },
        type, type === 'image/jpeg' ? 0.95 : undefined);
    });
  }
  function exportPDF(svg, name, scale) {
    var jsPDFctor = (window.jspdf && window.jspdf.jsPDF) || window.jsPDF;
    if (!jsPDFctor) { alert('jsPDF not loaded — exporting PNG instead.'); return exportRaster(svg, name, 'image/png', scale); }
    svgToCanvas(svg, scale, function (canvas, s) {
      var img = canvas.toDataURL('image/png');
      var orient = s.w >= s.h ? 'l' : 'p';
      var pdf = new jsPDFctor({ orientation: orient, unit: 'pt', format: [s.w, s.h] });
      pdf.addImage(img, 'PNG', 0, 0, s.w, s.h);
      pdf.save(name + '.pdf');
    });
  }
  function exportChartHTML(svg, name) {
    var s = serializeSVG(svg);
    var doc = '<!doctype html><meta charset="utf-8"><title>' + esc(name) + '</title>' +
      '<body style="margin:0;display:flex;justify-content:center;background:#fff">' + s.str + '</body>';
    download(name + '.html', new Blob([doc], { type: 'text/html;charset=utf-8;' }));
  }

  /* ── popover plumbing ───────────────────────────────────────────────── */
  var openPop = null;
  function closePop() { if (openPop) { openPop.remove(); openPop = null; } var b = document.querySelector('.mbon-backdrop'); if (b) b.remove(); }
  function showPop(anchor, node) {
    closePop();
    var back = el('div', 'mbon-backdrop'); back.addEventListener('click', closePop); document.body.appendChild(back);
    node.classList.add('mbon-pop'); document.body.appendChild(node); openPop = node;
    var r = anchor.getBoundingClientRect();
    var w = node.offsetWidth, h = node.offsetHeight;
    var left = Math.min(r.right - w, window.innerWidth - w - 8); left = Math.max(8, left);
    var top = r.bottom + 6; if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 6);
    node.style.left = left + 'px'; node.style.top = top + 'px';
    node.addEventListener('click', function (e) { e.stopPropagation(); });
  }

  /* ── EXPORT popover (legacy — no longer wired to a button) ──────────── */
  function buildExportPop(card, anchor, baseName) {
    var svg = card.querySelector('.d3-wrap svg, svg');
    var table = card.querySelector('table');
    var pop = el('div');

    if (svg) {
      pop.appendChild(el('h4', null, 'Export chart'));
      var fmt = LS.get('chartFmt', 'png');
      var fmts = [['png', 'PNG'], ['jpeg', 'JPEG'], ['svg', 'SVG'], ['pdf', 'PDF'], ['html', 'HTML']];
      var grid = el('div', 'mbon-fmt-grid');
      fmts.forEach(function (f) {
        var b = el('button', 'mbon-fmt-btn' + (fmt === f[0] ? ' active' : ''), f[1]);
        b.addEventListener('click', function () { fmt = f[0]; LS.set('chartFmt', fmt); grid.querySelectorAll('.mbon-fmt-btn').forEach(function (x) { x.classList.remove('active'); }); b.classList.add('active'); syncDpi(); });
        grid.appendChild(b);
      });
      pop.appendChild(grid);

      var dpiRow = el('div', 'mbon-row');
      dpiRow.appendChild(el('label', null, 'DPI'));
      var dpi = el('input'); dpi.type = 'number'; dpi.min = 72; dpi.max = 1200; dpi.step = 1; dpi.style.width = '72px';
      dpi.value = LS.get('chartDpi', 192);
      dpiRow.appendChild(dpi);
      dpiRow.appendChild(el('label', null, 'Width px'));
      var wpx = el('input'); wpx.type = 'number'; wpx.min = 100; wpx.step = 10; wpx.style.width = '84px'; wpx.placeholder = 'auto';
      dpiRow.appendChild(wpx);
      pop.appendChild(dpiRow);
      var hint = el('div', 'mbon-hint', 'Raster scale = DPI ÷ 96. SVG/HTML ignore DPI (vector).');
      pop.appendChild(hint);
      function syncDpi() {
        var vec = (fmt === 'svg' || fmt === 'html');
        dpiRow.style.opacity = vec ? '.4' : '1';
        dpi.disabled = wpx.disabled = vec;
      }
      syncDpi();

      var go = el('button', 'mbon-mini-btn primary', 'Download chart');
      go.addEventListener('click', function () {
        LS.set('chartDpi', +dpi.value || 192);
        var scale = (+dpi.value || 192) / 96;
        var natW = svg.clientWidth || +svg.getAttribute('width') || 700;
        if (+wpx.value > 0) scale = (+wpx.value) / natW;
        scale = Math.max(0.2, Math.min(scale, 12));
        var nm = baseName + '_' + ts();
        if (fmt === 'png') exportRaster(svg, nm, 'image/png', scale);
        else if (fmt === 'jpeg') exportRaster(svg, nm, 'image/jpeg', scale);
        else if (fmt === 'svg') exportSVGfile(svg, nm);
        else if (fmt === 'pdf') exportPDF(svg, nm, scale);
        else if (fmt === 'html') exportChartHTML(svg, nm);
        closePop();
      });
      var act = el('div', 'mbon-actions'); act.appendChild(go); pop.appendChild(act);
    }

    if (table) {
      if (svg) pop.appendChild(el('div', 'mbon-divider'));
      pop.appendChild(el('h4', null, 'Export table'));
      var trow = el('div', 'mbon-row');
      [['csv', 'CSV'], ['xlsx', 'XLSX'], ['html', 'HTML']].forEach(function (f) {
        var b = el('button', 'mbon-mini-btn', f[1]);
        b.addEventListener('click', function () {
          var nm = baseName + '_' + ts();
          if (f[0] === 'csv') exportCSV(table, nm);
          else if (f[0] === 'xlsx') exportXLSX(table, nm);
          else exportTableHTML(table, nm);
          closePop();
        });
        trow.appendChild(b);
      });
      pop.appendChild(trow);
      var m = readTable(table);
      pop.appendChild(el('div', 'mbon-hint', m.rows.length + ' rows × ' + m.headers.length + ' visible columns (respects hidden columns & sort).'));
    }

    if (!svg && !table) pop.appendChild(el('div', 'mbon-hint', 'Nothing exportable in this card.'));
    showPop(anchor, pop);
  }

  /* ── COLUMNS popover (hide/show + derived) ──────────────────────────── */
  function buildColumnsPop(table, anchor) {
    var sig = tableSig(table);
    var pop = el('div');
    pop.appendChild(el('h4', null, 'Show / hide columns'));

    var headCells = Array.prototype.slice.call(table.querySelectorAll('thead tr:last-child th'));
    var hidden = LS.get('hidden.' + sig, {});
    var derived = LS.get('derived.' + sig, []);

    var list = el('div', 'mbon-col-list');
    headCells.forEach(function (th, i) {
      var name = txt(th).replace(/[↕↓↑]/g, '').trim() || ('Column ' + (i + 1));
      var item = el('label', 'mbon-col-item');
      var cb = el('input'); cb.type = 'checkbox'; cb.checked = !hidden[i];
      var isDerived = th.classList.contains('mbon-derived-th');
      cb.addEventListener('change', function () {
        if (cb.checked) delete hidden[i]; else hidden[i] = true;
        LS.set('hidden.' + sig, hidden); applyHidden(table);
      });
      item.appendChild(cb);
      item.appendChild(el('span', 'mbon-col-name', esc(name)));
      if (isDerived) {
        item.appendChild(el('span', 'mbon-col-derived', 'derived'));
        var del = el('button', 'mbon-col-del', '✕'); del.title = 'Remove derived column';
        del.addEventListener('click', function (e) {
          e.preventDefault();
          var dn = th.dataset.mbonDerived;
          derived = derived.filter(function (d) { return d.name !== dn; });
          LS.set('derived.' + sig, derived);
          delete hidden[i]; LS.set('hidden.' + sig, hidden);
          reapply(table);
          buildColumnsPop(table, anchor);
        });
        item.appendChild(del);
      }
      list.appendChild(item);
    });
    pop.appendChild(list);

    var qa = el('div', 'mbon-row');
    var showAll = el('button', 'mbon-mini-btn ghost', 'Show all');
    showAll.addEventListener('click', function () { LS.set('hidden.' + sig, {}); applyHidden(table); buildColumnsPop(table, anchor); });
    qa.appendChild(showAll);
    pop.appendChild(qa);

    pop.appendChild(el('div', 'mbon-divider'));
    pop.appendChild(el('h4', null, 'Add derived column'));
    var names = headCells.map(function (th) { return txt(th).replace(/[↕↓↑]/g, '').trim(); });
    var nrow = el('div', 'mbon-row');
    nrow.appendChild(el('label', null, 'Name'));
    var nameIn = el('input'); nameIn.type = 'text'; nameIn.placeholder = 'e.g. Reads/Sample'; nameIn.style.flex = '1';
    nrow.appendChild(nameIn);
    pop.appendChild(nrow);
    var erow = el('div', 'mbon-row');
    erow.appendChild(el('label', null, 'Formula'));
    var exprIn = el('input'); exprIn.type = 'text'; exprIn.placeholder = 'reads / samples'; exprIn.style.flex = '1';
    erow.appendChild(exprIn);
    pop.appendChild(erow);
    var varList = names.map(slug).filter(Boolean);
    pop.appendChild(el('div', 'mbon-hint', 'Use column names as variables: ' + esc(varList.slice(0, 8).join(', ')) + (varList.length > 8 ? '…' : '') + '. Also col("Exact Name"). Math like + - * / and Math.* allowed.'));
    var add = el('button', 'mbon-mini-btn primary', 'Add column');
    add.addEventListener('click', function () {
      var nm = nameIn.value.trim(), ex = exprIn.value.trim();
      if (!nm || !ex) { alert('Enter a name and a formula.'); return; }
      derived.push({ name: nm, expr: ex });
      LS.set('derived.' + sig, derived);
      reapply(table);
      buildColumnsPop(table, anchor);
    });
    var act = el('div', 'mbon-actions'); act.appendChild(add); pop.appendChild(act);

    showPop(anchor, pop);
  }

  /* ── apply hidden columns via CSS (survives re-render) ──────────────── */
  function applyHidden(table) {
    var sig = tableSig(table);
    var hidden = LS.get('hidden.' + sig, {});
    var styleId = 'mbon-hide-' + sig;
    var st = document.getElementById(styleId);
    var idx = Object.keys(hidden).filter(function (k) { return hidden[k]; }).map(Number);
    if (!idx.length) { if (st) st.remove(); return; }
    if (!st) { st = el('style'); st.id = styleId; document.head.appendChild(st); }
    var sel = 'table[data-mbon-tid="' + sig + '"]';
    st.textContent = idx.map(function (i) {
      var nth = ':nth-child(' + (i + 1) + ')';
      return sel + ' thead th' + nth + ',' + sel + ' tbody td' + nth + '{display:none!important}';
    }).join('');
  }

  /* ── derived columns: ensure header + per-row cells exist ───────────── */
  function applyDerived(table) {
    var sig = tableSig(table);
    var derived = LS.get('derived.' + sig, []);
    var headRow = table.querySelector('thead tr:last-child');
    if (!headRow) return;
    // base (non-derived) header names, in order
    var baseHeads = [];
    Array.prototype.forEach.call(headRow.children, function (th) {
      if (!th.classList.contains('mbon-derived-th')) baseHeads.push(txt(th).replace(/[↕↓↑]/g, '').trim());
    });
    // remove derived headers no longer defined
    Array.prototype.slice.call(headRow.querySelectorAll('th.mbon-derived-th')).forEach(function (th) {
      if (!derived.some(function (d) { return d.name === th.dataset.mbonDerived; })) th.remove();
    });
    // remove derived cells in body that are orphaned
    table.querySelectorAll('tbody td.mbon-derived-cell').forEach(function (td) {
      if (!derived.some(function (d) { return d.name === td.dataset.mbonDerived; })) td.remove();
    });
    if (!derived.length) return;

    derived.forEach(function (d) {
      // header
      var th = headRow.querySelector('th.mbon-derived-th[data-mbon-derived="' + cssesc(d.name) + '"]');
      if (!th) {
        th = el('th', 'mbon-derived-th'); th.dataset.mbonDerived = d.name;
        th.textContent = d.name;
        headRow.appendChild(th);
      }
    });

    var fn = {};
    derived.forEach(function (d) { fn[d.name] = compileExpr(d.expr, baseHeads); });

    table.querySelectorAll('tbody tr').forEach(function (tr) {
      var baseCells = Array.prototype.filter.call(tr.children, function (td) { return !td.classList.contains('mbon-derived-cell'); });
      if (baseCells.length === 1 && baseCells[0].hasAttribute('colspan')) {
        // placeholder row: widen colspan to include derived cols, no values
        return;
      }
      var nums = baseCells.map(function (td) { return num(txt(td)); });
      var textsByName = {};
      baseHeads.forEach(function (h, i) { textsByName[h] = baseCells[i] ? txt(baseCells[i]) : ''; });
      derived.forEach(function (d) {
        var td = tr.querySelector('td.mbon-derived-cell[data-mbon-derived="' + cssesc(d.name) + '"]');
        if (!td) { td = el('td', 'mbon-derived-cell'); td.dataset.mbonDerived = d.name; tr.appendChild(td); }
        var v = fn[d.name] ? fn[d.name](nums, textsByName) : null;
        if (typeof v === 'number') { td.textContent = isFinite(v) ? (Math.round(v * 1000) / 1000).toLocaleString() : '–'; }
        else { td.textContent = (v == null ? '–' : String(v)); }
      });
    });
  }
  function cssesc(s) { return String(s).replace(/(["\\])/g, '\\$1'); }

  /* ── universal click-to-sort (only tables NOT already app-sorted) ───── */
  function isAppSorted(table) {
    // the app renders {{ sortArrow(...) }} -> a ↕/↓/↑ glyph in managed headers
    return /[↕↓↑]/.test(table.querySelector('thead') ? table.querySelector('thead').textContent : '');
  }
  function makeSortable(table) {
    if (table.dataset.mbonSortReady) return;
    if (isAppSorted(table)) return;           // let Vue own these
    var headRow = table.querySelector('thead tr:last-child');
    if (!headRow) return;
    table.dataset.mbonSortReady = '1';
    Array.prototype.forEach.call(headRow.children, function (th, i) {
      if (th.classList.contains('mbon-derived-th')) return; // handled when derived added
      wireSortHeader(table, th, i);
    });
  }
  function wireSortHeader(table, th, i) {
    if (th.dataset.mbonSortCol != null) return;
    th.dataset.mbonSortCol = i;
    th.classList.add('mbon-sortable');
    var ind = el('span', 'mbon-sort-ind', '↕'); th.appendChild(ind);
    th.addEventListener('click', function () {
      var cur = table.dataset.mbonSortDir === 'asc' && +table.dataset.mbonSortIdx === i ? 'desc' : 'asc';
      table.dataset.mbonSortIdx = i; table.dataset.mbonSortDir = cur;
      domSort(table);
    });
  }
  function domSort(table) {
    var idx = +table.dataset.mbonSortIdx, dir = table.dataset.mbonSortDir || 'asc';
    if (isNaN(idx)) return;
    var tbody = table.querySelector('tbody'); if (!tbody) return;
    var rows = Array.prototype.filter.call(tbody.children, function (tr) {
      return !(tr.children.length === 1 && tr.children[0].hasAttribute('colspan'));
    });
    rows.sort(function (a, b) {
      var av = txt(a.children[idx]), bv = txt(b.children[idx]);
      var an = num(av), bn = num(bv);
      var both = !isNaN(an) && !isNaN(bn);
      if (both) return dir === 'asc' ? an - bn : bn - an;
      return dir === 'asc' ? av.localeCompare(bv) : bv.localeCompare(av);
    });
    var guard = beginMutate(table);
    rows.forEach(function (tr) { tbody.appendChild(tr); });
    endMutate(guard);
    // update indicators
    table.querySelectorAll('th.mbon-sortable').forEach(function (th) {
      th.classList.remove('mbon-sorted');
      var ind = th.querySelector('.mbon-sort-ind'); if (ind) ind.textContent = '↕';
    });
    var th = table.querySelector('th.mbon-sortable[data-mbon-sort-col="' + idx + '"]');
    if (th) { th.classList.add('mbon-sorted'); var ind = th.querySelector('.mbon-sort-ind'); if (ind) ind.textContent = dir === 'asc' ? '↑' : '↓'; }
  }

  /* ── re-apply everything for one table ──────────────────────────────── */
  function reapply(table) {
    var guard = beginMutate(table);
    try {
      applyDerived(table);
      // derived headers need sort wiring too (if table is dom-sortable)
      if (table.dataset.mbonSortReady) {
        table.querySelectorAll('th.mbon-derived-th').forEach(function (th) {
          var idx = Array.prototype.indexOf.call(th.parentNode.children, th);
          wireSortHeader(table, th, idx);
        });
        if (table.dataset.mbonSortIdx != null) domSort(table);
      }
    } catch (e) { /* never break the page */ }
    endMutate(guard);
    applyHidden(table);
  }

  /* ── mutation guard so our own DOM edits don't loop the observer ─────── */
  var mutating = new WeakSet();
  function beginMutate(t) { mutating.add(t); return t; }
  function endMutate(t) { setTimeout(function () { mutating.delete(t); }, 0); }

  /* ── toolbar injection per card ─────────────────────────────────────── */
  function enhanceCard(card) {
    if (card.dataset.mbonTooled) { refreshCard(card); return; }
    var hasSvg = !!card.querySelector('.d3-wrap svg, svg');
    var hasTable = !!card.querySelector('table');
    if (!hasSvg && !hasTable) return;
    card.dataset.mbonTooled = '1';

    var bar = el('div', 'mbon-tools');

    // NOTE: the ⤓ Export button that used to live here has been retired.
    // chart-export.js now owns every export (charts: SVG/PNG/JPEG/PDF with
    // size + legend control; tables: CSV/XLSX/HTML) and places one button per
    // chart/table, so two competing export affordances is just confusing.
    // buildExportPop() below is kept — call it from the console if you ever
    // want the old popover back.

    if (hasTable) {
      var cog = el('button', 'mbon-tool-btn', '⚙');
      cog.title = 'Columns / edit';
      cog.addEventListener('click', function (e) { e.stopPropagation(); buildColumnsPop(card.querySelector('table'), cog); });
      bar.appendChild(cog);
    }
    if (bar.childNodes.length) card.appendChild(bar);
    refreshCard(card);
  }
  function refreshCard(card) {
    card.querySelectorAll('table').forEach(function (t) {
      tableSig(t);
      makeSortable(t);
      reapply(t);
    });
  }
  function guessName(card) {
    var h = card.querySelector('h3, .card-title, h2');
    var base = h ? slug(txt(h)) : 'mbon_export';
    return 'mbon_' + (base || 'export');
  }

  /* ── scan + observe ─────────────────────────────────────────────────── */
  function scan() {
    document.querySelectorAll('main .card').forEach(enhanceCard);
    // tables that live outside .card (e.g. inside .table-wrap directly) get sort+sig too
    document.querySelectorAll('main table').forEach(function (t) {
      if (t.closest('.card')) return;
      tableSig(t); makeSortable(t); reapply(t);
    });
  }

  var pending = false;
  function schedule() {
    if (pending) return; pending = true;
    requestAnimationFrame(function () { pending = false; try { scan(); } catch (e) {} });
  }

  function start() {
    scan();
    var root = document.getElementById('app') || document.body;
    var obs = new MutationObserver(function (muts) {
      for (var i = 0; i < muts.length; i++) {
        var t = muts[i].target;
        var tbl = t.closest ? t.closest('table') : null;
        if (tbl && mutating.has(tbl)) continue;   // ignore our own edits
        schedule();
        return;
      }
    });
    obs.observe(root, { childList: true, subtree: true });
    window.addEventListener('resize', function () { /* charts redraw via app; nothing to do */ });
    window.addEventListener('keydown', function (e) { if (e.key === 'Escape') closePop(); });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { setTimeout(start, 300); });
  else setTimeout(start, 300);
})();
