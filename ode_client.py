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
ode_client.py — pull projects from the Ocean DNA Explorer (oceandnaexplorer.org)
==============================================================================

Standard library only. Three ways to use it:

1. **Python** (the low-level API)::

       from ode_client import ODEClient, pull_project
       c = ODEClient()
       c.projects()                         # every project, with analyses + file URLs
       c.project("noaa-aoml-mire")          # one project (KeyError-free: ODEError 404)
       c.find_samples("Horseshoe")          # samples across all projects
       pull_project(c, "noaa-aoml-mire", analyses=["MiFish"], match="Horseshoe")

2. **Command line**::

       python ode_client.py projects                       # list projects
       python ode_client.py show noaa-aoml-mire            # analyses, samples, dates, extent
       python ode_client.py find-samples Horseshoe         # samples across projects
       python ode_client.py pull noaa-aoml-mire --analysis MiFish --manifest --build
       python ode_client.py catalog                        # data/ode_catalog.json for reports
       python ode_client.py serve                          # local bridge for the HTML report

3. **Local bridge** (``serve``, or ``api/run.sh`` which mounts the same routes):
   the HTML report's "Import from Ocean DNA Explorer" dialog talks to it to
   search projects, preview them and run import jobs with live status. The
   Ocean DNA Explorer API does not send CORS headers, so a report opened from
   disk cannot call it directly; its file store does, so without a bridge the
   report still imports from the catalog snapshot embedded at build time.

   GET  /api/ode/health                      bridge + upstream status
   GET  /api/ode/projects?q=                 projects (+ analyses, counts, file URLs)
   GET  /api/ode/projects/<project_id>       one project
   GET  /api/ode/projects/<project_id>/preview   samples / libraries / dates / extent
   GET  /api/ode/samples?q=&project_id=      sample search (all projects)
   GET  /api/ode/catalog                     the catalog snapshot format, live
   GET  /api/ode/file?url=                   proxied (cached) ODE file download
   POST /api/ode/jobs                        start an import  {project_id, analyses?,
                                             samples?, match?, build?, save?, manifest?}
   GET  /api/ode/jobs/<id>                   job status: state + per-step progress
   GET  /api/ode/jobs/<id>/result            runs (report JSON) and/or raw files
   DELETE /api/ode/jobs/<id>                 cancel

Files are cached by URL under data/ode_cache/ (ODE file URLs are immutable:
a resubmission gets a new random suffix). ``pull`` writes the standard project
layout the report builder already reads::

    data/projects/<project_id>/faire/        <project_id>_{project,sample,experimentRun}Metadata.tsv
    data/projects/<project_id>/tourmaline/   table_<analysis>-<id>.tsv + asvTaxaFeatures_<analysis>-<id>.tsv
    data/projects/<project_id>/meta/         ode_import.json, analysisMetadata_<analysis>.tsv
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

__version__ = "1.0.0"

log = logging.getLogger("ode_client")

REPO_ROOT = Path(__file__).resolve().parent
ODE_BASE = os.getenv("ODE_BASE_URL", "https://www.oceandnaexplorer.org").rstrip("/")
DEFAULT_CACHE = Path(os.getenv("ODE_CACHE_DIR") or (REPO_ROOT / "data" / "ode_cache"))
DEFAULT_CATALOG = REPO_ROOT / "data" / "ode_catalog.json"
DEFAULT_PROJECTS = Path(os.getenv("EDNA_PROJECTS_DIR") or (REPO_ROOT / "data" / "projects"))
BRIDGE_HOST = os.getenv("ODE_BRIDGE_HOST", "127.0.0.1")
BRIDGE_PORT = int(os.getenv("ODE_BRIDGE_PORT", "5051"))
USER_AGENT = f"eDNA_processing-ode_client/{__version__} (+https://github.com/jhuapl-bio/eDNA_processing)"

# Only these hosts are fetched by the /file proxy (and by downloads generally).
FILE_HOST_RE = re.compile(r"(^|\.)(public\.blob\.vercel-storage\.com|oceandnaexplorer\.org)$", re.I)

PROJECT_FIELDS = ["id", "project_id", "project_name", "institution", "dateSubmitted",
                  "projectMetadataFileUrl_ODE", "sampleMetadataFileUrl_ODE",
                  "libraryMetadataFileUrl_ODE"]
ANALYSIS_FIELDS = ["id", "project_id", "analysis_run_name", "assay_name", "trusted", "dateSubmitted",
                   "asvFileUrl_ODE", "occurrenceFileUrl_ODE", "analysisMetadataFileUrl_ODE"]
SAMPLE_FIELDS = ["id", "project_id", "samp_name", "decimalLatitude", "decimalLongitude", "eventDate",
                 "station_id", "geo_loc_name", "samp_category", "expedition_id"]

# Short marker labels used in run names ('Fish-12S-MiFish-U-Miya-Sales' -> 'MiFish'),
# same order as build_mbon_html._ode_short_assay.
_SHORT_TOKENS = ("MiFish", "Riaz", "Leray", "Folmer", "Teleo", "MarVer1", "MarVer3", "Parada",
                 "COI", "18S", "16S", "12S")


class ODEError(Exception):
    """An Ocean DNA Explorer request failed. ``status`` is the HTTP status
    (0 = network error / unreachable)."""

    def __init__(self, message, status=0, url=None):
        super().__init__(message)
        self.status = status
        self.url = url

    def to_json(self):
        return {"error": str(self), "status": self.status, "url": self.url}


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------- helpers
def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slug(v):
    return re.sub(r"[^A-Za-z0-9]+", "_", str(v)).strip("_") or "x"


def short_assay(assay):
    """'Fish-12S-MiFish-U-Miya-Sales' -> 'MiFish'; 'Universal-SSU-V4V5-Parada' -> 'Parada'."""
    s = str(assay or "")
    for tok in _SHORT_TOKENS:
        m = re.search(tok, s, re.I)
        if m:
            return s[m.start():m.end()]
    m = re.search(r"ITS\d?", s, re.I)
    return m.group(0) if m else s


def file_basename(url):
    """Decoded file name of an ODE file URL (blob names are sometimes double-encoded)."""
    name = urllib.parse.urlparse(url or "").path.rsplit("/", 1)[-1]
    for _ in range(3):
        dec = urllib.parse.unquote(name)
        if dec == name:
            break
        name = dec
    return name


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


_BLANK_RE = re.compile(r"^(nan|none|null|na|n/a|not applicable|missing.*|not provided|not collected|"
                       r"restricted access|unknown|-?9999(\.0+)?)$", re.I)


def iso_date(v):
    """'7/6/19' / '2019-07-06T14:20:00Z' / '20190706' -> '2019-07-06' (else the input)."""
    s = str(v or "").strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})\b", s)
    if m:
        y = int(m.group(3))
        y = y + (2000 if y < 70 else 1900) if y < 100 else y
        return f"{y:04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    m = re.match(r"^(\d{4})-?(\d{2})-?(\d{2})", s)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else (s or None)


def blank(v):
    s = str(v if v is not None else "").strip()
    return not s or bool(_BLANK_RE.match(s))


def read_tsv(text):
    """FAIRe / Tourmaline text table -> (header, rows). '#' comment lines and
    blank lines are skipped; the delimiter is sniffed from the first line."""
    if isinstance(text, bytes):
        text = text.decode("utf-8-sig", errors="replace")
    text = text.lstrip("﻿")
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        return [], []
    first = lines[0]
    sep = max(["\t", ",", ";", "|"], key=first.count)
    if not first.count(sep):
        sep = "\t"
    rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=sep))
    hdr = [h.strip() for h in rows[0]]
    return hdr, rows[1:]


