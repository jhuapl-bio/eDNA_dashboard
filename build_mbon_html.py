#!/usr/bin/env python3
# ##############################################################################################
# # Copyright 2026 The Johns Hopkins University Applied Physics Laboratory LLC
# # All rights reserved.
# # Permission is hereby granted, free of charge, to any person obtaining a copy of this
# # software and associated documentation files (the "Software"), to deal in the Software
# # without restriction, including without limitation the rights to use, copy, modify,
# # merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
# # permit persons to whom the Software is furnished to do so.
# #
# # THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
# # INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR
# # PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
# # LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# # TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE
# # OR OTHER DEALINGS IN THE SOFTWARE.
# #
"""
build_mbon_html.py — build the interactive, map-based eDNA report (HTML)
=======================================================================

Reads taxonomic-hit results (Wilderlab ASV workbooks, Kraken2 reports, or
Wilderlab Samples/Taxa CSV exports) plus per-sample station metadata
(FAIRe ``sampleMetadata`` sheets) and renders a single self-contained HTML
report from ``templates/station_top10_species_map_template.html``.

Inputs are driven by ``report_manifest.xlsx`` inside the projects folder::

    data/projects/
        report_manifest.xlsx          <- one row per sample / run (see docs/DATA_FORMATS.md)
        <project>/faire/   FAIRe checklist(s)  (station coords + env metadata)
        <project>/kraken2/ Kraken2 reports     (optionally <DATABASE>/<run>/...)
        <project>/wilder/  Wilderlab WLM*.xlsx (metadata / aggregated / full sheets)
        <project>/meta/    concordance.json    (written by `ingest.py concordance`)

Every default path is RELATIVE TO THIS REPOSITORY (``data/``, ``templates/``,
``reports/``, ``logs/``) — nothing is tied to a particular machine.

Quick start::

    python build_mbon_html.py --example            # demo report from data/example/
    python build_mbon_html.py                      # data/projects -> reports/station_top10_species_map.html
    python build_mbon_html.py --validate           # check inputs only, write nothing
    python build_mbon_html.py --projects OceanX --split -o reports/all.html

Each run becomes one entry in ``mbonData.runs[]``; the template renders a Run
selector + Cross-Sample / Map / Explore / Data Table / Metadata tabs and lets
users load/remove additional files in the browser.

Problems with individual input files are logged (console + ``logs/build_mbon_html.log``)
and that file is skipped — one bad workbook never aborts the whole report.
Exit code: 0 = report written, 1 = errors (see log), 2 = bad arguments / no data.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import openpyxl
import pandas as pd

import edna_common as ec
from edna_common import InputError

__version__ = "2.0.0"

log = logging.getLogger("build_mbon_html")

# ---------------------------------------------------------------- defaults
# All relative to the repository root (see edna_common.REPO_ROOT).
DATA_DIR = ec.DATA_DIR
# Standardized per-project layout shared with ingest.py:
#   data/projects/<project_dir>/{faire,kraken2,wilder,meta}/
DATA_PROJECTS = ec.PROJECTS_DIR
TEMPLATE = ec.TEMPLATES_DIR / "station_top10_species_map_template.html"
OUTPUT = ec.REPORTS_DIR / "station_top10_species_map.html"

# Default station-metadata workbook(s) searched for when a manifest row / run
# folder names none. Glob patterns, matched inside the run folder and then the
# data folder. Override with --station-metadata (explicit file) or
# --station-metadata-glob.
STATION_METADATA_GLOBS = ["MBON_Data*.xlsx", "*sampleMetadata*.xlsx"]
STATION_METADATA_FILE = None      # explicit file from --station-metadata

# Map view used only when no station has coordinates.
DEFAULT_MAP_CENTER = [0.0, 0.0]
DEFAULT_MAP_ZOOM = 2

REPORT_TITLE = "MBON eDNA Explorer"

# sampleMetadata tooltip fields (carried through to the map popups)
SAMPLE_TOOLTIP_FIELDS = {
    # (order == display order in the map popup's per-sample tooltip)
    'samp_name': 'Sample Name', 'Station': 'Station', 'Cruise Number': 'Cruise',
    'eventDate': 'Date', 'samp_category': 'Category', 'neg_cont_type': 'Control Type',
    'Sample Depth': 'Sample Depth', 'minimumDepthInMeters': 'Min Depth (m)',
    'maximumDepthInMeters': 'Max Depth (m)', 'tot_depth_water_col': 'Water Column Depth (m)',
    'temp': 'Temperature', 'salinity': 'Salinity',
    'decimalLatitude': 'Latitude', 'decimalLongitude': 'Longitude',
    'geo_loc_name': 'Location', 'samp_collect_device': 'Collection Device',
    'filter_name': 'Filter', 'size_frac': 'Size Fraction (um)',
    'samp_store_sol': 'Storage Solution', 'Coordinate Note': 'Coordinate Note',
}
INFO_COLS = ['ScientificName', 'Rank', 'TaxID', 'CommonName', 'Group']


def _clean(v):
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, pd.Timestamp) or hasattr(v, 'strftime'):
        try:
            if getattr(v, 'hour', 0) == 0 and getattr(v, 'minute', 0) == 0:
                return v.strftime('%Y-%m-%d')
        except Exception:
            pass
    return str(v).strip().replace('\xa0', '')


def _norm_uid(v):
    """Normalize a UID-like header/value: 569063.0 -> '569063'."""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    s = str(v).strip()
    if s.endswith('.0') and s[:-2].isdigit():
        return s[:-2]
    return s


def _norm_colname(v):
    return re.sub(r'[^a-z0-9]+', '', str(v).strip().lower())


def _coerce_latlon(lat, lon, who=''):
    """(lat, lon) rounded to 5 dp (~1 m), or (None, None) when missing / non-numeric /
    out of range. Swapped coordinates are repaired (see _fix_latlon); anything
    still impossible is logged and dropped rather than plotted off the map."""
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        if lat not in (None, '') and not (isinstance(lat, float) and np.isnan(lat)):
            log.debug("non-numeric coordinates for %s: %r, %r", who, lat, lon)
        return None, None
    if np.isnan(lat) or np.isnan(lon):
        return None, None
    lat, lon = _fix_latlon(lat, lon)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        log.warning("out-of-range coordinates for %s ignored: lat=%s lon=%s", who or '?', lat, lon)
        return None, None
    if lat == 0 and lon == 0:
        log.warning("coordinates for %s are (0, 0) — treated as missing", who or '?')
        return None, None
    return round(lat, 5), round(lon, 5)


def read_aggregated(fpath):
    """Read an 'aggregated' sheet that may have a clean single-row header
    (WLM611808-style) OR a two-row header with merged ClientSampleID labels
    and empty spacer columns (WLM612422-style). Returns a DataFrame with the
    INFO_COLS plus one column per sample UID (string, no trailing .0)."""
    sheets = ec.excel_sheet_names(fpath)
    sheet = next((s for s in sheets if s.lower() == 'aggregated'), None)
    if sheet is None:
        raise InputError(f"{Path(fpath).name}: no 'aggregated' sheet (found {sheets})")
    raw = pd.read_excel(fpath, sheet_name=sheet, header=None, engine='openpyxl')
    if raw.empty:
        raise InputError(f"{Path(fpath).name}: 'aggregated' sheet is empty")
    # find the header row containing 'ScientificName'
    hdr_idx = None
    for i in range(min(8, len(raw))):
        cells = [str(c).strip().lower() for c in raw.iloc[i].tolist()]
        if 'scientificname' in cells:
            hdr_idx = i
            break
    if hdr_idx is None:
        raise InputError(f"{Path(fpath).name}: 'aggregated' sheet has no 'ScientificName' "
                         f"header in its first 8 rows")
    header = raw.iloc[hdr_idx].tolist()
    body = raw.iloc[hdr_idx + 1:].reset_index(drop=True)
    cols, keep = [], []
    for j, h in enumerate(header):
        if pd.isna(h):
            continue  # drop spacer / unnamed columns
        name = str(h).strip()
        if name in INFO_COLS:
            cols.append(name)
        else:
            cols.append(_norm_uid(h))
        keep.append(j)
    df = body.iloc[:, keep].copy()
    df.columns = cols
    for c in INFO_COLS:          # tolerate exports that drop optional columns
        if c not in df.columns:
            log.debug("%s: 'aggregated' has no %s column — filling blank", Path(fpath).name, c)
            df[c] = 'species' if c == 'Rank' else None
    df = df.dropna(subset=['ScientificName'])
    return df


# ---------------------------------------------------------------- FAIRe
def _load_faire_samplesheet(fpath):
    """Real per-sample coordinates for a FAIRe checklist live in its
    'samplesheet_v1' tab (the OceanX intake sheet), NOT in 'sampleMetadata'
    (which in these exports holds only placeholder sample_IDs and blank coords).

    Returns {sample_ID: {decimalLatitude, decimalLongitude, Station, geo_loc_name}}
    keyed by the sheet's computed sample_ID (e.g. OXR20260514-002). The sample_ID
    and coordinate cells are formulas, so we read cached values via
    data_only=True. Workbooks without a samplesheet_v1-style tab yield {}."""
    try:
        wb = openpyxl.load_workbook(fpath, data_only=True, read_only=True)
    except Exception as e:
        log.debug("samplesheet scan skipped for %s: %s", Path(fpath).name, e)
        return {}
    sheet = next((s for s in wb.sheetnames
                  if _norm_colname(s).startswith('samplesheet')), None)
    if sheet is None:
        return {}
    ws = wb[sheet]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    # term-name header row = the one that actually contains 'sample_ID'
    hdr_idx = next((i for i, r in enumerate(rows[:8])
                    if any(_norm_colname(c) == 'sampleid' for c in r if c is not None)), None)
    if hdr_idx is None:
        return {}
    hdr = {_norm_colname(c): j for j, c in enumerate(rows[hdr_idx]) if c is not None}
    c_id = hdr.get('sampleid')
    c_lat = hdr.get('decimallatitude')
    c_lon = hdr.get('decimallongitude')
    c_site = hdr.get('site')
    c_geo = hdr.get('geolocname')
    if c_id is None or c_lat is None or c_lon is None:
        return {}
    out = {}
    for r in rows[hdr_idx + 1:]:
        sid = _clean(r[c_id]) if c_id < len(r) else None
        if not sid or sid.startswith('='):
            continue
        lat = r[c_lat] if c_lat < len(r) else None
        lon = r[c_lon] if c_lon < len(r) else None
        if lat is None or lon is None:
            continue
        rec = {'decimalLatitude': lat, 'decimalLongitude': lon}
        site = _clean(r[c_site]) if c_site is not None and c_site < len(r) else None
        geo = _clean(r[c_geo]) if c_geo is not None and c_geo < len(r) else None
        if site:
            rec['Station'] = site
        if geo:
            rec['geo_loc_name'] = geo
        out.setdefault(sid, rec)   # first row per sample_ID wins
    return out


# ---------------------------------------------------------------- MBON_Data
def load_station_map_from_file(fpath):
    """samp_number -> {all sampleMetadata columns} from any MBON_Data-style
    workbook. Finds a 'sampleMetadata' sheet (or the first sheet that contains
    a samp_number column) and auto-detects the header row."""
    try:
        ec.require_file(fpath, 'station metadata workbook', ec.SPREADSHEET_EXT + ('.csv',))
        if str(fpath).lower().endswith('.csv'):
            smap = _station_map_from_frame(pd.read_csv(fpath, header=None, dtype=str), fpath)
            _fill_station_coords(smap)
            return smap
        xls = pd.ExcelFile(fpath, engine='openpyxl')
    except (InputError, Exception) as e:
        log.error("station metadata unreadable — %s", e)
        return {}
    sheet = next((s for s in xls.sheet_names if _norm_colname(s) == 'samplemetadata'), None)
    if sheet is None:
        for s in xls.sheet_names:
            head = pd.read_excel(fpath, sheet_name=s, header=None, nrows=6, engine='openpyxl')
            toks = {_norm_colname(v) for r in range(len(head))
                    for v in head.iloc[r].tolist() if pd.notna(v)}
            if 'sampnumber' in toks:
                sheet = s
                break
    if sheet is None:
        log.warning("%s: no 'sampleMetadata' sheet or samp_number column found — "
                    "no station coordinates from this file", Path(fpath).name)
        return {}
    raw = pd.read_excel(fpath, sheet_name=sheet, header=None, engine='openpyxl')
    smap = _station_map_from_frame(raw, fpath)
    # Overlay real per-sample coordinates from a FAIRe 'samplesheet_v1' tab when
    # present. OceanX checklists carry lat/lon there (keyed by the OXR...-###
    # sample_IDs that the barcoding workbook also emits), while 'sampleMetadata'
    # holds only placeholder IDs with blank coords. No-op for MBON_Data files.
    for sid, rec in _load_faire_samplesheet(fpath).items():
        if sid in smap:
            smap[sid].update({k: v for k, v in rec.items() if v is not None})
        else:
            smap[sid] = dict(rec)
    _fill_station_coords(smap)
    n_xy = sum(1 for r in smap.values()
               if r.get('decimalLatitude') not in (None, '') and r.get('decimalLongitude') not in (None, ''))
    log.info("station metadata %s: %d sample(s), %d with coordinates",
             Path(fpath).name, len(smap), n_xy)
    if smap and not n_xy:
        log.warning("%s: no sample has decimalLatitude/decimalLongitude — nothing can be "
                    "placed on the map from this file", Path(fpath).name)
    return smap


def _station_map_from_frame(raw, fpath):
    """Header-detecting core of load_station_map_from_file (raw = header=None frame)."""
    if raw is None or raw.empty:
        log.warning("%s: sample metadata sheet is empty", Path(fpath).name)
        return {}
    # header row is not consistent across files (some exports use row 0, older
    # ones use row 2; the FAIRe checklist uses row 2 under requirement/section
    # rows). Auto-detect the best header row.
    expected = {'sampnumber', 'sampname', 'sampleid', 'station',
                'decimallatitude', 'decimallongitude'}
    max_scan = min(6, len(raw))
    best_idx, best_score = 0, -1
    for i in range(max_scan):
        tokens = {_norm_colname(v) for v in raw.iloc[i].tolist() if pd.notna(v)}
        score = len(tokens & expected)
        if score > best_score:
            best_idx, best_score = i, score
    headers = [str(h).strip() if pd.notna(h) else '' for h in raw.iloc[best_idx].tolist()]
    body = raw.iloc[best_idx + 1:].reset_index(drop=True)
    body.columns = headers
    # Key column: a Wilder/MBON 'samp_number' when present, otherwise the FAIRe
    # checklist's 'sample_ID'. This lets one loader serve both MBON_Data-style
    # workbooks and FAIRe_checklist sampleMetadata sheets — the universal
    # sample_ID emitted for barcoded runs then joins straight to FAIRe coords.
    keycol = (next((h for h in headers if _norm_colname(h) == 'sampnumber'), None)
              or next((h for h in headers if _norm_colname(h) == 'sampleid'), None))
    if keycol is None:
        log.warning("%s: sample metadata has no samp_number / sample_ID column "
                    "(header row %d: %s)", Path(fpath).name, best_idx, [h for h in headers if h][:12])
        return {}
    body = body.loc[:, [c for c in body.columns if c]]          # drop blank headers
    body = body.loc[:, ~pd.Index(body.columns).duplicated()]   # first of duplicate headers wins
    smap, dupes = {}, 0
    for _, row in body.iterrows():
        snum = str(row.get(keycol, '')).strip()
        if not snum or snum == 'nan':
            continue
        if snum in smap:
            dupes += 1
        smap[snum] = {c: _clean(row.get(c)) for c in body.columns}
    if dupes:
        log.warning("%s: %d duplicate %s value(s) — last occurrence kept",
                    Path(fpath).name, dupes, keycol)
    return smap


_STATION_CACHE = {}


def _num_or_none(v):
    try:
        f = float(str(v).strip().replace('\xa0', ''))
        return None if np.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _fill_station_coords(smap):
    """A sample row with a Station but blank decimalLatitude/Longitude inherits
    the most common coordinates recorded for that same Station elsewhere in the
    sheet (e.g. a field blank logged without a position). The record is tagged
    with a 'Coordinate Note' so the dashboard shows the position was inferred.
    Rows with their own coordinates are never touched."""
    votes = {}
    for rec in smap.values():
        st = (rec.get('Station') or '').strip()
        lat, lon = _num_or_none(rec.get('decimalLatitude')), _num_or_none(rec.get('decimalLongitude'))
        if st and lat is not None and lon is not None:
            v = votes.setdefault(st, {})
            v[(lat, lon)] = v.get((lat, lon), 0) + 1
    modal = {st: max(v.items(), key=lambda kv: kv[1])[0] for st, v in votes.items()}
    for rec in smap.values():
        st = (rec.get('Station') or '').strip()
        if not st or st not in modal:
            continue
        if (_num_or_none(rec.get('decimalLatitude')) is None
                or _num_or_none(rec.get('decimalLongitude')) is None):
            rec['decimalLatitude'], rec['decimalLongitude'] = modal[st]
            rec['Coordinate Note'] = f'No coordinates in sheet; using Station {st} position'


def load_mbon_station_map(folder):
    """Default station map used when no metadata file is named explicitly:
    --station-metadata if given, else the first workbook matching
    STATION_METADATA_GLOBS in `folder`, then in the data folder."""
    if STATION_METADATA_FILE:
        cand = [Path(STATION_METADATA_FILE)]
    else:
        cand = []
        for root in (Path(folder), DATA_DIR):
            if not root or not Path(root).is_dir():
                continue
            for pat in STATION_METADATA_GLOBS:
                cand += sorted(c for c in Path(root).glob(pat) if not ec.is_temp_file(c))
            if cand:
                break
    if not cand:
        log.debug("no default station-metadata workbook in %s", ec.rel(folder))
        return {}, None
    key = Path(cand[0]).resolve()
    if key not in _STATION_CACHE:
        _STATION_CACHE[key] = load_station_map_from_file(cand[0])
    return _STATION_CACHE[key], cand[0]


def _loose_key(v):
    """Loose match key: lowercase, alphanumerics only. So 'Miseq_Run_A',
    'Miseq_RunA' and 'miseq run a' all collapse to 'miseqruna'."""
    return re.sub(r'[^a-z0-9]+', '', str(v).lower())


def _zero_key(v):
    """Like _loose_key, but also drops leading zeros inside every digit run, so
    report names such as 'MBON3_024_A_071825' or 'T1C_9_A_112425' still match a
    sheet samp_name 'MBON3_24_A_071825' / 'T1C_09_A_112425'."""
    return _loose_key(re.sub(r'\d+', lambda m: str(int(m.group(0))), str(v)))


def _zero_index(station_map):
    """zero-insensitive key -> samp_number over samp_number + samp_name only
    (NOT Station, so 'MBON1' can't collide with samp_number 'MBON0001').
    Ambiguous keys map to None and are ignored."""
    idx = {}
    for num, rec in station_map.items():
        for v in {num, (rec.get('samp_name') or '').strip()}:
            if not v:
                continue
            k = _zero_key(v)
            if k in idx and idx[k] != num:
                idx[k] = None
            else:
                idx[k] = num
    return idx


def _name_station_indexes(station_map):
    """Build samp_name->samp_number and Station->samp_number lookups, plus a
    loose (case/punctuation-insensitive) index over samp_number, samp_name and
    Station so trivial formatting differences don't drop a sample."""
    name_to_num, station_to_num, loose_index = {}, {}, {}
    for num, rec in station_map.items():
        loose_index.setdefault(_loose_key(num), num)
        nm = (rec.get('samp_name') or '').strip()
        if nm:
            name_to_num[nm] = num
            loose_index.setdefault(_loose_key(nm), num)
        st = (rec.get('Station') or '').strip()
        if st:
            station_to_num.setdefault(st, num)
            loose_index.setdefault(_loose_key(st), num)
    return name_to_num, station_to_num, loose_index


def tooltip_meta(sid, station_map):
    m = station_map.get(sid, {})
    out = {}
    for col, label in SAMPLE_TOOLTIP_FIELDS.items():
        val = m.get(col)
        if val is None:
            continue
        try:
            f = float(val); out[label] = int(f) if f == int(f) else round(f, 5)
        except (ValueError, TypeError):
            out[label] = val
    return out


# ---------------------------------------------------------------- Wilder_01
def load_wilder01(folder, run_id, label, files=None):
    xlsx_files = files if files is not None else \
        sorted([p for p in folder.glob("WLM*.xlsx") if not ec.is_temp_file(p)])
    # Validate every workbook up front; drop the ones that can't be used.
    good = []
    for fpath in xlsx_files:
        try:
            ec.require_file(fpath, 'Wilder ASV workbook', ec.SPREADSHEET_EXT)
            ec.require_sheets(fpath, ['metadata', 'aggregated'], 'Wilder ASV workbook')
            good.append(fpath)
        except InputError as e:
            log.error("skipping %s", e)
    xlsx_files = good
    if not xlsx_files:
        return None
    station_map, mbon_path = load_mbon_station_map(folder)

    uid_to_sample, sample_coords, samples = {}, {}, []
    job_meta = {}
    sources = []
    for fpath in xlsx_files:
        file_sample_count = 0
        meta = pd.read_excel(fpath, sheet_name='metadata', header=None, engine='openpyxl')
        # locate header row (UID + ClientSampleID)
        hdr_idx = None
        for i in range(len(meta)):
            rowl = [str(c).strip().lower() for c in meta.iloc[i].tolist()]
            if 'uid' in rowl and 'clientsampleid' in rowl:
                hdr_idx = i; break
        if hdr_idx is None:
            log.warning("%s: 'metadata' sheet has no header row with UID + ClientSampleID — "
                        "sample coordinates/IDs from this workbook are unavailable", fpath.name)
            continue
        # job-level metadata (rows above header, key:value pairs)
        for r in range(hdr_idx):
            row = meta.iloc[r]
            k = _clean(row.iloc[0]); v = _clean(row.iloc[1])
            if k and v:
                job_meta.setdefault(k.rstrip(':'), v)
        hdr = meta.iloc[hdr_idx]
        sub = meta.iloc[hdr_idx + 1:].reset_index(drop=True)
        sub.columns = hdr.values
        sub = sub.dropna(subset=['UID'])
        # Some exports (e.g., WLM612422) store a single run label in
        # ClientSampleID for every row. In that case we must key samples by UID
        # so joins to aggregated columns and MBON samp_number values can work.
        use_uid_as_sid = False
        if 'ClientSampleID' in sub.columns:
            sid_values = [(_clean(v) or '') for v in sub['ClientSampleID'].tolist()]
            sid_values = [v for v in sid_values if v and v.lower() != 'nan']
            uid_values = [(_norm_uid(v) or '') for v in sub['UID'].tolist()]
            uid_values = [v for v in uid_values if v and v.lower() != 'nan']
            if len(set(sid_values)) <= 1 and len(set(uid_values)) > 1:
                use_uid_as_sid = True
        for _, row in sub.iterrows():
            uid = _norm_uid(row['UID'])
            sid_raw = _clean(row['ClientSampleID']) if 'ClientSampleID' in sub.columns else None
            sid = uid if use_uid_as_sid else (sid_raw if sid_raw else uid)
            uid_to_sample[uid] = sid
            lat, lon = _coerce_latlon(row.get('Latitude'), row.get('Longitude'), sid)
            if lat is not None:
                sample_coords[sid] = (lat, lon)
            full_meta = {str(c): _clean(row[c]) for c in sub.columns if str(c) != 'nan'}
            station_id = (station_map.get(sid) or station_map.get(uid) or {}).get('Station')
            samples.append({'sampleId': sid, 'uid': uid, 'station': station_id,
                            'lat': lat, 'lon': lon, 'meta': {k: v for k, v in full_meta.items() if v is not None}})
            file_sample_count += 1

        # record this workbook as a data source (sheets actually present)
        try:
            sheet_names = pd.ExcelFile(fpath, engine='openpyxl').sheet_names
        except Exception:
            sheet_names = []
        known = [s for s in sheet_names if s.lower() in ('metadata', 'aggregated', 'full')]
        sources.append({'file': fpath.name, 'sheets': known or sheet_names,
                        'role': 'ASV workbook (Wilder_01)', 'samples': file_sample_count})

    if mbon_path is not None:
        sources.append({'file': mbon_path.name, 'sheets': ['sampleMetadata'],
                        'role': 'Station metadata (MBON_Data)', 'samples': len(station_map)})

    # aggregated species matrix across all workbooks (layout-robust)
    all_agg = []
    for fpath in xlsx_files:
        try:
            df = read_aggregated(fpath)
        except (InputError, Exception) as e:
            log.error("%s: cannot read 'aggregated' sheet — %s", fpath.name, e)
            continue
        uid_cols = [c for c in df.columns if c not in INFO_COLS]
        df = df.rename(columns={c: uid_to_sample.get(_norm_uid(c), _norm_uid(c)) for c in uid_cols})
        all_agg.append(df)
    if not all_agg:
        log.error("run %s: none of %d workbook(s) had a readable 'aggregated' sheet",
                  run_id, len(xlsx_files))
        return None
    combined = pd.concat(all_agg, axis=0, ignore_index=True)
    sample_cols = [c for c in combined.columns if c not in INFO_COLS]
    combined[sample_cols] = combined[sample_cols].apply(pd.to_numeric, errors='coerce').fillna(0)
    combined['Rank'] = combined['Rank'].astype(str).str.strip()

    # species -> target (from 'full' sheets)
    species_target = {}
    for fpath in xlsx_files:
        try:
            full_df = pd.read_excel(fpath, sheet_name='full', engine='openpyxl')
        except Exception as e:
            log.debug("%s: no usable 'full' sheet (assay targets unavailable): %s", fpath.name, e)
            continue
        low = {str(c).strip().lower(): c for c in full_df.columns}
        sp_c = next((low[c] for c in ['scientificname', 'species', 'name'] if c in low), None)
        tg_c = next((low[c] for c in ['target', 'assaytarget', 'target name'] if c in low), None)
        if not sp_c or not tg_c:
            continue
        for _, row in full_df[[sp_c, tg_c]].dropna().iterrows():
            species_target.setdefault(str(row[sp_c]).strip(), []).append(str(row[tg_c]).strip())
    species_target = {s: pd.Series(v).mode().iat[0] for s, v in species_target.items() if v}

    species_only = combined[combined['Rank'] == 'species'].copy()
    group_map = (species_only[['ScientificName', 'Group']].dropna(subset=['ScientificName'])
                 .groupby('ScientificName')['Group']
                 .agg(lambda s: s.mode().iat[0] if not s.mode().empty else s.iloc[0]).to_dict())

    # nicer label from job metadata when available; only tie to a single
    # JobID when this run came from exactly one workbook.
    acct, job = job_meta.get('AccountName'), job_meta.get('JobID')
    if acct:
        label = acct + (f" (Job {job})" if (job and len(xlsx_files) == 1) else "")
    elif job and len(xlsx_files) == 1:
        label = f"Job {job}"

    run = build_run_from_matrix(species_only, sample_coords, station_map, samples,
                                group_map, species_target, run_id, label, job_meta, kind='Wilder_01')
    run['sources'] = sources
    return run


def build_run_from_matrix(species_only, sample_coords, station_map, samples,
                          group_map, species_target, run_id, label, job_meta, kind):
    """Shared station/taxa builder; species_only has ScientificName + per-sample cols."""
    valid_cols = lambda sids: [s for s in sids if s in species_only.columns]

    # group samples into stations by coordinate
    station_samples = {}
    for sid, coord in sample_coords.items():
        station_samples.setdefault(coord, []).append(sid)

    # taxa aggregates (across all samples)
    taxa = {}
    sp_matrix_all = species_only.groupby('ScientificName')[
        [c for c in species_only.columns if c not in INFO_COLS]].sum()
    for sp, row in sp_matrix_all.iterrows():
        per = {str(s): int(v) for s, v in row.items() if v and v > 0}
        if not per:
            continue
        taxa[sp] = {'species': sp, 'group': group_map.get(sp, 'Unknown') or 'Unknown',
                    'rank': 'species', 'target': species_target.get(sp, 'Unknown'),
                    'reads': int(sum(per.values())), 'perSample': per, 'stationCount': 0}

    stations = []
    if station_samples:
        totals = {}
        for coord, sids in station_samples.items():
            vc = valid_cols(sids)
            mat = species_only.groupby('ScientificName')[vc].sum() if vc else pd.DataFrame()
            mat = mat[mat.sum(axis=1) > 0] if not mat.empty else mat
            totals[coord] = int(mat.sum().sum()) if not mat.empty else 0
        max_reads = max(totals.values()) if totals else 1
        for coord, sids in sorted(station_samples.items()):
            vc = valid_cols(sids)
            if not vc:
                continue
            mat = species_only.groupby('ScientificName')[vc].sum()
            mat = mat[mat.sum(axis=1) > 0]
            if mat.empty:
                continue
            sums = mat.sum(axis=1).sort_values(ascending=False)
            total = int(sums.sum())
            mbon_ids = sorted({(station_map.get(s) or {}).get('Station') for s in vc
                               if (station_map.get(s) or {}).get('Station')})
            station_id_str = ', '.join(mbon_ids) if mbon_ids else None
            sample_meta_lookup = {s: tooltip_meta(s, station_map) for s in vc if tooltip_meta(s, station_map)}
            # Per-group, per-sample read distribution so clicking a Group cell in
            # the popup/dock renders the group's combined distribution (previously
            # this was always empty -> "No hits in any sample").
            grp_index = [group_map.get(sp, 'Unknown') or 'Unknown' for sp in mat.index]
            group_mat = mat.groupby(grp_index).sum()
            group_dist_map = {
                g: [{'sample': str(s), 'reads': int(r)} for s, r in group_mat.loc[g].items() if r > 0]
                for g in group_mat.index
            }
            rows, groups_present = [], set()
            for rank_i, (sp, reads) in enumerate(sums.items(), 1):
                grp = group_map.get(sp, 'Unknown') or 'Unknown'
                groups_present.add(grp)
                rc = mat.loc[sp]
                dist = [{'sample': str(s), 'reads': int(r)} for s, r in rc.items() if r > 0]
                rows.append({'rank': rank_i, 'species': sp, 'group': grp,
                             'target': species_target.get(sp, 'Unknown'),
                             'percent': float(reads) / total * 100 if total else 0.0,
                             'reads': int(reads), 'sampleHits': [d['sample'] for d in dist],
                             'speciesDist': dist, 'groupDist': group_dist_map.get(grp, [])})
                if sp in taxa:
                    taxa[sp]['stationCount'] += 1
            stations.append({'lat': float(coord[0]), 'lon': float(coord[1]),
                             'sampleCount': len(vc), 'totalReads': total,
                             'radius': 8 + 17 * (totals[coord] / max_reads),
                             'tooltip': (f"Station {station_id_str} ({coord[0]}, {coord[1]})"
                                         if station_id_str else f"Station ({coord[0]}, {coord[1]})"),
                             'stationId': station_id_str, 'groups': sorted(groups_present),
                             'targets': sorted({r['target'] for r in rows if r['target']}),
                             'sampleMeta': sample_meta_lookup, 'rows': rows})

    warning = None if stations else (
        "No mappable coordinates for this run — taxa available in charts/table only.")
    return {'id': run_id, 'label': label, 'builtin': True, 'kind': kind,
            'stations': stations, 'samples': samples,
            'taxa': list(taxa.values()), 'meta': job_meta, 'warning': warning,
            'sources': []}


# ---------------------------------------------------------------- Wilder_02
def _read_any(path):
    """Read a CSV/TSV/TXT/XLSX table, sniffing the delimiter for text files.
    Raises InputError with a readable message on failure."""
    ec.require_file(path, 'table', ec.TABULAR_EXT)
    try:
        if str(path).lower().endswith(('.csv', '.tsv', '.txt')):
            return pd.read_csv(path, sep=None, engine='python', encoding_errors='replace')
        return pd.read_excel(path, engine='openpyxl')
    except Exception as e:
        raise InputError(f"cannot parse {Path(path).name}: {type(e).__name__}: {e}") from e


def load_wilder02(folder, run_id, label):
    samp_files = [p for p in folder.iterdir() if 'sample' in p.name.lower()
                  and not p.name.startswith('~$') and p.suffix.lower() in ('.csv', '.tsv', '.txt', '.xlsx')]
    taxa_files = [p for p in folder.iterdir() if 'taxa' in p.name.lower()
                  and not p.name.startswith('~$') and p.suffix.lower() in ('.csv', '.tsv', '.txt', '.xlsx')]
    if not samp_files or not taxa_files:
        return None
    samp_file = sorted(samp_files)[0]
    taxa_file = sorted(taxa_files)[0]
    if len(samp_files) > 1 or len(taxa_files) > 1:
        log.warning("%s: several Samples/Taxa files found — using %s and %s",
                    folder.name, samp_file.name, taxa_file.name)
    try:
        sdf = _read_any(samp_file)
        tdf = _read_any(taxa_file)
        ec.require_columns(tdf.columns, ['TaxID', 'Rank', 'Name'], f'Taxa file {taxa_file.name}')
    except InputError as e:
        log.error("Wilder_02 run %s skipped — %s", run_id, e)
        return None
    if not any(c in sdf.columns for c in ('ClientSampleID', 'UID', 'SID')):
        log.warning("%s: Samples file has none of ClientSampleID / UID / SID — sample IDs "
                    "will be blank", samp_file.name)

    # optional detection/records table: has SID + TaxID + a count column
    rec_df = None
    rec_file = None
    for p in folder.iterdir():
        if p.name.startswith('~$') or p in samp_files or p in taxa_files:
            continue
        if p.suffix.lower() not in ('.csv', '.tsv', '.txt', '.xlsx'):
            continue
        try:
            d = _read_any(p)
        except (InputError, Exception) as e:
            log.debug("%s: not a detections table (%s)", p.name, e)
            continue
        cl = [str(c).lower() for c in d.columns]
        if 'sid' in cl and 'taxid' in cl:
            rec_df = d; rec_file = p; break

    samples = []
    for _, s in sdf.iterrows():
        lat, lon = _coerce_latlon(s.get('Latitude'), s.get('Longitude'), s.get('SID'))
        sid = str(s.get('ClientSampleID') or s.get('UID') or s.get('SID') or '').strip()
        meta = {k: _clean(v) for k, v in s.items() if _clean(v) is not None}
        samples.append({'sampleId': sid or str(s.get('SID')), 'uid': str(s.get('UID', '')),
                        'sid': str(s.get('SID', '')), 'station': None,
                        'lat': lat, 'lon': lon, 'meta': meta})

    job_meta = {'Source': 'Wilder_02 (separated Samples + Taxa export)',
                'Samples file': sorted(samp_files)[0].name, 'Taxa file': sorted(taxa_files)[0].name,
                'Sample count': str(len(sdf)), 'Taxonomy entries': str(len(tdf))}
    if 'AccountName' in sdf.columns and len(sdf):
        job_meta['AccountName'] = str(sdf.iloc[0]['AccountName'])
    if 'JobID' in sdf.columns and len(sdf):
        job_meta['JobID'] = str(sdf.iloc[0]['JobID'])

    sources = [
        {'file': samp_file.name, 'sheets': ['Samples'],
         'role': 'Sample metadata (Wilder_02)', 'samples': len(sdf)},
        {'file': taxa_file.name, 'sheets': ['Taxa'],
         'role': 'Taxonomy reference (Wilder_02)', 'samples': 0},
    ]
    if rec_file is not None:
        sources.append({'file': rec_file.name, 'sheets': ['Detections'],
                        'role': 'Per-sample detections (SID, TaxID, reads)', 'samples': 0})

    if rec_df is not None:
        # BUILD the matrix (mimic Wilder_01 aggregated): pivot records -> species x sample
        tax_by_id = {str(r['TaxID']): r for _, r in tdf.iterrows()}
        cl = {c.lower(): c for c in rec_df.columns}
        count_col = next((cl[c] for c in cl if re.search('read|count|abund', c)), None)
        sid_to_sample = {s['sid']: s['sampleId'] for s in samples}
        rows = []
        for _, rec in rec_df.iterrows():
            tax = tax_by_id.get(str(rec[cl['taxid']]))
            if tax is None or str(tax.get('Rank', '')).lower() != 'species':
                continue
            sp = str(tax.get('Name', '')).strip()
            sid = sid_to_sample.get(str(rec[cl['sid']]), str(rec[cl['sid']]))
            cnt = float(rec[count_col]) if count_col else 1.0
            rows.append({'ScientificName': sp, 'Group': tax.get('Group', 'Unknown'), 'sample': sid, 'reads': cnt})
        long_df = pd.DataFrame(rows)
        if long_df.empty:
            return _w02_metadata_only(run_id, label, samples, job_meta, tdf, sources)
        pivot = long_df.pivot_table(index=['ScientificName', 'Group'], columns='sample',
                                    values='reads', aggfunc='sum', fill_value=0).reset_index()
        pivot['Rank'] = 'species'
        sample_coords = {s['sampleId']: (s['lat'], s['lon']) for s in samples if s['lat'] is not None}
        group_map = dict(zip(pivot['ScientificName'], pivot['Group']))
        run = build_run_from_matrix(pivot, sample_coords, {}, samples, group_map, {},
                                    run_id, label, job_meta, kind='Wilder_02')
        run['sources'] = sources
        return run

    return _w02_metadata_only(run_id, label, samples, job_meta, tdf, sources)


def _w02_metadata_only(run_id, label, samples, job_meta, tdf, sources=None):
    warning = ("Wilder_02 export contains Samples + Taxa but no per-sample detection/read table "
               "(SID, TaxID, reads). Sample metadata is loaded; taxonomic hit charts & map need a "
               "detections file dropped into this Run folder.")
    return {'id': run_id, 'label': label, 'builtin': True, 'kind': 'Wilder_02',
            'stations': [], 'samples': samples, 'taxa': [], 'meta': job_meta, 'warning': warning,
            'sources': sources or []}


# ---------------------------------------------------------------- Kraken2
# A Kraken2 report is a 6-column TSV (one file == one sample):
#   0 pct  1 clade_reads  2 direct_reads  3 rank_code  4 taxid  5 indented_name
# We normally take rank-code "S" rows (species) using clade reads (covers S1
# subspecies). Some assays/reference databases (e.g. short 16S amplicons
# classified against a genus-resolution database) never assign any read to a
# species-rank node — every read stops at Genus or higher. Filtering strictly
# on 'S' in that case silently produces zero taxa/zero reads for the whole
# run. To avoid that, we pick the finest rank that actually has classified
# reads in *this* report (S, else G, else F, else O, else C, else P, else K,
# else D) and use that as the report's effective taxon level. Names pulled
# from a coarser-than-species rank get a "(genus)"/"(family)"/etc. suffix so
# they aren't mistaken for real species IDs downstream.
KRAKEN_GLOBS = ("*.kraken2.report", "*.kraken2.report.txt", "*.kreport", "*.k2report")

# Rank-code -> human label, in order of preference (finest first). 'S1' etc.
# subspecies-style codes are treated as belonging to their base rank ('S').
_RANK_FALLBACK = [('S', None), ('G', 'genus'), ('F', 'family'), ('O', 'order'),
                  ('C', 'class'), ('P', 'phylum'), ('K', 'kingdom'), ('D', 'domain')]


def _split_common(name):
    """'Tursiops truncatus (bottlenose dolphin)' -> ('Tursiops truncatus', 'bottlenose dolphin')."""
    m = re.match(r'^(.*?)\s*\(([^()]*)\)\s*$', name)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return name.strip(), None


def _parse_kraken2_rows(fpath):
    """Read a Kraken2 report into a flat list of
    {rank, base_rank, name, taxid, reads, stack} rows (one per line), where
    `stack` is the lineage of (rank_code, name) tuples from root to this row.
    `base_rank` collapses subspecies-style codes like 'S1'/'G2' to 'S'/'G'."""
    rows = []
    stack = []  # [(indent_depth, rank_code, name)]
    bad = 0
    try:
        ec.require_file(fpath, 'Kraken2 report')
        fh = open(fpath, encoding='utf-8', errors='replace')
    except (InputError, OSError) as e:
        log.error("Kraken2 report skipped — %s", e)
        return rows
    with fh:
        for line in fh:
            if not line.strip():
                continue
            if line.startswith('#'):
                continue
            parts = line.rstrip('\n').split('\t')
            if len(parts) < 6:
                bad += 1
                continue
            try:
                clade = float(parts[1]); direct = float(parts[2])
            except ValueError:
                bad += 1
                continue
            # Kraken2 writes either the standard 6-column report
            #   pct, clade_reads, direct_reads, rank, taxid, name
            # or, with --report-minimizer-data, an 8-column variant
            #   pct, clade_reads, direct_reads, clade_minimizers,
            #   distinct_minimizers, rank, taxid, name
            # Detect which by finding the rank-code column (a letter code such
            # as U/R/D/K/P/C/O/F/G/S, optionally with a depth digit) rather
            # than assuming a fixed offset.
            ri = None
            for i in (3, 5):
                if i + 2 < len(parts) and re.fullmatch(
                        r'[URDKPCOFGS-]\d*', parts[i].strip()):
                    ri = i
                    break
            if ri is None:
                continue
            rank = parts[ri].strip()
            taxid = parts[ri + 1].strip()
            raw_name = parts[ri + 2]
            depth = (len(raw_name) - len(raw_name.lstrip(' '))) // 2
            name = raw_name.strip()
            while stack and stack[-1][0] >= depth:
                stack.pop()
            stack.append((depth, rank, name))
            reads = int(clade) if clade else int(direct)
            base_rank = rank[:1] if rank else ''
            rows.append({'rank': rank, 'base_rank': base_rank, 'name': name,
                        'taxid': taxid, 'reads': reads, 'stack': list(stack)})
    if not rows:
        log.warning("%s: no parsable Kraken2 report lines (expected tab-separated "
                    "pct/clade/direct/rank/taxid/name) — is this really a Kraken2 report?",
                    Path(fpath).name)
    elif bad:
        log.debug("%s: %d malformed line(s) ignored", Path(fpath).name, bad)
    return rows


def parse_kraken2_report(fpath):
    """Parse one Kraken2 report -> list of
    {species, commonName, group, taxid, reads, isEuk}, taken from the finest
    taxonomic rank that has any classified reads in this particular report
    (see _RANK_FALLBACK)."""
    rows = _parse_kraken2_rows(fpath)
    target_rank, suffix = None, None
    for code, label in _RANK_FALLBACK:
        if any(r['base_rank'] == code and r['reads'] > 0 for r in rows):
            target_rank, suffix = code, label
            break
    if target_rank is None:
        return []
    out = []
    for r in rows:
        if r['base_rank'] != target_rank or r['reads'] <= 0:
            continue
        anc = {}
        for _, rc, nm in r['stack']:
            anc.setdefault(rc[:1] if rc else '', nm)  # first (highest) occurrence per rank code
        group = anc.get('C') or anc.get('P') or anc.get('K') or anc.get('D') or 'Unknown'
        is_euk = any(nm == 'Eukaryota' for _, _, nm in r['stack'])
        sci, common = _split_common(r['name'])
        if suffix:
            sci = f'{sci} ({suffix})'
        # lineage names (domain -> genus) for the organism-icon matcher
        lineage = [nm for _, rc, nm in r['stack'][:-1] if rc and rc[:1] in 'DKPCOFG']
        out.append({'species': sci, 'commonName': common, 'group': group,
                    'taxid': r['taxid'], 'lineage': lineage,
                    'reads': r['reads'], 'isEuk': bool(is_euk)})
    return out


def load_kraken2(folder, run_id, label, files=None):
    """Build a kind='kraken2' run from every Kraken2 report in `folder`
    (each report == one sample). Coordinates/Station are linked from an
    a station-metadata workbook (FAIRe sampleMetadata) when the report's sample id matches samp_number/
    samp_name; otherwise the run loads taxa/table-only (rich linking is the
    in-browser 'assign sample' dialog)."""
    if files is None:
        files = []
        for pat in KRAKEN_GLOBS:
            files += [p for p in folder.glob(pat) if not p.name.startswith('~$')]
        files = sorted(set(files))
    if not files:
        return None
    station_map, mbon_path = load_mbon_station_map(folder)
    # samp_name -> samp_number, and Station -> samp_number, for filename matching
    name_to_num, station_to_num = {}, {}
    for num, rec in station_map.items():
        nm = (rec.get('samp_name') or '').strip()
        if nm:
            name_to_num[nm] = num
        st = (rec.get('Station') or '').strip()
        if st:
            station_to_num.setdefault(st, num)

    def strip_ext(stem):
        s = stem
        if s.lower().endswith('.txt'):
            s = s[:-4]
        for suff in ('.kraken2.report', '.kraken2', '.kreport', '.k2report', '.report'):
            if s.endswith(suff):
                s = s[: -len(suff)]
                break
        return s

    samples, sample_coords = [], {}
    sp_meta = {}            # species -> {commonName, group, isEuk}
    per_sample_reads = {}   # sampleId -> {species: reads}
    sources = []
    for fpath in files:
        sid = strip_ext(fpath.name)
        recs = parse_kraken2_report(fpath)
        if not recs:
            sources.append({'file': fpath.name, 'sheets': ['kraken2 report'],
                            'role': 'Kraken2 report (no species rows)', 'samples': 1})
            continue
        # link coordinates via MBON metadata (samp_number, samp_name, or Station)
        snum = (sid if sid in station_map else
                name_to_num.get(sid) or station_to_num.get(sid))
        m = station_map.get(snum, {}) if snum else {}
        lat, lon = _coerce_latlon(m.get('decimalLatitude'), m.get('decimalLongitude'), snum or '?')
        if lat is not None:
            sample_coords[sid] = (lat, lon)
        reads_map = {}
        for r in recs:
            reads_map[r['species']] = reads_map.get(r['species'], 0) + r['reads']
            sp_meta.setdefault(r['species'], {'commonName': r['commonName'],
                                              'group': r['group'], 'isEuk': r['isEuk'],
                                              'taxid': r.get('taxid'), 'lineage': r.get('lineage')})
        per_sample_reads[sid] = reads_map
        samples.append({'sampleId': sid, 'uid': sid, 'station': m.get('Station'),
                        'lat': lat, 'lon': lon,
                        'meta': {k: v for k, v in (m or {}).items() if v is not None} or
                                {'Source file': fpath.name, 'Assay': 'Kraken2'}})
        sources.append({'file': fpath.name, 'sheets': ['kraken2 report'],
                        'role': 'Kraken2 report (1 sample)', 'samples': 1})
    if mbon_path is not None:
        sources.append({'file': mbon_path.name, 'sheets': ['sampleMetadata'],
                        'role': 'Station metadata (MBON_Data)', 'samples': len(station_map)})

    # species x sample matrix (mirrors the Wilder_01 'aggregated' pivot)
    sids = [s['sampleId'] for s in samples]
    species_list = sorted(sp_meta.keys())
    pivot = pd.DataFrame({'ScientificName': species_list})
    for sid in sids:
        pivot[sid] = [per_sample_reads.get(sid, {}).get(sp, 0) for sp in species_list]
    group_map = {sp: sp_meta[sp]['group'] for sp in species_list}

    run = build_run_from_matrix(pivot, sample_coords, station_map, samples,
                                group_map, {}, run_id, label,
                                {'Source': 'Kraken2 reports', 'Reports': str(len(files))},
                                kind='kraken2')
    # annotate taxa with commonName / Eukaryota flag so the template can filter
    has_euk = False
    for t in run['taxa']:
        meta = sp_meta.get(t['species'], {})
        t['commonName'] = meta.get('commonName')
        t['isEuk'] = bool(meta.get('isEuk'))
        if meta.get('taxid'):
            t['taxid'] = str(meta['taxid'])
        if meta.get('lineage'):
            t['lineage'] = meta['lineage']
        has_euk = has_euk or t['isEuk']
    run['hasEuk'] = has_euk
    run['sources'] = sources
    if not run['stations']:
        run['warning'] = ("Kraken2 reports loaded (taxa/table only) — no matching station "
                          "coordinates. Load the MBON sample-metadata file, or use the in-browser "
                          "'assign sample' dialog to place each report on the map.")
    return run


# ---------------------------------------------------------------- Manifest
# A per-sample "report manifest" workbook links each sample to its run /
# project / cruise metadata AND to one or more report files. Each row is one
# sample; report paths may be comma- OR semicolon-delimited so a single sample
# can point at several reports.
#
#   Recognized columns (header matching is case/space/punctuation-insensitive):
#     sample_id  (or sample, samp_number, samp_name, sid, uid)  -- required
#     run        (or run_id)
#     project    (or project_id)            <- runs are grouped by this
#     cruise     (or cruise_number)
#     k2_report  (or kraken2_report, kraken, k2)  -- Kraken2 report path(s)
#     asv_report (or asv, wilder_report)          -- Wilder/ASV workbook path(s)
#   Any other columns are carried through as per-sample metadata.
#
# Reports are resolved relative to the file as written, then under the row's
# project folder (data/projects/<project>/{faire,meta,kraken2,wilder}/), then
# under ./data, then ./ , then by basename search. Coordinates/Station are
# linked from the row's metadata workbook (FAIRe sampleMetadata) via samp_number / samp_name / Station,
# exactly like the Kraken2 and Wilder loaders.
MANIFEST = DATA_PROJECTS / ec.MANIFEST_NAME

# When report_manifest.xlsx exists and has at least one row, it's treated as
# the authoritative sample list: only its non-disabled rows contribute data.
# Folder-scan / loose-file auto-discovery (steps 1-3 in _gather_runs) is
# skipped entirely in that case, so a file sitting in data/ that was never
# added to the manifest doesn't quietly show up anyway. Set to False (or
# pass --include-unmanifested on the CLI) to restore the old behavior of
# auto-discovering + merging in anything under data/ alongside the manifest.
MANIFEST_EXCLUSIVE = True

# ---------------------------------------------------------------- run muting
# Manifest rows whose `run` (or `sample_id`) matches one of these entries are
# dropped before anything is loaded, WITHOUT touching the manifest workbook
# itself. Set them on the command line (--skip-runs a,b,c) or with the
# EDNA_SKIP_RUNS env var. Matching is case-insensitive and ignores _ / - /
# spaces, so '2026_run01', '2026-RUN01' and '2026 run01' are the same entry.
# This is a purely local, reversible mute — use the manifest's `disable`
# column for a permanent, shared exclusion.
SKIP_MANIFEST_RUNS = ec.split_list(os.getenv("EDNA_SKIP_RUNS"))


# Optional build-time filters, set from --projects / --runs (see main()).
# Empty set == no filtering. Matching is case/punctuation-insensitive, same
# normalization as SKIP_MANIFEST_RUNS.
ONLY_PROJECTS = set()
ONLY_RUNS = set()


def _run_mute_key(v):
    """Normalize a run / sample id for SKIP_MANIFEST_RUNS matching."""
    return re.sub(r'[^a-z0-9]', '', str(v or '').lower())


def _skip_keys():
    return {_run_mute_key(v) for v in SKIP_MANIFEST_RUNS if _run_mute_key(v)}


def _drop_muted_runs(df, cmap):
    """Remove manifest rows naming a run listed in SKIP_MANIFEST_RUNS. Returns
    the filtered frame; prints one line per muted run so the build log says
    plainly what was left out and why."""
    SKIP_MANIFEST_RUN_KEYS = _skip_keys()
    if not SKIP_MANIFEST_RUN_KEYS or df is None or df.empty:
        return df
    cols = [cmap[k] for k in ('run', 'sample_id') if k in cmap]
    if not cols:
        return df
    def _muted(row):
        for c in cols:
            if _run_mute_key(row.get(c)) in SKIP_MANIFEST_RUN_KEYS:
                return True
        return False
    mask = df.apply(_muted, axis=1)
    n = int(mask.sum())
    if n:
        names = sorted({str(v) for c in cols for v in df.loc[mask, c].dropna().unique()
                        if _run_mute_key(v) in SKIP_MANIFEST_RUN_KEYS})
        log.info(f"manifest: muted {n} row(s) via --skip-runs -> {', '.join(names)}")
    return df.loc[~mask].copy()


def _apply_only_filters(df, cmap):
    """Keep only manifest rows whose project is in ONLY_PROJECTS (when set)
    and whose run / sample_id is in ONLY_RUNS (when set)."""
    if df is None or df.empty:
        return df
    if ONLY_PROJECTS and cmap.get('project'):
        keys = {_run_mute_key(v) for v in ONLY_PROJECTS}
        mask = df[cmap['project']].map(lambda v: _run_mute_key(v) in keys)
        n = int((~mask).sum())
        if n:
            log.info(f"manifest: --projects kept "
                  f"{int(mask.sum())} row(s), dropped {n}")
        df = df.loc[mask].copy()
    if ONLY_RUNS:
        cols = [cmap[k] for k in ('run', 'sample_id') if k in cmap]
        keys = {_run_mute_key(v) for v in ONLY_RUNS}
        if cols:
            mask = df.apply(
                lambda r: any(_run_mute_key(r[c]) in keys for c in cols), axis=1)
            n = int((~mask).sum())
            if n:
                log.info(f"manifest: --runs kept "
                      f"{int(mask.sum())} row(s), dropped {n}")
            df = df.loc[mask].copy()
    return df


_MANIFEST_CACHE = {}


def read_manifest_df(manifest_path, quiet=False):
    """Read + validate the report manifest. Returns a DataFrame, or None when
    the file is missing/unreadable (logged). Accepts .xlsx (first sheet, or
    'Sheet1'/'manifest' when present) or .csv/.tsv. Cached per path."""
    manifest_path = Path(manifest_path)
    key = manifest_path.resolve()
    if key in _MANIFEST_CACHE:
        return _MANIFEST_CACHE[key].copy()
    if not manifest_path.exists():
        if not quiet:
            log.info("no manifest at %s", ec.rel(manifest_path))
        return None
    try:
        ec.require_file(manifest_path, 'report manifest', ec.TABULAR_EXT)
        if manifest_path.suffix.lower() in ('.csv', '.tsv', '.txt'):
            df = pd.read_csv(manifest_path, sep=None, engine='python')
        else:
            sheets = ec.excel_sheet_names(manifest_path)
            sheet = next((s for s in sheets if s.lower() in ('manifest', 'sheet1')), sheets[0])
            df = pd.read_excel(manifest_path, sheet_name=sheet, engine='openpyxl')
    except (InputError, Exception) as e:
        log.error("report manifest unreadable — %s", e)
        return None
    df = df.dropna(how='all')
    df.columns = [str(c).strip() for c in df.columns]
    cmap = _manifest_colmap(df.columns)
    if 'sample_id' not in cmap:
        log.error("manifest %s has no sample_id column (columns: %s). Expected one of: %s",
                  manifest_path.name, list(df.columns), ', '.join(_MANIFEST_ALIASES['sample_id']))
    else:
        sid = df[cmap['sample_id']].map(_clean)
        n_blank = int(sid.isna().sum())
        if n_blank:
            log.warning("manifest: %d row(s) have a blank sample_id and will be ignored", n_blank)
        if not cmap.get('k2') and not cmap.get('asv'):
            log.warning("manifest has neither a k2_report nor an asv_report column — "
                        "no taxonomic data can be loaded from it")
        log.info("manifest %s: %d row(s); recognised columns: %s",
                 ec.rel(manifest_path), len(df), ', '.join(f"{k}={v}" for k, v in cmap.items()))
    _MANIFEST_CACHE[key] = df
    return df.copy()


def _manifest_has_rows(manifest_path):
    """True when the manifest workbook exists and has at least one sample row
    (used to decide whether it should exclusively govern what gets built)."""
    df = read_manifest_df(manifest_path, quiet=True)
    if df is None:
        return False
    cmap = _manifest_colmap(df.columns)
    if 'sample_id' not in cmap:
        return False
    return df[cmap['sample_id']].map(_clean).notna().any()

_MANIFEST_ALIASES = {
    'sample_id': ['sampleid', 'sample', 'sampnumber', 'sampnum', 'sid',
                  'sampname', 'samplename', 'uid'],
    'run':       ['run', 'runid', 'runname'],
    'project':   ['project', 'projectid', 'projectname'],
    'project_dir': ['projectdir', 'projectfolder', 'folder'],
    'cruise':    ['cruise', 'cruisenumber', 'cruiseid', 'cruisenum'],
    'k2':        ['k2report', 'k2reports', 'kraken2report', 'krakenreport',
                  'k2', 'kraken', 'kraken2'],
    'asv':       ['asvreport', 'asvreports', 'asv', 'wilderreport', 'wilder',
                  'asvworkbook'],
    'metadata':  ['metadata', 'metadatafile', 'mbondata', 'mbondatafile',
                  'stationmetadata', 'samplemetadata', 'metadatareport'],
    'barcodes':  ['barcodes', 'barcode', 'isbarcode', 'barcoded',
                  'demultiplexed', 'demux'],
    'disable':   ['disable', 'disabled', 'skip', 'exclude', 'ignore',
                  'omit', 'hide'],
}


def _manifest_colmap(cols):
    norm = {_norm_colname(c): c for c in cols}
    found = {}
    for key, aliases in _MANIFEST_ALIASES.items():
        for a in aliases:
            if a in norm:
                found[key] = norm[a]
                break
    return found


def _split_paths(cell):
    """Split a comma/semicolon-delimited path cell into individual paths."""
    if cell is None:
        return []
    s = _clean(cell)
    if not s or s.lower() == 'nan':
        return []
    return [p.strip() for p in re.split(r'[;,]', s) if p.strip()]


def _search_ok(path):
    """Basename-search filter: skip Office lock files, and never let a real
    build pick up a file from the bundled example data (or vice versa)."""
    path = Path(path)
    if ec.is_temp_file(path):
        return False
    example_root = (ec.DATA_DIR / 'example').resolve()
    try:
        in_example = path.resolve().is_relative_to(example_root)
        building_example = DATA_PROJECTS.resolve().is_relative_to(example_root)
    except (AttributeError, OSError):       # Python < 3.9 / broken link
        return True
    return in_example == building_example


def _resolve_report(p, project=None):
    """Resolve a manifest path: as-written; then under the row's standardized
    project folder (data/projects/<project>/{faire,meta,kraken2,wilder}/, plus
    the project root itself); then under legacy ./data / ./; else by basename
    search (project folder first, then ./data)."""
    cand = Path(p)
    search_roots = []
    if project and str(project).strip() and str(project).lower() != 'nan':
        proj_dir = DATA_PROJECTS / str(project).strip()
        search_roots += [proj_dir / sub for sub in ("faire", "meta", "kraken2", "wilder")]
        search_roots.append(proj_dir)
    search_roots += [DATA_DIR, Path('.')]
    # Tolerant resolution: manifest entries sometimes omit the .xlsx extension
    # (e.g. "WLM611810_260307"), which previously caused the file to be
    # silently skipped.
    name_variants = [p]
    if not str(p).lower().endswith(('.xlsx', '.xls', '.csv')):
        name_variants += [f'{p}.xlsx', f'{p}.xls']
    for root in search_roots:
        for nv in name_variants:
            o = root / nv
            if o.exists():
                return o
    if cand.exists():
        return cand
    rglob_roots = []
    if project and str(project).strip() and str(project).lower() != 'nan':
        rglob_roots.append(DATA_PROJECTS / str(project).strip())
    rglob_roots.append(DATA_DIR)
    for root in rglob_roots:
        if not root.is_dir():
            continue
        for nv in name_variants:
            hits = [h for h in root.rglob(Path(nv).name) if _search_ok(h)]
            if hits:
                return hits[0]
        # last resort: prefix match (e.g. "WLM611810_260307" -> "WLM611810_260307_v2.xlsx")
        hits = sorted(h for h in root.rglob(f'{cand.name}*.xlsx') if _search_ok(h))
        if hits:
            return hits[0]
    return None


def _kraken2_file_stem(filename):
    """Sample-identifying stem of a kraken2 report filename (mirrors
    ingest.py's kraken2_stem so both tools agree on the key used in
    meta/concordance.json)."""
    name = filename
    for ext in ('.kraken2.report.txt', '.kraken2.report', '.report.txt', '.report'):
        if name.endswith(ext):
            return name[: -len(ext)]
    return Path(name).stem


def _load_concordance(project):
    """Load data/projects/<project>/meta/concordance.json (written by
    ingest.py), used to resolve a k2_report cell that names a whole folder of
    per-sample Kraken2 reports (report filename stem -> FAIRe samp_number)."""
    if not project or str(project).lower() == 'nan':
        return None
    p = DATA_PROJECTS / str(project).strip() / "meta" / "concordance.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


# Discovery regex for demultiplexed (Nanopore-style) report folders: each
# barcode plus the 'unclassified' bin lives in its own subdirectory named like
# '<prefix>__barcode01' / '<prefix>__unclassified'.
_BARCODE_TOKEN_RE = re.compile(r'(unclassified|barcode\d+)', re.IGNORECASE)

# A report FILE is claimed for a barcode when its name *starts with* that
# barcode token, whatever follows it. This is what makes
# 'barcode23-18S_CaleDNA.kraken.report' importable without renaming: only the
# leading token has to match, the rest of the name (assay, database, caller)
# is free-form and ignored.
_BARCODE_PREFIX_RE = re.compile(r'^(unclassified|barcode[\s_-]*\d+)', re.IGNORECASE)

# Filename suffixes treated as a Kraken2 report. '.kraken.report' (single 'n',
# no '2') is what the CaleDNA-style outputs use.
_KRAKEN_REPORT_SUFFIXES = ('.kraken2.report.txt', '.kraken2.report',
                           '.kraken.report.txt', '.kraken.report',
                           '.k2report', '.kreport', '.report.txt', '.report')


def _is_kraken_report_file(p):
    n = p.name.lower()
    return (p.is_file() and n.endswith(_KRAKEN_REPORT_SUFFIXES)
            and not n.endswith('.report.out') and not n.startswith('~$')
            and n != 'full.report')


def _barcode_token(text, anchored):
    """Normalized barcode token ('barcode07' / 'unclassified') found in `text`,
    or None. `anchored` requires the token at the very start (files); otherwise
    it may appear anywhere (directory names like '<run>__barcode07')."""
    m = (_BARCODE_PREFIX_RE.match(text) if anchored
         else _BARCODE_TOKEN_RE.search(text))
    if not m:
        return None
    tok = re.sub(r'[\s_-]+', '', m.group(1)).lower()
    num = re.search(r'(\d+)', tok)
    return 'unclassified' if not num else f'barcode{int(num.group(1)):02d}'


def _project_k2_root(project):
    if not project or str(project).strip().lower() in ('', 'nan'):
        return None
    return DATA_PROJECTS / str(project).strip() / 'kraken2'


def _resolve_k2_dirs(cell, project=None):
    """Resolve a manifest k2_report cell to [(path, database)] pairs.

    Reports for one run may now be classified against several Kraken2
    databases, filed as
        data/projects/<project>/kraken2/<DATABASE>/<run>/...
    The database name is simply the folder level directly under kraken2/, so a
    manifest cell naming only the run ('20260522_OX_APL_18S_test01') matches
    under EVERY database folder and yields one (path, database) pair each.
    The legacy flat layout kraken2/<run>/... still resolves, with database None.
    """
    out, seen = [], set()
    root = _project_k2_root(project)
    if root and root.is_dir():
        direct = root / cell
        if direct.exists():
            out.append((direct, None)); seen.add(direct.resolve())
        for dbdir in sorted(p for p in root.iterdir()
                            if p.is_dir() and not p.name.startswith('.')):
            c = dbdir / cell
            if c.exists() and c.resolve() not in seen:
                out.append((c, dbdir.name)); seen.add(c.resolve())
    if out:
        return out
    rp = _resolve_report(cell, project=project)
    return [(rp, _db_from_path(rp, project))] if rp is not None else []


def _db_from_path(path, project):
    """Database name for an already-resolved report path: the folder directly
    under the project's kraken2/ root, when the path is nested deeper than one
    level (i.e. kraken2/<DATABASE>/<run>/...). Flat layouts return None."""
    root = _project_k2_root(project)
    if root is None or path is None:
        return None
    try:
        rel = Path(path).resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return None
    return rel.parts[0] if len(rel.parts) > 1 else None

# Separator joining a universal sample_ID to its run/assay tab in an expanded
# barcode sample id, e.g. 'OXR20260514-002 · 20260524_16S_test01'. The same
# physical sample (universal ID) is sequenced under several assay tabs; keeping
# the tab in the id makes each its own Explore option and stops reads from
# different assays being summed together. The universal ID (text before the
# separator) is what links to the FAIRe checklist's sampleMetadata.sample_ID.
_RUN_SEP = ' · '

# Internal column added to expanded manifest rows carrying the Kraken2
# database a report came from. Not a user-facing manifest column -- it is
# derived from the folder level under kraken2/.
_K2_DB_COL = '__k2_db'


def _split_run_suffix(sid, run):
    """Recover the universal sample_ID from an expanded barcode sample id by
    stripping a trailing '<_RUN_SEP><run>'. Returns sid unchanged when no such
    suffix is present (non-barcode samples, fallbacks, etc.)."""
    if run and isinstance(sid, str) and sid.endswith(_RUN_SEP + run):
        return sid[:-len(_RUN_SEP + run)]
    return sid


def _fix_latlon(lat, lon):
    """Guard against transposed coordinates. A decimalLatitude outside the valid
    [-90, 90] range while the longitude is in-range almost always means the two
    were entered swapped (a common spreadsheet mistake) — swap them back so the
    point lands on the map instead of off it."""
    try:
        if abs(lat) > 90 and abs(lon) <= 90:
            return lon, lat
    except TypeError:
        pass
    return lat, lon


def _is_barcodes_flag(v):
    """True when a manifest 'barcodes' cell is set. Handles bool True, numeric
    1/1.0 (Excel TRUE often parses to a float), and the strings
    true/yes/y/t/1. Blank / NaN / 0 -> False."""
    if isinstance(v, bool):
        return v
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return False
    if isinstance(v, (int, float)):
        return v != 0
    return str(v).strip().lower() in ('true', 'yes', 'y', 't', '1', '1.0')


def _barcode_report_in(subdir, token):
    """The Kraken2 report file inside a barcode subfolder. Prefers an exact
    '<token>.<suffix>' name; otherwise any report whose name *starts with* the
    barcode token; otherwise the first report file in the folder. Never the
    combined 'full.report' or a per-read '*.report.out'."""
    cand = [p for p in sorted(subdir.iterdir()) if _is_kraken_report_file(p)]
    if not cand:
        return None
    for p in cand:
        for suf in _KRAKEN_REPORT_SUFFIXES:
            if p.name.lower() == f'{token}{suf}':
                return p
    pref = [p for p in cand if _barcode_token(p.name, anchored=True) == token]
    return pref[0] if pref else cand[0]


def _barcode_reports_in_run(run_dir):
    """Every (token, report_path) pair under a run folder, covering both
    layouts in use:

      A. one sub-folder per barcode, report inside
         <run>/<run>__barcode07/barcode07.report
      B. flat -- reports sit directly in the run folder and carry the barcode
         as a filename PREFIX
         <run>/barcode23-18S_CaleDNA.kraken.report

    In both cases only the leading barcode token has to match; everything after
    it in the name is free-form."""
    out, seen = [], set()
    if not run_dir.is_dir():
        return out
    for sub in sorted(p for p in run_dir.iterdir()
                      if p.is_dir() and not p.name.startswith('.')):
        tok = _barcode_token(sub.name, anchored=False)
        if not tok:
            continue
        rep = _barcode_report_in(sub, tok)
        if rep is None:
            log.warning(f"manifest barcodes: no report file in {sub.name}")
            continue
        if tok not in seen:
            out.append((tok, rep)); seen.add(tok)
    for f in sorted(run_dir.iterdir()):
        if not _is_kraken_report_file(f):
            continue
        tok = _barcode_token(f.name, anchored=True)
        if tok and tok not in seen:
            out.append((tok, f)); seen.add(tok)
    return out


def _norm_tokens(s):
    """Lowercased alphanumeric tokens with leading zeros stripped inside each
    digit run, so 'MV01_test1' -> ['mv1','test1'] and '..._MV1_test01' ->
    ['mv1','test01'->'test1'] line up for matching."""
    out = []
    for t in re.split(r'[^A-Za-z0-9]+', str(s).lower()):
        if t:
            out.append(re.sub(r'0*(\d+)', r'\1', t))
    return out


def _match_barcode_tab(key, tab_names):
    """Pick the workbook tab that best matches a run/sample id by shared
    normalized tokens. Ranked by the fraction of the tab's tokens that appear in
    the key (so the specific assay tab '18S' beats a generic 'test1' overlap),
    tie-broken by the absolute number of shared tokens."""
    ktoks = set(_norm_tokens(key))
    best, best_score = None, (0.0, 0)
    for tab in tab_names:
        ttoks = set(_norm_tokens(tab))
        if not ttoks:
            continue
        shared = sum(1 for t in ttoks if t in ktoks)
        if shared == 0:
            continue
        score = (shared / len(ttoks), shared)
        if score > best_score:
            best, best_score = tab, score
    return best


def _load_barcode_workbook(path):
    """Parse a barcoding workbook into {tab_name: {barcode_number: universal_sample_ID}}.
    Each tab holds a Sample / Barcode table (one row per barcode); tabs without
    both columns are ignored (e.g. the workbook's own 'metadata' tab).

    The value stored per barcode is the *universal sample_ID* — the identifier
    that ties a barcode back to the FAIRe checklist's sampleMetadata.sample_ID.
    It is taken from the tab's 'sample_ID' column when that holds a literal
    value; otherwise it is rebuilt exactly the way the spreadsheet's
    =CONCAT(run,'-',Sample) formula does, i.e. '<run>-<Sample>'
    (e.g. OXR20260514-002), so a stored formula string never leaks through. If
    no run is present it falls back to the bare Sample name."""
    out = {}
    try:
        xls = pd.ExcelFile(path, engine='openpyxl')
    except Exception as e:
        log.error(f"barcode workbook unreadable {path}: {e}")
        return out
    for sh in xls.sheet_names:
        try:
            tdf = pd.read_excel(path, sheet_name=sh, engine='openpyxl', dtype=str)
        except Exception:
            continue
        low = {_norm_colname(c): c for c in tdf.columns}
        scol = low.get('sample') or low.get('samplename')
        bcol = low.get('barcode') or low.get('barcodes') or low.get('bc')
        idcol = low.get('sampleid')              # explicit universal 'sample_ID'
        runcol = low.get('run') or low.get('runid')
        if not scol or not bcol:
            continue
        mapping = {}
        for _, row in tdf.iterrows():
            b = _clean(row[bcol]); s = _clean(row[scol])
            if not b or not s or s.lower() == 'nan':
                continue
            num = re.search(r'(\d+)', str(b))
            if not num:
                continue
            uid = None
            if idcol:
                raw = _clean(row[idcol])
                # skip cached formula text like '=_xlfn.CONCAT(...)'
                if raw and not raw.startswith('=') and raw.lower() != 'nan':
                    uid = raw
            if not uid:
                rv = _clean(row[runcol]) if runcol else None
                uid = f'{rv}-{s}' if rv and rv.lower() != 'nan' else s
            mapping[int(num.group(1))] = uid
        if mapping:
            out[sh] = mapping
    return out


def _is_disabled(v):
    """True when a manifest 'disable' cell asks to skip the row. Accepts bool
    True, non-zero numbers, and the strings true/yes/y/t/1/x/skip/disable/
    exclude/omit/hide. Blank / NaN / 0 / false / no -> keep the row."""
    if isinstance(v, bool):
        return v
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return False
    if isinstance(v, (int, float)):
        return v != 0
    return str(v).strip().lower() in (
        'true', 'yes', 'y', 't', '1', '1.0', 'x',
        'skip', 'disable', 'disabled', 'exclude', 'omit', 'hide')


def _drop_disabled(df, cmap):
    """Remove manifest rows whose 'disable' cell is set, excluding those samples
    (and their barcode expansions) from every output."""
    dcol = cmap.get('disable')
    if not dcol or dcol not in df.columns:
        return df
    keep = ~df[dcol].map(_is_disabled)
    dropped = int((~keep).sum())
    if dropped:
        log.info(f"manifest: skipping {dropped} disabled row(s)")
    return df[keep].reset_index(drop=True)


def _expand_barcode_rows(df, cmap):
    """Expand any manifest row whose 'barcodes' cell is set into one row per
    barcode (+ unclassified) discovered inside that row's k2_report folder, so
    demultiplexed runs don't have to be typed out by hand.

    The 'barcodes' cell may be:
      * TRUE / 1 / yes  -> samples are named '<barcode>_<run>'
                           (e.g. barcode01_20260523_MV1_test01).
      * an .xlsx path   -> a barcoding workbook whose tabs each hold a
                           Sample / Barcode table. The tab is fuzzy-matched to
                           the row's run, and each barcode is renamed to its
                           *universal sample_ID* — the workbook's own sample_ID
                           value, i.e. '<run>-<Sample>' (e.g. OXR20260514-002).
                           That universal id is what joins to the FAIRe
                           checklist's sampleMetadata.sample_ID, so it is used
                           verbatim (no '_<run>' suffix). Barcodes not listed in
                           the tab fall back to '<barcode>_<run>'; 'unclassified'
                           is always kept.

    Non-barcode rows pass through unchanged."""
    bcol = cmap.get('barcodes')
    if not bcol or 'k2' not in cmap or 'sample_id' not in cmap:
        return df

    wb_cache = {}   # resolved workbook Path -> {tab: {barcode_num: sample}}

    def _order(item):
        token = item[0]
        if token == 'unclassified':
            return (1, 0)
        num = re.search(r'(\d+)', token)
        return (0, int(num.group(1)) if num else 0)

    out_rows = []
    for _, r in df.iterrows():
        cell = r.get(bcol)
        cv = _clean(cell)
        is_file = bool(cv) and cv.lower().endswith(('.xlsx', '.xlsm', '.xls', '.csv'))
        if not is_file and not _is_barcodes_flag(cell):
            out_rows.append(r)
            continue

        run = _clean(r[cmap['run']]) if cmap.get('run') else None
        key = run or _clean(r[cmap['sample_id']]) or ''
        proj_val = (_clean(r[cmap['project_dir']]) if cmap.get('project_dir')
                    else _clean(r[cmap['project']]) if cmap.get('project') else None)

        # barcode -> real sample-name map (file mode only)
        bc_map = None
        if is_file:
            wb_path = _resolve_report(cv, project=proj_val)
            if wb_path is None:
                log.warning(f"manifest barcodes: workbook not found: {cv}")
            else:
                if wb_path not in wb_cache:
                    wb_cache[wb_path] = _load_barcode_workbook(wb_path)
                wb = wb_cache[wb_path]
                tab = _match_barcode_tab(key, list(wb.keys()))
                if tab is None:
                    log.warning(f"manifest barcodes: no tab in {wb_path.name} matched run '{key}' "
                          f"(tabs: {', '.join(wb.keys())})")
                else:
                    bc_map = wb[tab]
                    log.info(f"barcodes: run '{key}' -> tab '{tab}' "
                          f"({len(bc_map)} barcode→sample mappings)")

        expanded = []
        for folder in _split_paths(r[cmap['k2']]):
            bases = [(b, db) for b, db in _resolve_k2_dirs(folder, project=proj_val)
                     if b is not None and b.is_dir()]
            if not bases:
                log.warning(f"manifest barcodes: report folder not found: {folder}")
                continue
            for base, db in bases:
                found = _barcode_reports_in_run(base)
                if not found:
                    log.warning(f"manifest barcodes: no barcode reports under {base}")
                    continue
                log.info(f"barcodes: {len(found)} report(s) in '{folder}'"
                      + (f" [db: {db}]" if db else ""))
                for token, rep in found:
                    expanded.append((token, rep, db))

        if not expanded:
            log.warning(f"manifest barcodes: nothing matched for run '{key}'")
            continue
        for token, rep, db in sorted(expanded, key=_order):
            nr = r.copy()
            if token == 'unclassified':
                sid = f'unclassified_{run}' if run else 'unclassified'
            else:
                num = int(re.search(r'(\d+)', token).group(1))
                uid = bc_map.get(num) if bc_map else None
                if uid:
                    # Universal sample_ID from the barcoding workbook
                    # (e.g. OXR20260514-002), suffixed with the run/assay tab so
                    # the same sample across assays stays a distinct Explore
                    # option and its reads aren't merged. The universal ID
                    # (before _RUN_SEP) still links to the FAIRe checklist.
                    sid = f'{uid}{_RUN_SEP}{run}' if run else uid
                else:
                    # No workbook mapping for this barcode (flag/TRUE mode, or a
                    # barcode missing from the tab): keep the legacy name.
                    sid = f'{token}_{run}' if run else token
            nr[cmap['sample_id']] = sid
            nr[cmap['k2']] = str(rep)
            # The sample id is deliberately identical across databases (same
            # physical sample, same station); the database travels separately
            # in _K2_DB_COL so its reads land in their own run.
            nr[_K2_DB_COL] = db
            out_rows.append(nr)

    return pd.DataFrame(out_rows).reset_index(drop=True) if out_rows else df


def _slug(v):
    return re.sub(r'[^A-Za-z0-9]+', '_', str(v)).strip('_') or 'manifest'


def _wlm_uid_to_sid(fpath):
    """UID -> ClientSampleID map from a WLM workbook's 'metadata' sheet."""
    meta = pd.read_excel(fpath, sheet_name='metadata', header=None, engine='openpyxl')
    hdr_idx = None
    for i in range(len(meta)):
        rowl = [str(c).strip().lower() for c in meta.iloc[i].tolist()]
        if 'uid' in rowl and 'clientsampleid' in rowl:
            hdr_idx = i
            break
    if hdr_idx is None:
        return {}
    hdr = meta.iloc[hdr_idx]
    sub = meta.iloc[hdr_idx + 1:].reset_index(drop=True)
    sub.columns = hdr.values
    sub = sub.dropna(subset=['UID'])
    out = {}
    for _, row in sub.iterrows():
        uid = _norm_uid(row['UID'])
        sid = _clean(row['ClientSampleID']) if 'ClientSampleID' in sub.columns else None
        out[uid] = sid or uid
    return out


def _manifest_job_meta(gkey, runs_set, proj_set, cruise_set, n_samples):
    clean = lambda s: sorted({x for x in s if x and str(x).lower() != 'nan'})
    meta = {'Source': 'Report manifest', 'Group': str(gkey),
            'Sample count': str(n_samples)}
    if clean(proj_set):
        meta['Project'] = ', '.join(clean(proj_set))
    if clean(runs_set):
        meta['Run(s)'] = ', '.join(clean(runs_set))
    if clean(cruise_set):
        meta['Cruise(s)'] = ', '.join(clean(cruise_set))
    return meta


def _coverage_record(sid, meta, why):
    """Compact sample record for the HTML coverage panel."""
    meta = meta or {}
    pick = lambda *ks: next((str(meta[k]) for k in ks
                             if meta.get(k) not in (None, '') and str(meta[k]).lower() != 'nan'), None)
    return {'sampleId': str(sid),
            'name': pick('samp_name', 'Sample Name', 'materialSampleID'),
            'cruise': pick('Cruise Number', 'Cruise', 'cruise'),
            'run': pick('Run', 'run'),
            'station': pick('Station', 'station'),
            'category': pick('samp_category', 'Category'),
            'why': why}


def _coverage_no_data(samples, runs, station_map, project_specific=True):
    """Samples with no reads in any run of a project (see _build_manifest_project)."""
    with_data = set()
    for run in runs:
        for s in run.get('samples') or []:
            with_data.add(s['sampleId'])
    out, linked = [], set()
    for s in samples:
        m = s.get('meta') or {}
        if s['sampleId'] in with_data:
            linked.update(x for x in (s['sampleId'], s.get('uid'), m.get('samp_number'),
                                      m.get('samp_name')) if x)
            continue
        out.append(_coverage_record(s['sampleId'], m, 'manifest'))
        linked.update(x for x in (s['sampleId'], s.get('uid'), m.get('samp_number'),
                                  m.get('samp_name')) if x)
    if project_specific:
        for key, m in (station_map or {}).items():
            m = m or {}
            if key in linked or (m.get('samp_name') and m.get('samp_name') in linked):
                continue
            rec = _coverage_record(key, m, 'metadata')
            # skip blank template rows (an id and nothing else)
            if rec['name'] or rec['cruise'] or rec['station']:
                out.append(rec)
    return out


def _build_manifest_project(gkey, rows, cmap, extra_cols,
                            default_station_map, station_cache):
    samples, sample_coords = [], {}
    per_db_reads, sp_meta = {}, {}   # database (None == unnamed) -> {sampleId: {species: reads}}
    meta_sources, k2_sources, asv_sources, k2_seen = [], [], [], set()
    asv_files = {}                 # resolved Path -> set(sample_ids referencing it)
    asv_file_project = {}          # resolved Path -> project (for concordance lookups)
    group_sids = set()
    runs_set, proj_set, cruise_set = set(), set(), set()

    # ---- Station metadata for this project ---------------------------------
    # Collect the unique metadata-workbook paths named in this project's rows
    # (deduped, loaded once via the shared cache), merge them into one
    # samp_number -> sampleMetadata map, and fall back to the default
    # station-metadata workbook (--station-metadata) when no metadata column / value is given.
    station_map = {}
    seen_meta = set()
    if cmap.get('metadata'):
        for r in rows:
            proj_val = (_clean(r[cmap['project_dir']]) if cmap.get('project_dir')
                        else _clean(r[cmap['project']]) if cmap.get('project') else None)
            for mp in _split_paths(r[cmap['metadata']]):
                rp = _resolve_report(mp, project=proj_val)
                if rp is None:
                    log.warning(f"manifest metadata not found: {mp}")
                    continue
                if rp in seen_meta:
                    continue
                seen_meta.add(rp)
                if rp not in station_cache:
                    station_cache[rp] = load_station_map_from_file(rp)
                sm = station_cache[rp]
                for k, v in sm.items():
                    station_map.setdefault(k, v)
                meta_sources.append({'file': rp.name, 'sheets': ['sampleMetadata'],
                                     'role': f'Station metadata ({len(sm)} samples)',
                                     'samples': len(sm)})
    if not station_map:
        station_map = default_station_map
    name_to_num, station_to_num, loose_index = _name_station_indexes(station_map)
    zero_index = _zero_index(station_map)
    unlinked = []   # samples that carry report data but found no coordinates
    samples_seen = set()   # sampleIds already appended to `samples`

    def _link_station(qbase, qsid, run_val=None):
        """(lat, lon, station_map_record) for a sample/report id, matching this
        project's station_map (samp_number / samp_name / Station, then a loose
        fallback, then the row's run value)."""
        snum = (qbase if qbase in station_map else
                qsid if qsid in station_map else
                name_to_num.get(qbase) or station_to_num.get(qbase) or
                loose_index.get(_loose_key(qbase)) or zero_index.get(_zero_key(qbase)))
        if not snum and run_val and str(run_val).lower() != 'nan':
            snum = (run_val if run_val in station_map else
                    name_to_num.get(run_val) or station_to_num.get(run_val) or
                    loose_index.get(_loose_key(run_val)))
        m = station_map.get(snum, {}) if snum else {}
        lat, lon = _coerce_latlon(m.get('decimalLatitude'), m.get('decimalLongitude'), snum or '?')
        return lat, lon, m

    def _resolve_snum(q):
        """samp_number for a report/sample id via the sheet (exact, loose,
        zero-insensitive), or None."""
        if not q:
            return None
        return (q if q in station_map else name_to_num.get(q) or
                loose_index.get(_loose_key(q)) or zero_index.get(_zero_key(q)))

    for r in rows:
        sid = _clean(r[cmap['sample_id']])
        if not sid:
            continue
        proj_val = (_clean(r[cmap['project_dir']]) if cmap.get('project_dir')
                    else _clean(r[cmap['project']]) if cmap.get('project') else None)
        group_sids.add(sid)
        if cmap.get('run'):
            runs_set.add(_clean(r[cmap['run']]))
        if cmap.get('project'):
            proj_set.add(_clean(r[cmap['project']]))
        if cmap.get('cruise'):
            cruise_set.add(_clean(r[cmap['cruise']]))

        # Recover the universal sample_ID for coordinate linking: an expanded
        # barcode sample carries a ' · <run>' assay suffix that isn't part of the
        # FAIRe/MBON key, so strip it before matching.
        rv = _clean(r[cmap['run']]) if cmap.get('run') else None
        base = _split_run_suffix(sid, rv)

        # link station/coords from MBON metadata: exact samp_number / samp_name
        # / Station first, then a loose (case/punctuation-insensitive) fallback
        # so 'Miseq_Run_A' still matches a 'Miseq_RunA' samp_number.
        snum = (base if base in station_map else
                sid if sid in station_map else
                name_to_num.get(base) or station_to_num.get(base) or
                loose_index.get(_loose_key(base)) or zero_index.get(_zero_key(base)))
        # Expanded barcode samples ('barcode01_<run>', 'unclassified_<run>')
        # won't match a samp_number directly — fall back to the row's run value
        # so every barcode inherits the run-level station coordinates.
        if not snum and cmap.get('run'):
            rv = _clean(r[cmap['run']])
            if rv and rv.lower() != 'nan':
                snum = (rv if rv in station_map else
                        name_to_num.get(rv) or station_to_num.get(rv) or
                        loose_index.get(_loose_key(rv)))
        m = station_map.get(snum, {}) if snum else {}
        lat, lon = _coerce_latlon(m.get('decimalLatitude'), m.get('decimalLongitude'), snum or '?')
        if lat is not None:
            sample_coords[sid] = (lat, lon)

        meta = {k: v for k, v in (m or {}).items() if v is not None}
        for key in ('run', 'project', 'cruise'):
            if cmap.get(key):
                val = _clean(r[cmap[key]])
                if val and val.lower() != 'nan':
                    meta[key.capitalize()] = val
        for c in extra_cols:
            v = _clean(r[c])
            if v is not None and v.lower() != 'nan':
                meta[str(c)] = v
        meta.setdefault('Source', 'Manifest')
        samples.append({'sampleId': sid, 'uid': base, 'station': m.get('Station'),
                        'lat': lat, 'lon': lon, 'meta': meta})
        samples_seen.add(sid)

        # --- Kraken2 report(s) for this sample (1 report == this sample), OR a
        # whole folder of per-sample reports (data/projects/<project>/kraken2/<name>/)
        # resolved via that project's meta/concordance.json (written by ingest.py) ---
        row_db = r[_K2_DB_COL] if _K2_DB_COL in r.index else None
        if isinstance(row_db, float) and pd.isna(row_db):
            row_db = None
        k2_targets = []
        for kp in (_split_paths(r[cmap['k2']]) if cmap.get('k2') else []):
            hits = _resolve_k2_dirs(kp, project=proj_val)
            if not hits:
                log.warning(f"manifest K2 not found: {kp}")
                continue
            k2_targets += [(h, row_db if row_db is not None else d) for h, d in hits]
        for rp, db in k2_targets:
            per_sample_reads = per_db_reads.setdefault(db, {})
            if rp.is_dir():
                conc = _load_concordance(proj_val) or {}
                conc_k2 = conc.get('kraken2', {})
                kraken2_root = (DATA_PROJECTS / str(proj_val).strip() / 'kraken2'
                                if proj_val and str(proj_val).lower() != 'nan' else rp.parent)
                report_files = []
                for pat in ('*.kraken2.report.txt', '*.kraken2.report', '*.report', '*.kreport', '*.k2report'):
                    report_files += [f for f in rp.rglob(pat) if not f.name.endswith('.out') and not ec.is_temp_file(f)]
                report_files = sorted(set(report_files))
                n_linked = 0
                for rf in report_files:
                    stem = _kraken2_file_stem(rf.name)
                    if stem.lower() in ('full', 'unclassified') or '__unclassified' in str(rf):
                        continue
                    try:
                        rel = str(rf.relative_to(kraken2_root))
                    except ValueError:
                        rel = rf.name
                    conc_info = conc_k2.get(rel)
                    conc_num = conc_info.get('samp_number') if conc_info else None
                    # The metadata workbook is the source of truth: a concordance
                    # entry is used only if that samp_number still exists in it;
                    # otherwise the report name is linked straight to the sheet
                    # (samp_name / samp_number, punctuation + leading-zero tolerant),
                    # so edits to the workbook flow through without re-running ingest.py.
                    # Only an EXACT match adopts the samp_number as the sample id;
                    # a fuzzy match (e.g. re-run 'A5C_024_A_071825' next to
                    # 'A5C_24_A_071825') keeps its own id so two reports are never
                    # pooled into one sample -- it still inherits the sheet's
                    # coordinates + metadata via _link_station / the alias pass.
                    sub_sid = (conc_num if conc_num in station_map else
                               stem if stem in station_map else
                               name_to_num.get(stem) or stem)
                    if sub_sid not in samples_seen:
                        sub_lat, sub_lon, sub_m = _link_station(sub_sid, sub_sid, rv)
                        sub_meta = {k: v for k, v in (sub_m or {}).items() if v is not None}
                        sub_meta.setdefault('Source', 'Manifest (kraken2 folder)')
                        if cmap.get('project'):
                            pv = _clean(r[cmap['project']])
                            if pv and pv.lower() != 'nan':
                                sub_meta['Project'] = pv
                        samples.append({'sampleId': sub_sid, 'uid': sub_sid,
                                        'station': sub_m.get('Station') if sub_m else None,
                                        'lat': sub_lat, 'lon': sub_lon, 'meta': sub_meta})
                        samples_seen.add(sub_sid)
                        if sub_lat is not None:
                            sample_coords[sub_sid] = (sub_lat, sub_lon)
                        else:
                            unlinked.append(sub_sid)
                    recs = parse_kraken2_report(rf)
                    rmap = per_sample_reads.setdefault(sub_sid, {})
                    for x in recs:
                        rmap[x['species']] = rmap.get(x['species'], 0) + x['reads']
                        sp_meta.setdefault(x['species'], {'commonName': x['commonName'],
                                                          'group': x['group'], 'isEuk': x['isEuk'],
                                                          'taxid': x.get('taxid'), 'lineage': x.get('lineage')})
                    n_linked += 1
                if (db, rp.name, sid) not in k2_seen:
                    k2_seen.add((db, rp.name, sid))
                    k2_sources.append({'file': rp.name, 'sheets': ['kraken2 reports (folder)'],
                                       'db': db,
                                       'role': (f'Kraken2 folder → {n_linked} report(s) via concordance'
                                                + (f' [db: {db}]' if db else '')),
                                       'samples': n_linked})
                continue
            recs = parse_kraken2_report(rp)
            rmap = per_sample_reads.setdefault(sid, {})
            for x in recs:
                rmap[x['species']] = rmap.get(x['species'], 0) + x['reads']
                sp_meta.setdefault(x['species'], {'commonName': x['commonName'],
                                                  'group': x['group'], 'isEuk': x['isEuk'],
                                                  'taxid': x.get('taxid'), 'lineage': x.get('lineage')})
            if (db, rp.name, sid) not in k2_seen:
                k2_seen.add((db, rp.name, sid))
                k2_sources.append({'file': rp.name, 'sheets': ['kraken2 report'], 'db': db,
                                   'role': (f'Kraken2 report → {sid}'
                                            + (f' [db: {db}]' if db else '')), 'samples': 1})

        # --- ASV/Wilder workbook(s) (workbook-level; parsed once below) ---
        asv_paths = _split_paths(r[cmap['asv']]) if cmap.get('asv') else []
        for ap in asv_paths:
            rp = _resolve_report(ap, project=proj_val)
            if rp is None:
                log.warning(f"manifest ASV not found: {ap}")
                continue
            asv_files.setdefault(rp, set()).add(sid)
            asv_file_project.setdefault(rp, proj_val)

        # flag samples that carry report data but couldn't be placed on the map
        k2_paths = _split_paths(r[cmap['k2']]) if cmap.get('k2') else []
        if lat is None and (k2_paths or asv_paths):
            unlinked.append(sid)

    # ---- Fold unclassified bins into their run's primary station -----------
    # Unclassified demultiplexing bins have no coordinates of their own. Per the
    # chosen policy, attach each to the modal (most common) station coordinate
    # among its run's mapped samples, so it appears on the Map and counts toward
    # that station's totals. Controls and unmatched barcodes stay off-map.
    coord_station, run_votes = {}, {}
    for s in samples:
        c = sample_coords.get(s['sampleId'])
        if c is None:
            continue
        coord_station.setdefault(c, s.get('station'))
        rv = (s.get('meta') or {}).get('Run')
        if rv:
            votes = run_votes.setdefault(rv, {})
            votes[c] = votes.get(c, 0) + 1
    run_primary = {rv: max(v.items(), key=lambda kv: kv[1])[0]
                   for rv, v in run_votes.items() if v}
    folded = []
    for s in samples:
        sid = s['sampleId']
        if sid in sample_coords or _sample_category(sid) != 'unclassified':
            continue
        coord = run_primary.get((s.get('meta') or {}).get('Run'))
        if not coord:
            continue
        sample_coords[sid] = coord
        s['lat'], s['lon'] = float(coord[0]), float(coord[1])
        s['station'] = coord_station.get(coord)
        s.setdefault('meta', {})['Note'] = 'Unclassified bin folded into run station'
        folded.append(sid)
    if folded:
        folded_set = set(folded)
        unlinked = [u for u in unlinked if u not in folded_set]
        log.info(f"manifest '{gkey}': folded {len(folded)} unclassified bin(s) "
              f"into their run's primary station: {', '.join(folded)}")

    # Alias each composite per-assay sample id ('<uid> · <run>') to its universal
    # ID's station metadata, so station labels and sample tooltips resolve for
    # the per-tab sample columns. Folded unclassified bins (no universal-ID
    # metadata) get a minimal synthesized record from their assigned station.
    for s in samples:
        m_ = s.get('meta') or {}
        if m_.get('eventDate') and not m_.get('CollectionDate'):
            m_['CollectionDate'] = m_['eventDate']
    for s in samples:
        sid = s['sampleId']
        if sid in station_map:
            continue
        base = _split_run_suffix(sid, (s.get('meta') or {}).get('Run'))
        snum_ = _resolve_snum(base)
        if base in station_map:
            station_map[sid] = station_map[base]
        elif snum_:
            station_map[sid] = station_map[snum_]
        elif s.get('station') is not None and s.get('lat') is not None:
            station_map[sid] = {'Station': s['station'],
                                'decimalLatitude': s['lat'],
                                'decimalLongitude': s['lon']}

    if unlinked:
        log.warning(f"manifest '{gkey}': {len(unlinked)} sample(s) have report data but no "
              f"matching station coordinates (not shown on map): {', '.join(sorted(set(unlinked)))}. "
              f"Check that each sample_id matches a samp_number / samp_name / Station in the "
              f"linked metadata workbook.")

    # parse each ASV workbook once; keep only columns matching this group's sids
    asv_long, asv_group_map, species_target = [], {}, {}
    for fpath, want_sids in asv_files.items():
        try:
            uid_map = _wlm_uid_to_sid(fpath)
        except Exception:
            uid_map = {}
        try:
            agg = read_aggregated(fpath)
        except Exception as e:
            log.error(f"manifest ASV parse failed {fpath.name}: {e}")
            asv_sources.append({'file': fpath.name, 'sheets': ['(unreadable)'],
                                'role': 'ASV report (parse failed)', 'samples': 0})
            continue
        agg['Rank'] = agg['Rank'].astype(str).str.strip()
        species_only = agg[agg['Rank'] == 'species'].copy()
        uid_cols = [c for c in species_only.columns if c not in INFO_COLS]

        # Fall back to this project's meta/concordance.json (written by
        # ingest.py) to resolve a UID -> FAIRe samp_number when the manifest
        # row is a whole-batch container (its sample_id isn't the physical
        # sample the UID belongs to). job_id matches ingest.py's derivation
        # (fp.stem.split('_')[0]) so the two tools agree on the key.
        proj_for_file = asv_file_project.get(fpath)
        conc_wilder = (_load_concordance(proj_for_file) or {}).get('wilder', {})
        job_id = fpath.stem.split('_')[0]

        kept = 0
        for c in uid_cols:
            nc = _norm_uid(c)
            candidates = {nc, uid_map.get(nc, nc)}
            target = next((cc for cc in candidates if cc in want_sids or cc in group_sids), None)
            if target is None:
                conc_entry = conc_wilder.get(f'{job_id}:{nc}')
                target = conc_entry.get('samp_number') if conc_entry else None
                if target is not None and target not in station_map:
                    target = None
            if target is None:
                # no concordance: link the WLM ClientSampleID to the sheet directly
                target = _resolve_snum(uid_map.get(nc))
            if target is None:
                continue
            if target not in samples_seen:
                t_lat, t_lon, t_m = _link_station(target, target)
                t_meta = {k: v for k, v in (t_m or {}).items() if v is not None}
                t_meta.setdefault('Source', 'Manifest (wilder concordance)')
                samples.append({'sampleId': target, 'uid': target,
                                'station': t_m.get('Station') if t_m else None,
                                'lat': t_lat, 'lon': t_lon, 'meta': t_meta})
                samples_seen.add(target)
                if t_lat is not None:
                    sample_coords[target] = (t_lat, t_lon)
                else:
                    unlinked.append(target)
            vals = pd.to_numeric(species_only[c], errors='coerce').fillna(0)
            for sp, v in zip(species_only['ScientificName'], vals):
                if v and v > 0:
                    asv_long.append({'ScientificName': str(sp).strip(),
                                     'sample': target, 'reads': float(v)})
            kept += 1
        if 'Group' in species_only.columns:
            for sp, g in zip(species_only['ScientificName'], species_only['Group']):
                if pd.notna(sp):
                    asv_group_map.setdefault(str(sp).strip(), _clean(g) or 'Unknown')
        try:
            full_df = pd.read_excel(fpath, sheet_name='full', engine='openpyxl')
            low = {str(c).strip().lower(): c for c in full_df.columns}
            sp_c = next((low[c] for c in ['scientificname', 'species', 'name'] if c in low), None)
            tg_c = next((low[c] for c in ['target', 'assaytarget', 'target name'] if c in low), None)
            if sp_c and tg_c:
                for _, rr in full_df[[sp_c, tg_c]].dropna().iterrows():
                    species_target.setdefault(str(rr[sp_c]).strip(), []).append(str(rr[tg_c]).strip())
        except Exception:
            pass
        asv_sources.append({'file': fpath.name, 'sheets': ['aggregated', 'full'],
                            'role': f'ASV report ({kept} sample col(s) matched)', 'samples': kept})
    species_target = {s: pd.Series(v).mode().iat[0] for s, v in species_target.items() if v}

    job_meta = _manifest_job_meta(gkey, runs_set, proj_set, cruise_set, len(samples))
    base_id, base_label = f'manifest_{_slug(gkey)}', str(gkey)
    samples_by_id = {s['sampleId']: s for s in samples}

    # The template's ASV/Kraken2 method toggle is binary *per run* (a run is
    # Kraken2 iff kind == 'kraken2'; everything else is ASV-class). To keep the
    # two methods from bleeding across toggles we emit a SEPARATE run per method
    # present in this project: a Kraken2 run from the K2 reports and an ASV run
    # from the Wilder workbooks. Single-method projects still yield one run.
    # A sample can end up with an *empty* read map (e.g. a report that
    # parsed to zero usable rows). Pivoting an empty long frame raises
    # KeyError('reads') and used to abort the whole manifest load, which
    # silently left the previous HTML in place. Require real rows.
    per_db_reads = {db: {k: v for k, v in m.items() if v}
                    for db, m in per_db_reads.items()}
    per_db_reads = {db: m for db, m in per_db_reads.items() if m}
    k2_dbs = sorted(per_db_reads, key=lambda d: (d is None, str(d)))
    has_k2 = bool(per_db_reads)
    has_asv = bool(asv_long)
    both = has_k2 and has_asv
    # More than one Kraken2 database for this project means more than one
    # Kraken2 run, so their reads are never pooled and the HTML can filter on
    # the database.
    multi_db = len(k2_dbs) > 1

    def _subset_samples(sids):
        subs = [samples_by_id[s] for s in sids if s in samples_by_id]
        coords = {s['sampleId']: sample_coords[s['sampleId']]
                  for s in subs if s['sampleId'] in sample_coords}
        return subs, coords

    def _finish(run, sources_subset):
        run['sources'] = sources_subset
        if not run['stations']:
            run['warning'] = ('Manifest run loaded (taxa/table only) — no matching station '
                              'coordinates. Check the metadata workbook in the manifest, or use '
                              "the in-browser 'assign sample' dialog to place samples on the map.")
        return run

    runs = []

    for db in k2_dbs:
        per_sample_reads = per_db_reads[db]
        long_df = pd.DataFrame(
            [{'ScientificName': sp, 'sample': sid, 'reads': float(v)}
             for sid, rmap in per_sample_reads.items() for sp, v in rmap.items()])
        pivot = (long_df.pivot_table(index='ScientificName', columns='sample',
                                     values='reads', aggfunc='sum', fill_value=0)
                 .reset_index())
        gmap = {sp: (sp_meta.get(sp, {}) or {}).get('group') or 'Unknown'
                for sp in pivot['ScientificName']}
        pivot.insert(1, 'Rank', 'species')
        pivot['Group'] = [gmap[sp] for sp in pivot['ScientificName']]
        subs, coords = _subset_samples(per_sample_reads.keys())
        meta = dict(job_meta, Method='Kraken2')
        if db:
            meta['Database'] = str(db)
        rid = f'{base_id}_kraken2' if both else base_id
        rlabel = f'{base_label} (Kraken2)' if both else base_label
        if multi_db:
            rid = f'{rid}_{_slug(db) if db else "unnamed_db"}'
            rlabel = f'{rlabel}{_RUN_SEP}{db}' if db else f'{rlabel}{_RUN_SEP}(no database)'
        run = build_run_from_matrix(pivot, coords, station_map, subs, gmap, {},
                                    rid, rlabel, meta, kind='kraken2')
        run['db'] = str(db) if db else None
        has_euk = False
        for t in run['taxa']:
            m = sp_meta.get(t['species'], {})
            t['commonName'] = m.get('commonName')
            t['isEuk'] = bool(m.get('isEuk'))
            if m.get('taxid'):
                t['taxid'] = str(m['taxid'])
            if m.get('lineage'):
                t['lineage'] = m['lineage']
            has_euk = has_euk or t['isEuk']
        run['hasEuk'] = has_euk
        src = [x for x in k2_sources if not multi_db or x.get('db') == db]
        runs.append(_finish(run, src + meta_sources))

    if has_asv:
        long_df = pd.DataFrame(asv_long)
        pivot = (long_df.pivot_table(index='ScientificName', columns='sample',
                                     values='reads', aggfunc='sum', fill_value=0)
                 .reset_index())
        gmap = {sp: asv_group_map.get(sp, 'Unknown') for sp in pivot['ScientificName']}
        pivot.insert(1, 'Rank', 'species')
        pivot['Group'] = [gmap[sp] for sp in pivot['ScientificName']]
        asv_sids = {r['sample'] for r in asv_long}
        subs, coords = _subset_samples(asv_sids)
        meta = dict(job_meta, Method='ASV (amplicon)')
        run = build_run_from_matrix(pivot, coords, station_map, subs, gmap, species_target,
                                    f'{base_id}_asv' if both else base_id,
                                    f'{base_label} (ASV)' if both else base_label,
                                    meta, kind='manifest')
        runs.append(_finish(run, asv_sources + meta_sources))

    # ---- Coverage gaps -------------------------------------------------------
    # Samples this project knows about that carry NO reads in any run: manifest
    # rows whose reports were missing/empty, and (when the project names its own
    # metadata workbook) metadata-sheet samples never linked to a report. The
    # HTML's "Data coverage" panel lists these next to the per-method gaps it
    # works out itself by comparing the project's ASV and Kraken2 runs.
    if runs:
        no_data = _coverage_no_data(samples, runs, station_map,
                                    project_specific=station_map is not default_station_map)
        for run in runs:
            run['noData'] = no_data

    if not runs:
        return [{'id': base_id, 'label': base_label, 'builtin': True, 'kind': 'manifest',
                 'stations': [], 'samples': samples, 'taxa': [], 'meta': job_meta,
                 'warning': ('Manifest run loaded but no K2/ASV read data parsed — '
                             'check the report paths in the manifest.'),
                 'sources': k2_sources + asv_sources + meta_sources}]
    return runs


def manifest_referenced_reports(manifest_path=MANIFEST):
    """Return the set of resolved report Paths (Kraken2 + ASV) referenced by the
    manifest -- for EVERY row, including disabled ones. Loose-file discovery
    (folder scan + loose WLM/Kraken2 scans) uses this to skip reports the
    manifest already owns, so they don't also surface as a duplicate standalone
    run. Disabled rows must stay in this set: otherwise a disabled sample's
    report file would simply be picked back up by the loose scan instead of
    being excluded, which defeats the manifest 'disable' flag entirely."""
    refs = set()
    df = read_manifest_df(manifest_path, quiet=True)
    if df is None:
        return refs
    cmap = _manifest_colmap(df.columns)
    df = _expand_barcode_rows(df, cmap)
    for _, r in df.iterrows():
        proj_val = (_clean(r[cmap['project_dir']]) if cmap.get('project_dir')
                    else _clean(r[cmap['project']]) if cmap.get('project') else None)
        for key in ('k2', 'asv'):
            if not cmap.get(key):
                continue
            for p in _split_paths(r[cmap[key]]):
                rp = _resolve_report(p, project=proj_val)
                if rp is None:
                    continue
                refs.add(rp.resolve())
                # The same filename sometimes exists in more than one place
                # (e.g. a loose copy plus one inside the project folder).
                # _resolve_report only returns the first match it finds, but
                # every same-named copy is the same manifest sample, so all of
                # them need to be excluded from loose discovery -- otherwise a
                # duplicate copy elsewhere just slips back in as its own run.
                dup_roots = [DATA_DIR]
                if proj_val and str(proj_val).lower() != 'nan':
                    dup_roots.append(DATA_PROJECTS / str(proj_val).strip())
                for root in dup_roots:
                    if not root.is_dir():
                        continue
                    for dup in (d for d in root.rglob(rp.name) if _search_ok(d)):
                        if not dup.name.startswith('~$'):
                            refs.add(dup.resolve())
    return refs


def load_manifest_runs(manifest_path=MANIFEST, group_field='project'):
    """Build runs from a per-sample report manifest, one run per distinct
    value of `group_field` (default 'project')."""
    df = read_manifest_df(manifest_path)
    if df is None or df.empty:
        return []
    cmap = _manifest_colmap(df.columns)
    if 'sample_id' not in cmap:
        log.error(f"manifest: no sample-id column found (have {list(df.columns)})")
        return []
    df = _drop_disabled(df, cmap)
    # Locally muted runs (SKIP_MANIFEST_RUNS) come out here -- before barcode
    # expansion, so none of their per-barcode children are built either.
    df = _drop_muted_runs(df, cmap)
    df = _apply_only_filters(df, cmap)
    df = _expand_barcode_rows(df, cmap)
    extra_cols = [c for c in df.columns
                  if c not in cmap.values() and c != _K2_DB_COL]

    # default station map (used only when a row names no metadata workbook)
    default_station_map, _ = load_mbon_station_map(DATA_DIR)
    station_cache = {}   # resolved metadata Path -> station map (loaded once)

    gcol = cmap.get(group_field) or cmap.get('project') or cmap.get('run')
    groups = {}
    for _, r in df.iterrows():
        if not _clean(r[cmap['sample_id']]):
            continue
        gkey = (_clean(r[gcol]) if gcol else None) or 'Manifest'
        groups.setdefault(gkey, []).append(r)

    runs = []
    for gkey, rows in groups.items():
        runs.extend(_build_manifest_project(gkey, rows, cmap, extra_cols,
                                            default_station_map, station_cache))
    return runs


# ---------------------------------------------------------------- discovery
# Toggle to re-enable Wilder_02-only (separated Samples+Taxa CSV) folders.
SKIP_WILDER02_ONLY = True       # --include-wilder02 turns this off
# Folder names to skip entirely during --include-unmanifested auto-discovery
# (case-insensitive). Set with --skip-folders or EDNA_SKIP_FOLDERS.
SKIP_FOLDERS = ec.split_list(os.getenv("EDNA_SKIP_FOLDERS"))   # --skip-folders


def detect_and_load(folder, man_refs=frozenset()):
    rid = folder.name
    label = folder.name.replace('_', ' ').title()
    if folder.name.lower() in {s.lower() for s in SKIP_FOLDERS}:
        log.info(f"{folder.name}: skipped (--skip-folders)")
        return None
    wlm_files = [p for p in folder.iterdir()
                 if p.name.startswith('WLM') and p.suffix == '.xlsx' and not p.name.startswith('~$')]
    # Skip files the manifest already owns (whether that manifest row is
    # active or disabled) so this folder-scan path can't reintroduce a sample
    # the manifest is explicitly excluding, or duplicate one it already loads.
    wlm_files_free = [p for p in wlm_files if p.resolve() not in man_refs]
    if wlm_files and not wlm_files_free:
        log.info(f"{folder.name}: skipped (all WLM files owned by manifest)")
        return None
    if wlm_files_free:
        # A WLM workbook (Wilder_01 layout with the read matrix) takes priority;
        # any Samples/Taxa CSVs in the same folder are ignored. Pass the
        # filtered file list explicitly (not just the folder) so any WLM file
        # in here that the manifest already owns -- active or disabled -- is
        # left out instead of being loaded twice / against the disable flag.
        return load_wilder01(folder, rid, label, files=sorted(wlm_files_free))
    kraken_files = [p for p in folder.iterdir()
                    if any(p.name.endswith(suf) for suf in
                           ('.kraken2.report', '.kraken2.report.txt', '.kreport', '.k2report'))
                    and not p.name.startswith('~$')]
    kraken_files_free = [p for p in kraken_files if p.resolve() not in man_refs]
    if kraken_files and not kraken_files_free:
        log.info(f"{folder.name}: skipped (all Kraken2 reports owned by manifest)")
        return None
    if kraken_files_free:
        return load_kraken2(folder, rid, label, files=sorted(kraken_files_free))
    has_s = any('sample' in p.name.lower() for p in folder.iterdir())
    has_t = any('taxa' in p.name.lower() for p in folder.iterdir())
    if has_s and has_t:
        if SKIP_WILDER02_ONLY:
            log.info(f"{folder.name}: skipped (Wilder_02-only folder; pass --include-wilder02 to load it)")
            return None
        return load_wilder02(folder, rid, label)
    return None


def _run_project(run):
    """Project key used to split runs into per-project HTML files. Manifest runs
    carry meta['Project']; folder/loose runs fall back to their label/id."""
    return (run.get('meta') or {}).get('Project') or run.get('label') or run.get('id') or 'runs'


def _sample_category(sid):
    """Classify a sample id so unplaceable bins can be flagged in the UI:
    'unclassified' (demux bin that failed barcode assignment), 'control'
    (negative/no-template controls), or 'sample' (a real biological sample)."""
    s = str(sid).lower()
    if 'unclassified' in s:
        return 'unclassified'
    if 'lntc' in s or 'ntc' in s or 'neg' in s or 'blank' in s:
        return 'control'
    return 'sample'


def _flag_unmappable(run):
    """Per the 'keep but flag separately' policy: keep all read totals intact,
    but annotate the run so the viewer can see how much of the data is
    unplaceable. Tags every sample with a 'category', records per-taxon
    unclassified/off-map read counts, and adds a run.meta summary (rendered in
    the Metadata tab) quantifying the unclassified fraction and what actually
    reaches the map. No effect on the Table/Map totals themselves."""
    samples = run.get('samples') or []
    for s in samples:
        s['category'] = _sample_category(s.get('sampleId', ''))
    mapped = {s['sampleId'] for s in samples if s.get('lat') is not None}
    cat = {s['sampleId']: s['category'] for s in samples}

    grand = uncl = ctrl = offmap = 0
    for t in run.get('taxa') or []:
        ps = t.get('perSample') or {}
        t_uncl = sum(v for sid, v in ps.items()
                     if cat.get(sid, _sample_category(sid)) == 'unclassified')
        t_ctrl = sum(v for sid, v in ps.items()
                     if cat.get(sid, _sample_category(sid)) == 'control')
        t_off = sum(v for sid, v in ps.items() if sid not in mapped)
        t['unclassifiedReads'] = int(t_uncl)
        t['controlReads'] = int(t_ctrl)
        t['offMapReads'] = int(t_off)
        grand += t.get('reads', 0) or 0
        uncl += t_uncl
        ctrl += t_ctrl
        offmap += t_off

    if grand:
        meta = run.setdefault('meta', {})
        pct = lambda n: f'{int(n):,} ({n / grand:.0%})'
        meta['Reads on map'] = pct(grand - offmap)
        meta['Reads off map (no coordinates)'] = pct(offmap)
        if uncl:
            meta['— in unclassified bins'] = pct(uncl)
        if ctrl:
            meta['— in controls'] = pct(ctrl)
        n_uncl = sum(1 for s in samples if s['category'] == 'unclassified')
        if offmap and offmap / grand >= 0.5:
            note = (f"{offmap / grand:.0%} of reads are in samples with no coordinates "
                    f"({n_uncl} unclassified demultiplexing bin(s) + controls/unmatched "
                    f"barcodes) and cannot be shown on the map; they remain in the "
                    f"Table/charts. See the Metadata tab for the breakdown.")
            run['warning'] = (run.get('warning') + ' ' + note) if run.get('warning') else note
    return run


def _assemble_payload(runs, title=None, is_example=False, inputs=None):
    """Top-level mbonData object consumed by the template."""
    all_lats = [s['lat'] for r in runs for s in r['stations']]
    all_lons = [s['lon'] for r in runs for s in r['stations']]
    if all_lats:
        center, zoom = [float(np.mean(all_lats)), float(np.mean(all_lons))], 9
        span = max(max(all_lats) - min(all_lats), max(all_lons) - min(all_lons))
        # rough zoom so every station fits: ~360/2^z degrees per screen width
        zoom = int(max(2, min(11, np.floor(np.log2(360 / max(span * 3, 0.05))))))
    else:
        center, zoom = list(DEFAULT_MAP_CENTER), DEFAULT_MAP_ZOOM
    return {'map': {'center': center, 'zoom': zoom},
            'title': title or REPORT_TITLE,
            'isExample': bool(is_example),
            'generatedAt': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
            'generator': f'build_mbon_html.py {__version__}',
            'inputs': inputs or {},
            'runs': runs,
            'stations': runs[0]['stations'] if runs else []}  # legacy seed


# Template slot. New templates wrap the payload in explicit markers so the
# replacement can never stop early at a '};' that happens to appear inside the
# data; the legacy `var mbonData = {...};` form is still accepted.
_SLOT_RE = re.compile(r"/\*@@MBON_DATA_START@@\*/.*?/\*@@MBON_DATA_END@@\*/", re.S)
_LEGACY_SLOT_RE = re.compile(r"var mbonData = \{.*?\};", re.S)


def payload_to_js(payload):
    """JSON for embedding inside <script>: ASCII-only and with '</' escaped so a
    stray '</script>' in a species name or note can't terminate the block."""
    return (json.dumps(payload, ensure_ascii=True, allow_nan=False, default=_json_default)
            .replace('</', '<\\/').replace('<!--', '<\\!--'))


def _json_default(o):
    """Make numpy / pandas / datetime values JSON-safe instead of crashing."""
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if np.isnan(o) else float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (pd.Timestamp, datetime)):
        return o.isoformat()
    if isinstance(o, Path):
        return o.name
    if isinstance(o, set):
        return sorted(o)
    return str(o)


