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
MBON Dashboard — data-source adapters.

A *data source* is anything the dashboard can read sample / observation data
from. Today we support:

    - postgres : the canonical MBON Postgres schema (psycopg2)
    - xlsx     : a local Excel workbook (one sheet of samples, one of observations)
    - gsheet   : a hosted/published Google Sheet (read as CSV export)

Spreadsheet sources are loaded into an in-memory pandas representation and a
lightweight semantic *mapping* (which column means latitude, depth, salinity,
species, read-count, …).  That mapping is what makes the Explore view able to
run the same env-range + species cross-filter regardless of where the data
lives.

Secrets (e.g. a Postgres password) live only in the process / on disk in
connections.json — they are never returned to the browser.  See connections.py.
"""

from __future__ import annotations

import io
import os
import re
import math
import time
import threading
from decimal import Decimal
from typing import Any, Optional

# pandas / requests are only needed for spreadsheet sources; import lazily so a
# Postgres-only deployment doesn't have to install them.
try:
    import pandas as pd
except Exception:  # pragma: no cover
    pd = None

try:
    import requests
except Exception:  # pragma: no cover
    requests = None


# ── semantic roles ───────────────────────────────────────────────────────────
# Each role maps to a list of candidate column names (normalised, lower-cased,
# non-alphanumerics stripped) used for auto-detection.  The stored connection
# mapping can always override the auto-detected choice.

SAMPLE_ROLES = {
    "key":       ["sampname", "samplename", "sampleid", "sampid", "id", "mbonsampalias"],
    "latitude":  ["decimallatitude", "latitude", "lat", "verbatimlatitude"],
    "longitude": ["decimallongitude", "longitude", "lon", "lng", "verbatimlongitude"],
    "depth":     ["minimumdepthinmeters", "sampledepth", "depth", "maximumdepthinmeters",
                  "totdepthwatercol", "depthm"],
    "salinity":  ["salinity", "sal", "salinityppt", "salinitypsu"],
    "temp":      ["temp", "temperature", "watertemp", "tempc"],
    "sst":       ["seasurfacetemperature", "sst"],
    "date":      ["eventdate", "collectedat", "date", "cruisedate", "verbatimeventdate"],
    "station":   ["station", "stationid", "stationname"],
}

OBS_ROLES = {
    "key":      ["sampleid", "sampname", "sample", "samplename", "mbondataid"],
    "species":  ["scientificname", "species", "taxon", "organism", "name"],
    "common":   ["commonname", "common"],
    "group":    ["organismgroup", "group", "taxongroup"],
    "rank":     ["taxonomicrank", "rank", "taxrank"],
    "reads":    ["readcount", "reads", "count", "abundance", "numreads"],
}

ENV_ROLES = ("depth", "salinity", "temp", "sst")
_NUMERIC_TEXT_RE = r"^\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)\s*$"


def _pg_numeric_expr(column: str, alias: Optional[str] = None) -> str:
    ref = f"{alias}.{column}" if alias else column
    return (
        f"CASE WHEN {ref}::text ~ '{_NUMERIC_TEXT_RE}' "
        f"THEN {ref}::double precision END"
    )


def _norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _auto_map(columns: list[str], roles: dict[str, list[str]]) -> dict[str, Optional[str]]:
    """Pick the best-matching real column for each semantic role."""
    norm_to_real = {}
    for c in columns:
        norm_to_real.setdefault(_norm(c), c)
    out: dict[str, Optional[str]] = {}
    for role, candidates in roles.items():
        chosen = None
        for cand in candidates:
            if cand in norm_to_real:
                chosen = norm_to_real[cand]
                break
        out[role] = chosen
    return out


def _num(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _valid_bboxes(filters: dict) -> list[dict[str, float]]:
    raw = filters.get("bboxes") or ([filters.get("bbox")] if filters.get("bbox") else [])
    if not isinstance(raw, list):
        return []
    out = []
    for box in raw:
        if not isinstance(box, dict):
            continue
        vals = {k: _num(box.get(k)) for k in ("s", "n", "w", "e")}
        if any(v is None for v in vals.values()):
            continue
        south, north = sorted((vals["s"], vals["n"]))
        west, east = sorted((vals["w"], vals["e"]))
        out.append({"s": south, "n": north, "w": west, "e": east})
    return out


def _clean_records(records: list[dict]) -> list[dict]:
    """Make a list of dicts JSON-safe (numpy / NaN / pandas types)."""
    out = []
    for r in records:
        d = {}
        for k, v in r.items():
            if v is None:
                d[k] = None
            elif isinstance(v, float):
                d[k] = None if (math.isnan(v) or math.isinf(v)) else v
            elif isinstance(v, Decimal):
                d[k] = int(v) if v == v.to_integral_value() else float(v)
            elif hasattr(v, "item"):          # numpy scalar
                try:
                    iv = v.item()
                    d[k] = None if (isinstance(iv, float) and math.isnan(iv)) else iv
                except Exception:
                    d[k] = str(v)
            elif pd is not None and isinstance(v, pd.Timestamp):
                d[k] = v.isoformat()
            else:
                d[k] = v
        out.append(d)
    return out


# ── base ─────────────────────────────────────────────────────────────────────

class DataSource:
    kind = "base"

    def test(self) -> dict:
        raise NotImplementedError

    def explore_schema(self) -> dict:
        raise NotImplementedError

    def explore_query(self, filters: dict) -> dict:
        raise NotImplementedError

    def close(self):
        pass


# ── Postgres ─────────────────────────────────────────────────────────────────

class PostgresSource(DataSource):
    """The canonical MBON schema.  Read-only by contract for Explore."""
    kind = "postgres"

    def __init__(self, config: dict):
        self.config = config or {}

    def _connect(self):
        import psycopg2
        import psycopg2.extras
        return psycopg2.connect(
            dbname=self.config.get("dbname", os.getenv("PGDATABASE", "postgres")),
            user=self.config.get("user", os.getenv("PGUSER", "postgres")),
            password=self.config.get("password", os.getenv("PGPASSWORD", "")),
            host=self.config.get("host", os.getenv("PGHOST", "127.0.0.1")),
            port=self.config.get("port", os.getenv("PGPORT", "5432")),
            connect_timeout=self.config.get("connect_timeout", 6),
            cursor_factory=psycopg2.extras.RealDictCursor,
        )

    def query(self, sql: str, params=None) -> list[dict]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def test(self) -> dict:
        t0 = time.time()
        try:
            self.query("SELECT 1")
            return {"ok": True, "ms": round((time.time() - t0) * 1000)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # The Explore feature targets mbon_samples (env + geo) ⋈ wilder_results (taxa).
    def explore_schema(self) -> dict:
        def rng(col):
            expr = _pg_numeric_expr(col)
            try:
                r = self.query(
                    f"SELECT MIN({expr}) AS mn, MAX({expr}) AS mx FROM mbon_samples "
                    f"WHERE {expr} IS NOT NULL")
                if r and r[0]["mn"] is not None:
                    return [_num(r[0]["mn"]), _num(r[0]["mx"])]
            except Exception:
                pass
            return None

        ranges = {
            "depth":    rng("sample_depth"),
            "salinity": rng("salinity"),
            "temp":     rng("temp"),
            "sst":      rng("sea_surface_temperature"),
        }
        try:
            groups = [r["organism_group"] for r in self.query(
                "SELECT DISTINCT organism_group FROM wilder_results "
                "WHERE organism_group IS NOT NULL ORDER BY 1")]
        except Exception:
            groups = []
        try:
            ranks = [r["taxonomic_rank"] for r in self.query(
                "SELECT DISTINCT taxonomic_rank FROM wilder_results "
                "WHERE taxonomic_rank IS NOT NULL ORDER BY 1")]
        except Exception:
            ranks = []
        try:
            bbox = self.query(
                "SELECT MIN(latitude) AS s, MAX(latitude) AS n, "
                "MIN(longitude) AS w, MAX(longitude) AS e FROM mbon_samples "
                "WHERE latitude IS NOT NULL AND longitude IS NOT NULL")[0]
            bbox = {k: _num(v) for k, v in bbox.items()}
        except Exception:
            bbox = None
        return {"kind": self.kind, "ranges": ranges, "groups": groups,
                "ranks": ranks, "bbox": bbox, "env_available": [k for k, v in ranges.items() if v]}

    def explore_query(self, f: dict) -> dict:
        where = ["1=1"]
        params: list = []

        def rng(col, key):
            v = f.get(key)
            if v and isinstance(v, (list, tuple)) and len(v) == 2:
                expr = _pg_numeric_expr(col, "ms")
                lo, hi = v
                if lo is not None:
                    where.append(f"{expr} >= %s"); params.append(lo)
                if hi is not None:
                    where.append(f"{expr} <= %s"); params.append(hi)

        rng("sample_depth", "depth")
        rng("salinity", "salinity")
        rng("temp", "temp")
        rng("sea_surface_temperature", "sst")

        bboxes = _valid_bboxes(f)
        if bboxes:
            parts = []
            for bbox in bboxes:
                parts.append("(ms.latitude BETWEEN %s AND %s AND ms.longitude BETWEEN %s AND %s)")
                params += [bbox["s"], bbox["n"], bbox["w"], bbox["e"]]
            where.append("(" + " OR ".join(parts) + ")")

        if f.get("stations"):
            where.append("ms.station = ANY(%s)"); params.append(list(f["stations"]))
        if f.get("date_from"):
            where.append("s.collected_at >= %s"); params.append(f["date_from"])
        if f.get("date_to"):
            where.append("s.collected_at <= %s"); params.append(f["date_to"])

        if f.get("groups"):
            where.append("wr.organism_group = ANY(%s)"); params.append(list(f["groups"]))
        if f.get("ranks"):
            where.append("wr.taxonomic_rank = ANY(%s)"); params.append(list(f["ranks"]))
        if f.get("species"):
            where.append("wr.scientific_name = ANY(%s)"); params.append(list(f["species"]))
        if f.get("search"):
            where.append("(wr.scientific_name ILIKE %s OR wr.common_name ILIKE %s)")
            params += [f"%{f['search']}%", f"%{f['search']}%"]
        if f.get("min_reads"):
            where.append("wr.read_count >= %s"); params.append(int(f["min_reads"]))

        clause = " AND ".join(where)
        top_n = min(int(f.get("top", 50)), 500)
        depth_expr = _pg_numeric_expr("sample_depth", "ms")
        salinity_expr = _pg_numeric_expr("salinity", "ms")
        temp_expr = _pg_numeric_expr("temp", "ms")
        sst_expr = _pg_numeric_expr("sea_surface_temperature", "ms")

        species = self.query(f"""
            SELECT wr.scientific_name, MAX(wr.common_name) AS common_name,
                   MAX(wr.organism_group) AS organism_group,
                   MAX(wr.taxonomic_rank) AS taxonomic_rank,
                   SUM(wr.read_count) AS total_reads,
                   COUNT(DISTINCT wr.mbon_data_id) AS sample_count
            FROM wilder_results wr
            JOIN mbon_samples ms ON ms.mbon_data_id = wr.mbon_data_id
            LEFT JOIN sample s ON s.sample_id = ms.sample_id
            WHERE {clause}
            GROUP BY wr.scientific_name
            ORDER BY total_reads DESC NULLS LAST
            LIMIT %s
        """, params + [top_n])

        samples = self.query(f"""
            SELECT ms.mbon_data_id AS sample_key, ms.mbon_samp_alias AS label,
                   ms.station, ms.latitude, ms.longitude,
                 {depth_expr} AS depth, {salinity_expr} AS salinity, {temp_expr} AS temp,
                 {sst_expr} AS sst, s.collected_at AS date,
                   COUNT(DISTINCT wr.scientific_name) AS species_count,
                   SUM(wr.read_count) AS total_reads
            FROM mbon_samples ms
            JOIN wilder_results wr ON wr.mbon_data_id = ms.mbon_data_id
            LEFT JOIN sample s ON s.sample_id = ms.sample_id
            WHERE {clause}
            GROUP BY ms.mbon_data_id, ms.mbon_samp_alias, ms.station, ms.latitude,
                    ms.longitude, {depth_expr}, {salinity_expr}, {temp_expr},
                    {sst_expr}, s.collected_at
            ORDER BY total_reads DESC NULLS LAST
        """, params)

        points = self.query(f"""
                 SELECT ms.latitude, ms.longitude, {depth_expr} AS depth,
                     {salinity_expr} AS salinity, {temp_expr} AS temp, {sst_expr} AS sst,
                   wr.organism_group, wr.scientific_name, wr.read_count
            FROM wilder_results wr
            JOIN mbon_samples ms ON ms.mbon_data_id = wr.mbon_data_id
            LEFT JOIN sample s ON s.sample_id = ms.sample_id
            WHERE {clause}
            ORDER BY wr.read_count DESC NULLS LAST
            LIMIT 4000
        """, params)

        return _assemble(self, species, samples, points)


# ── Spreadsheet (xlsx / Google Sheet) ────────────────────────────────────────

class SpreadsheetSource(DataSource):
    """
    Loads two logical tables — *samples* (env + geo) and *observations*
    (per-sample, per-taxon read counts) — from an Excel workbook or a hosted
    Google Sheet, then runs the Explore filter in pandas.

    config keys:
        kind:           "xlsx" | "gsheet"
        path:           local .xlsx path                       (xlsx)
        url:            Google Sheet share/edit/publish URL     (gsheet)
        samples_sheet:  sheet name for sample metadata          (optional, auto)
        obs_sheet:      sheet name for observations             (optional, auto)
        mapping:        {"samples": {role: col}, "obs": {role: col}}  (optional)
    """
    kind = "spreadsheet"
    _cache: dict[str, tuple[float, "SpreadsheetSource"]] = {}

    def __init__(self, config: dict):
        if pd is None:
            raise RuntimeError("pandas is required for spreadsheet sources "
                               "(pip install pandas openpyxl)")
        self.config = config or {}
        self.kind = self.config.get("kind", "xlsx")
        self._sheets: dict[str, "pd.DataFrame"] = {}
        self._load()
        self._resolve_mapping()

    # — loading —
    def _load(self):
        if self.kind == "gsheet":
            self._load_gsheet()
        else:
            self._load_xlsx()

    def _load_xlsx(self):
        path = self.config.get("path")
        if path and not os.path.isabs(path):
            # relative workbook paths are relative to the repository root
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), path)
        if not path or not os.path.exists(path):
            raise FileNotFoundError(f"workbook not found: {self.config.get('path')}")
        if not str(path).lower().endswith((".xlsx", ".xlsm", ".xls")):
            raise ValueError(f"not an Excel workbook: {os.path.basename(path)}")
        try:
            raw = pd.read_excel(path, sheet_name=None, header=None, dtype=object)
        except Exception as e:
            raise ValueError(f"cannot read workbook {os.path.basename(path)}: {e}") from e
        for name, df in raw.items():
            self._sheets[name] = _frame_with_header(df)

    def _load_gsheet(self):
        if requests is None:
            raise RuntimeError("requests is required for Google Sheet sources")
        url = self.config.get("url", "")
        key = _gsheet_key(url)
        if not key:
            raise ValueError("could not parse a Google Sheet key from the URL")
        # Pull every tab via the gviz CSV endpoint (works for link-shared sheets).
        export = f"https://docs.google.com/spreadsheets/d/{key}/gviz/tq?tqx=out:csv"
        # If a specific sheet/gid was supplied, honour it; otherwise grab the first.
        gid = _gsheet_gid(url)
        if gid:
            export += f"&gid={gid}"
        resp = requests.get(export, timeout=20)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text), header=None, dtype=object)
        # Google's gviz CSV already returns the header row first.
        self._sheets["Sheet1"] = _frame_with_header(df, assume_first=True)

    # — mapping —
    def _resolve_mapping(self):
        names = list(self._sheets.keys())
        cfg = self.config

        samples_sheet = cfg.get("samples_sheet") or _pick_sheet(
            self._sheets, SAMPLE_ROLES, prefer=("sample", "meta", "metadata", "sheet1"))
        obs_sheet = cfg.get("obs_sheet") or _pick_sheet(
            self._sheets, OBS_ROLES, prefer=("wilder", "result", "observ", "taxa", "sheet1"))

        self.samples_sheet = samples_sheet
        self.obs_sheet = obs_sheet

        s_cols = list(self._sheets[samples_sheet].columns) if samples_sheet else []
        o_cols = list(self._sheets[obs_sheet].columns) if obs_sheet else []

        stored = cfg.get("mapping") or {}
        self.smap = {**_auto_map(s_cols, SAMPLE_ROLES), **(stored.get("samples") or {})}
        self.omap = {**_auto_map(o_cols, OBS_ROLES), **(stored.get("obs") or {})}

    # — public —
    def test(self) -> dict:
        try:
            ns = len(self._sheets.get(self.samples_sheet, [])) if self.samples_sheet else 0
            no = len(self._sheets.get(self.obs_sheet, [])) if self.obs_sheet else 0
            return {"ok": True, "sheets": list(self._sheets.keys()),
                    "samples_sheet": self.samples_sheet, "obs_sheet": self.obs_sheet,
                    "sample_rows": int(ns), "obs_rows": int(no)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def explore_schema(self) -> dict:
        sdf = self._samples_df()
        odf = self._obs_df()
        ranges = {}
        for role in ENV_ROLES:
            col = self.smap.get(role)
            if col and col in sdf:
                vals = pd.to_numeric(sdf[col], errors="coerce").dropna()
                if len(vals):
                    ranges[role] = [float(vals.min()), float(vals.max())]
                else:
                    ranges[role] = None
            else:
                ranges[role] = None

        groups = ranks = []
        gcol, rcol = self.omap.get("group"), self.omap.get("rank")
        if gcol and gcol in odf:
            groups = sorted(x for x in odf[gcol].dropna().unique().tolist() if str(x).strip())
        if rcol and rcol in odf:
            ranks = sorted(x for x in odf[rcol].dropna().unique().tolist() if str(x).strip())

        bbox = None
        latc, lonc = self.smap.get("latitude"), self.smap.get("longitude")
        if latc and lonc and latc in sdf and lonc in sdf:
            la = pd.to_numeric(sdf[latc], errors="coerce").dropna()
            lo = pd.to_numeric(sdf[lonc], errors="coerce").dropna()
            if len(la) and len(lo):
                bbox = {"s": float(la.min()), "n": float(la.max()),
                        "w": float(lo.min()), "e": float(lo.max())}

        return {"kind": self.kind, "ranges": ranges, "groups": groups, "ranks": ranks,
                "bbox": bbox, "env_available": [k for k, v in ranges.items() if v],
                "sheets": list(self._sheets.keys()),
                "mapping": {"samples": self.smap, "obs": self.omap},
                "samples_sheet": self.samples_sheet, "obs_sheet": self.obs_sheet}

    def explore_query(self, f: dict) -> dict:
        sdf = self._samples_df().copy()
        odf = self._obs_df().copy()
        sm, om = self.smap, self.omap

        # numeric coercion for env + geo
        for role in ("latitude", "longitude", *ENV_ROLES):
            c = sm.get(role)
            if c and c in sdf:
                sdf[c] = pd.to_numeric(sdf[c], errors="coerce")
        rc = om.get("reads")
        if rc and rc in odf:
            odf[rc] = pd.to_numeric(odf[rc], errors="coerce").fillna(0)

        # env range filters
        def apply_rng(role):
            nonlocal sdf
            v = f.get(role)
            c = sm.get(role)
            if v and c and c in sdf and isinstance(v, (list, tuple)) and len(v) == 2:
                lo, hi = v
                if lo is not None:
                    sdf = sdf[sdf[c] >= lo]
                if hi is not None:
                    sdf = sdf[sdf[c] <= hi]
        for role in ENV_ROLES:
            apply_rng(role)

        latc, lonc = sm.get("latitude"), sm.get("longitude")
        bboxes = _valid_bboxes(f)
        if bboxes and latc and lonc:
            mask = False
            for bbox in bboxes:
                mask = mask | ((sdf[latc] >= bbox["s"]) & (sdf[latc] <= bbox["n"]) &
                               (sdf[lonc] >= bbox["w"]) & (sdf[lonc] <= bbox["e"]))
            sdf = sdf[mask]

        stc = sm.get("station")
        if f.get("stations") and stc and stc in sdf:
            sdf = sdf[sdf[stc].isin(f["stations"])]

        # join obs → samples on key
        skey, okey = sm.get("key"), om.get("key")
        if not skey or not okey:
            return _assemble(self, [], [], [])
        sdf["_k"] = sdf[skey].astype(str).str.strip()
        odf["_k"] = odf[okey].astype(str).str.strip()
        keep_keys = set(sdf["_k"])
        odf = odf[odf["_k"].isin(keep_keys)]

        # obs filters
        spc, gc, rk = om.get("species"), om.get("group"), om.get("rank")
        if f.get("groups") and gc and gc in odf:
            odf = odf[odf[gc].isin(f["groups"])]
        if f.get("ranks") and rk and rk in odf:
            odf = odf[odf[rk].isin(f["ranks"])]
        if f.get("species") and spc and spc in odf:
            odf = odf[odf[spc].isin(f["species"])]
        if f.get("search") and spc and spc in odf:
            s = str(f["search"]).lower()
            cc = om.get("common")
            mask = odf[spc].astype(str).str.lower().str.contains(s, na=False)
            if cc and cc in odf:
                mask = mask | odf[cc].astype(str).str.lower().str.contains(s, na=False)
            odf = odf[mask]
        if f.get("min_reads") and rc and rc in odf:
            odf = odf[odf[rc] >= int(f["min_reads"])]

        # only keep samples that still have observations
        kept = set(odf["_k"])
        sdf = sdf[sdf["_k"].isin(kept)]

        try:
            top_n = int(f.get("top", 50) or 50)
        except (TypeError, ValueError):
            top_n = 50
        species = self._agg_species(odf, min(top_n, 500))
        samples = self._agg_samples(sdf, odf)
        points = self._points(sdf, odf)
        return _assemble(self, species, samples, points)

    # — helpers —
    def _samples_df(self):
        return self._sheets.get(self.samples_sheet, pd.DataFrame())

    def _obs_df(self):
        return self._sheets.get(self.obs_sheet, pd.DataFrame())

    def _agg_species(self, odf, top_n):
        om = self.omap
        spc, rc = om.get("species"), om.get("reads")
        if spc is None or spc not in odf or odf.empty:
            return []
        g = odf.groupby(spc, dropna=True)
        rows = []
        for name, grp in g:
            reads = float(grp[rc].sum()) if rc and rc in grp else float(len(grp))
            rows.append({
                "scientific_name": name,
                "common_name": _first(grp, om.get("common")),
                "organism_group": _first(grp, om.get("group")),
                "taxonomic_rank": _first(grp, om.get("rank")),
                "total_reads": reads,
                "sample_count": int(grp["_k"].nunique()),
            })
        rows.sort(key=lambda r: r["total_reads"], reverse=True)
        return _clean_records(rows[:top_n])

    def _agg_samples(self, sdf, odf):
        sm, om = self.smap, self.omap
        spc, rc = om.get("species"), om.get("reads")
        per = {}
        if spc and spc in odf and not odf.empty:
            for k, grp in odf.groupby("_k"):
                per[k] = (int(grp[spc].nunique()),
                          float(grp[rc].sum()) if rc and rc in grp else float(len(grp)))
        rows = []
        for _, r in sdf.iterrows():
            k = r["_k"]
            sc, tr = per.get(k, (0, 0.0))
            rows.append({
                "sample_key": k,
                "label": k,
                "station": _val(r, sm.get("station")),
                "latitude": _num(_val(r, sm.get("latitude"))),
                "longitude": _num(_val(r, sm.get("longitude"))),
                "depth": _num(_val(r, sm.get("depth"))),
                "salinity": _num(_val(r, sm.get("salinity"))),
                "temp": _num(_val(r, sm.get("temp"))),
                "sst": _num(_val(r, sm.get("sst"))),
                "date": _val(r, sm.get("date")),
                "species_count": sc,
                "total_reads": tr,
            })
        rows.sort(key=lambda r: r["total_reads"], reverse=True)
        return _clean_records(rows)

    def _points(self, sdf, odf, cap=4000):
        sm, om = self.smap, self.omap
        env_by_key = {}
        for _, r in sdf.iterrows():
            env_by_key[r["_k"]] = {
                "latitude": _num(_val(r, sm.get("latitude"))),
                "longitude": _num(_val(r, sm.get("longitude"))),
                "depth": _num(_val(r, sm.get("depth"))),
                "salinity": _num(_val(r, sm.get("salinity"))),
                "temp": _num(_val(r, sm.get("temp"))),
                "sst": _num(_val(r, sm.get("sst"))),
            }
        spc, gc, rc = om.get("species"), om.get("group"), om.get("reads")
        rows = []
        if spc and spc in odf:
            odf2 = odf.sort_values(rc, ascending=False) if rc and rc in odf else odf
            for _, o in odf2.head(cap).iterrows():
                env = env_by_key.get(o["_k"], {})
                rows.append({**env,
                             "organism_group": _val(o, gc),
                             "scientific_name": _val(o, spc),
                             "read_count": _num(_val(o, rc)) or 0})
        return _clean_records(rows)


# ── shared assembly + env envelopes ──────────────────────────────────────────

def _assemble(source: DataSource, species, samples, points) -> dict:
    species = _clean_records([dict(r) for r in species])
    samples = _clean_records([dict(r) for r in samples])
    points = _clean_records([dict(r) for r in points])

    total_reads = sum((s.get("total_reads") or 0) for s in species)
    counts = {
        "samples": len(samples),
        "species": len(species),
        "observations": len(points),
        "total_reads": total_reads,
    }
    envelopes = _envelopes(points, species)
    return {"counts": counts, "species": species, "samples": samples,
            "points": points, "envelopes": envelopes}


def _envelopes(points, species, top=12):
    """min / median / max of each env var for the top species (for the
    'environmental envelope' plot)."""
    if not points:
        return []
    top_names = [s["scientific_name"] for s in species[:top]]
    by_name: dict[str, dict[str, list]] = {}
    for p in points:
        nm = p.get("scientific_name")
        if nm not in top_names:
            continue
        d = by_name.setdefault(nm, {"depth": [], "salinity": [], "temp": [], "sst": [],
                                    "group": p.get("organism_group")})
        for k in ENV_ROLES:
            v = _num(p.get(k))
            if v is not None:
                d[k].append(v)

    def stat(vals):
        if not vals:
            return None
        vals = sorted(vals)
        n = len(vals)
        med = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
        return {"min": vals[0], "med": med, "max": vals[-1], "n": n}

    out = []
    for nm in top_names:
        d = by_name.get(nm)
        if not d:
            continue
        out.append({"scientific_name": nm, "organism_group": d.get("group"),
                    "depth": stat(d["depth"]), "salinity": stat(d["salinity"]),
                    "temp": stat(d["temp"]), "sst": stat(d["sst"])})
    return out


# ── spreadsheet utilities ────────────────────────────────────────────────────

def _frame_with_header(df, assume_first=False):
    """Detect the header row in a raw (header=None) frame and return a clean
    frame with string column names."""
    if df is None or df.empty:
        return df
    if assume_first:
        hdr_idx = 0
    else:
        hdr_idx, best = 0, -1
        for i in range(min(8, len(df))):
            row = df.iloc[i]
            non_null = row.notna().sum()
            shortish = sum(1 for v in row if v is not None and len(str(v)) <= 40)
            score = non_null + shortish
            # a banner row is one long cell — penalise rows with <3 filled cells
            if non_null >= 3 and score > best:
                best, hdr_idx = score, i
    header = df.iloc[hdr_idx].tolist()
    cols, seen = [], {}
    for j, h in enumerate(header):
        name = str(h).strip() if h is not None else f"col{j}"
        if name == "" or name.lower() == "nan":
            name = f"col{j}"
        if name in seen:
            seen[name] += 1
            name = f"{name}.{seen[name]}"
        else:
            seen[name] = 0
        cols.append(name)
    body = df.iloc[hdr_idx + 1:].copy()
    body.columns = cols
    body = body.dropna(how="all").reset_index(drop=True)
    return body


def _pick_sheet(sheets: dict, roles: dict, prefer=()):
    """Choose the sheet that best matches a role set, biased by name hints."""
    best, best_score = None, -1
    for name, df in sheets.items():
        cols = list(df.columns)
        m = _auto_map(cols, roles)
        score = sum(1 for v in m.values() if v)
        lname = name.lower()
        if any(p in lname for p in prefer):
            score += 3
        if "example" in lname or "validation" in lname or "drop" in lname:
            score -= 5
        if score > best_score:
            best, best_score = name, score
    return best


def _first(grp, col):
    if col and col in grp:
        s = grp[col].dropna()
        if len(s):
            return s.iloc[0]
    return None


def _val(row, col):
    if col is None:
        return None
    try:
        v = row[col]
    except (KeyError, IndexError):
        return None
    if pd is not None and isinstance(v, float) and math.isnan(v):
        return None
    return v


def _gsheet_key(url: str) -> Optional[str]:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url or "")
    if m:
        return m.group(1)
    m = re.search(r"[?&]key=([a-zA-Z0-9-_]+)", url or "")
    return m.group(1) if m else None


def _gsheet_gid(url: str) -> Optional[str]:
    m = re.search(r"[#&?]gid=(\d+)", url or "")
    return m.group(1) if m else None


# ── factory ──────────────────────────────────────────────────────────────────

def build_source(profile: dict) -> DataSource:
    kind = profile.get("kind")
    config = dict(profile.get("config") or {})
    if kind == "postgres":
        return PostgresSource(config)
    if kind in ("xlsx", "gsheet"):
        config["kind"] = kind
        return SpreadsheetSource(config)
    raise ValueError(f"unknown data-source kind: {kind!r}")