def records(text):
    hdr, rows = read_tsv(text)
    return [{h: (r[i].strip() if i < len(r) else "") for i, h in enumerate(hdr) if h} for r in rows]


def write_tsv(header, rows):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter="\t", lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


def parse_project_metadata(text):
    """FAIRe projectMetadata -> {'project': {term: value}, 'assays': {assay: {term: value}}}."""
    hdr, rows = read_tsv(text)
    if not hdr:
        return {"project": {}, "assays": {}}
    low = [_norm(h) for h in hdr]
    ti = low.index("termname") if "termname" in low else 0
    pi = low.index("projectlevel") if "projectlevel" in low else (low.index("values") if "values" in low else ti + 1)
    assay_cols = [i for i, h in enumerate(hdr) if i not in (ti, pi) and _norm(h) not in
                  ("requirementlevelcode", "section", "termname", "projectlevel", "values", "")]
    proj, assays = {}, {hdr[i]: {} for i in assay_cols}
    for r in rows:
        t = r[ti].strip() if ti < len(r) else ""
        if not t:
            continue
        if pi < len(r) and not blank(r[pi]):
            proj[t] = r[pi].strip()
        for i in assay_cols:
            if i < len(r) and not blank(r[i]):
                assays[hdr[i]][t] = r[i].strip()
    return {"project": proj, "assays": {k: v for k, v in assays.items() if v}}


def _float(v):
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    return None if f != f or f <= -9000 else f


def summarize_samples(sample_text, library_text=None):
    """Compact per-sample summary of a project's FAIRe sheets (for previews):
    [{samp_name, lat, lon, date, station, category, control, cruise, place, depth,
      libs: {assay: [lib_id, ...]}}]."""
    samples = records(sample_text) if sample_text else []
    libs = records(library_text) if library_text else []
    by_samp = {}
    for lr in libs:
        sn = lr.get("samp_name") or ""
        if not sn or not lr.get("lib_id"):
            continue
        by_samp.setdefault(sn, {}).setdefault(lr.get("assay_name") or "?", []).append(lr["lib_id"])
    out, seen = [], set()
    for s in samples:
        sn = s.get("samp_name") or s.get("samp_number") or s.get("sample_ID")
        if not sn or sn in seen:
            continue
        seen.add(sn)
        cat = (s.get("samp_category") or "").strip()
        neg = (s.get("neg_cont_type") or "").strip()
        ctrl = bool(re.search(r"control|blank", cat, re.I)) or (bool(neg) and not blank(neg))
        lmin, lmax = _float(s.get("minimumDepthInMeters")), _float(s.get("maximumDepthInMeters"))
        depth = (lmin + lmax) / 2 if lmin is not None and lmax is not None else (lmin if lmin is not None else lmax)
        pick = lambda *ks: next((s[k] for k in ks if k in s and not blank(s[k])), None)
        out.append({
            "samp_name": sn,
            "lat": _float(s.get("decimalLatitude")), "lon": _float(s.get("decimalLongitude")),
            "date": iso_date(pick("eventDate", "collection_date", "verbatimEventDate")),
            "station": pick("station_id", "ctd_bottle_number", "site", "Station", "locality"),
            "cruise": pick("expedition_id", "cruise_id", "Cruise Number"),
            "place": pick("geo_loc_name"),
            "category": cat or None, "control": ctrl,
            "depth": depth, "depthClass": pick("depth_category"),
            "libs": by_samp.get(sn, {}),
        })
    # libraries whose sample is missing from the sample sheet
    for sn, assays in by_samp.items():
        if sn not in seen:
            out.append({"samp_name": sn, "lat": None, "lon": None, "date": None, "station": None,
                        "cruise": None, "place": None, "category": None, "control": False,
                        "depth": None, "depthClass": None, "libs": assays, "notInSheet": True})
    return out


def preview_summary(samples):
    """Totals over summarize_samples() output."""
    dates = sorted(str(s["date"])[:10] for s in samples if s.get("date"))
    lats = [s["lat"] for s in samples if s.get("lat") is not None and s.get("lon") is not None]
    lons = [s["lon"] for s in samples if s.get("lat") is not None and s.get("lon") is not None]
    assays = {}
    for s in samples:
        for a, ls in (s.get("libs") or {}).items():
            assays[a] = assays.get(a, 0) + len(ls)
    return {
        "samples": len(samples),
        "controls": sum(1 for s in samples if s.get("control")),
        "withCoords": len(lats),
        "stations": len({s["station"] for s in samples if s.get("station")}),
        "cruises": sorted({s["cruise"] for s in samples if s.get("cruise")}),
        "dateRange": [dates[0], dates[-1]] if dates else None,
        "bbox": [min(lats), min(lons), max(lats), max(lons)] if lats else None,
        "librariesByAssay": assays,
    }


def select_samples(samples, names=None, match=None, no_controls=False):
    """samp_names chosen by an explicit list and/or a case-insensitive substring
    `match` against samp_name / station / place / cruise / date. None = all."""
    if not names and not match and not no_controls:
        return None
    want = set(names or [])
    pat = (match or "").strip().lower()
    out = []
    for s in samples:
        if no_controls and s.get("control"):
            continue
        if want and s["samp_name"] not in want:
            continue
        if pat:
            hay = " ".join(str(s.get(k) or "") for k in ("samp_name", "station", "place", "cruise", "date")).lower()
            if pat not in hay:
                continue
        out.append(s["samp_name"])
    return out


def pick_analyses(project, wanted=None, trusted_only=False):
    """Analyses of `project` chosen by analysis_run_name, assay_name or short
    marker (case-insensitive substring); None / [] = all."""
    out = []
    for a in project.get("analyses") or []:
        if trusted_only and not a.get("trusted"):
            continue
        if wanted:
            keys = [a.get("analysis_run_name"), a.get("assay_name"), short_assay(a.get("assay_name"))]
            if not any(w.lower() == (k or "").lower() or w.lower() in (k or "").lower()
                       for w in wanted for k in keys):
                continue
        out.append(a)
    return out


def subset_feature_table(text, keep_libs):
    """Feature table text restricted to `keep_libs` columns (features with no
    reads left are dropped). A blank first header cell becomes 'featureid'."""
    hdr, rows = read_tsv(text)
    if not hdr:
        return text
    hdr = list(hdr)
    if not hdr[0]:
        hdr[0] = "featureid"
    if keep_libs is None:
        idx = list(range(1, len(hdr)))
    else:
        keep = set(keep_libs)
        idx = [i for i in range(1, len(hdr)) if hdr[i] in keep]
    out = []
    for r in rows:
        vals = [r[i] if i < len(r) else "0" for i in idx]
        if keep_libs is not None and not any(_float(v) for v in vals):
            continue
        out.append([r[0]] + vals)
    return write_tsv([hdr[0]] + [hdr[i] for i in idx], out)


def subset_rows(text, key, keep):
    """FAIRe sheet text keeping only rows whose `key` column is in `keep`
    ('#' comment rows are kept as-is)."""
    if keep is None:
        return text
    keep = set(keep)
    if isinstance(text, bytes):
        text = text.decode("utf-8-sig", errors="replace")
    lines = text.lstrip("﻿").splitlines()
    comments = [ln for ln in lines if ln.lstrip().startswith("#")]
    hdr, rows = read_tsv(text)
    if key not in hdr:
        return text
    k = hdr.index(key)
    body = write_tsv(hdr, [r for r in rows if k < len(r) and r[k].strip() in keep])
    return ("\n".join(comments) + "\n" if comments else "") + body