def _sanitize(obj):
    """Replace NaN/inf floats (invalid JSON) with None, recursively."""
    if isinstance(obj, float):
        return None if (np.isnan(obj) or np.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {str(k): _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj


# ---------------------------------------------------------------- organism icons
# PhyloPic silhouettes live OUTSIDE this repo (default: ../icons next to it, or
# $EDNA_ICONS_DIR / --icons-dir). That folder's phylopic_icons.py matches every
# organism by name -> taxid -> genus -> lineage -> group and the matched SVGs are
# embedded as mbonData.icons, so the report stays offline/self-contained.
ICONS_DIR = None            # set in main(); None = icons off


def _default_icons_dir():
    env = os.getenv('EDNA_ICONS_DIR')
    if env:
        return ec.resolve_path(env)
    cand = ec.REPO_ROOT.parent / 'icons'
    return cand if (cand / 'phylopic_icons.py').exists() else None


def _attach_icons(payload):
    """Add payload['icons'] from ICONS_DIR (best effort: never fails a build)."""
    if not ICONS_DIR:
        return payload
    mod_path = Path(ICONS_DIR) / 'phylopic_icons.py'
    if not mod_path.exists():
        log.warning("icons: %s not found — report built without organism icons", mod_path)
        return payload
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location('phylopic_icons', mod_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        payload = dict(payload)
        payload['icons'] = mod.icon_payload(payload, ICONS_DIR, log=log.info)
    except Exception as e:      # icons are decoration; a problem there must not lose the report
        log.warning("icons: skipped (%s)", e)
    return payload


def _render(payload, template_path, output_path):
    """Inject the payload into the template's data slot and write the HTML to
    output_path atomically (a failed build never leaves a half-written report)."""
    template_path = ec.require_file(template_path, 'HTML template', ('.html', '.htm'))
    tpl = Path(template_path).read_text(encoding='utf-8')
    payload = _attach_icons(payload)
    data_js = payload_to_js(_sanitize(payload))
    if _SLOT_RE.search(tpl):
        repl = "/*@@MBON_DATA_START@@*/" + data_js + "/*@@MBON_DATA_END@@*/"
        rendered = _SLOT_RE.sub(lambda _m: repl, tpl, count=1)
    elif _LEGACY_SLOT_RE.search(tpl):
        log.warning("template uses the legacy `var mbonData = {...};` slot — consider "
                    "updating to the /*@@MBON_DATA_START@@*/ markers")
        rendered = _LEGACY_SLOT_RE.sub(lambda _m: "var mbonData = " + data_js + ";", tpl, count=1)
    else:
        raise InputError(f"no mbonData placeholder in template {template_path} "
                         f"(expected /*@@MBON_DATA_START@@*/ ... /*@@MBON_DATA_END@@*/)")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=output_path.name + '.', suffix='.tmp', dir=output_path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(rendered)
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(tmp, 0o666 & ~umask)        # mkstemp creates 0600; use normal perms
        os.replace(tmp, output_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    n_st = sum(len(r.get('stations') or []) for r in payload['runs'])
    log.info("saved %s  (%.1f MB, %d run(s), %d station(s))", ec.rel(output_path),
             len(rendered) / 1e6, len(payload['runs']), n_st)
    return rendered


def _gather_runs():
    runs = []
    loaded_wlm = set()   # WLM filenames already pulled in via a loaded folder
    # Every report file the manifest touches, active or disabled. Computed once
    # up front so ALL loose-discovery steps below (folder scan, loose WLM,
    # loose Kraken2) can skip files the manifest already owns -- including
    # disabled ones, so disabling a manifest row actually excludes it instead
    # of letting it leak back in via folder/loose discovery.
    man_refs = manifest_referenced_reports(MANIFEST)

    # When the manifest is authoritative (default), steps 1-3 below --
    # folder-scan and loose-file auto-discovery -- are skipped outright, so a
    # file under data/ that was never added to the manifest can't sneak into
    # the report next to it. Only the manifest's own non-disabled rows (step
    # 4) contribute data. This is what makes the `disable` column -- and
    # simply never adding a row for a file -- a complete, not partial, way to
    # keep something out of the report.
    exclusive = MANIFEST_EXCLUSIVE and _manifest_has_rows(MANIFEST)
    if exclusive:
        log.info("manifest: authoritative -- skipping folder/loose-file auto-discovery "
              "(pass --include-unmanifested to also scan data/ directly)")

    # 1) Run folders under data/
    if not exclusive and not DATA_DIR.is_dir():
        log.warning("data folder %s not found — skipping auto-discovery", DATA_DIR)
        exclusive = True
    for folder in ([] if exclusive else sorted([p for p in DATA_DIR.iterdir()
                                                if p.is_dir() and not ec.is_temp_file(p)])):
        try:
            run = detect_and_load(folder, man_refs=man_refs)
        except Exception as e:
            log.error(f"{folder.name}: {e}")
            run = None
        if run:
            runs.append(run)
            loaded_wlm |= {p.name for p in folder.glob("WLM*.xlsx")}
            log.info(f"{folder.name}: {len(run['stations'])} stations, "
                  f"{len(run['taxa'])} taxa, {len(run['samples'])} samples"
                  + (f"  [WARN: {run['warning'][:60]}...]" if run['warning'] else ""))

    # 2) Loose WLM*.xlsx workbooks placed directly under data/ (each its own run),
    #    skipping any whose filename was already loaded inside a folder above,
    #    or that the manifest already owns (active or disabled).
    loose = [] if exclusive else sorted([p for p in DATA_DIR.glob("WLM*.xlsx")
                    if not p.name.startswith('~$') and p.name not in loaded_wlm
                    and p.resolve() not in man_refs])
    for f in loose:
        try:
            run = load_wilder01(DATA_DIR, f.stem, f.stem, files=[f])
        except Exception as e:
            log.error(f"{f.name}: {e}")
            run = None
        if run:
            runs.append(run)
            log.info(f"{f.name}: {len(run['stations'])} stations, "
                  f"{len(run['taxa'])} taxa, {len(run['samples'])} samples"
                  + (f"  [WARN: {run['warning'][:60]}...]" if run['warning'] else ""))

    # 3) Loose Kraken2 reports placed directly under data/ collapse into one run,
    #    EXCEPT any report already referenced by the manifest (those belong to a
    #    manifest project run, so listing them again as a generic "Kraken2 reports"
    #    run would be a duplicate).
    loose_k2 = []
    if not exclusive:
        for pat in KRAKEN_GLOBS:
            loose_k2 += [p for p in DATA_DIR.glob(pat) if not p.name.startswith('~$')]
    loose_k2 = sorted({p for p in set(loose_k2) if p.resolve() not in man_refs})
    if loose_k2:
        try:
            run = load_kraken2(DATA_DIR, "kraken2_loose", "Kraken2 reports", files=loose_k2)
        except Exception as e:
            log.error(f"loose kraken2: {e}")
            run = None
        if run:
            runs.append(run)
            log.info(f"Kraken2 ({len(loose_k2)} reports): {len(run['stations'])} stations, "
                  f"{len(run['taxa'])} taxa, {len(run['samples'])} samples"
                  + (f"  [WARN: {run['warning'][:60]}...]" if run['warning'] else ""))

    # 4) Manifest-driven runs: a per-sample sheet linking each sample to its
    #    run/project/cruise and to its K2 + ASV report files. Grouped by project.
    try:
        man_runs = load_manifest_runs(MANIFEST, group_field='project')
    except Exception as e:
        log.error(f"manifest: {e}")
        man_runs = []
    for run in man_runs:
        runs.append(run)
        log.info(f"manifest '{run['label']}': {len(run['stations'])} stations, "
              f"{len(run['taxa'])} taxa, {len(run['samples'])} samples"
              + (f"  [WARN: {run['warning'][:60]}...]" if run['warning'] else ""))

    return runs


def build(template=None, output=None, split=False, title=None, is_example=False):
    """Gather every run and render the combined interactive HTML. When
    split=True, also write one standalone HTML per project alongside it.
    Returns the payload, or None when nothing could be built."""
    template = Path(template) if template else TEMPLATE
    output = Path(output) if output else OUTPUT
    ec.require_file(template, 'HTML template', ('.html', '.htm'))   # fail fast
    runs = _gather_runs()
    if not runs:
        log.error("no runs could be built. Checked manifest %s and data folder %s. "
                  "Run with --validate for a full input check, or --example for a demo.",
                  ec.rel(MANIFEST), ec.rel(DATA_DIR))
        return None

    # Flag unplaceable (unclassified/control) reads without altering totals.
    for r in runs:
        _flag_unmappable(r)
        if not r.get('taxa'):
            log.warning("run '%s' has no taxa with reads > 0", r.get('label'))

    inputs = {'manifest': MANIFEST.name if MANIFEST.exists() else None,
              'projectsDir': ec.rel(DATA_PROJECTS)}
    payload = _assemble_payload(runs, title=title, is_example=is_example, inputs=inputs)
    _render(payload, template, output)

    if split:
        groups = {}
        for r in runs:
            groups.setdefault(_run_project(r), []).append(r)
        out_dir = output.parent
        log.info(f"--split: writing {len(groups)} per-project file(s) to {ec.rel(out_dir)}")
        for proj, subset in groups.items():
            sub_out = out_dir / f"{_slug(proj)}.html"
            try:
                _render(_assemble_payload(subset, title=f"{title or REPORT_TITLE} — {proj}",
                                          is_example=is_example, inputs=inputs),
                        template, sub_out)
            except Exception as e:      # one bad project must not lose the others
                log.error("--split: project %s failed: %s", proj, e)
    return payload


# ---------------------------------------------------------------- validation
def validate_inputs(template=None):
    """Check every input the build would touch WITHOUT building anything:
    template, projects folder, manifest columns, and that every report /
    metadata / barcode file named in the manifest resolves to a readable file.
    Returns an ec.ValidationReport."""
    rep = ec.ValidationReport()
    template = Path(template) if template else TEMPLATE
    try:
        ec.require_file(template, 'HTML template', ('.html', '.htm'))
        txt = template.read_text(encoding='utf-8', errors='replace')
        if not (_SLOT_RE.search(txt) or _LEGACY_SLOT_RE.search(txt)):
            rep.error(f"template {template.name} has no mbonData placeholder")
    except InputError as e:
        rep.error(str(e))
    rep.checked += 1

    if not DATA_PROJECTS.is_dir():
        rep.error(f"projects folder not found: {DATA_PROJECTS}")
        return rep
    df = read_manifest_df(MANIFEST)
    rep.checked += 1
    if df is None:
        rep.warn(f"no readable manifest at {ec.rel(MANIFEST)} — only "
                 f"--include-unmanifested auto-discovery would run")
        return rep
    cmap = _manifest_colmap(df.columns)
    if 'sample_id' not in cmap:
        rep.error(f"manifest {MANIFEST.name}: no sample_id column")
        return rep
    df = _drop_disabled(df, cmap)
    seen = {}
    for i, r in df.iterrows():
        row_no = i + 2                                   # 1-based + header row
        sid = _clean(r[cmap['sample_id']])
        if not sid:
            continue
        rep.checked += 1
        proj = (_clean(r[cmap['project_dir']]) if cmap.get('project_dir')
                else _clean(r[cmap['project']]) if cmap.get('project') else None)
        if proj and not (DATA_PROJECTS / proj).is_dir():
            rep.error(f"manifest row {row_no} ({sid}): project folder "
                      f"{ec.rel(DATA_PROJECTS / proj)} does not exist")
        key = (sid, _clean(r[cmap['run']]) if cmap.get('run') else None)
        if key in seen:
            rep.warn(f"manifest row {row_no}: duplicate sample_id/run {key} (also row {seen[key]})")
        seen[key] = row_no
        has_data = False
        for col, label in (('k2', 'k2_report'), ('asv', 'asv_report'), ('metadata', 'metadata')):
            if not cmap.get(col):
                continue
            for ref in _split_paths(r[cmap[col]]):
                hits = (_resolve_k2_dirs(ref, project=proj) if col == 'k2'
                        else [(_resolve_report(ref, project=proj), None)])
                hits = [h for h, _ in hits if h is not None]
                if not hits:
                    msg = f"manifest row {row_no} ({sid}): {label} '{ref}' not found"
                    (rep.warn if col == 'metadata' else rep.error)(msg)
                    continue
                has_data = has_data or col in ('k2', 'asv')
                for h in hits:
                    if h.is_file():
                        try:
                            ext = (ec.SPREADSHEET_EXT + ('.csv',)) if col != 'k2' else None
                            ec.require_file(h, label, ext)
                            if col == 'asv':
                                ec.require_sheets(h, ['metadata', 'aggregated'], 'ASV workbook')
                        except InputError as e:
                            rep.error(f"manifest row {row_no} ({sid}): {e}")
                    elif h.is_dir() and col == 'k2':
                        n = sum(1 for f in h.rglob('*') if _is_kraken_report_file(f))
                        if n == 0:
                            rep.error(f"manifest row {row_no} ({sid}): k2_report folder "
                                      f"{ec.rel(h)} contains no Kraken2 report files")
        if cmap.get('barcodes'):
            cv = _clean(r[cmap['barcodes']])
            if cv and cv.lower().endswith(('.xlsx', '.xlsm', '.xls', '.csv')):
                if _resolve_report(cv, project=proj) is None:
                    rep.error(f"manifest row {row_no} ({sid}): barcodes workbook '{cv}' not found")
        if not has_data and (cmap.get('k2') or cmap.get('asv')):
            rep.warn(f"manifest row {row_no} ({sid}): no k2_report / asv_report resolved — "
                     f"the sample will carry no taxonomic data")
    return rep


# ---------------------------------------------------------------- CLI
def build_parser():
    import argparse
    p = argparse.ArgumentParser(
        prog="build_mbon_html.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Build the interactive, map-based eDNA taxonomic-hit report (HTML) from "
                    "data/projects/ and its report_manifest.xlsx.",
        epilog="examples:\n"
               "  python build_mbon_html.py --example\n"
               "  python build_mbon_html.py --validate\n"
               "  python build_mbon_html.py --projects OceanX --split\n"
               "  python build_mbon_html.py --projects-dir /mnt/share/projects -o reports/latest.html\n")
    p.add_argument('-o', '--output', default=None,
                   help=f"output HTML path (default: {ec.rel(OUTPUT)}, or "
                        f"examples/example_report.html with --example)")
    p.add_argument('-t', '--template', default=str(TEMPLATE),
                   help=f"input HTML template (default: {ec.rel(TEMPLATE)})")
    p.add_argument('--projects-dir', dest='projects_dir', default=None,
                   help="standardized projects folder -- <dir>/<project>/{faire,kraken2,wilder,meta}/ "
                        f"(default: {ec.rel(DATA_PROJECTS)}; env EDNA_PROJECTS_DIR)")
    p.add_argument('-m', '--manifest', default=None,
                   help="report manifest (.xlsx/.csv). Default: <projects-dir>/report_manifest.xlsx")
    p.add_argument('-d', '--data', default=None,
                   help="legacy flat data directory, used only for --include-unmanifested "
                        f"auto-discovery (default: {ec.rel(DATA_DIR)})")
    p.add_argument('--example', action='store_true',
                   help="build the demo report from the bundled synthetic data in "
                        f"{ec.rel(ec.EXAMPLE_PROJECTS_DIR)}")
    p.add_argument('--validate', action='store_true',
                   help="check all inputs (manifest, referenced files, template) and exit "
                        "without writing a report")
    p.add_argument('--title', default=None, help=f"report title (default: '{REPORT_TITLE}')")
    p.add_argument('--projects', default=None,
                   help="comma-separated project name(s) to build (manifest 'project' column). "
                        "Default: all projects.")
    p.add_argument('--runs', default=None,
                   help="comma-separated run / sample_id value(s) to build. Default: all runs.")
    p.add_argument('--skip-runs', default=None,
                   help="comma-separated run / sample_id value(s) to leave out without editing "
                        "the manifest (env EDNA_SKIP_RUNS)")
    p.add_argument('--skip-folders', default=None,
                   help="comma-separated folder names to ignore during --include-unmanifested "
                        "discovery (env EDNA_SKIP_FOLDERS)")
    p.add_argument('--station-metadata', default=None,
                   help="default station-metadata workbook (FAIRe sampleMetadata) used when a "
                        "manifest row names none")
    p.add_argument('--station-metadata-glob', default=None,
                   help="comma-separated glob(s) used to find a default station-metadata workbook "
                        f"(default: {','.join(STATION_METADATA_GLOBS)})")
    p.add_argument('--split', action='store_true',
                   help="also write one standalone HTML per project")
    p.add_argument('--json-out', default=None,
                   help="also save the report payload as JSON (re-render later with --from-json)")
    p.add_argument('--from-json', default=None,
                   help="skip all data loading and render a previously saved payload JSON")
    p.add_argument('--include-unmanifested', action='store_true',
                   help="also auto-discover run folders / loose WLM & Kraken2 files under the "
                        "--data folder that aren't in the manifest")
    p.add_argument('--include-wilder02', action='store_true',
                   help="load Wilder Samples+Taxa CSV-only folders during auto-discovery")
    p.add_argument('--icons-dir', default=None,
                   help="PhyloPic icons folder with phylopic_icons.py + svg/ (default: $EDNA_ICONS_DIR, "
                        "else ../icons next to this repo when present)")
    p.add_argument('--no-icons', action='store_true', help='build without organism silhouettes')
    p.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    ec.add_logging_args(p)
    return p


_PROJECT_SUBDIRS = ('faire', 'kraken2', 'wilder', 'meta')


def _resolve_projects_root(path, given):
    """(projects_root, single_project_or_None) for a --projects-dir value.

    ``data/projects``       -> (data/projects, None)
    ``data/projects/MBON``  -> (data/projects, 'MBON')   one project folder
    ``data/MBON``           -> (data/projects, 'MBON')   shorthand, if that exists
    """
    path = Path(path)
    if (path.is_dir() and any((path / d).is_dir() for d in _PROJECT_SUBDIRS)
            and not (path / ec.MANIFEST_NAME).exists()):
        log.info("--projects-dir %r is a single project -> projects root %s, project '%s'",
                 given, ec.rel(path.parent), path.name)
        return path.parent, path.name
    if not path.is_dir() and given:
        for root in (path.parent / 'projects', ec.DATA_DIR / 'projects'):
            if (root / path.name).is_dir():
                log.info("--projects-dir %r not found -> using projects root %s, project '%s'",
                         given, ec.rel(root), path.name)
                return root, path.name
    return path, None


def main(argv=None):
    global DATA_DIR, DATA_PROJECTS, MANIFEST, MANIFEST_EXCLUSIVE, ONLY_PROJECTS, ONLY_RUNS
    global SKIP_MANIFEST_RUNS, SKIP_FOLDERS, SKIP_WILDER02_ONLY
    global STATION_METADATA_FILE, STATION_METADATA_GLOBS, ICONS_DIR
    args = build_parser().parse_args(argv)
    ec.setup_logging("build_mbon_html", args.log_level, args.log_file, args.quiet)
    ec.load_dotenv_if_present()
    log.info("build_mbon_html %s (pandas %s)", __version__, pd.__version__)

    _MANIFEST_CACHE.clear()
    _STATION_CACHE.clear()
    try:
        # Reassign module globals so the resolver/manifest loaders pick up overrides.
        if args.example:
            DATA_PROJECTS = ec.EXAMPLE_PROJECTS_DIR
        elif args.projects_dir or os.getenv("EDNA_PROJECTS_DIR"):
            DATA_PROJECTS = ec.resolve_path(args.projects_dir or os.getenv("EDNA_PROJECTS_DIR"))
        # Tolerate --projects-dir pointing at ONE project (data/projects/MBON) or
        # a shorthand that doesn't exist (data/MBON): resolve it to the real
        # projects root and build just that project. Otherwise the bad root
        # silently breaks concordance + report lookups and samples lose metadata.
        auto_project = None
        if not args.example:
            DATA_PROJECTS, auto_project = _resolve_projects_root(DATA_PROJECTS, args.projects_dir)
        # The legacy/basename-search data folder defaults to the folder that
        # contains the projects dir (data/ for data/projects), so searches never
        # wander into an unrelated tree.
        DATA_DIR = ec.resolve_path(args.data) if args.data else DATA_PROJECTS.parent
        MANIFEST = ec.resolve_path(args.manifest) if args.manifest else DATA_PROJECTS / ec.MANIFEST_NAME
        template = ec.resolve_path(args.template)
        output = (ec.resolve_path(args.output) if args.output else
                  (ec.REPO_ROOT / "examples" / "example_report.html" if args.example else OUTPUT))
        # (re)set every option from this invocation, so repeated main() calls
        # (tests, notebooks) never inherit state from a previous run
        ONLY_PROJECTS = ec.split_list(args.projects) or ({auto_project} if auto_project else set())
        ONLY_RUNS = ec.split_list(args.runs)
        SKIP_MANIFEST_RUNS = ec.split_list(os.getenv("EDNA_SKIP_RUNS")) | ec.split_list(args.skip_runs)
        SKIP_FOLDERS = ec.split_list(os.getenv("EDNA_SKIP_FOLDERS")) | ec.split_list(args.skip_folders)
        SKIP_WILDER02_ONLY = not args.include_wilder02
        MANIFEST_EXCLUSIVE = not args.include_unmanifested
        ICONS_DIR = (None if args.no_icons else
                     ec.resolve_path(args.icons_dir) if args.icons_dir else _default_icons_dir())
        if ICONS_DIR:
            log.info("icons        : %s", ICONS_DIR)
        STATION_METADATA_FILE = None
        if args.station_metadata:
            STATION_METADATA_FILE = ec.require_file(ec.resolve_path(args.station_metadata),
                                                    'station metadata workbook')
        if args.station_metadata_glob:
            STATION_METADATA_GLOBS = sorted(ec.split_list(args.station_metadata_glob))

        log.info("projects dir : %s", ec.rel(DATA_PROJECTS))
        log.info("manifest     : %s%s", ec.rel(MANIFEST), "" if MANIFEST.exists() else "  (missing)")
        log.info("template     : %s", ec.rel(template))
        if SKIP_MANIFEST_RUNS:
            log.info("skipping runs: %s", ', '.join(sorted(SKIP_MANIFEST_RUNS)))
        if (not args.from_json and not DATA_PROJECTS.is_dir()
                and not (args.include_unmanifested and DATA_DIR.is_dir())):
            log.error("projects folder not found: %s — create it (see docs/DATA_FORMATS.md), "
                      "pass --projects-dir, or try --example", DATA_PROJECTS)
            return 2

        if args.from_json:
            src = ec.require_file(ec.resolve_path(args.from_json), 'payload JSON', ('.json',))
            try:
                payload = json.loads(src.read_text(encoding='utf-8'))
            except json.JSONDecodeError as e:
                raise InputError(f"{src.name} is not valid JSON: {e}") from e
            if not isinstance(payload, dict) or not isinstance(payload.get('runs'), list):
                raise InputError(f"{src.name} is not a report payload (needs a 'runs' list)")
            if args.title:
                payload['title'] = args.title
            payload.setdefault('map', {'center': list(DEFAULT_MAP_CENTER), 'zoom': DEFAULT_MAP_ZOOM})
            _render(payload, template, output)
            return ec.log_summary(log)

        if args.validate:
            rep = validate_inputs(template)
            return rep.emit(log, "input validation")

        payload = build(template=template, output=output, split=args.split,
                        title=args.title, is_example=args.example)
        if payload is None:
            ec.log_summary(log)
            return 2
        if args.json_out:
            jp = ec.resolve_path(args.json_out)
            jp.parent.mkdir(parents=True, exist_ok=True)
            jp.write_text(json.dumps(_sanitize(payload), default=_json_default, indent=1),
                          encoding='utf-8')
            log.info("payload JSON saved to %s", ec.rel(jp))
        return ec.log_summary(log)
    except InputError as e:
        log.error("%s", e)
        return 2
    except KeyboardInterrupt:
        log.error("interrupted")
        return 130
    except Exception:
        log.exception("unexpected failure — please report this with the log file")
        return 1


if __name__ == '__main__':
    sys.exit(main())
