/* ════════════════════════════════════════════════════════════════════════
   MBON Dashboard — universal table search + pagination
   ------------------------------------------------------------------------
   Adds a control bar (search box · rows-per-page · pager) above every large
   listing table so results stay usable when there are hundreds of rows.

   • Search    — case-insensitive substring over the whole row (all columns).
   • Paginate  — 10 / 25 / 50 / 100 / All rows per page, with prev/next.
   • Framework-agnostic: rows are shown/hidden via a CSS class only, so it
     cooperates with Vue re-renders AND with enhance.js click-to-sort
     (which reorders the same <tr> nodes). Re-applies itself after any
     mutation via a guarded MutationObserver. Loaded AFTER enhance.js.

   Only attaches to tables with more than MIN_ROWS body rows, and never to
   tables inside detail panels, map popups, the import panel or export pops.
   Rows-per-page choice persists in localStorage.
   ════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var MIN_ROWS = 11;                 // don't clutter small tables
  var SIZE_KEY = 'mbon.tabletools.size';
  var SIZES = [10, 25, 50, 100, 0];  // 0 = All
  var EXCLUDE = '.detail-panel, .map-popup, .mbon-cluster-popup, .mbon-pop, .import-panel, #import-panel, .mbon-backdrop';

  function el(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  }
  function savedSize() {
    var v = parseInt(localStorage.getItem(SIZE_KEY), 10);
    return SIZES.indexOf(v) >= 0 ? v : 25;
  }
  function saveSize(v) { try { localStorage.setItem(SIZE_KEY, String(v)); } catch (e) {} }

  // real (non-placeholder) body rows for a table
  function realRows(table) {
    var tb = table.tBodies[0];
    if (!tb) return [];
    return Array.prototype.filter.call(tb.rows, function (tr) {
      return !(tr.cells.length === 1 && tr.cells[0].hasAttribute('colspan'));
    });
  }

  /* ── build + wire the control bar for a table ───────────────────────── */
  function buildControls(table) {
    var st = table.__tt;
    var bar = el('div', 'tt-controls');

    var search = el('input', 'tt-search');
    search.type = 'search';
    search.placeholder = '🔍 Search table…';
    search.value = st.q;
    search.addEventListener('input', function () {
      st.q = search.value; st.page = 0; apply(table);
    });
    bar.appendChild(search);

    var count = el('span', 'tt-count');
    bar.appendChild(count);

    bar.appendChild(el('span', 'tt-spacer'));

    var sizeLbl = el('label', 'tt-size-lbl', 'Rows ');
    var size = el('select', 'tt-size');
    SIZES.forEach(function (s) {
      var o = el('option', null, s === 0 ? 'All' : String(s));
      o.value = String(s);
      if (s === st.size) o.selected = true;
      size.appendChild(o);
    });
    size.addEventListener('change', function () {
      st.size = parseInt(size.value, 10) || 0; st.page = 0;
      saveSize(st.size); apply(table);
    });
    sizeLbl.appendChild(size);
    bar.appendChild(sizeLbl);

    var pager = el('div', 'tt-pager');
    var prev = el('button', 'tt-prev', '‹'); prev.type = 'button'; prev.title = 'Previous page';
    var pageInfo = el('span', 'tt-page');
    var next = el('button', 'tt-next', '›'); next.type = 'button'; next.title = 'Next page';
    prev.addEventListener('click', function () { if (st.page > 0) { st.page--; apply(table); } });
    next.addEventListener('click', function () { st.page++; apply(table); });
    pager.appendChild(prev); pager.appendChild(pageInfo); pager.appendChild(next);
    bar.appendChild(pager);

    table.__ttEl = { bar: bar, count: count, page: pageInfo, prev: prev, next: next, size: size };
    // Insert directly above the table, inside its own wrapper (static DOM that
    // survives Vue tbody re-renders).
    table.parentNode.insertBefore(bar, table);
  }

  /* ── compute + apply filter/pagination ──────────────────────────────── */
  function apply(table) {
    var st = table.__tt; if (!st) return;
    // Re-attach the control bar if a Vue re-render detached it.
    var elx0 = table.__ttEl;
    if (elx0 && elx0.bar && !elx0.bar.isConnected && table.parentNode) {
      table.parentNode.insertBefore(elx0.bar, table);
    }
    var rows = realRows(table);
    var q = (st.q || '').trim().toLowerCase();
    var matched = q
      ? rows.filter(function (tr) { return tr.textContent.toLowerCase().indexOf(q) !== -1; })
      : rows;

    var size = st.size > 0 ? st.size : (matched.length || 1);
    var pages = Math.max(1, Math.ceil(matched.length / size));
    if (st.page >= pages) st.page = pages - 1;
    if (st.page < 0) st.page = 0;
    var start = st.page * size, end = start + size;

    for (var i = 0; i < rows.length; i++) rows[i].classList.add('tt-hidden');
    for (var j = start; j < end && j < matched.length; j++) matched[j].classList.remove('tt-hidden');

    var elx = table.__ttEl; if (!elx) return;
    var from = matched.length ? start + 1 : 0;
    var to = Math.min(end, matched.length);
    elx.count.textContent = q
      ? 'showing ' + from + '–' + to + ' of ' + matched.length + ' (filtered from ' + rows.length + ')'
      : 'showing ' + from + '–' + to + ' of ' + matched.length;
    elx.page.textContent = (st.page + 1) + ' / ' + pages;
    elx.prev.disabled = st.page <= 0;
    elx.next.disabled = st.page >= pages - 1;
    elx.bar.classList.toggle('tt-onepage', pages <= 1);
  }

  /* ── attach to a qualifying table ───────────────────────────────────── */
  function ensure(table) {
    if (table.dataset.ttReady) return true;
    if (!table.tHead || !table.tBodies[0]) return false;
    if (table.closest(EXCLUDE)) return false;
    if (realRows(table).length < MIN_ROWS) return false;   // wait for data / stay clean
    table.dataset.ttReady = '1';
    table.__tt = { q: '', page: 0, size: savedSize() };
    buildControls(table);
    apply(table);
    return true;
  }

  /* ── scan the document; attach new, refresh existing ────────────────── */
  function scan() {
    var tables = document.querySelectorAll('main table');
    for (var i = 0; i < tables.length; i++) {
      var t = tables[i];
      if (t.dataset.ttReady) apply(t);   // re-apply after Vue re-render / sort
      else ensure(t);
    }
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
        // ignore mutations we caused inside our own control bars
        if (t && t.closest && t.closest('.tt-controls')) continue;
        schedule();
        return;
      }
    });
    obs.observe(root, { childList: true, subtree: true });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { setTimeout(start, 350); });
  } else { setTimeout(start, 350); }
})();