# ---------------------------------------------------------------- client
class ODEClient:
    """Thin client for the Ocean DNA Explorer REST API (GET /api/<table>?...).

    Every list method returns plain dicts. Errors raise ODEError. File downloads
    are cached on disk by URL (``cache_dir``; None disables)."""

    def __init__(self, base=None, cache_dir="__default__", timeout=60, trusted=False, ttl=300,
                 allow_hosts=None):
        # defaults are read at construction so ODE_BASE_URL / ODE_CACHE_DIR set later still apply
        self.base = (base or os.getenv("ODE_BASE_URL") or ODE_BASE).rstrip("/")
        if cache_dir == "__default__":
            cache_dir = os.getenv("ODE_CACHE_DIR") or DEFAULT_CACHE
        # extra file hosts (tests / mirrors); env ODE_FILE_HOSTS=host1,host2
        self.allow_hosts = set(allow_hosts or []) | {h.strip() for h in
                                                     os.getenv("ODE_FILE_HOSTS", "").split(",") if h.strip()}
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.timeout = timeout
        self.trusted = trusted          # True = only reviewed analyses, like the website default
        self.ttl = ttl                  # seconds API JSON is memoised (bridge)
        self._memo = {}
        self._lock = threading.Lock()

    # ---- low level
    def _open(self, url, accept="application/json"):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            msg = ""
            try:
                body = e.read().decode("utf-8", "replace")
                msg = (json.loads(body) or {}).get("error") or body[:200]
            except Exception:
                pass
            raise ODEError(f"HTTP {e.code} from Ocean DNA Explorer: {msg or e.reason}", e.code, url) from None
        except urllib.error.URLError as e:
            raise ODEError(f"cannot reach {urllib.parse.urlparse(url).netloc}: {e.reason}", 0, url) from None
        except (TimeoutError, OSError) as e:
            raise ODEError(f"cannot reach {urllib.parse.urlparse(url).netloc}: {e}", 0, url) from None

    def api(self, path, params=None, memo=True):
        """GET {base}/api/{path}?{params} -> the 'result' value."""
        q = {k: (",".join(v) if isinstance(v, (list, tuple)) else v) for k, v in (params or {}).items()
             if v is not None and v != ""}
        if self.trusted and "trusted" not in q:
            q["trusted"] = "true"
        url = f"{self.base}/api/{path.lstrip('/')}" + ("?" + urllib.parse.urlencode(q) if q else "")
        if memo:
            with self._lock:
                hit = self._memo.get(url)
            if hit and time.time() - hit[0] < self.ttl:
                return hit[1]
        with self._open(url) as resp:
            body = resp.read()
        try:
            data = json.loads(body.decode("utf-8"))
        except ValueError:
            raise ODEError("Ocean DNA Explorer returned something that is not JSON", 502, url) from None
        if not isinstance(data, dict) or data.get("statusMessage") != "success":
            raise ODEError((data or {}).get("error") or "Ocean DNA Explorer request failed", 400, url)
        if memo:
            with self._lock:
                self._memo[url] = (time.time(), data.get("result"))
        return data.get("result")

    def clear_memo(self):
        with self._lock:
            self._memo.clear()

    # ---- tables
    def _norm_analysis(self, a):
        c = a.get("_count") or {}
        return {
            "id": a.get("id"), "project_id": a.get("project_id"),
            "analysis_run_name": a.get("analysis_run_name"), "assay_name": a.get("assay_name"),
            "short": short_assay(a.get("assay_name")), "trusted": bool(a.get("trusted")),
            "dateSubmitted": a.get("dateSubmitted"),
            "counts": {"libraries": c.get("Libraries"), "occurrences": c.get("Occurrences"),
                       "assignments": c.get("Assignments"), "taxonomies": c.get("Taxonomies")},
            "files": {"table": a.get("occurrenceFileUrl_ODE"), "taxonomy": a.get("asvFileUrl_ODE"),
                      "analysisMetadata": a.get("analysisMetadataFileUrl_ODE")},
        }

    def _norm_project(self, p, analyses=None):
        c = p.get("_count") or {}
        pid = p.get("project_id")
        return {
            "id": p.get("id"), "project_id": pid, "project_name": p.get("project_name"),
            "institution": p.get("institution"), "dateSubmitted": p.get("dateSubmitted"),
            "url": f"{self.base}/explore/project/{urllib.parse.quote(str(pid))}",
            "counts": {"samples": c.get("Samples"), "analyses": c.get("Analyses"), "libraries": c.get("Libraries")},
            "files": {"projectMetadata": p.get("projectMetadataFileUrl_ODE"),
                      "sampleMetadata": p.get("sampleMetadataFileUrl_ODE"),
                      "libraryMetadata": p.get("libraryMetadataFileUrl_ODE")},
            "analyses": analyses if analyses is not None else [],
        }

    def analyses(self, project_id=None):
        res = self.api("analysis", {"fields": ANALYSIS_FIELDS, "project_id": project_id,
                                    "relCounts": "libraries,occurrences,assignments,taxonomies",
                                    "orderBy": "analysis_run_name,asc"})
        out = [self._norm_analysis(a) for a in res or []]
        if project_id:   # project_id filters with 'contains' — keep exact matches only
            out = [a for a in out if a["project_id"] == project_id]
        return out

    def projects(self, q=None, with_analyses=True):
        """Every project (optionally filtered by a case-insensitive substring
        across id / name / institution), newest data model first."""
        res = self.api("project", {"fields": PROJECT_FIELDS, "relCounts": "samples,analyses,libraries",
                                   "orderBy": "project_id,asc"})
        by_pid = {}
        if with_analyses:
            for a in self.analyses():
                by_pid.setdefault(a["project_id"], []).append(a)
        out = [self._norm_project(p, by_pid.get(p.get("project_id"), [])) for p in res or []]
        if q:
            ql = q.strip().lower()
            out = [p for p in out if ql in " ".join(str(p.get(k) or "") for k in
                                                     ("project_id", "project_name", "institution")).lower()]
        return out

    def project(self, project_id):
        project_id = parse_ode_ref(project_id)["project_id"] or project_id
        res = self.api("project", {"fields": PROJECT_FIELDS, "project_id": project_id,
                                   "relCounts": "samples,analyses,libraries"})
        hits = [p for p in res or [] if p.get("project_id") == project_id]
        if not hits:
            near = [p.get("project_id") for p in res or []]
            raise ODEError(f"no Ocean DNA Explorer project '{project_id}'" +
                           (f" (did you mean {', '.join(near[:5])}?)" if near else ""), 404)
        return self._norm_project(hits[0], self.analyses(project_id))

    def samples(self, project_id, fields=None, page_size=1000):
        """Every sample row of a project from the API (paged)."""
        out, page = [], 1
        while True:
            res = self.api("sample", {"fields": fields or SAMPLE_FIELDS, "project_id": project_id,
                                      "orderBy": "samp_name,asc", "limit": page_size, "page": page})
            res = [r for r in res or [] if r.get("project_id") == project_id]
            out += res
            if len(res) < page_size:
                return out
            page += 1

    def find_samples(self, q, project_id=None, limit=200):
        """Samples whose text fields contain `q` (ODE 'search'), across projects
        or within one project (advanced query)."""
        if project_id:
            adv = json.dumps([[["samp_name", "contains", q], ["station_id", "contains", q],
                               ["geo_loc_name", "contains", q]], ["project_id", "equals", project_id]])
            params = {"advanced": adv}
        else:
            params = {"search": q}
        params.update({"fields": SAMPLE_FIELDS, "limit": limit, "orderBy": "samp_name,asc"})
        return self.api("sample", params)

    # ---- files
    def _cache_path(self, url):
        h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", file_basename(url))[-120:] or "file"
        return self.cache_dir / "files" / f"{h}_{name}"

    def fetch_file(self, url, progress=None, use_cache=True, cancel=None):
        """Download an ODE file (bytes). progress(done_bytes, total_or_None, cached_bool)."""
        if not url:
            raise ODEError("no file URL", 400)
        host = urllib.parse.urlparse(url).hostname or ""
        if not (FILE_HOST_RE.search(host) or host in self.allow_hosts):
            raise ODEError(f"refusing to fetch from {host or url!r}: not an Ocean DNA Explorer file host", 400, url)
        cp = self._cache_path(url) if (self.cache_dir and use_cache) else None
        if cp and cp.is_file() and cp.stat().st_size:
            data = cp.read_bytes()
            if progress:
                progress(len(data), len(data), True)
            return data
        with self._open(url, accept="text/tab-separated-values, text/plain, */*") as resp:
            total = int(resp.headers.get("Content-Length") or 0) or None
            buf, got, last = io.BytesIO(), 0, 0.0
            if progress:
                progress(0, total, False)
            while True:
                if cancel and cancel():
                    raise Cancelled()
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                buf.write(chunk)
                got += len(chunk)
                if progress and time.time() - last > 0.1:
                    last = time.time()
                    progress(got, total, False)
            data = buf.getvalue()
        if progress:
            progress(len(data), total or len(data), False)
        if cp:
            try:
                cp.parent.mkdir(parents=True, exist_ok=True)
                tmp = cp.with_suffix(cp.suffix + ".part")
                tmp.write_bytes(data)
                os.replace(tmp, cp)
            except OSError as e:
                log.warning("cannot cache %s: %s", cp, e)
        return data

    def fetch_text(self, url, **kw):
        return self.fetch_file(url, **kw).decode("utf-8-sig", errors="replace")

    # ---- catalog
    def catalog(self):
        """Snapshot of every project + analysis + file URL, embedded in reports
        (payload.odeCatalog) so the import dialog works without a bridge."""
        return {"version": 1, "source": self.base, "generatedAt": now_iso(),
                "generator": f"ode_client.py {__version__}", "projects": self.projects()}

    def preview(self, project_id):
        p = self.project(project_id)
        st = self.fetch_text(p["files"]["sampleMetadata"]) if p["files"].get("sampleMetadata") else ""
        lt = self.fetch_text(p["files"]["libraryMetadata"]) if p["files"].get("libraryMetadata") else ""
        samples = summarize_samples(st, lt)
        pm = {}
        if p["files"].get("projectMetadata"):
            try:
                pm = parse_project_metadata(self.fetch_text(p["files"]["projectMetadata"]))["project"]
            except ODEError as e:
                log.warning("projectMetadata: %s", e)
        return {"project": p, "projectMeta": pm, "summary": preview_summary(samples), "samples": samples}


