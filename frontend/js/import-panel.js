/* ============================================================
   MBON Import Panel  (framework-agnostic, no build step)
   ------------------------------------------------------------
   Exposes two globals:
     window.MBONImportLib    — pure parse/validate/normalize fns
                               (unit-testable in Node)
     window.MBONImportPanel  — DOM panel + import popup factory
   Requires SheetJS (XLSX) to be loaded on the page.
   ============================================================ */
(function (global) {
  "use strict";

  /* =========================================================
     PART 1 — PURE LIBRARY  (window.MBONImportLib)
     ========================================================= */
  const Lib = {};

  // normalize a header token for fuzzy matching
  function norm(s) {
    return String(s == null ? "" : s).toLowerCase().replace(/[^a-z0-9]+/g, "");
  }
  Lib.norm = norm;

  // Role definitions. `sig` = tokens we expect; `required` = tokens (or
  // any-of arrays) that MUST be present for a clean "ok". `label`/`desc`
  // power the popup. Header keys are matched fuzzily via norm().
  const ROLES = {
    samples: {
      label: "Samples",
      desc: "Per-sample metadata (one row per sample): IDs, station, coordinates, environment.",
      sig: ["sampleid","sampname","cruisenumber","station","decimallatitude","decimallongitude","sampcategory","salinity","temp","minimumdepthinmeters"],
      required: [["sampname","sampleid"]],
      idHint: ["sampname","sampleid"],
    },
    cruise: {
      label: "Cruise (Collection)",
      desc: "One row per cruise/collection event: vessel, PI, region, dates.",
      sig: ["cruiseid","cruisenumber","cruisedate","platformvessel","piname","locationregion","assayname","seqrunid"],
      required: ["cruisenumber"],
      idHint: ["cruisenumber","cruiseid"],
    },
    runs: {
      label: "Runs",
      desc: "Sequencing / analysis runs. One row per run; a sample may have many.",
      sig: ["runid","sampname","cruisenumber","rundate","platform","instrumentname","directory","basecallersoftware"],
      required: ["runid"],
      idHint: ["runid"],
    },
    bioinformatics: {
      label: "Bioinformatics",
      desc: "Pipeline runs per sample, linked to a Run and a Sample.",
      sig: ["bioinfoid","runid","sampleid","pipelinename","versionpipeline","sopbioinformatics","coderepo"],
      required: ["bioinfoid"],
      idHint: ["bioinfoid"],
    },
    wilder: {
      label: "Wilder Results",
      desc: "Per-sample, per-taxon read counts (Wilderlab). One row = one taxon in one sample.",
      sig: ["wilderresultid","sampleid","bioinfoid","wilderjobid","wilderuid","scientificname","commonname","taxonomicrank","organismgroup","taxid","readcount"],
      required: [["sampleid"], ["scientificname"]],
      idHint: ["wilderresultid","sampleid"],
    },
    kraken2: {
      label: "Kraken2",
      desc: "Kraken2 reports + LCA assignments.",
      sig: ["k2reportid","bioinfoid","database","reportdate","kronalocation","lcasid","taxonomyid","parenttaxonomyid","rank","name","abundance","count"],
      required: [["k2reportid","lcasid"]],
      idHint: ["k2reportid","lcasid"],
    },
    taxtriage: {
      label: "TaxTriage",
      desc: "TaxTriage pipeline runs + detected-taxon reports.",
      sig: ["taxtriagerunid","bioinfoid","database","runlocation","rundate","kronalocation","reportid","organismdetected","taxid","specimenid","specimentype","tassscore"],
      required: [["taxtriagerunid","reportid"]],
      idHint: ["taxtriagerunid","reportid"],
    },
  };
  Lib.ROLES = ROLES;
  Lib.ROLE_KEYS = Object.keys(ROLES);

  // does a normalized header list satisfy a single required entry?
  function reqMet(normSet, entry) {
    if (Array.isArray(entry)) return entry.some((t) => normSet.has(t));
    return normSet.has(entry);
  }

  // score how well a row of cells works as a header for `role` (0..1)
  function scoreRowForRole(cells, role) {
    const def = ROLES[role];
    if (!def) return 0;
    const present = new Set(cells.map(norm).filter(Boolean));
    let hits = 0;
    def.sig.forEach((t) => { if (present.has(t)) hits++; });
    return hits / def.sig.length;
  }
  Lib.scoreRowForRole = scoreRowForRole;

  // For a sheet (array-of-arrays), find best (headerRowIdx, role).
  Lib.detectHeaderAndRole = function (rows, maxScan) {
    maxScan = Math.min(maxScan || 12, rows.length);
    let best = { headerRowIdx: 0, role: "unknown", score: 0 };
    for (let r = 0; r < maxScan; r++) {
      const cells = rows[r] || [];
      const nonEmpty = cells.filter((c) => c != null && String(c).trim() !== "").length;
      if (nonEmpty < 2) continue;
      for (const role of Lib.ROLE_KEYS) {
        const sc = scoreRowForRole(cells, role);
        if (sc > best.score) best = { headerRowIdx: r, role, score: sc };
      }
    }
    return best;
  };

  // Given a chosen role, find the most likely header row in the sheet.
  Lib.bestHeaderRowForRole = function (rows, role, maxScan) {
    maxScan = Math.min(maxScan || 12, rows.length);
    let best = { idx: 0, score: -1 };
    for (let r = 0; r < maxScan; r++) {
      const cells = rows[r] || [];
      const nonEmpty = cells.filter((c) => c != null && String(c).trim() !== "").length;
      if (nonEmpty < 2) continue;
      const sc = scoreRowForRole(cells, role);
      if (sc > best.score) best = { idx: r, score: sc };
    }
    return best.idx;
  };

  // Validate a header list against a role. Returns status + explanation.
  Lib.validate = function (role, headers) {
    const def = ROLES[role];
    if (!def) {
      return { status: "bad", found: [], missing: [], score: 0,
        reason: "No role selected — choose what this tab represents, or set it to Ignore." };
    }
    const normSet = new Set(headers.map(norm).filter(Boolean));
    const found = def.sig.filter((t) => normSet.has(t));
    const missing = def.sig.filter((t) => !normSet.has(t));
    const score = def.sig.length ? found.length / def.sig.length : 0;
    const reqOk = def.required.every((e) => reqMet(normSet, e));
    let status, reason;
    if (reqOk && score >= 0.4) {
      status = "ok";
      reason = `Looks like a valid ${def.label} tab — ${found.length}/${def.sig.length} expected columns present, all key identifier columns found.`;
    } else if (score >= 0.2 || reqOk) {
      status = "warn";
      const miss = def.required.filter((e) => !reqMet(normSet, e))
        .map((e) => (Array.isArray(e) ? e.join(" or ") : e));
      reason = reqOk
        ? `Acceptable as ${def.label}, but only ${found.length}/${def.sig.length} expected columns matched — double-check the header row.`
        : `Partial match for ${def.label}. Missing key identifier column(s): ${miss.join(", ")}. It will import but may not link correctly.`;
    } else {
      status = "bad";
      reason = `This doesn't match the expected ${def.label} format (only ${found.length}/${def.sig.length} columns matched). Pick a different role or set it to Ignore.`;
    }
    return { status, found, missing, score, reason };
  };

  // Build record objects from a sheet given a header row.
  Lib.extractRecords = function (rows, headerRowIdx) {
    const headerRow = rows[headerRowIdx] || [];
    const headers = headerRow.map((h) => (h != null ? String(h).trim() : ""));
    const out = [];
    for (let r = headerRowIdx + 1; r < rows.length; r++) {
      const row = rows[r] || [];
      if (!row.some((c) => c != null && String(c).trim() !== "")) continue;
      const obj = {};
      headers.forEach((h, i) => { if (h) obj[h] = row[i] != null ? row[i] : null; });
      out.push(obj);
    }
    return { headers: headers.filter(Boolean), records: out };
  };

  // sheet -> array-of-arrays via SheetJS
  Lib.sheetToRows = function (XLSX, ws) {
    return XLSX.utils.sheet_to_json(ws, { header: 1, defval: null, raw: false, blankrows: true });
  };

  // Analyze every sheet in a workbook.
  Lib.analyzeWorkbook = function (XLSX, wb) {
    return wb.SheetNames.map((name) => {
      const rows = Lib.sheetToRows(XLSX, wb.Sheets[name]);
      const det = Lib.detectHeaderAndRole(rows);
      const role = det.score >= 0.25 ? det.role : "unknown";
      const headerRowIdx = role === "unknown" ? det.headerRowIdx : Lib.bestHeaderRowForRole(rows, role);
      const ext = Lib.extractRecords(rows, headerRowIdx);
      return {
        sheetName: name, rows, role, headerRowIdx,
        headers: ext.headers, records: ext.records,
        validation: Lib.validate(role, ext.headers),
        autoScore: det.score,
      };
    });
  };

  // Re-derive a sheet analysis after the user changes role / header row / name.
  Lib.reanalyzeSheet = function (sheet, role, headerRowIdx) {
    const idx = headerRowIdx != null ? headerRowIdx
      : (role === "unknown" ? sheet.headerRowIdx : Lib.bestHeaderRowForRole(sheet.rows, role));
    const ext = Lib.extractRecords(sheet.rows, idx);
    return Object.assign({}, sheet, {
      role, headerRowIdx: idx, headers: ext.headers, records: ext.records,
      validation: Lib.validate(role, ext.headers),
    });
  };

  /* ---------- Raw CSV (stripped Wilder-style) ---------- */

  // Map raw-CSV header -> normalized wilder field. Patterns are matched
  // against norm(header). First match wins.
  const RAW_WILDER_PATTERNS = [
    { field: "sample_id",       any: ["samplenumberuid","samplenumber","sampleuid","sampleid","sampname","samplereference"] },
    { field: "tax_id",          any: ["taxonid","taxid"] },
    { field: "taxonomic_rank",  any: ["rank","taxonomicrank"] },
    { field: "scientific_name", any: ["scientificname","species","taxon","name"] },
    { field: "common_name",     any: ["commonname"] },
    { field: "organism_group",  any: ["group","organismgroup"] },
    { field: "read_count",      any: ["taxoncount","readcount","count","reads","abundance"] },
  ];

  Lib.guessColumn = function (headers, anyTokens) {
    for (const tok of anyTokens) {
      const hit = headers.find((h) => norm(h) === tok);
      if (hit) return hit;
    }
    // looser contains-match fallback
    for (const tok of anyTokens) {
      const hit = headers.find((h) => norm(h).includes(tok));
      if (hit) return hit;
    }
    return "";
  };

  // Decide if a flat CSV looks like raw Wilder data + propose a mapping.
  Lib.analyzeRawCsv = function (headers, records) {
    const mapping = {};
    RAW_WILDER_PATTERNS.forEach((p) => { mapping[p.field] = Lib.guessColumn(headers, p.any); });
    const haveSample = !!mapping.sample_id;
    const haveTaxon = !!(mapping.scientific_name || mapping.tax_id);
    const haveCount = !!mapping.read_count;
    const kind = haveSample && haveTaxon ? "wilder_raw" : "generic";
    // lat/lon if present (sample-level extras)
    const latCol = Lib.guessColumn(headers, ["latitude","decimallatitude","lat"]);
    const lonCol = Lib.guessColumn(headers, ["longitude","decimallongitude","lon","lng"]);
    const refCol = Lib.guessColumn(headers, ["samplereference","station","site"]);
    let status, reason;
    if (kind === "wilder_raw" && haveCount) {
      status = "ok";
      reason = "Recognized as stripped Wilder results (Sample ID + taxon + count). Will import as Wilder Results keyed by Sample ID.";
    } else if (haveSample && haveTaxon) {
      status = "warn";
      reason = "Looks Wilder-like but no clear read-count column was found — counts will default to blank. Confirm the mapping below.";
    } else {
      status = "bad";
      reason = "Couldn't find a Sample-ID column and a taxon column. Map them manually below, or this file can't be imported as Wilder results.";
    }
    return { kind, mapping, latCol, lonCol, refCol, status, reason };
  };

  // Build normalized wilder records + synthesized sample rows from a raw CSV.
  Lib.buildRawWilder = function (records, opts) {
    const m = opts.mapping, link = opts.link || { mode: "standalone", cruise: "" },
      sidCol = m.sample_id, latCol = opts.latCol, lonCol = opts.lonCol, refCol = opts.refCol,
      srcId = opts.srcId || "csv";
    const wilder = [];
    const sampleSet = new Map();
    records.forEach((row, i) => {
      const sid = sidCol ? row[sidCol] : null;
      if (sid == null || String(sid).trim() === "") return;
      const sidS = String(sid).trim();
      wilder.push({
        wilder_result_id: `${srcId}-W${i + 1}`,
        sample_id: sidS,
        scientific_name: m.scientific_name ? row[m.scientific_name] : null,
        common_name: m.common_name ? row[m.common_name] : null,
        taxonomic_rank: m.taxonomic_rank ? row[m.taxonomic_rank] : null,
        organism_group: m.organism_group ? row[m.organism_group] : null,
        tax_id: m.tax_id ? row[m.tax_id] : null,
        read_count: m.read_count ? row[m.read_count] : null,
        _source: srcId,
      });
      if (!sampleSet.has(sidS)) {
        sampleSet.set(sidS, {
          SAMPLE_ID: sidS, samp_name: sidS,
          "Cruise Number": link.mode === "cruise" ? link.cruise : "",
          Station: refCol && row[refCol] ? row[refCol] : "",
          decimalLatitude: latCol && row[latCol] ? row[latCol] : null,
          decimalLongitude: lonCol && row[lonCol] ? row[lonCol] : null,
          samp_category: "imported (Wilder CSV)",
          _source: srcId, _synthetic: true,
        });
      }
    });
    return { wilder, samples: [...sampleSet.values()] };
  };

  global.MBONImportLib = Lib;

  /* =========================================================
     PART 2 — DOM PANEL  (window.MBONImportPanel)
     ========================================================= */

  function el(tag, attrs, kids) {
    const n = document.createElement(tag);
    if (attrs) for (const k in attrs) {
      if (k === "class") n.className = attrs[k];
      else if (k === "html") n.innerHTML = attrs[k];
      else if (k === "text") n.textContent = attrs[k];
      else if (k.slice(0, 2) === "on" && typeof attrs[k] === "function") n.addEventListener(k.slice(2), attrs[k]);
      else if (attrs[k] != null) n.setAttribute(k, attrs[k]);
    }
    (kids || []).forEach((c) => { if (c != null) n.appendChild(typeof c === "string" ? document.createTextNode(c) : c); });
    return n;
  }

  function uid() { return "src_" + Math.random().toString(36).slice(2, 9); }

  const Panel = {};

  Panel.create = function (opts) {
    opts = opts || {};
    const XLSX = global.XLSX;
    const getCruises = opts.getCruises || (() => []);
    const onChange = opts.onChange || function () {};
    const startWidth = opts.width || 380;

    const state = {
      sources: [],          // committed sources
      open: false,
      regex: "",
      useRegex: true,
    };

    // ---------- root + launcher ----------
    const root = el("div", { class: "mip-root" });
    const launcher = el("button", { class: "mip-launcher", title: "Open data import panel",
      onclick: () => api.toggle(true) }, ["⬆ Import data ", el("span", { class: "mip-badge", text: "0" })]);
    const launcherBadge = launcher.querySelector(".mip-badge");

    const panel = el("div", { class: "mip-panel mip-hidden" });
    panel.style.width = startWidth + "px";

    // resizer (left border)
    const resizer = el("div", { class: "mip-resizer", title: "Drag to resize" });
    resizer.addEventListener("mousedown", (e) => {
      e.preventDefault();
      const startX = e.clientX, w0 = panel.getBoundingClientRect().width;
      document.body.classList.add("mip-resizing");
      function mv(ev) {
        let w = w0 + (startX - ev.clientX);
        w = Math.max(280, Math.min(window.innerWidth * 0.9, w));
        panel.style.width = w + "px";
      }
      function up() {
        document.body.classList.remove("mip-resizing");
        window.removeEventListener("mousemove", mv);
        window.removeEventListener("mouseup", up);
      }
      window.addEventListener("mousemove", mv);
      window.addEventListener("mouseup", up);
    });

    const head = el("div", { class: "mip-head" }, [
      el("h3", { text: "Data Import" }),
      el("button", { title: "Collapse", text: "✕", onclick: () => api.toggle(false) }),
    ]);

    const body = el("div", { class: "mip-body" });
    panel.appendChild(resizer);
    panel.appendChild(head);
    panel.appendChild(body);
    root.appendChild(launcher);
    root.appendChild(panel);
    (opts.mount || document.body).appendChild(root);

    // ---------- search / regex bar ----------
    const searchInput = el("input", { type: "text", placeholder: "Filter sources & rows… (regex)" });
    const rxToggle = el("button", { class: "mip-rx-toggle on", title: "Toggle regex mode", text: ".*" });
    const searchErr = el("small", {});
    rxToggle.addEventListener("click", () => {
      state.useRegex = !state.useRegex;
      rxToggle.classList.toggle("on", state.useRegex);
      renderSources();
    });
    searchInput.addEventListener("input", () => { state.regex = searchInput.value; renderSources(); });
    const searchBox = el("div", { class: "mip-search" }, [
      el("div", { class: "mip-search-row" }, [searchInput, rxToggle]),
      searchErr,
    ]);

    // ---------- drop zone ----------
    const fileInput = el("input", { type: "file", accept: ".xlsx,.xlsm,.xls,.csv,.tsv,.txt", multiple: "multiple" });
    fileInput.style.display = "none";
    fileInput.addEventListener("change", (e) => { handleFiles([...e.target.files]); fileInput.value = ""; });
    const drop = el("div", { class: "mip-drop", onclick: () => fileInput.click() }, [
      el("strong", { text: "Drop files here or click to choose" }),
      el("div", { text: "One or more files — workbooks or raw tables" }),
      el("div", { class: "mip-formats", text: ".xlsx · .xlsm · .csv · .tsv" }),
      fileInput,
    ]);
    ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("mip-over"); }));
    ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("mip-over"); }));
    drop.addEventListener("drop", (e) => { if (e.dataTransfer && e.dataTransfer.files.length) handleFiles([...e.dataTransfer.files]); });

    // ---------- sources list ----------
    const sourcesLabel = el("div", { class: "mip-label" }, [
      "Imported sources ", el("span", { class: "mip-count", text: "0" }),
    ]);
    const sourcesWrap = el("div", { class: "mip-sources" });

    body.appendChild(searchBox);
    body.appendChild(drop);
    body.appendChild(sourcesLabel);
    body.appendChild(sourcesWrap);

    // ================= file handling =================
    function readFile(file) {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onerror = () => reject(new Error("Could not read " + file.name));
        const lower = file.name.toLowerCase();
        const isText = /\.(csv|tsv|txt)$/.test(lower);
        reader.onload = (ev) => {
          try {
            if (isText) {
              const wb = XLSX.read(ev.target.result, { type: "string", raw: false });
              const first = wb.SheetNames[0];
              const rows = Lib.sheetToRows(XLSX, wb.Sheets[first]);
              resolve({ file, type: "csv", rows });
            } else {
              const wb = XLSX.read(new Uint8Array(ev.target.result), { type: "array", cellDates: true, raw: false });
              resolve({ file, type: "workbook", wb });
            }
          } catch (err) { reject(err); }
        };
        if (isText) reader.readAsText(file); else reader.readAsArrayBuffer(file);
      });
    }

    async function handleFiles(files) {
      for (const f of files) {
        try {
          const parsed = await readFile(f);
          if (parsed.type === "workbook") openWorkbookPopup(parsed);
          else openCsvPopup(parsed);
        } catch (err) {
          alert("Error reading " + f.name + ": " + err.message);
        }
      }
    }

    // ================= rendering sources =================
    function buildRegex() {
      if (!state.regex) return null;
      if (!state.useRegex) {
        const esc = state.regex.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        return new RegExp(esc, "i");
      }
      try { const r = new RegExp(state.regex, "i"); searchInput.classList.remove("mip-bad-regex"); searchErr.textContent = ""; searchErr.className = ""; return r; }
      catch (e) { searchInput.classList.add("mip-bad-regex"); searchErr.textContent = "Invalid regex: " + e.message; searchErr.className = "mip-err"; return null; }
    }

    function sourceMatches(src, rx) {
      if (!rx) return true;
      if (rx.test(src.name) || rx.test(src.fileName)) return true;
      return src.datasets.some((d) =>
        rx.test(d.role) ||
        (d.headers || []).some((h) => rx.test(String(h))) ||
        d.records.slice(0, 200).some((r) => Object.values(r).some((v) => v != null && rx.test(String(v))))
      );
    }

    function renderSources() {
      const rx = buildRegex();
      sourcesWrap.innerHTML = "";
      const list = state.sources.filter((s) => sourceMatches(s, rx));
      sourcesLabel.querySelector(".mip-count").textContent = state.sources.length;
      launcherBadge.textContent = state.sources.filter((s) => s.enabled).length;
      if (!state.sources.length) {
        sourcesWrap.appendChild(el("div", { class: "mip-empty", text: "No sources yet. Drop a file above to begin." }));
        return;
      }
      if (!list.length) {
        sourcesWrap.appendChild(el("div", { class: "mip-empty", text: "No sources match your filter." }));
        return;
      }
      list.forEach((src) => sourcesWrap.appendChild(renderSourceCard(src)));
    }

    function renderSourceCard(src) {
      const card = el("div", { class: "mip-source" + (src.enabled ? "" : " mip-disabled") });
      const nameInput = el("input", { class: "mip-sname", value: src.name, title: "Rename source" });
      nameInput.addEventListener("change", () => { src.name = nameInput.value.trim() || src.fileName; emit(); });
      const sw = el("label", { class: "mip-switch", title: "Show / hide in dashboard" });
      const cb = el("input", { type: "checkbox" }); cb.checked = src.enabled;
      cb.addEventListener("change", () => { src.enabled = cb.checked; card.classList.toggle("mip-disabled", !src.enabled); emit(); });
      sw.appendChild(cb); sw.appendChild(el("span", {}));
      const head = el("div", { class: "mip-source-head" }, [
        el("span", { class: "mip-kind-pill" + (src.kind === "csv" ? " csv" : ""), text: src.kind === "csv" ? "CSV" : "XLSX" }),
        nameInput, sw,
        el("button", { class: "mip-icon-btn", title: "Remove source", text: "🗑",
          onclick: () => { state.sources = state.sources.filter((s) => s !== src); renderSources(); emit(); } }),
      ]);
      const bodyEl = el("div", { class: "mip-source-body" });
      src.datasets.forEach((d) => {
        bodyEl.appendChild(el("div", { class: "mip-ds-row" }, [
          el("span", { class: "mip-ds-role", text: (ROLES[d.role] ? ROLES[d.role].label : d.role) }),
          el("span", { class: "mip-ds-meta", text: `${d.records.length} rows · ${d.headers.length} cols` }),
        ]));
      });
      // associate / dissociate (CSV wilder sources)
      if (src.kind === "csv" && src.csvMeta) {
        const sel = el("select", { title: "Associate these samples with a cruise" });
        sel.appendChild(el("option", { value: "", text: "— Standalone (no cruise) —" }));
        getCruises().forEach((c) => {
          const o = el("option", { value: c, text: c }); if (src.csvMeta.link.cruise === c && src.csvMeta.link.mode === "cruise") o.selected = true; sel.appendChild(o);
        });
        sel.value = src.csvMeta.link.mode === "cruise" ? src.csvMeta.link.cruise : "";
        sel.addEventListener("change", () => {
          if (sel.value) src.csvMeta.link = { mode: "cruise", cruise: sel.value };
          else src.csvMeta.link = { mode: "standalone", cruise: "" };
          rebuildCsvSource(src); renderStateLabel(); emit();
        });
        const stateLab = el("span", { class: "mip-assoc-state" });
        function renderStateLabel() {
          const linked = src.csvMeta.link.mode === "cruise";
          stateLab.textContent = linked ? "Linked → " + src.csvMeta.link.cruise : "Standalone";
          stateLab.className = "mip-assoc-state " + (linked ? "linked" : "standalone");
        }
        renderStateLabel();
        bodyEl.appendChild(el("div", { class: "mip-assoc" }, [
          el("span", { text: "Cruise:" }), sel, stateLab,
        ]));
      }
      card.appendChild(head); card.appendChild(bodyEl);
      return card;
    }

    // rebuild a CSV source's datasets/samples after a link change
    function rebuildCsvSource(src) {
      const built = Lib.buildRawWilder(src.csvMeta.records, {
        mapping: src.csvMeta.mapping, latCol: src.csvMeta.latCol, lonCol: src.csvMeta.lonCol,
        refCol: src.csvMeta.refCol, link: src.csvMeta.link, srcId: src.id,
      });
      src.datasets = [
        { role: "wilder", headers: Object.keys(built.wilder[0] || {}), records: built.wilder, sheetName: src.fileName },
        { role: "samples", headers: Object.keys(built.samples[0] || {}), records: built.samples, sheetName: "(synthesized)" },
      ];
    }

    // ================= merged output + emit =================
    function getMerged() {
      const merged = { samples: [], cruise: [], runs: [], bioinformatics: [], wilder: [], kraken2: [], taxtriage: [] };
      state.sources.filter((s) => s.enabled).forEach((s) => {
        s.datasets.forEach((d) => { if (merged[d.role]) merged[d.role] = merged[d.role].concat(d.records); });
      });
      return merged;
    }

    function emit() {
      renderSources();
      onChange({ sources: state.sources, merged: getMerged() });
    }

    // ================= WORKBOOK popup =================
    function openWorkbookPopup(parsed) {
      const analyses = Lib.analyzeWorkbook(XLSX, parsed.wb); // [{sheetName, role, ...}]
      // working copy (mutable) per sheet
      const working = analyses.map((a) => Object.assign({}, a));

      const bg = el("div", { class: "mip-modal-bg" });
      const foot = el("div", { class: "mip-modal-foot" });
      const footMsg = el("span", { class: "mip-foot-msg" });
      const importBtn = el("button", { class: "mip-btn primary", text: "Import selected tabs" });
      const cancelBtn = el("button", { class: "mip-btn ghost", text: "Cancel", onclick: () => bg.remove() });
      foot.appendChild(footMsg); foot.appendChild(cancelBtn); foot.appendChild(importBtn);

      const bodyWrap = el("div", { class: "mip-modal-body" });
      bodyWrap.appendChild(el("div", { class: "mip-note",
        html: "Each tab in this workbook is shown below with the role it was matched to. " +
          "<b>Change the role</b> if a tab is mismatched, <b>rename</b> it freely (the original sheet name isn't hard-coded), " +
          "or set it to <b>Ignore</b>. Validation explains why each tab is or isn't a good fit." }));

      function refreshFoot() {
        const importable = working.filter((w) => w.role !== "unknown" && w._ignore !== true);
        const okCount = importable.length;
        footMsg.textContent = okCount ? `${okCount} tab(s) will be imported.` : "Nothing selected to import.";
        importBtn.disabled = !okCount;
      }

      working.forEach((w) => bodyWrap.appendChild(renderTabRow(w, refreshFoot)));

      const modal = el("div", { class: "mip-modal" }, [
        el("div", { class: "mip-modal-head" }, [
          el("h3", { text: "Import workbook" }),
          el("span", { class: "mip-fname", text: parsed.file.name }),
        ]),
        bodyWrap, foot,
      ]);
      bg.appendChild(modal);
      document.body.appendChild(bg);
      refreshFoot();

      importBtn.addEventListener("click", () => {
        const datasets = working
          .filter((w) => w.role !== "unknown" && w._ignore !== true)
          .map((w) => ({ role: w.role, sheetName: w.displayName || w.sheetName, headers: w.headers, records: w.records }));
        if (!datasets.length) return;
        const src = { id: uid(), name: parsed.file.name.replace(/\.(xlsx|xlsm|xls)$/i, ""), fileName: parsed.file.name,
          kind: "workbook", enabled: true, datasets };
        state.sources.push(src);
        bg.remove();
        api.toggle(true);
        emit();
      });
    }

    function renderTabRow(w, refreshFoot) {
      const row = el("div", { class: "mip-tabrow" });
      const detail = el("div", { class: "mip-tabrow-detail" });

      const roleSel = el("select", {});
      roleSel.appendChild(el("option", { value: "ignore", text: "Ignore this tab" }));
      Lib.ROLE_KEYS.forEach((rk) => {
        const o = el("option", { value: rk, text: ROLES[rk].label }); if (w.role === rk) o.selected = true; roleSel.appendChild(o);
      });
      if (w.role === "unknown") { const o = el("option", { value: "ignore", selected: "selected", text: "Ignore this tab" }); }
      roleSel.value = (w.role === "unknown") ? "ignore" : w.role;

      const rename = el("input", { class: "mip-rename", value: w.sheetName, title: "Display name for this dataset" });
      rename.addEventListener("change", () => { w.displayName = rename.value.trim() || w.sheetName; });
      w.displayName = w.sheetName;

      const headInfo = el("div", { class: "mip-tabrow-head" }, [
        el("span", { class: "mip-tabname", text: w.sheetName }),
        el("span", { class: "mip-status-pill" }),
        el("div", { class: "mip-role-sel" }, [el("span", { text: "as" }), roleSel, el("span", { text: "named" }), rename]),
      ]);
      const statusPill = headInfo.querySelector(".mip-status-pill");

      function paint() {
        row.className = "mip-tabrow " + (w._ignore ? "ignored" : w.validation.status);
        statusPill.className = "mip-status-pill " + (w._ignore ? "ignored" : w.validation.status);
        statusPill.textContent = w._ignore ? "ignored" : w.validation.status;
        detail.innerHTML = "";
        if (w._ignore) {
          detail.appendChild(el("div", { class: "mip-valid-line", text: "This tab will be skipped." }));
          if (refreshFoot) refreshFoot();
          return;
        }
        const def = ROLES[w.role];
        if (def) detail.appendChild(el("div", { class: "mip-note", text: def.desc }));
        detail.appendChild(el("div", { class: "mip-valid-line " + w.validation.status }, [
          el("span", { class: "mip-vi", text: w.validation.status === "ok" ? "✓" : w.validation.status === "warn" ? "!" : "✕" }),
          el("span", { text: w.validation.reason }),
        ]));
        detail.appendChild(el("div", { text: `Header row detected at line ${w.headerRowIdx + 1} · ${w.records.length} data rows · ${w.headers.length} columns` }));
        // found / missing chips
        if (def) {
          const chips = el("div", { class: "mip-chips" });
          w.validation.found.forEach((t) => chips.appendChild(el("span", { class: "mip-chip found", text: pretty(t) })));
          w.validation.missing.forEach((t) => chips.appendChild(el("span", { class: "mip-chip missing", text: pretty(t) })));
          detail.appendChild(chips);
        }
        detail.appendChild(buildPreview(w.headers, w.records));
        if (refreshFoot) refreshFoot();
      }

      roleSel.addEventListener("change", () => {
        if (roleSel.value === "ignore") { w._ignore = true; }
        else {
          w._ignore = false;
          const re = Lib.reanalyzeSheet(w, roleSel.value);
          Object.assign(w, re);
        }
        paint();
      });

      row.appendChild(headInfo); row.appendChild(detail);
      if (w.role === "unknown") w._ignore = true;
      paint();
      return row;
    }

    function pretty(tok) {
      // map a normalized token back to something readable from any role sig
      return tok;
    }

    function buildPreview(headers, records) {
      const wrap = el("div", { class: "mip-preview" });
      if (!records.length) { wrap.appendChild(el("div", { class: "mip-empty", text: "No data rows." })); return wrap; }
      const cols = headers.slice(0, 8);
      const table = el("table");
      const thead = el("thead"); const trh = el("tr");
      cols.forEach((h) => trh.appendChild(el("th", { text: h })));
      if (headers.length > 8) trh.appendChild(el("th", { text: `+${headers.length - 8} more` }));
      thead.appendChild(trh); table.appendChild(thead);
      const tb = el("tbody");
      records.slice(0, 5).forEach((r) => {
        const tr = el("tr");
        cols.forEach((h) => tr.appendChild(el("td", { text: r[h] == null ? "" : String(r[h]) })));
        if (headers.length > 8) tr.appendChild(el("td", { text: "…" }));
        tb.appendChild(tr);
      });
      table.appendChild(tb); wrap.appendChild(table);
      return wrap;
    }

    // ================= CSV popup =================
    function openCsvPopup(parsed) {
      const ext = Lib.extractRecords(parsed.rows, Lib.detectHeaderAndRole(parsed.rows).headerRowIdx);
      // for flat CSV header is almost always row 0
      const headers0 = (parsed.rows[0] || []).map((h) => (h != null ? String(h).trim() : "")).filter(Boolean);
      const headers = headers0.length ? headers0 : ext.headers;
      const recExt = Lib.extractRecords(parsed.rows, 0);
      const records = recExt.records;
      const analysis = Lib.analyzeRawCsv(headers, records);
      const link = { mode: "standalone", cruise: "" };

      const bg = el("div", { class: "mip-modal-bg" });
      const bodyWrap = el("div", { class: "mip-modal-body" });
      bodyWrap.appendChild(el("div", { class: "mip-note",
        html: "This is a flat table (no cruise/run structure). It will be imported as <b>Wilder Results</b>, " +
          "keyed only by <b>Sample ID</b>. Choose whether to keep these samples standalone or associate them with a cruise — " +
          "you can change this later from the source card." }));

      // validation line
      bodyWrap.appendChild(el("div", { class: "mip-valid-line " + analysis.status }, [
        el("span", { class: "mip-vi", text: analysis.status === "ok" ? "✓" : analysis.status === "warn" ? "!" : "✕" }),
        el("span", { text: analysis.reason }),
      ]));

      // column mapping grid
      const mapping = Object.assign({}, analysis.mapping);
      const fields = [
        ["sample_id", "Sample ID  (required)"],
        ["scientific_name", "Scientific name"],
        ["common_name", "Common name"],
        ["taxonomic_rank", "Rank"],
        ["organism_group", "Group"],
        ["tax_id", "Taxon ID"],
        ["read_count", "Read / taxon count"],
      ];
      const grid = el("div", { class: "mip-mapgrid" });
      fields.forEach(([f, lbl]) => {
        const sel = el("select", {});
        sel.appendChild(el("option", { value: "", text: "— none —" }));
        headers.forEach((h) => { const o = el("option", { value: h, text: h }); if (mapping[f] === h) o.selected = true; sel.appendChild(o); });
        sel.value = mapping[f] || "";
        sel.addEventListener("change", () => { mapping[f] = sel.value; refresh(); });
        grid.appendChild(el("label", {}, [lbl, sel]));
      });
      bodyWrap.appendChild(el("div", { class: "mip-label", text: "Column mapping" }));
      bodyWrap.appendChild(grid);

      // associate / dissociate radio
      const cruiseSel = el("select", {});
      getCruises().forEach((c) => cruiseSel.appendChild(el("option", { value: c, text: c })));
      cruiseSel.disabled = true;
      const rStand = el("input", { type: "radio", name: "mip-link", checked: "checked" });
      const rCruise = el("input", { type: "radio", name: "mip-link" });
      rStand.addEventListener("change", () => { link.mode = "standalone"; cruiseSel.disabled = true; });
      rCruise.addEventListener("change", () => { link.mode = "cruise"; cruiseSel.disabled = false; link.cruise = cruiseSel.value || (getCruises()[0] || ""); });
      cruiseSel.addEventListener("change", () => { link.cruise = cruiseSel.value; });
      bodyWrap.appendChild(el("div", { class: "mip-label", text: "Cruise association" }));
      bodyWrap.appendChild(el("div", { class: "mip-radio-row" }, [
        el("label", {}, [rStand, "Dissociate — standalone samples (Sample ID only)"]),
        el("label", {}, [rCruise, "Associate with cruise:"]),
        cruiseSel,
      ]));

      const previewWrap = el("div", {});
      bodyWrap.appendChild(el("div", { class: "mip-label", text: "Preview (as Wilder Results)" }));
      bodyWrap.appendChild(previewWrap);

      const footMsg = el("span", { class: "mip-foot-msg" });
      const importBtn = el("button", { class: "mip-btn primary", text: "Import as Wilder Results" });
      const cancelBtn = el("button", { class: "mip-btn ghost", text: "Cancel", onclick: () => bg.remove() });
      const foot = el("div", { class: "mip-modal-foot" }, [footMsg, cancelBtn, importBtn]);

      function refresh() {
        const built = Lib.buildRawWilder(records, { mapping, latCol: analysis.latCol, lonCol: analysis.lonCol, refCol: analysis.refCol, link, srcId: "preview" });
        previewWrap.innerHTML = "";
        previewWrap.appendChild(buildPreview(["sample_id", "scientific_name", "common_name", "taxonomic_rank", "organism_group", "read_count"], built.wilder));
        const okay = !!mapping.sample_id && built.wilder.length > 0;
        importBtn.disabled = !okay;
        footMsg.textContent = okay
          ? `${built.wilder.length} taxon rows · ${built.samples.length} unique samples`
          : "Map a Sample ID column to continue.";
      }
      refresh();

      const modal = el("div", { class: "mip-modal" }, [
        el("div", { class: "mip-modal-head" }, [
          el("h3", { text: "Import raw table" }),
          el("span", { class: "mip-fname", text: parsed.file.name }),
        ]),
        bodyWrap, foot,
      ]);
      bg.appendChild(modal);
      document.body.appendChild(bg);

      importBtn.addEventListener("click", () => {
        const id = uid();
        const src = {
          id, name: parsed.file.name.replace(/\.(csv|tsv|txt)$/i, ""), fileName: parsed.file.name,
          kind: "csv", enabled: true, datasets: [],
          csvMeta: { records, mapping, latCol: analysis.latCol, lonCol: analysis.lonCol, refCol: analysis.refCol, link: JSON.parse(JSON.stringify(link)) },
        };
        rebuildCsvSource(src);
        state.sources.push(src);
        bg.remove();
        api.toggle(true);
        emit();
      });
    }

    // ================= public api =================
    const api = {
      el: root,
      toggle(open) {
        state.open = open == null ? !state.open : open;
        panel.classList.toggle("mip-hidden", !state.open);
        launcher.style.display = state.open ? "none" : "";
      },
      getMerged,
      getSources: () => state.sources,
      refresh: renderSources,
      addFiles: (files) => { api.toggle(true); return handleFiles([...files]); },
    };

    renderSources();
    return api;
  };

  global.MBONImportPanel = Panel;
})(window);