def parse_ode_ref(ref):
    """'noaa-aoml-mire', 'https://www.oceandnaexplorer.org/explore/project/noaa-aoml-mire'
    or '.../explore/sample/<project>/<samp_name>' -> {project_id, samp_name}."""
    s = str(ref or "").strip()
    m = re.search(r"oceandnaexplorer\.org/(?:explore|search)/(?:\w+/)?([^/?#]+)(?:/([^/?#]+))?", s, re.I)
    if m and "/explore/sample/" in s:
        return {"project_id": urllib.parse.unquote(m.group(1)),
                "samp_name": urllib.parse.unquote(m.group(2)) if m.group(2) else None}
    if m:
        return {"project_id": urllib.parse.unquote(m.group(1)), "samp_name": None}
    return {"project_id": s or None, "samp_name": None}


# ---------------------------------------------------------------- pull
def _layout_names(project_id, analysis):
    """File names the report builder pairs: table_<key>-<id>.tsv / asvTaxaFeatures_<key>-<id>.tsv."""
    tb = file_basename(analysis["files"].get("table") or "")
    tx = file_basename(analysis["files"].get("taxonomy") or "")
    std_t = re.match(r"^table_(.+?)-([A-Za-z0-9]{16,})\.(tsv|txt|csv)$", tb)
    std_x = re.match(r"^asvTaxaFeatures_(.+?)-([A-Za-z0-9]{16,})\.(tsv|txt|csv)$", tx)
    if std_t and std_x and std_t.group(1) == std_x.group(1):
        return tb, tx
    rid = (std_t.group(2) if std_t else (re.search(r"-([A-Za-z0-9]{16,})\.", tb) or [None, "ode"])[1])
    key = slug(analysis["analysis_run_name"])
    return f"table_{key}-{rid}.tsv", f"asvTaxaFeatures_{key}-{rid}.tsv"


def faire_names(project_id):
    p = slug(project_id)
    return {"projectMetadata": f"{p}_projectMetadata.tsv", "sampleMetadata": f"{p}_sampleMetadata.tsv",
            "libraryMetadata": f"{p}_experimentRunMetadata.tsv"}


def plan_files(project, analyses):
    """[(key, label, url, kind)] to download for an import."""
    out = []
    for k, lab in (("projectMetadata", "Project metadata (FAIRe)"),
                   ("sampleMetadata", "Sample metadata (FAIRe)"),
                   ("libraryMetadata", "Library ↔ sample links (experimentRunMetadata)")):
        if project["files"].get(k):
            out.append((k, lab, project["files"][k], "faire"))
    for a in analyses:
        nm = a["analysis_run_name"]
        out.append((f"table:{nm}", f"{a['short'] or a['assay_name']} feature table (ASV reads)",
                    a["files"].get("table"), "table"))
        out.append((f"taxonomy:{nm}", f"{a['short'] or a['assay_name']} ASV taxonomy",
                    a["files"].get("taxonomy"), "taxonomy"))
    return out


def libs_for_samples(library_text, keep_samples):
    if keep_samples is None:
        return None
    keep = set(keep_samples)
    return {r["lib_id"] for r in records(library_text) if r.get("lib_id") and r.get("samp_name") in keep}


def write_project_files(dest, project, analyses, texts, keep_samples=None, normalize=True):
    """Write downloaded texts into the standard layout under dest/<project_id>/.
    Returns {'dir', 'faire': [...], 'tourmaline': [(table, taxa, analysis)], 'meta': path}."""
    pid = project["project_id"]
    root = Path(dest) / pid
    fdir, tdir, mdir = root / "faire", root / "tourmaline", root / "meta"
    for d in (fdir, tdir, mdir):
        d.mkdir(parents=True, exist_ok=True)
    names = faire_names(pid)
    keep_libs = libs_for_samples(texts.get("libraryMetadata", ""), keep_samples)
    out = {"dir": str(root), "faire": [], "tourmaline": [], "meta": None}
    for k in ("projectMetadata", "sampleMetadata", "libraryMetadata"):
        if k not in texts:
            continue
        txt = texts[k]
        if keep_samples is not None and k == "sampleMetadata":
            txt = subset_rows(txt, "samp_name", keep_samples)
        if keep_samples is not None and k == "libraryMetadata":
            txt = subset_rows(txt, "samp_name", keep_samples)
        (fdir / names[k]).write_text(txt, encoding="utf-8")
        out["faire"].append(str(fdir / names[k]))
    for a in analyses:
        nm = a["analysis_run_name"]
        tname, xname = _layout_names(pid, a)
        tt, xt = texts.get(f"table:{nm}"), texts.get(f"taxonomy:{nm}")
        if tt is None:
            continue
        if normalize or keep_libs is not None:
            tt = subset_feature_table(tt, keep_libs)
        (tdir / tname).write_text(tt, encoding="utf-8")
        if xt is not None:
            (tdir / xname).write_text(xt, encoding="utf-8")
        if texts.get(f"analysisMetadata:{nm}"):
            (mdir / f"analysisMetadata_{slug(nm)}.tsv").write_text(texts[f"analysisMetadata:{nm}"], encoding="utf-8")
        out["tourmaline"].append((str(tdir / tname), str(tdir / xname) if xt is not None else None, nm))
    prov = {"source": "Ocean DNA Explorer", "importedAt": now_iso(), "tool": f"ode_client.py {__version__}",
            "project": {k: project.get(k) for k in ("project_id", "project_name", "institution", "url", "files")},
            "analyses": [{k: a.get(k) for k in ("analysis_run_name", "assay_name", "trusted", "files", "counts")}
                         for a in analyses],
            "samples": sorted(keep_samples) if keep_samples is not None else "all"}
    mp = mdir / "ode_import.json"
    mp.write_text(json.dumps(prov, indent=1), encoding="utf-8")
    out["meta"] = str(mp)
    return out


def download_all(client, project, analyses, progress=None, cancel=None, with_analysis_meta=True):
    """{key: text} for every planned file. progress(key, label, state, done, total, detail)."""
    texts = {}
    plan = plan_files(project, analyses)
    if with_analysis_meta:
        plan += [(f"analysisMetadata:{a['analysis_run_name']}", f"{a['short']} analysis metadata",
                  a["files"].get("analysisMetadata"), "meta") for a in analyses if a["files"].get("analysisMetadata")]
    for key, label, url, kind in plan:
        if not url:
            if progress:
                progress(key, label, "error", 0, None, "no file URL in Ocean DNA Explorer")
            if kind in ("table", "taxonomy", "faire") and key != "projectMetadata":
                raise ODEError(f"{label}: Ocean DNA Explorer lists no file for it", 404)
            continue
        if progress:
            progress(key, label, "running", 0, None, file_basename(url))

        def cb(done, total, cached, _k=key, _l=label, _u=url):
            if progress:
                progress(_k, _l, "running", done, total, ("cached · " if cached else "") + file_basename(_u))
        try:
            data = client.fetch_file(url, progress=cb, cancel=cancel)
        except ODEError as e:
            hint = (" — the file is gone from Ocean DNA Explorer (the project was probably re-submitted); "
                    "refresh the project list and try again") if e.status in (403, 404) else ""
            if progress:
                progress(key, label, "error", 0, None, str(e) + hint)
            if kind == "meta" or key == "projectMetadata":
                log.warning("%s: %s (continuing)", label, e)
                continue
            raise ODEError(f"{label}: {e}{hint}", e.status, e.url) from None
        texts[key] = data.decode("utf-8-sig", errors="replace")
        if progress:
            progress(key, label, "done", len(data), len(data), file_basename(url))
    return texts


def manifest_rows(project, analyses, files, cruise=None):
    """report_manifest rows (dicts) for pulled analyses."""
    fa = [Path(p).name for p in files["faire"]]
    rows = []
    for tp, _xp, nm in files["tourmaline"]:
        a = next((x for x in analyses if x["analysis_run_name"] == nm), {})
        rows.append({"sample_id": f"{slug(project['project_id'])}_{slug(a.get('short') or nm)}".lower(),
                     "run": nm, "project": project["project_id"], "cruise": cruise or "",
                     "metadata": "; ".join(fa), "ode_report": re.sub(r"-[A-Za-z0-9]{16,}\.tsv$|\.tsv$", "", Path(tp).name),
                     "disable": False, "platform": "Illumina"})
    return rows


def add_manifest_rows(manifest_path, rows):
    """Append rows to report_manifest.xlsx (backup first; rows already present —
    same project + ode_report — are skipped). Needs openpyxl. Returns n added."""
    try:
        import openpyxl
    except ImportError as e:
        raise ODEError("openpyxl is needed to edit report_manifest.xlsx (pip install openpyxl)") from e
    mp = Path(manifest_path)
    lock = mp.with_name("~$" + mp.name)
    if lock.exists():
        raise ODEError(f"{mp.name} looks open in Excel ({lock.name} exists) — close it and retry")
    if mp.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(mp, mp.with_name(f"{mp.stem}.BACKUP_{stamp}_pre_ode{mp.suffix}"))
        wb = openpyxl.load_workbook(mp)
        ws = wb.worksheets[0]
        hdr = [str(c.value).strip() if c.value is not None else "" for c in ws[1]]
    else:
        mp.parent.mkdir(parents=True, exist_ok=True)
        wb = openpyxl.Workbook()
        ws = wb.active
        hdr = ["sample_id", "run", "project", "cruise", "metadata", "k2_report", "asv_report",
               "barcodes", "disable", "platform", "ode_report"]
        ws.append(hdr)
    low = [_norm(h) for h in hdr]
    for need in ("ode_report",):
        if need not in low:
            hdr.append(need)
            low.append(_norm(need))
            ws.cell(row=1, column=len(hdr), value=need)
    existing = set()
    pi, oi = (low.index("project") if "project" in low else None), low.index("odereport")
    for r in ws.iter_rows(min_row=2, values_only=True):
        if pi is not None and r[pi] and len(r) > oi and r[oi]:
            existing.add((str(r[pi]).strip(), str(r[oi]).strip()))
    added = 0
    for row in rows:
        if (row["project"], row["ode_report"]) in existing:
            continue
        ws.append([row.get(h, row.get(h.lower(), None)) if _norm(h) != "odereport" else row["ode_report"]
                   for h in hdr])
        added += 1
    wb.save(mp)
    return added


def pull_project(client, project_id, dest=DEFAULT_PROJECTS, analyses=None, samples=None, match=None,
                 no_controls=False, trusted_only=False, manifest=None, progress=None):
    """Download one project (or a set of its samples / analyses) into the
    standard layout and optionally register it in the report manifest."""
    project = client.project(project_id)
    chosen = pick_analyses(project, analyses, trusted_only)
    if not chosen:
        raise ODEError(f"{project['project_id']}: no analysis matches {analyses or '(any)'}"
                       f" — available: {', '.join(a['analysis_run_name'] for a in project['analyses']) or 'none'}", 404)
    texts = download_all(client, project, chosen, progress=progress)
    keep = None
    if samples or match or no_controls:
        summ = summarize_samples(texts.get("sampleMetadata", ""), texts.get("libraryMetadata", ""))
        keep = select_samples(summ, samples, match, no_controls)
        if not keep:
            raise ODEError("no sample matches the selection", 404)
    files = write_project_files(dest, project, chosen, texts, keep)
    result = {"project": project, "analyses": chosen, "files": files, "samples": keep}
    if manifest:
        pm = parse_project_metadata(texts.get("projectMetadata", ""))["project"] if texts.get("projectMetadata") else {}
        rows = manifest_rows(project, chosen, files, pm.get("expedition_id") or pm.get("cruise_id"))
        result["manifestRows"] = rows
        result["manifestAdded"] = add_manifest_rows(manifest, rows)
    return result


# ---------------------------------------------------------------- report runs (needs the builder)
_BUILD_LOCK = threading.Lock()


def builder_module():
    """build_mbon_html (pandas) when importable, else None."""
    try:
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        import build_mbon_html as B   # noqa: WPS433
        return B
    except Exception as e:      # pandas missing, etc.
        log.info("report builder not importable (%s) — imports return raw files", e)
        return None


def build_runs(project, analyses, texts, keep_samples=None, source="bridge"):
    """Report runs (the same JSON build_mbon_html.py embeds) for the chosen
    analyses, built in a temporary folder. Returns (runs, None) or (None, reason)."""
    B = builder_module()
    if B is None:
        return None, "the report builder (pandas) is not available to the bridge"
    pid = project["project_id"]
    shorts = {}
    for a in analyses:
        shorts.setdefault(a["short"] or a["assay_name"], []).append(a["analysis_run_name"])
    runs = []
    with _BUILD_LOCK, tempfile.TemporaryDirectory(prefix="ode_build_") as tmp:
        files = write_project_files(tmp, project, analyses, texts, keep_samples)
        for tp, xp, nm in files["tourmaline"]:
            a = next(x for x in analyses if x["analysis_run_name"] == nm)
            short = a["short"] or a["assay_name"]
            tag = f" ({nm})" if len(shorts.get(short, [])) > 1 else ""
            sub = "" if keep_samples is None else f"_s{hashlib.sha1('|'.join(sorted(keep_samples)).encode()).hexdigest()[:6]}"
            rid = f"ode_{slug(pid)}_{slug(nm)}{sub}".lower()
            got = B.load_ode_folder(Path(tp).parent, rid, f"{pid}{tag}",
                                    pairs=[(Path(tp), Path(xp) if xp else None)]) or []
            for r in got:
                r["builtin"] = False
                if keep_samples is not None:
                    r["label"] += f" · {len(keep_samples)} sample{'s' if len(keep_samples) != 1 else ''}"
                r["ode"] = {"project_id": pid, "project_name": project.get("project_name"),
                            "analysis_run_name": nm, "assay_name": a.get("assay_name"),
                            "trusted": a.get("trusted"), "url": project.get("url"),
                            "samples": None if keep_samples is None else sorted(keep_samples),
                            "importedAt": now_iso(), "via": source}
                r.setdefault("meta", {})
                r["meta"]["Project ID"] = pid
                r["meta"]["Source"] = "Ocean DNA Explorer (live import)"
                r["meta"]["Analysis"] = nm
                r["meta"]["Reviewed (trusted)"] = "yes" if a.get("trusted") else "no"
                runs.append(B._sanitize(r) if hasattr(B, "_sanitize") else r)
    return runs, None


# ---------------------------------------------------------------- jobs
class Job:
    def __init__(self, req):
        self.id = uuid.uuid4().hex[:12]
        self.req = req
        self.state = "queued"
        self.created = now_iso()
        self.started = self.finished = None
        self.steps = []           # [{key,label,state,done,total,detail}]
        self.message = "Waiting to start…"
        self.error = None
        self.result = None
        self.cancelled = False
        self._lock = threading.Lock()

    def step(self, key, label=None, state=None, done=None, total=None, detail=None):
        with self._lock:
            s = next((x for x in self.steps if x["key"] == key), None)
            if s is None:
                s = {"key": key, "label": label or key, "state": "waiting", "done": 0, "total": None, "detail": None}
                self.steps.append(s)
            if label:
                s["label"] = label
            if state:
                s["state"] = state
            if done is not None:
                s["done"] = done
            if total is not None:
                s["total"] = total
            if detail is not None:
                s["detail"] = detail

    def to_json(self):
        with self._lock:
            done = sum(1 for s in self.steps if s["state"] in ("done", "skipped"))
            return {"id": self.id, "state": self.state, "message": self.message, "error": self.error,
                    "created": self.created, "started": self.started, "finished": self.finished,
                    "request": self.req, "steps": [dict(s) for s in self.steps],
                    "progress": {"steps": len(self.steps), "done": done},
                    "hasResult": self.result is not None}


class JobManager:
    def __init__(self, client, projects_dir=DEFAULT_PROJECTS, keep=25):
        self.client = client
        self.projects_dir = Path(projects_dir)
        self.jobs = {}
        self.keep = keep
        self._lock = threading.Lock()

    def start(self, req):
        job = Job(req)
        with self._lock:
            self.jobs[job.id] = job
            for old in sorted(self.jobs.values(), key=lambda j: j.created)[:-self.keep]:
                self.jobs.pop(old.id, None)
        threading.Thread(target=self._run, args=(job,), daemon=True, name=f"ode-job-{job.id}").start()
        return job

    def get(self, jid):
        return self.jobs.get(jid)

    def _run(self, job):
        req = job.req
        job.state, job.started = "running", now_iso()
        try:
            pid = parse_ode_ref(req.get("project_id"))["project_id"]
            if not pid:
                raise ODEError("project_id is required", 400)
            job.step("lookup", "Look up project on Ocean DNA Explorer", "running")
            job.message = f"Looking up {pid}…"
            project = self.client.project(pid)
            chosen = pick_analyses(project, req.get("analyses"), bool(req.get("trustedOnly")))
            if not chosen:
                raise ODEError(f"{pid}: none of the requested analyses exist "
                               f"({', '.join(a['analysis_run_name'] for a in project['analyses']) or 'project has no analyses'})", 404)
            job.step("lookup", state="done", detail=f"{project.get('project_name') or pid} · {len(chosen)} analysis(es)")
            for key, label, _u, _k in plan_files(project, chosen):
                job.step(key, label)
            want_build = req.get("build", True)
            if want_build:
                job.step("build", "Build report layers (stations, taxa)")
            if req.get("save"):
                job.step("save", f"Save to data/projects/{pid}/")

            def prog(key, label, state, done, total, detail):
                job.step(key, label, state, done, total, detail)
                if state == "running":
                    job.message = f"Downloading {label}…"

            texts = download_all(self.client, project, chosen, progress=prog,
                                 cancel=lambda: job.cancelled, with_analysis_meta=bool(req.get("save")))
            keep = None
            if req.get("samples") is not None or req.get("match") or req.get("noControls"):
                summ = summarize_samples(texts.get("sampleMetadata", ""), texts.get("libraryMetadata", ""))
                keep = select_samples(summ, req.get("samples"), req.get("match"), bool(req.get("noControls")))
                if req.get("samples") is not None and not req.get("samples"):
                    keep = []
                if keep is not None and not keep:
                    raise ODEError("none of the requested samples are in this project", 404)
            result = {"project": project, "analyses": chosen, "samples": keep}
            if want_build:
                job.step("build", state="running")
                job.message = "Building report layers…"
                runs, why = build_runs(project, chosen, texts, keep)
                if runs is not None:
                    result["runs"] = runs
                    job.step("build", state="done", detail=f"{len(runs)} run(s) · "
                             f"{sum(len(r.get('samples') or []) for r in runs)} sample rows")
                else:
                    job.step("build", state="skipped", detail=why + " — the report parses the files itself")
            if req.get("files") or "runs" not in result:
                result["files"] = texts
            if req.get("save"):
                job.step("save", state="running")
                files = write_project_files(self.projects_dir, project, chosen, texts, keep)
                result["saved"] = files
                if req.get("manifest"):
                    pm = parse_project_metadata(texts.get("projectMetadata", ""))["project"] if texts.get("projectMetadata") else {}
                    rows = manifest_rows(project, chosen, files, pm.get("expedition_id"))
                    result["manifestAdded"] = add_manifest_rows(self.projects_dir / "report_manifest.xlsx", rows)
                job.step("save", state="done", detail=files["dir"])
            job.result = result
            job.state, job.message = "done", f"Imported {pid}"
        except Cancelled:
            job.state, job.message = "cancelled", "Cancelled"
        except ODEError as e:
            job.state, job.error, job.message = "error", e.to_json(), str(e)
        except Exception as e:      # never leave a job hanging
            log.exception("job %s failed", job.id)
            job.state, job.error, job.message = "error", {"error": f"{type(e).__name__}: {e}", "status": 500}, str(e)
        finally:
            for s in job.steps:
                if s["state"] in ("running", "waiting") and job.state != "done":
                    s["state"] = "cancelled" if job.state == "cancelled" else ("error" if s["state"] == "running" else "waiting")
            job.finished = now_iso()


# ---------------------------------------------------------------- bridge (HTTP)
def _local_origin(origin):
    if not origin or origin == "null":
        return True
    host = urllib.parse.urlparse(origin).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost")


class BridgeAPI:
    """Framework-neutral request handler used by `serve` and the Flask blueprint.
    handle() returns (status, content_type, body_bytes)."""

    def __init__(self, client=None, projects_dir=DEFAULT_PROJECTS):
        self.client = client or ODEClient()
        self.jobs = JobManager(self.client, projects_dir)
        self.started = now_iso()

    @staticmethod
    def _json(status, obj):
        return status, "application/json", json.dumps(obj, default=str).encode("utf-8")

    def handle(self, method, path, query=None, body=None, origin=None):
        query = query or {}
        q1 = lambda k, d=None: (query.get(k) or [d])[0] if isinstance(query.get(k), list) else query.get(k, d)
        parts = [urllib.parse.unquote(p) for p in path.strip("/").split("/") if p]
        if parts[:2] == ["api", "ode"]:
            parts = parts[2:]
        try:
            if method == "GET" and parts in ([], ["health"]):
                up, err = True, None
                if q1("upstream") in ("1", "true"):
                    try:
                        self.client.api("tables", memo=True)
                    except ODEError as e:
                        up, err = False, str(e)
                return self._json(200, {"ok": True, "service": "ode-bridge", "version": __version__,
                                        "upstream": self.client.base, "upstreamOk": up, "upstreamError": err,
                                        "builder": builder_module() is not None, "started": self.started,
                                        "cache": str(self.client.cache_dir) if self.client.cache_dir else None})
            if method == "GET" and parts == ["projects"]:
                return self._json(200, {"projects": self.client.projects(q1("q"))})
            if method == "GET" and len(parts) == 2 and parts[0] == "projects":
                return self._json(200, {"project": self.client.project(parts[1])})
            if method == "GET" and len(parts) == 3 and parts[0] == "projects" and parts[2] == "preview":
                return self._json(200, self.client.preview(parts[1]))
            if method == "GET" and parts == ["samples"]:
                q = q1("q") or ""
                if len(q.strip()) < 2:
                    return self._json(400, {"error": "q (2+ characters) is required"})
                return self._json(200, {"samples": self.client.find_samples(q, q1("project_id"), int(q1("limit", 200)))})
            if method == "GET" and parts == ["catalog"]:
                return self._json(200, self.client.catalog())
            if method == "GET" and parts == ["file"]:
                data = self.client.fetch_file(q1("url"))
                return 200, "text/tab-separated-values; charset=utf-8", data
            if method == "POST" and parts == ["jobs"]:
                try:
                    req = json.loads((body or b"{}").decode("utf-8") or "{}")
                except ValueError:
                    return self._json(400, {"error": "request body must be JSON"})
                if not isinstance(req, dict) or not req.get("project_id"):
                    return self._json(400, {"error": "project_id is required"})
                if req.get("save") and not _local_origin(origin):
                    return self._json(403, {"error": "saving to disk is only allowed from a local page"})
                job = self.jobs.start(req)
                return self._json(202, {"job": job.to_json()})
            if len(parts) >= 2 and parts[0] == "jobs":
                job = self.jobs.get(parts[1])
                if not job:
                    return self._json(404, {"error": f"no job {parts[1]} (the bridge may have restarted)"})
                if method == "DELETE" and len(parts) == 2:
                    job.cancelled = True
                    return self._json(200, {"job": job.to_json()})
                if method == "GET" and len(parts) == 2:
                    return self._json(200, {"job": job.to_json()})
                if method == "GET" and len(parts) == 3 and parts[2] == "result":
                    if job.state != "done":
                        return self._json(409, {"error": f"job is {job.state}", "job": job.to_json()})
                    return self._json(200, {"job": job.to_json(), "result": job.result})
            return self._json(404, {"error": f"unknown endpoint {method} /api/ode/{'/'.join(parts)}"})
        except ODEError as e:
            return self._json(e.status if 400 <= (e.status or 0) < 600 else 502, e.to_json())
        except Exception as e:
            log.exception("bridge error")
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})


def make_handler(api):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"ode-bridge/{__version__}"

        def log_message(self, fmt, *args):
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _cors(self):
            origin = self.headers.get("Origin")
            self.send_header("Access-Control-Allow-Origin", origin if origin else "*")
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Max-Age", "600")

        def _send(self, status, ctype, data):
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _dispatch(self, method):
            u = urllib.parse.urlparse(self.path)
            body = None
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(min(n, 1 << 20)) if n else b""
            if not u.path.startswith("/api/ode") and u.path not in ("/", "/health"):
                return self._send(404, "application/json", b'{"error":"not found"}')
            st, ct, data = api.handle(method, u.path, urllib.parse.parse_qs(u.query), body,
                                      self.headers.get("Origin"))
            log.info("%s %s -> %s", method, self.path[:160], st)
            self._send(st, ct, data)

        def do_GET(self):
            self._dispatch("GET")

        def do_HEAD(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_DELETE(self):
            self._dispatch("DELETE")
    return Handler


def serve(host=BRIDGE_HOST, port=BRIDGE_PORT, client=None, projects_dir=DEFAULT_PROJECTS, ready=None):
    api = BridgeAPI(client, projects_dir)
    httpd = ThreadingHTTPServer((host, port), make_handler(api))
    httpd.daemon_threads = True
    if ready:
        ready(httpd)
    log.info("Ocean DNA Explorer bridge on http://%s:%d/api/ode  (upstream %s, cache %s)",
             host, httpd.server_address[1], api.client.base, api.client.cache_dir)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


# ---------------------------------------------------------------- CLI
def _cli_progress(key, label, state, done, total, detail):
    if state == "running" and total:
        sys.stderr.write(f"\r  ↓ {label[:48]:<48} {done / total * 100:5.1f}%  ({done / 1e6:.1f} MB)")
    elif state == "running":
        sys.stderr.write(f"\r  ↓ {label[:48]:<48} {done / 1e6:.1f} MB")
    elif state == "done":
        sys.stderr.write(f"\r  ✓ {label[:48]:<48} {done / 1e6:.2f} MB {'(cached)' if 'cached' in (detail or '') else ''}\n")
    elif state == "error":
        sys.stderr.write(f"\r  ✕ {label[:48]:<48} {detail}\n")
    sys.stderr.flush()


def _print_project(p, out=sys.stdout):
    c = p["counts"]
    out.write(f"{p['project_id']}  —  {p.get('project_name') or ''}\n")
    out.write(f"  {p.get('institution') or ''}\n  {c.get('samples')} samples · {c.get('libraries')} libraries · "
              f"{len(p['analyses'])} analyses   {p['url']}\n")
    for a in p["analyses"]:
        ac = a["counts"]
        out.write(f"   - {a['analysis_run_name']}  [{a['assay_name']}]  {ac.get('libraries')} libs · "
                  f"{ac.get('assignments')} ASVs · {ac.get('taxonomies')} taxa"
                  f"{'' if a['trusted'] else '  (not yet reviewed)'}\n")


def _samples_arg(v):
    if not v:
        return None
    if v.startswith("@"):
        return [ln.strip() for ln in Path(v[1:]).read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [x.strip() for x in v.split(",") if x.strip()]


def build_parser():
    p = argparse.ArgumentParser(prog="ode_client.py", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="examples:\n"
                                       "  python ode_client.py projects\n"
                                       "  python ode_client.py show noaa-aoml-mire\n"
                                       "  python ode_client.py pull noaa-aoml-mire --analysis MiFish --match Horseshoe --manifest\n"
                                       "  python ode_client.py catalog\n"
                                       "  python ode_client.py serve --port 5051\n")
    p.add_argument("--base", default=None, help=f"Ocean DNA Explorer URL (default {ODE_BASE}; env ODE_BASE_URL)")
    p.add_argument("--cache", default="__default__", help=f"download cache folder (default {DEFAULT_CACHE}; "
                                                         "env ODE_CACHE_DIR; '' disables)")
    p.add_argument("--trusted", action="store_true", help="only reviewed (trusted) analyses, like the website")
    p.add_argument("--json", action="store_true", help="print JSON instead of text")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("projects", help="list projects")
    s.add_argument("-q", "--query", help="filter by id / name / institution")
    s = sub.add_parser("show", help="one project: analyses, samples, dates, extent")
    s.add_argument("project", help="project_id or an oceandnaexplorer.org link")
    s.add_argument("--samples", action="store_true", help="also list every sample")
    s = sub.add_parser("find-samples", help="search samples across projects")
    s.add_argument("query")
    s.add_argument("--project", help="limit to one project")
    s.add_argument("--limit", type=int, default=200)
    s = sub.add_parser("catalog", help="write the project catalog snapshot embedded in reports")
    s.add_argument("-o", "--output", default=str(DEFAULT_CATALOG))
    s = sub.add_parser("pull", help="download a project (or a set of its samples) into data/projects/")
    s.add_argument("project", help="project_id or an oceandnaexplorer.org link")
    s.add_argument("--analysis", action="append", help="analysis_run_name, assay name or marker (repeatable); default all")
    s.add_argument("--samples", help="comma-separated samp_names, or @file with one per line")
    s.add_argument("--match", help="keep samples whose name / station / place / cruise / date contains this")
    s.add_argument("--no-controls", action="store_true", help="drop negative / positive controls")
    s.add_argument("--out", default=str(DEFAULT_PROJECTS), help="projects folder (default data/projects)")
    s.add_argument("--manifest", nargs="?", const="__default__",
                   help="also add rows to report_manifest.xlsx (default <out>/report_manifest.xlsx)")
    s.add_argument("--build", action="store_true", help="then build reports/<project>.html")
    s = sub.add_parser("serve", help="run the local bridge used by the report's import dialog")
    s.add_argument("--host", default=BRIDGE_HOST)
    s.add_argument("--port", type=int, default=BRIDGE_PORT)
    s.add_argument("--projects-dir", default=str(DEFAULT_PROJECTS))
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
    client = ODEClient(base=args.base, cache_dir=args.cache or None, trusted=args.trusted)
    out = sys.stdout
    try:
        if args.cmd == "projects":
            ps = client.projects(args.query)
            if args.json:
                json.dump(ps, out, indent=1)
            else:
                for pr in ps:
                    _print_project(pr)
                    out.write("\n")
                out.write(f"{len(ps)} project(s) on {client.base}\n")
        elif args.cmd == "show":
            pv = client.preview(args.project)
            if args.json:
                json.dump(pv if args.samples else {k: v for k, v in pv.items() if k != "samples"}, out, indent=1)
            else:
                _print_project(pv["project"])
                sm = pv["summary"]
                out.write(f"  samples in sheet: {sm['samples']} ({sm['controls']} controls, {sm['withCoords']} with coordinates, "
                          f"{sm['stations']} stations)\n")
                if sm["dateRange"]:
                    out.write(f"  dates: {sm['dateRange'][0]} → {sm['dateRange'][1]}\n")
                if sm["bbox"]:
                    b = sm["bbox"]
                    out.write(f"  extent: lat {b[0]:.3f}…{b[2]:.3f}, lon {b[1]:.3f}…{b[3]:.3f}\n")
                if sm["cruises"]:
                    out.write(f"  cruises: {', '.join(sm['cruises'][:12])}\n")
                for a, n in sorted(sm["librariesByAssay"].items()):
                    out.write(f"  libraries · {a}: {n}\n")
                if args.samples:
                    for s in pv["samples"]:
                        out.write(f"    {s['samp_name']}\t{s.get('date') or ''}\t{s.get('station') or ''}\t"
                                  f"{s.get('lat')},{s.get('lon')}\t{'control' if s.get('control') else ''}\n")
        elif args.cmd == "find-samples":
            res = client.find_samples(args.query, args.project, args.limit)
            if args.json:
                json.dump(res, out, indent=1)
            else:
                for s in res:
                    out.write(f"{s.get('project_id')}\t{s.get('samp_name')}\t{str(s.get('eventDate') or '')[:10]}\t"
                              f"{s.get('station_id') or ''}\t{s.get('decimalLatitude')},{s.get('decimalLongitude')}\n")
                out.write(f"{len(res)} sample(s)\n")
        elif args.cmd == "catalog":
            cat = client.catalog()
            op = Path(args.output)
            op.parent.mkdir(parents=True, exist_ok=True)
            op.write_text(json.dumps(cat, indent=1), encoding="utf-8")
            n_a = sum(len(p["analyses"]) for p in cat["projects"])
            out.write(f"wrote {op} — {len(cat['projects'])} projects, {n_a} analyses ({cat['generatedAt']})\n")
        elif args.cmd == "pull":
            man = None
            if args.manifest:
                man = Path(args.out) / "report_manifest.xlsx" if args.manifest == "__default__" else Path(args.manifest)
            res = pull_project(client, args.project, args.out, args.analysis, _samples_arg(args.samples),
                               args.match, args.no_controls, args.trusted, man, progress=_cli_progress)
            if args.json:
                json.dump({k: v for k, v in res.items() if k != "project"}, out, indent=1, default=str)
            else:
                out.write(f"✓ {res['project']['project_id']}: {len(res['analyses'])} analysis(es) → {res['files']['dir']}\n")
                if res["samples"] is not None:
                    out.write(f"  {len(res['samples'])} sample(s) selected\n")
                if man:
                    out.write(f"  manifest {man}: {res['manifestAdded']} row(s) added\n")
                else:
                    out.write("  (add --manifest to register it in report_manifest.xlsx)\n")
            if args.build:
                pid = res["project"]["project_id"]
                cmd = [sys.executable, str(REPO_ROOT / "build_mbon_html.py"), "--projects", pid,
                       "--projects-dir", str(args.out), "-o", str(REPO_ROOT / "reports" / f"{slug(pid)}.html")]
                if not man:
                    cmd.append("--include-unmanifested")
                out.write("  building: " + " ".join(cmd) + "\n")
                return subprocess.call(cmd)
        elif args.cmd == "serve":
            serve(args.host, args.port, client, args.projects_dir)
        return 0
    except ODEError as e:
        sys.stderr.write(f"\n✕ {e}\n")
        return 1 if e.status != 404 else 4
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
