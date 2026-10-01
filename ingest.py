#!/usr/bin/env python3
"""
MBON / eDNA project ingest tool
===============================

Replaces the old ``ingest.ipynb`` notebook with a single, argument-driven
Python script.  ``report_manifest.xlsx`` is the *global controller*: every row
describes one logical unit (a sample_id / batch) and points at the standardized
project folder layout::

    data/projects/<project_dir>/
        faire/     FAIRe checklist(s) (per-sample metadata)  -> sample + mbon_samples
        kraken2/   Kraken2 report files / folders            -> kraken2_report + kraken2_lcas
        wilder/    Wilderlab ASV result xlsx                 -> wilder_results
        meta/      auto-generated concordance files          -> links reports <-> sample_ids

Database schema is *run-based* (see ``create_tables``):

    project -> run -> sample -> data_management -> bioinformatics -> kraken2_report -> kraken2_lcas
    sample  -> mbon_samples (1:1 metadata extension)
    wilder_results -> (mbon_samples, bioinformatics)

CLI (see ``--help``)::

    python ingest.py validate                    # check manifest + every referenced file (no DB needed)
    python ingest.py concordance --all           # (re)generate meta/concordance.json (no DB needed)
    python ingest.py recreate-all --yes          # DROP+CREATE all tables, then bulk import every enabled row
    python ingest.py import --all                # import every enabled manifest row (no drop)
    python ingest.py import --project MBON_2025  # import a single project
    python ingest.py import --sample-id MBON0001
    python ingest.py add    --sample-id MBON0002 # alias of import for one row
    python ingest.py delete --project MBON_2025 --yes
    python ingest.py delete --run  Wilder_611808
    python ingest.py delete --sample-id MBON0001
    python ingest.py inspect-schema              # print live table + FK layout

    python ingest.py --example validate          # same commands against the bundled demo data

Paths default to folders INSIDE this repository (``data/projects/`` and
``data/projects/report_manifest.xlsx``); override with ``--projects-dir`` /
``--manifest`` or the ``EDNA_PROJECTS_DIR`` env var.

Database settings come from the PG* environment variables (PGHOST, PGPORT,
PGDATABASE, PGUSER, PGPASSWORD), optionally loaded from a ``.env`` file in the
repo root (see ``.env.example``). Real environment variables always win.

Safety: destructive commands (``recreate-all``, ``delete``) ask for
confirmation unless ``--yes`` is given, and refuse to run non-interactively
without it. Everything is logged to the console and ``logs/ingest.log``.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import edna_common as ec
from edna_common import InputError

__version__ = "2.0.0"

log = logging.getLogger("ingest")

# ---------------------------------------------------------------------------
# Paths / constants  (all relative to the repository root)
# ---------------------------------------------------------------------------
ROOT = ec.REPO_ROOT
DATA_PROJECTS = ec.PROJECTS_DIR
MANIFEST_PATH = DATA_PROJECTS / ec.MANIFEST_NAME
CREATE_TABLES_SQL = ec.SQL_DIR / "create_tables.sql"

# Standardized per-project subfolders.
SUBFOLDERS = ("faire", "kraken2", "wilder", "meta")

# username recorded as recorded_by / entered_by / submitted_by
DEFAULT_USER = os.getenv("EDNA_INGEST_USER", "postgres")

# Manifest columns (the global controller schema).
MANIFEST_COLUMNS = [
    "sample_id",     # logical unit key (Wilder ClientSampleID / run name / etc.)
    "run",           # run directory / name
    "project",       # project_name in DB
    "project_dir",   # folder under data/projects/  (NEW - maps row -> standardized folders)
    "cruise",        # cruise identifier
    "metadata",      # FAIRe checklist filename in faire/
    "k2_report",     # kraken2 report file or folder (relative to kraken2/)
    "asv_report",    # wilder xlsx (';'-separated) in wilder/
    "barcodes",      # barcoding / eDNA barcoding file (in faire/ or wilder/)
    "meta",          # concordance file in meta/  (NEW - links reports <-> sample_ids)
    "disable",       # bool: skip this row
    "platform",      # sequencing platform (Illumina / Oxford / PacBio ...)
]


# ===========================================================================
# 1. Database layer
# ===========================================================================
def connect():
    """Open a psycopg2 connection from the PG* env vars (see .env.example).
    Raises SystemExit with a readable message instead of a driver traceback."""
    try:
        import psycopg2
    except ImportError:
        raise SystemExit("psycopg2 is not installed: pip install -r requirements.txt")
    cfg = ec.pg_settings(log)
    safe = {k: v for k, v in cfg.items() if k != "password"}
    log.info("connecting to postgres %(user)s@%(host)s:%(port)s/%(dbname)s", safe)
    try:
        conn = psycopg2.connect(**cfg)
    except psycopg2.OperationalError as e:
        raise SystemExit(f"cannot connect to Postgres ({safe['host']}:{safe['port']}/"
                         f"{safe['dbname']}): {str(e).strip()}\n"
                         f"Check PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD in .env")
    conn.autocommit = False
    return conn


def terminate_other_backends(conn):
    """Kill other sessions on this database (e.g. Metabase) that hold table
    locks and would block a DROP ... CASCADE. Safe for a dev recreate."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname = current_database()
              AND pid <> pg_backend_pid()
              AND backend_type = 'client backend';
            """
        )
        killed = [r for r in cur.fetchall() if r[0]]
    conn.commit()
    if killed:
        log.info(f"terminated {len(killed)} competing DB session(s) before DDL")


def recreate_schema(conn, force=False):
    """Execute the create_tables DDL (drops + recreates every table).

    ``force`` (``--terminate-sessions``) terminates other client sessions
    (Metabase, stray notebooks) that hold locks; otherwise we just wait up to
    60s for the DDL lock and fail with a clear message.
    """
    sql = ec.require_file(CREATE_TABLES_SQL, "schema DDL", (".sql",)).read_text()
    if force:
        try:
            terminate_other_backends(conn)
        except Exception as e:
            conn.rollback()
            log.info(f"(could not terminate other sessions: {e})")
    for attempt in (1, 2):
        try:
            with conn.cursor() as cur:
                cur.execute("SET lock_timeout = '60s';")
                cur.execute("SET statement_timeout = '300s';")
                cur.execute(sql)
            conn.commit()
            log.info("Schema (re)created from create_tables.")
            return
        except Exception as e:
            conn.rollback()
            if attempt == 1 and force:
                log.warning(f"DDL blocked ({str(e).strip()[:80]}); killing sessions and retrying...")
                terminate_other_backends(conn)
                continue
            raise


def refresh_table(conn, table_name):
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", table_name):
        raise ValueError(f"refusing to TRUNCATE suspicious table name {table_name!r}")
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE TABLE {table_name} RESTART IDENTITY CASCADE;")
    conn.commit()
    log.info(f"cleared {table_name}")


def inspect_schema(conn):
    """Print tables, columns and FK relationships as they exist in the live DB."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' ORDER BY table_name;"
        )
        tables = [r[0] for r in cur.fetchall()]
        print(f"# {len(tables)} tables in public schema\n")
        for t in tables:
            cur.execute(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position;",
                (t,),
            )
            cols = cur.fetchall()
            print(f"TABLE {t}")
            for name, dtype, nullable in cols:
                flag = "" if nullable == "YES" else " NOT NULL"
                print(f"    {name:<32} {dtype}{flag}")
            cur.execute(
                """
                SELECT kcu.column_name, ccu.table_name, ccu.column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                    ON tc.constraint_name = kcu.constraint_name
                JOIN information_schema.constraint_column_usage ccu
                    ON ccu.constraint_name = tc.constraint_name
                WHERE tc.constraint_type='FOREIGN KEY'
                    AND tc.table_schema='public' AND tc.table_name=%s;
                """,
                (t,),
            )
            for col, ftable, fcol in cur.fetchall():
                print(f"    FK  {col} -> {ftable}.{fcol}")
            print()


def table_columns(cur, table_name, schema="public"):
    cur.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema=%s AND table_name=%s;",
        (schema, table_name),
    )
    return {r[0].lower(): r[1] for r in cur.fetchall()}


def check_allowed_values(cur, table_name, schema="public"):
    """Return {column: {allowed values}} for CHECK (col = ANY(ARRAY[...])) constraints."""
    cur.execute(
        """
        SELECT pg_get_constraintdef(con.oid)
        FROM pg_constraint con
        JOIN pg_class rel ON rel.oid = con.conrelid
        JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
        WHERE con.contype='c' AND nsp.nspname=%s AND rel.relname=%s;
        """,
        (schema, table_name),
    )
    allowed = {}
    for (condef,) in cur.fetchall():
        mcol = re.search(r"\(\(?\s*([a-zA-Z0-9_]+)\s*=\s*ANY", condef)
        if not mcol:
            continue
        marr = re.search(r"ARRAY\[(.*?)\]", condef, flags=re.DOTALL)
        if not marr:
            continue
        vals = re.findall(r"'([^']*)'", marr.group(1))
        if vals:
            allowed[mcol.group(1).lower()] = set(vals)
    return allowed


# --- get-or-create helpers (run-based schema) ------------------------------
def get_or_create_user(cur, username):
    cur.execute("SELECT id FROM users WHERE username=%s", (username,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("INSERT INTO users (username) VALUES (%s) RETURNING id", (username,))
    return cur.fetchone()[0]


def get_or_create_instrument(cur, platform, instrument_name=None):
    if not platform:
        return None
    platform = str(platform).strip()
    instrument_name = (instrument_name or platform).strip()
    cur.execute(
        "SELECT instrument_id FROM instrument WHERE platform=%s AND instrument_name=%s",
        (platform, instrument_name),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        "INSERT INTO instrument (platform, instrument_name) VALUES (%s,%s) RETURNING instrument_id",
        (platform, instrument_name),
    )
    return cur.fetchone()[0]


def get_or_create_project(cur, project_name, recorded_by, project_contact, assay_type="eDNA"):
    cur.execute("SELECT project_id FROM project WHERE project_name=%s", (project_name,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        """
        INSERT INTO project (recorded_by, project_contact, institution_id, project_name,
                             parent_project_id, study_factor, assay_type)
        VALUES (%s,%s,NULL,%s,NULL,NULL,%s)
        RETURNING project_id
        """,
        (recorded_by, project_contact, project_name, assay_type or "eDNA"),
    )
    return cur.fetchone()[0]


def get_or_create_collection(cur, project_id, collection_name, collection_type=None,
                             collected_at=None, description=None):
    cur.execute(
        "SELECT collection_id FROM collection WHERE project_id=%s AND collection_name=%s",
        (project_id, collection_name),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        """
        INSERT INTO collection (project_id, collection_name, collection_type,
                                collected_at, description)
        VALUES (%s,%s,%s,%s,%s)
        RETURNING collection_id
        """,
        (project_id, collection_name, collection_type, collected_at, description),
    )
    return cur.fetchone()[0]


def get_or_create_sample(cur, collection_id, sample_name, submitted_by, sample_type="SAMPLE",
                         collected_at=None, directory=None, instrument_id=None,
                         location_id=None):
    cur.execute(
        "SELECT sample_id FROM sample WHERE collection_id=%s AND sample_name=%s",
        (collection_id, sample_name),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    st = sample_type if sample_type in (
        "SAMPLE", "POSITIVE_CONTROL", "NEGATIVE_CONTROL", "OTHER") else "SAMPLE"
    cur.execute(
        """
        INSERT INTO sample (collection_id, sample_name, submitted_by, sample_type,
                            collected_at, completed_at, directory, instrument_id, location_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING sample_id
        """,
        (collection_id, sample_name, submitted_by, st, collected_at, collected_at,
         directory, instrument_id, location_id),
    )
    return cur.fetchone()[0]


def get_or_create_run(cur, sample_id, directory, entered_by, instrument_id=None, comments=None):
    """A run belongs to a sample (collection-based schema)."""
    cur.execute(
        "SELECT run_id FROM run WHERE sample_id=%s AND directory=%s",
        (sample_id, directory),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        """
        INSERT INTO run (sample_id, directory, created_at, entered_by, updated_at,
                         comments, instrument_id)
        VALUES (%s,%s,NOW(),%s,NOW(),%s,%s)
        RETURNING run_id
        """,
        (sample_id, directory, entered_by, comments, instrument_id),
    )
    return cur.fetchone()[0]


def get_or_create_data_management(cur, run_id, developers="MBON ingest",
                                  associated_resource=None):
    cur.execute("SELECT id FROM data_management WHERE run_id=%s", (run_id,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        "INSERT INTO data_management (run_id, developers, associated_resource) "
        "VALUES (%s,%s,%s) RETURNING id",
        (run_id, developers, associated_resource),
    )
    return cur.fetchone()[0]


def get_or_create_bioinformatics(cur, data_management_id, sop="MBON_PIPELINE", version="v1"):
    cur.execute(
        "SELECT id FROM bioinformatics WHERE data_management_id=%s", (data_management_id,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        "INSERT INTO bioinformatics (data_management_id, sop_bioinformatics, version_pipeline, last_run) "
        "VALUES (%s,%s,%s,NOW()) RETURNING id",
        (data_management_id, sop, version),
    )
    return cur.fetchone()[0]


def ensure_location(cur, conn, alias, latitude, longitude, project_id=None):
    latitude, longitude = _to_float_or_none(latitude), _to_float_or_none(longitude)
    if latitude is None or longitude is None or latitude != latitude or longitude != longitude:
        return None  # None or NaN
    alias = _clean_text(alias)
    if _is_missing(alias):
        alias = "Unknown Location"
    alias = str(alias).strip()
    try:
        cur.execute(
            "INSERT INTO location (location_alias, latitude, longitude, project_id) "
            "VALUES (%s,%s,%s,%s) ON CONFLICT (location_alias) DO NOTHING;",
            (alias, float(latitude), float(longitude), project_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        return None
    cur.execute("SELECT location_id FROM location WHERE location_alias=%s", (alias,))
    row = cur.fetchone()
    return row[0] if row else None


# ===========================================================================
# 2. Value cleaning / parsing helpers
# ===========================================================================
def _is_missing(v):
    import pandas as pd
    if v is None:
        return True
    try:
        if pd.isna(v):
            return True
    except (ValueError, TypeError):
        pass
    return isinstance(v, str) and v.strip() == ""


def _norm(k):
    return str(k).strip().lower()


def _norm_col(k):
    return re.sub(r"[^a-z0-9]+", "", str(k).strip().lower())


def _clean_text(v):
    return v.replace("\xa0", " ").strip() if isinstance(v, str) else v


def _to_float_or_none(v):
    v = _clean_text(v)
    if _is_missing(v):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if s == "" or s.lower() == "nan":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def normalize_mbon_id(value):
    """Normalize MBON IDs to MBON#### where possible (MBON1 -> MBON0001)."""
    s = str(value).strip()
    m = re.match(r"MBON0*(\d+)$", s)
    return f"MBON{int(m.group(1)):04d}" if m else s


def parse_event_date_to_noon(value):
    import pandas as pd
    if value is None or _is_missing(value):
        return None
    if isinstance(value, datetime):
        return value.replace(hour=12, minute=0, second=0, microsecond=0)
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime().replace(hour=12, minute=0, second=0, microsecond=0)
    s = str(value).strip()
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).replace(hour=12, minute=0, second=0, microsecond=0)
        except ValueError:
            continue
    try:
        return pd.to_datetime(s).to_pydatetime().replace(hour=12, minute=0, second=0, microsecond=0)
    except Exception:
        return None


def parse_volume_to_ml(value):
    if value is None or _is_missing(value):
        return None
    if isinstance(value, (int, float)):
        return int(round(value))
    s = str(value).strip().lower()
    if s in ("", "nan"):
        return None
    m = re.match(r"^\s*([0-9]*\.?[0-9]+)\s*([a-zµ]+)\s*$", s)
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2).replace("μ", "u")
    if unit in ("l", "liter", "liters"):
        return int(round(num * 1000))
    if unit in ("ml", "milliliter", "milliliters"):
        return int(round(num))
    return None


# Rename map: FAIRe/spreadsheet header -> mbon_samples column.
KEY_MAP = {
    "volume_filtered": "water_volume_filtered",
    "filter_pore_size_um": "filter_pore_size",
    "sample depth": "sample_depth",
    "cruise number": "cruise_number",
    "decimallatitude": "latitude",
    "decimallongitude": "longitude",
    "samp_name": "mbon_samp_alias",
    "station": "station",
}


# ===========================================================================
# 3. FAIRe checklist loader (robust header detection)
# ===========================================================================
# Different checklists put the header on different rows and use different key
# columns (samp_number / sample_ID).  We detect the header row by scanning for
# one of these anchor columns.
FAIRE_ANCHORS = ("samp_number", "sample_id", "materialsampleid")
FAIRE_ID_COLUMNS = ("samp_number", "sample_id")  # the per-sample primary key column


def load_faire_samples(path, sheet="sampleMetadata"):
    """
    Return a pandas DataFrame of the FAIRe sampleMetadata table with the header
    row auto-detected.  Adds helper columns: ``_samp_id`` (primary id as str)
    and ``_samp_norm`` (MBON-normalized id).
    """
    import pandas as pd
    ec.require_file(path, "FAIRe checklist", ec.SPREADSHEET_EXT + (".csv",))
    if str(path).lower().endswith(".csv"):
        raw = pd.read_csv(path, header=None, dtype=str, keep_default_na=False)
    else:
        names = ec.excel_sheet_names(path)
        if sheet not in names:
            alt = next((n for n in names if _norm_col(n) == _norm_col(sheet)), None)
            if alt is None:
                log.warning("%s: no '%s' sheet — using first sheet '%s'",
                            Path(path).name, sheet, names[0])
            sheet = alt or names[0]
        raw = pd.read_excel(path, sheet_name=sheet, header=None, engine="openpyxl")
    if raw.empty:
        raise InputError(f"{Path(path).name}: sheet '{sheet}' is empty")

    header_idx = None
    for i in range(min(len(raw), 12)):
        cells = {_norm(c) for c in raw.iloc[i].values if c is not None}
        if cells & set(FAIRE_ANCHORS):
            header_idx = i
            break
    if header_idx is None:
        log.warning("%s: no header row with %s found in the first 12 rows — assuming row 1",
                    Path(path).name, "/".join(FAIRE_ANCHORS))
        header_idx = 0

    df = raw.iloc[header_idx + 1:].reset_index(drop=True)
    df.columns = [str(c).strip() if c is not None else f"col{j}"
                  for j, c in enumerate(raw.iloc[header_idx].values)]

    # find the id column
    id_col = None
    for cand in FAIRE_ID_COLUMNS:
        for c in df.columns:
            if _norm(c) == cand:
                id_col = c
                break
        if id_col:
            break
    if id_col is None:
        id_col = df.columns[0]
        log.warning("%s: no samp_number / sample_ID column — using first column '%s' as the "
                    "sample key", Path(path).name, id_col)

    df = df.dropna(subset=[id_col]).copy()
    df["_samp_id"] = df[id_col].astype(str).str.strip()
    df = df[df["_samp_id"].str.lower() != "nan"]
    df["_samp_norm"] = df["_samp_id"].apply(normalize_mbon_id)
    dup = df["_samp_id"][df["_samp_id"].duplicated()].unique().tolist()
    if dup:
        log.warning("%s: duplicate sample id(s) %s — later rows will reuse the first sample",
                    Path(path).name, dup[:10])
    if df.empty:
        log.warning("%s: no sample rows under the header", Path(path).name)
    df.attrs["id_col"] = id_col
    df.attrs["header_idx"] = header_idx
    return df


def faire_material_col(df):
    for c in df.columns:
        if _norm(c) == "materialsampleid":
            return c
    return None


def faire_name_col(df):
    for c in df.columns:
        if _norm(c) in ("samp_name", "pi_sample_name"):
            return c
    return None


# ===========================================================================
# 4. mbon_samples metadata insertion (schema-driven, check-constraint aware)
# ===========================================================================
def insert_mbon_sample(cur, conn, sample_id, row_dict, table_cols, allowed_checks,
                       key_map=None, table="mbon_samples"):
    """
    Insert one mbon_samples row for a given sample_id.  Only columns that exist
    in the table are used; values violating CHECK constraints are dropped
    (nulled) rather than failing the row.  Returns mbon_data_id or None.
    """
    key_map = key_map or KEY_MAP
    numeric_types = {"double precision", "real", "numeric"}
    data = {"sample_id": sample_id}

    for k, v in row_dict.items():
        nk = _norm(k)
        nk = key_map.get(nk, nk)
        if nk in ("mbon_data_id", "sample_id"):
            continue
        if nk not in table_cols or _is_missing(v):
            continue
        cleaned = _clean_text(v)
        if table_cols[nk].lower() in numeric_types:
            cleaned = _to_float_or_none(cleaned)
            if cleaned is None:
                continue
        data[nk] = cleaned

    if "water_volume_filtered" in data:
        parsed = parse_volume_to_ml(data["water_volume_filtered"])
        if parsed is None:
            del data["water_volume_filtered"]
        else:
            data["water_volume_filtered"] = parsed

    # drop values that violate CHECK (allowed-value) constraints
    for col, allowed in allowed_checks.items():
        if col in data and not _is_missing(data[col]) and str(data[col]) not in allowed:
            del data[col]

    if list(data.keys()) == ["sample_id"]:
        # still create the linking row so downstream FKs resolve
        pass

    cols = sorted(data.keys())
    placeholders = ", ".join(f"%({c})s" for c in cols)
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) RETURNING mbon_data_id;"
    cur.execute("SAVEPOINT mbon_row")
    try:
        cur.execute(sql, data)
        mbon_data_id = cur.fetchone()[0]
        cur.execute("RELEASE SAVEPOINT mbon_row")
        return mbon_data_id
    except Exception as e:
        cur.execute("ROLLBACK TO SAVEPOINT mbon_row")
        cur.execute("RELEASE SAVEPOINT mbon_row")
        log.error(f"mbon_samples insert failed for sample_id={sample_id}: {e}")
        return None


# ===========================================================================
# 5. Kraken2 report parsing / ingest
# ===========================================================================
def parse_kraken2_report(fp):
    """Yield (abundance, count, rank, taxonomy_id, parent_taxonomy_id, name) tuples."""
    parent_stack = []  # (indent, taxonomy_id)
    with open(fp, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 6:
                continue
            try:
                abundance = float(parts[0].strip())
            except ValueError:
                abundance = 0.0
            try:
                count = int(parts[1].strip())
            except ValueError:
                count = 0
            rank = (parts[3].strip() or None) if parts[3] is not None else None
            try:
                taxonomy_id = int(parts[4].strip())
            except ValueError:
                taxonomy_id = 0
            raw_name = parts[5]
            indent = len(raw_name) - len(raw_name.lstrip(" "))
            name = raw_name.strip()
            while parent_stack and parent_stack[-1][0] >= indent:
                parent_stack.pop()
            parent_taxonomy_id = parent_stack[-1][1] if parent_stack else None
            parent_stack.append((indent, taxonomy_id))
            yield abundance, count, rank, taxonomy_id, parent_taxonomy_id, name


def find_kraken2_report_files(kraken2_root, k2_ref):
    """
    Resolve a manifest k2_report reference to a list of report files.
    ``k2_ref`` may be a single file, a folder (walked recursively for
    *.report / *.kraken2.report / *.kraken2.report.txt), or None.
    """
    if not k2_ref or _is_missing(k2_ref):
        return []
    ref = str(k2_ref).strip()
    # strip a leading "reports/" (legacy manifest prefix)
    ref = re.sub(r"^reports/", "", ref)
    files = []
    patterns = ("*.kraken2.report.txt", "*.kraken2.report", "*.kraken.report", "*.report")
    # The reference may sit directly under kraken2/ or under a per-database
    # folder (kraken2/<DATABASE>/<ref>) — the same layout build_mbon_html.py reads.
    candidates = [kraken2_root / ref]
    if kraken2_root.is_dir():
        candidates += [d / ref for d in sorted(kraken2_root.iterdir())
                       if d.is_dir() and not ec.is_temp_file(d)]
    for candidate in candidates:
        if candidate.is_file():
            files.append(candidate)
        elif candidate.is_dir():
            for pat in patterns:
                files.extend(sorted(candidate.rglob(pat)))
    if not files and Path(ref).suffix == "" and kraken2_root.is_dir():
        # maybe a stem inside kraken2_root
        for pat in patterns:
            files.extend(sorted(kraken2_root.rglob(ref + pat.lstrip("*"))))
    if not files:
        log.warning("k2_report '%s' matched no Kraken2 report under %s", ref, ec.rel(kraken2_root))
    # de-duplicate; skip .out and run-level aggregates (full/unclassified)
    files = [f for f in dict.fromkeys(files)
             if not f.name.endswith(".out")
             and kraken2_stem(f.name).lower() not in ("full", "unclassified")
             and "__unclassified" not in str(f)]
    return files


def kraken2_stem(filename):
    """Return the sample-identifying stem of a kraken2 report filename."""
    name = filename
    for ext in (".kraken2.report.txt", ".kraken2.report", ".kraken.report.txt",
                ".kraken.report", ".report.txt", ".report"):
        if name.endswith(ext):
            return name[: -len(ext)]
    return Path(name).stem


def ingest_kraken2(cur, conn, files, stem_to_bioinfo, database="kraken2", clear=False):
    """Insert kraken2_report + kraken2_lcas rows. ``stem_to_bioinfo`` maps a
    concordance key (report stem) -> bioinfo_id."""
    if clear:
        refresh_table(conn, "kraken2_lcas")
        refresh_table(conn, "kraken2_report")
    reports_in, lcas_in, unmatched, failed = 0, 0, [], []
    for fp in files:
        stem = kraken2_stem(fp.name)
        bioinfo_id = stem_to_bioinfo.get(stem) or stem_to_bioinfo.get(normalize_mbon_id(stem))
        if bioinfo_id is None:
            unmatched.append(fp.name)
            continue
        krona = ec.rel(fp).replace(".report", ".krona.html")
        try:
            ec.require_file(fp, "Kraken2 report")
            rows = list(parse_kraken2_report(fp))
            if not rows:
                raise InputError(f"{fp.name}: no parsable Kraken2 lines")
            cur.execute(
                "INSERT INTO kraken2_report (bioinfo_id, database, report_date, krona_location, report_location) "
                "VALUES (%s,%s,NOW(),%s,%s) RETURNING id",
                (bioinfo_id, database, krona, ec.rel(fp)),
            )
            report_id = cur.fetchone()[0]
            for ab, cnt, rank, taxid, parent, name in rows:
                cur.execute(
                    "INSERT INTO kraken2_lcas (taxonomy_id, parent_taxonomy_id, kraken2_report_id, "
                    "rank, abundance, count, name) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (taxid, parent, report_id, rank, ab, cnt, name),
                )
            conn.commit()
            reports_in += 1
            lcas_in += len(rows)
        except (InputError, OSError) as e:
            conn.rollback()
            failed.append(fp.name)
            log.error("kraken2 report skipped — %s", e)
        except Exception as e:          # DB error on one report must not lose the rest
            conn.rollback()
            failed.append(fp.name)
            log.error("kraken2 report %s failed to load: %s", fp.name, e)
    if unmatched:
        log.warning("%d kraken2 report(s) had no matching sample in the concordance: %s",
                    len(unmatched), ", ".join(unmatched[:10]) + (" ..." if len(unmatched) > 10 else ""))
    return {"reports": reports_in, "lcas": lcas_in, "unmatched": unmatched, "failed": failed}


# ===========================================================================
# 6. Wilderlab ASV loader
# ===========================================================================
def load_wilder_uid_map(path):
    """Read the wilder 'metadata' tab -> {UID(str): ClientSampleID(str)}."""
    import pandas as pd
    ec.require_sheets(path, ["metadata"], "Wilder workbook")
    meta = pd.read_excel(path, sheet_name="metadata", header=None, engine="openpyxl")
    # header row is the one containing 'UID' and 'ClientSampleID'
    hidx = None
    for i in range(min(len(meta), 40)):
        cells = {_norm(c) for c in meta.iloc[i].values if c is not None}
        if "uid" in cells and "clientsampleid" in cells:
            hidx = i
            break
    if hidx is None:
        log.warning("%s: 'metadata' sheet has no UID + ClientSampleID header row",
                    Path(path).name)
        return {}
    sub = meta.iloc[hidx + 1:].reset_index(drop=True)
    sub.columns = [str(c).strip() for c in meta.iloc[hidx].values]
    sub = sub.dropna(subset=["UID"])
    out = {}
    for _, r in sub.iterrows():
        uid = str(r["UID"]).strip()
        if uid.endswith(".0") and uid[:-2].isdigit():
            uid = uid[:-2]
        if not uid or uid.lower() == "nan":
            continue
        client = r.get("ClientSampleID")
        out[uid] = str(client).strip() if pd.notna(client) and str(client).strip() else ""
    return out


def _load_wilder_aggregated(path):
    """Read the wilder 'aggregated' tab, auto-detecting the header row that
    contains ScientificName/Rank/TaxID."""
    import pandas as pd
    ec.require_sheets(path, ["aggregated"], "Wilder workbook")
    raw = pd.read_excel(path, sheet_name="aggregated", header=None, engine="openpyxl")
    hidx = None
    for i in range(min(len(raw), 15)):
        cells = {_norm(c) for c in raw.iloc[i].values if c is not None}
        if "scientificname" in cells and "rank" in cells:
            hidx = i
            break
    if hidx is None:
        raise InputError(f"{Path(path).name}: 'aggregated' sheet has no ScientificName/Rank header")
    df = raw.iloc[hidx + 1:].reset_index(drop=True)
    df.columns = [str(c).strip() if c is not None else f"col{j}"
                  for j, c in enumerate(raw.iloc[hidx].values)]
    # UID headers often arrive as floats ('571301.0') — normalise to '571301'
    df.columns = [c[:-2] if c.endswith(".0") and c[:-2].isdigit() else c for c in df.columns]
    missing = [c for c in ("ScientificName", "Rank", "TaxID", "CommonName", "Group")
               if c not in df.columns]
    for c in missing:
        df[c] = None
    if "ScientificName" in missing or "Rank" in missing:
        raise InputError(f"{Path(path).name}: 'aggregated' is missing {missing}")
    return df


def _to_int_or_zero(v):
    f = _to_float_or_none(v)
    return int(f) if f is not None and f == f else 0


def ingest_wilder(cur, conn, path, job_id, client_to_sample, sample_to_mbon,
                  sample_to_bioinfo, taxid_to_taxonomy, id_to_sample=None,
                  norm_to_sample=None):
    """Ingest one wilder xlsx 'aggregated' tab into wilder_results.

    Sample resolution order per UID column:
      1) UID -> ClientSampleID -> sample  (client_to_sample)
      2) UID -> FAIRe samp_number -> sample  (id_to_sample / norm_to_sample)
    """
    import pandas as pd
    id_to_sample = id_to_sample or {}
    norm_to_sample = norm_to_sample or {}
    try:
        ec.require_file(path, "Wilder workbook", ec.SPREADSHEET_EXT)
        uid_to_client = load_wilder_uid_map(path)
        df = _load_wilder_aggregated(path)
    except InputError as e:
        log.error("wilder workbook skipped — %s", e)
        return {"inserted": 0, "skipped_zero": 0, "unresolved_uids": 0, "error": str(e)}
    info_cols = {"ScientificName", "Rank", "TaxID", "CommonName", "Group"}
    uid_cols = [str(c) for c in df.columns if str(c) not in info_cols]

    inserted, skipped, unresolved = 0, 0, 0
    for uid_str in uid_cols:
        client = uid_to_client.get(uid_str)
        sample_id = None
        if client:
            sample_id = (client_to_sample.get(client)
                         or client_to_sample.get(normalize_mbon_id(client)))
        if not sample_id:  # fallback: UID matches FAIRe samp_number directly
            sample_id = (id_to_sample.get(uid_str)
                         or norm_to_sample.get(normalize_mbon_id(uid_str)))
        if not sample_id:
            unresolved += 1
            continue
        mbon_data_id = sample_to_mbon.get(sample_id)
        bioinfo_id = sample_to_bioinfo.get(sample_id)
        if not mbon_data_id or not bioinfo_id:
            unresolved += 1
            continue
        col_key = uid_str if uid_str in df.columns else None
        if col_key is None:
            continue
        for _, r in df.iterrows():
            read_count = _to_int_or_zero(r.get(col_key))
            if read_count <= 0:
                skipped += 1
                continue
            if _is_missing(r["ScientificName"]) or _is_missing(r["Rank"]):
                skipped += 1
                continue
            tid = _to_float_or_none(r["TaxID"])
            tax_id = int(tid) if tid is not None else None
            data = {
                "mbon_data_id": mbon_data_id,
                "bioinfo_id": bioinfo_id,
                "wilder_job_id": job_id,
                "wilder_uid": uid_str,
                "scientific_name": str(r["ScientificName"]) if pd.notna(r["ScientificName"]) else None,
                "taxonomic_rank": str(r["Rank"]) if pd.notna(r["Rank"]) else None,
                "tax_id": tax_id,
                "common_name": str(r["CommonName"]) if pd.notna(r["CommonName"]) else None,
                "organism_group": str(r["Group"]) if pd.notna(r["Group"]) else None,
                "taxonomy_id": taxid_to_taxonomy.get(tax_id) if tax_id else None,
                "read_count": read_count,
            }
            cur.execute(
                """
                INSERT INTO wilder_results
                    (mbon_data_id, bioinfo_id, wilder_job_id, wilder_uid, scientific_name,
                     taxonomic_rank, tax_id, common_name, organism_group, taxonomy_id, read_count)
                VALUES (%(mbon_data_id)s,%(bioinfo_id)s,%(wilder_job_id)s,%(wilder_uid)s,
                        %(scientific_name)s,%(taxonomic_rank)s,%(tax_id)s,%(common_name)s,
                        %(organism_group)s,%(taxonomy_id)s,%(read_count)s)
                ON CONFLICT (wilder_job_id, wilder_uid, tax_id, taxonomic_rank) DO UPDATE SET
                    read_count=EXCLUDED.read_count, updated_at=NOW();
                """,
                data,
            )
            inserted += 1
        conn.commit()
    if unresolved:
        log.warning("%s: %d UID column(s) could not be linked to a FAIRe sample "
                    "(check ClientSampleID / samp_number)", Path(path).name, unresolved)
    return {"inserted": inserted, "skipped_zero": skipped, "unresolved_uids": unresolved}


# ===========================================================================
# 7. Concordance generation (report file  <->  FAIRe sample_id)
# ===========================================================================
def load_barcode_maps(barcode_files):
    """
    Read Oxford-style eDNA barcoding workbooks. Each non-'metadata' sheet is a
    sequencing run; rows map a Barcode number -> sample_ID. Returns
    {run_sheet_name: {barcode_int: sample_ID}}.
    """
    import openpyxl
    out = {}
    for bf in barcode_files:
        try:
            ec.require_file(bf, "barcoding workbook", ec.SPREADSHEET_EXT)
            wb = openpyxl.load_workbook(bf, data_only=True, read_only=True)
        except Exception as e:
            log.error("barcoding workbook skipped — %s", e)
            continue
        for sheet in wb.sheetnames:
            if _norm(sheet) == "metadata":
                continue
            ws = wb[sheet]
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                continue
            header = [_norm(c) for c in rows[0]]
            try:
                bi = header.index("barcode")
                si = header.index("sample_id")
            except ValueError:
                continue
            mp = {}
            for r in rows[1:]:
                if bi >= len(r) or si >= len(r):
                    continue
                bc, sid = r[bi], r[si]
                if bc is None or sid is None:
                    continue
                try:
                    mp[int(bc)] = str(sid).strip()
                except (ValueError, TypeError):
                    continue
            if mp:
                out[sheet] = mp
    return out


def _barcode_from_stem(stem):
    m = re.search(r"barcode0*(\d+)", stem, flags=re.IGNORECASE)
    if m:
        return int(m.group(1))
    if stem.isdigit():
        return int(stem)
    return None


def _trailing_int(s):
    """Trailing integer of an id, e.g. 'samp_002'->2, 'OXR20260514-002'->2."""
    m = re.search(r"(\d+)$", str(s).strip())
    return int(m.group(1)) if m else None


def load_faire_coords(faire_files):
    """
    Read decimalLatitude/Longitude from FAIRe 'samplesheet_v1' / 'sampleMetadata'
    sheets (some checklists, e.g. OceanX, put coordinates on the samplesheet keyed
    by submission id rather than on sampleMetadata).

        Returns (by_id, by_suffix):
            by_id     : {sample_ID string -> {lat, lon, station, geo_loc_name}}
            by_suffix : {trailing integer of sample_ID -> same dict}
    """
    import openpyxl
    by_id, by_suffix = {}, {}
    for f in faire_files:
        if str(f).lower().endswith(".csv"):
            continue
        try:
            wb = openpyxl.load_workbook(f, data_only=True, read_only=True)
        except Exception as e:
            log.warning("could not scan %s for coordinates: %s", Path(f).name, e)
            continue
        for sheet in wb.sheetnames:
            if _norm_col(sheet) not in ("samplesheetv1", "samplemetadata"):
                continue
            rows = list(wb[sheet].iter_rows(values_only=True))
            hidx = None
            for i, r in enumerate(rows[:8]):
                cells = {_norm_col(c) for c in r if c is not None}
                if {"sampleid", "decimallatitude", "decimallongitude"} <= cells:
                    hidx = i
                    break
            if hidx is None:
                continue
            hdr = [_norm_col(c) for c in rows[hidx]]
            si = hdr.index("sampleid")
            lai, loi = hdr.index("decimallatitude"), hdr.index("decimallongitude")
            site_i = hdr.index("site") if "site" in hdr else None
            geo_i = hdr.index("geolocname") if "geolocname" in hdr else None
            for r in rows[hidx + 1:]:
                if si >= len(r) or r[si] is None:
                    continue
                lat = _to_float_or_none(r[lai]) if lai < len(r) else None
                lon = _to_float_or_none(r[loi]) if loi < len(r) else None
                if lat is None or lon is None:
                    continue
                sid = str(r[si]).strip()
                rec = {"lat": lat, "lon": lon}
                if site_i is not None and site_i < len(r) and not _is_missing(r[site_i]):
                    rec["station"] = str(r[site_i]).strip()
                if geo_i is not None and geo_i < len(r) and not _is_missing(r[geo_i]):
                    rec["geo_loc_name"] = str(r[geo_i]).strip()
                by_id.setdefault(sid, rec)
                suf = _trailing_int(sid)
                if suf is not None:
                    by_suffix.setdefault(suf, rec)
    return by_id, by_suffix


def generate_concordance(project_dir, faire_files, barcode_files=None, write=True):
    """
    Build a concordance mapping every kraken2 report file and wilder UID to a
    FAIRe sample identifier, written to data/projects/<project_dir>/meta/concordance.json.

    kraken2:  report filename stem  == materialSampleID       -> samp_number
              or (Oxford) barcodeNN  -> run sheet -> sample_ID -> samp_number
    wilder:   UID                    -> ClientSampleID         -> samp_number/samp_name

    Returns the concordance dict. Never raises on unmatched items — they are
    listed under "unmatched" for review.
    """
    barcode_maps = load_barcode_maps(barcode_files or [])
    proj = DATA_PROJECTS / project_dir
    meta_dir = proj / "meta"
    kraken2_root = proj / "kraken2"
    wilder_root = proj / "wilder"

    # Build FAIRe lookup tables from all provided checklists.
    material_to_id, name_to_id, norm_to_id, all_ids = {}, {}, {}, set()
    for fpath in faire_files:
        try:
            df = load_faire_samples(fpath)
        except Exception as e:
            log.error(f"could not read FAIRe file {fpath}: {e}")
            continue
        mcol = faire_material_col(df)
        ncol = faire_name_col(df)
        for _, r in df.iterrows():
            sid = r["_samp_id"]
            all_ids.add(sid)
            norm_to_id[r["_samp_norm"]] = sid
            if mcol and not _is_missing(r.get(mcol)):
                material_to_id[str(r[mcol]).strip()] = sid
            if ncol and not _is_missing(r.get(ncol)):
                name_to_id.setdefault(str(r[ncol]).strip(), sid)

    concordance = {
        "project_dir": project_dir,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "kraken2": {},   # report filename -> {stem, samp_number}
        "wilder": {},    # "<job>:<uid>" -> {uid, client_sample_id, samp_number}
        "unmatched": {"kraken2": [], "wilder": []},
    }

    # trailing-int -> FAIRe id, used to bridge barcode submission ids (OXR...-002)
    # to the matching sampleMetadata sample (samp_002).
    suffix_to_id = {}
    for sid_ in all_ids:
        suf = _trailing_int(sid_)
        if suf is not None:
            suffix_to_id.setdefault(suf, sid_)

    # --- kraken2 ---
    if kraken2_root.is_dir():
        seen_rel = set()
        for pat in ("*.kraken2.report.txt", "*.kraken2.report", "*.kraken.report", "*.report"):
            for fp in sorted(kraken2_root.rglob(pat)):
                if fp.name.endswith(".out"):
                    continue
                stem = kraken2_stem(fp.name)
                # skip run-level aggregates / non-sample dirs
                if stem.lower() in ("full", "unclassified") or "__unclassified" in str(fp):
                    continue
                rel = str(fp.relative_to(kraken2_root))
                if rel in seen_rel:
                    continue
                seen_rel.add(rel)
                # 1) direct match by materialSampleID / name / normalized id
                sid = (material_to_id.get(stem) or norm_to_id.get(normalize_mbon_id(stem))
                       or name_to_id.get(stem))
                bc_sample = None
                # 2) Oxford barcode match: run-sheet name is the path prefix before "__"
                if sid is None and barcode_maps:
                    bc = _barcode_from_stem(stem)
                    run_prefix = rel.split("/")[0].split("__")[0]
                    for sheet, mp in barcode_maps.items():
                        if (sheet == run_prefix or run_prefix in sheet or sheet in run_prefix) \
                                and bc in mp:
                            bc_sample = mp[bc]
                            # prefer the real FAIRe sample: direct id, then
                            # numeric-suffix bridge (OXR...-002 -> samp_002)
                            sid = (bc_sample if bc_sample in all_ids
                                   else material_to_id.get(bc_sample)
                                   or name_to_id.get(bc_sample)
                                   or suffix_to_id.get(_trailing_int(bc_sample))
                                   or bc_sample)
                            break
                if sid:
                    entry = {"stem": stem, "samp_number": sid}
                    if bc_sample:
                        entry["barcode_sample_id"] = bc_sample
                    concordance["kraken2"][rel] = entry
                else:
                    concordance["unmatched"]["kraken2"].append(rel)

    # --- wilder ---
    if wilder_root.is_dir():
        for fp in sorted(wilder_root.glob("*.xlsx")):
            if ec.is_temp_file(fp):
                continue
            job_id = fp.stem.split("_")[0]
            try:
                uid_map = load_wilder_uid_map(fp)
            except Exception as e:
                log.error("concordance: cannot read %s — %s", fp.name, e)
                uid_map = {}
            for uid, client in uid_map.items():
                sid = None
                if client:  # 1) via ClientSampleID
                    sid = (material_to_id.get(client) or name_to_id.get(client)
                           or norm_to_id.get(normalize_mbon_id(client)))
                if not sid:  # 2) UID matches FAIRe samp_number directly
                    sid = (uid if uid in all_ids
                           else norm_to_id.get(normalize_mbon_id(uid)))
                matched = bool(sid) and (sid in all_ids
                                         or (client and normalize_mbon_id(client) in norm_to_id))
                key = f"{job_id}:{uid}"
                concordance["wilder"][key] = {
                    "uid": uid, "client_sample_id": client,
                    "samp_number": sid if matched else None,
                }
                if not matched:
                    concordance["unmatched"]["wilder"].append(key)

    if write:
        meta_dir.mkdir(parents=True, exist_ok=True)
        out = meta_dir / "concordance.json"
        out.write_text(json.dumps(concordance, indent=2))
        if not faire_files:
            log.warning("concordance for %s built without any FAIRe checklist — nothing can match",
                        project_dir)
        nk = len(concordance["kraken2"])
        nw = len(concordance["wilder"])
        uk = len(concordance["unmatched"]["kraken2"])
        uw = len(concordance["unmatched"]["wilder"])
        log.info(f"concordance -> {ec.rel(out)}  "
              f"(kraken2 {nk} matched/{uk} unmatched; wilder {nw} matched/{uw} unmatched)")
    return concordance


def load_concordance(project_dir):
    p = DATA_PROJECTS / project_dir / "meta" / "concordance.json"
    if p.is_file():
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.error("corrupt concordance %s (%s) — regenerate with `ingest.py concordance`",
                      ec.rel(p), e)
    return None


# ===========================================================================
# 8. Manifest reader
# ===========================================================================
MANIFEST_REQUIRED = ("sample_id",)
MANIFEST_RECOMMENDED = ("project", "metadata")


def read_manifest(path=None):
    """Read and validate the report manifest (.xlsx 'Sheet1'/first sheet, or
    .csv) into a list of dict rows with lower-cased keys. Raises InputError
    with an actionable message when the file is missing or malformed."""
    path = Path(path or MANIFEST_PATH)
    ec.require_file(path, "report manifest", ec.SPREADSHEET_EXT + (".csv", ".tsv"))
    if path.suffix.lower() in (".csv", ".tsv"):
        import csv
        with open(path, newline="", encoding="utf-8-sig") as fh:
            sample = fh.read(4096)
            fh.seek(0)
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
            rows = [list(r) for r in csv.reader(fh, dialect)]
        rows = [[(c if c != "" else None) for c in r] for r in rows]
    else:
        import openpyxl
        try:
            wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
        except Exception as e:
            raise InputError(f"cannot open manifest {path.name}: {e}") from e
        ws = wb["Sheet1"] if "Sheet1" in wb.sheetnames else wb.worksheets[0]
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        wb.close()
    rows = [r for r in rows if r and not all(c is None or str(c).strip() == "" for c in r)]
    if not rows:
        raise InputError(f"manifest {path.name} is empty")
    header = [(_norm(c) if c is not None else "") for c in rows[0]]
    missing = [c for c in MANIFEST_REQUIRED if c not in header]
    if missing:
        raise InputError(f"manifest {path.name} is missing required column(s) {missing}; "
                         f"header is {[h for h in header if h]}")
    for c in MANIFEST_RECOMMENDED:
        if c not in header:
            log.warning("manifest has no '%s' column", c)
    if "project_dir" not in header and "project" not in header:
        raise InputError("manifest needs a 'project_dir' or 'project' column to locate "
                         "data/projects/<project>/")
    unknown = [h for h in header if h and h not in MANIFEST_COLUMNS
               and h not in ("sample_type", "notes", "comments")]
    if unknown:
        log.debug("manifest: extra columns carried through: %s", unknown)
    out = []
    for n, r in enumerate(rows[1:], start=2):
        r = list(r) + [None] * (len(header) - len(r))
        d = {header[i]: (_clean_text(r[i]) if isinstance(r[i], str) else r[i])
             for i in range(len(header)) if header[i]}
        if _is_missing(d.get("sample_id")):
            log.warning("manifest row %d has no sample_id — ignored", n)
            continue
        d["_row"] = n
        out.append(d)
    log.info("manifest %s: %d usable row(s), %d disabled", ec.rel(path), len(out),
             sum(1 for d in out if _truthy(d.get("disable"))))
    return out


def _truthy(v):
    return str(v).strip().lower() in ("1", "1.0", "true", "yes", "y", "t", "x")


# Subfolders a per-sample metadata (FAIRe / checklist) file might live in.
META_SEARCH_DIRS = ("faire", "meta", ".", "wilder")


def resolve_metadata_file(project_dir, filename):
    """Locate a metadata filename within a project's standardized subfolders."""
    proj = DATA_PROJECTS / project_dir
    for sub in META_SEARCH_DIRS:
        cand = (proj / sub / filename) if sub != "." else (proj / filename)
        if cand.is_file():
            return cand
    return None


def collect_faire_files(project_dir, rows):
    """Resolve the distinct FAIRe/checklist files referenced by a project's rows,
    falling back to any *.xlsx in faire/ then meta/."""
    files = []
    for r in rows:
        name = r.get("metadata")
        if name and not _is_missing(name):
            fp = resolve_metadata_file(project_dir, str(name).strip())
            if fp is None:
                log.warning("metadata file '%s' (manifest row %s) not found in %s/{%s}",
                            name, r.get("_row", "?"), ec.rel(DATA_PROJECTS / project_dir),
                            ",".join(META_SEARCH_DIRS))
            elif fp not in files:
                files.append(fp)
    if not files:
        for sub in ("faire", "meta"):
            d = DATA_PROJECTS / project_dir / sub
            if d.is_dir():
                files.extend(p for p in sorted(d.glob("*.xlsx"))
                             if not ec.is_temp_file(p) and p not in files)
            if files:
                break
    return files


def collect_barcode_files(project_dir, rows):
    """Resolve the distinct barcoding files referenced by a project's rows."""
    files = []
    for r in rows:
        name = r.get("barcodes")
        if name and not _is_missing(name) and not _truthy(name):
            fp = resolve_metadata_file(project_dir, str(name).strip())
            if fp is None:
                log.warning("barcodes file '%s' not found for project %s", name, project_dir)
            elif fp not in files:
                files.append(fp)
    return files


# ===========================================================================
# 9. Project ingest orchestration
# ===========================================================================
# Values that FAIRe/spreadsheets use to mean "empty / not a control". These must
# NOT be treated as a control marker — that loose check was marking every sample
# NEGATIVE_CONTROL because real samples carry neg_cont_type="not applicable".
_SENTINEL_EMPTY = {
    "", "none", "nan", "na", "n/a", "n.a.", "not applicable", "notapplicable",
    "null", "-", "--", "0", "false", "no", "not collected", "not provided",
    "missing", "not_applicable",
}


def _is_control_value(v):
    """True only when a control-type cell holds a real (non-sentinel) value."""
    return str(v).strip().lower() not in _SENTINEL_EMPTY


def _sample_type_from_faire(row):
    """Classify a sample as SAMPLE / POSITIVE_CONTROL / NEGATIVE_CONTROL.

    Precedence:
      1. An explicit sample_type / samp_type override (from the manifest or the
         FAIRe sheet) always wins.
      2. neg_cont_type / pos_cont_type, but only when they hold a *real* value
         (sentinels like "not applicable" are ignored).
      3. samp_category keywords (control / blank / ntc / positive / negative).
      4. Default: SAMPLE.
    """
    # 1. explicit override
    st = str(row.get("sample_type") or row.get("samp_type") or "").strip().upper().replace(" ", "_")
    if st in ("SAMPLE", "POSITIVE_CONTROL", "NEGATIVE_CONTROL", "OTHER"):
        return st

    cat = str(row.get("samp_category") or "").strip().lower()
    neg = row.get("neg_cont_type")
    pos = row.get("pos_cont_type")

    # 2. dedicated control-type columns (real values only)
    if _is_control_value(neg):
        return "NEGATIVE_CONTROL"
    if _is_control_value(pos):
        return "POSITIVE_CONTROL"

    # 3. category keywords — require a control-ish word, not a bare substring
    if any(k in cat for k in ("neg", "blank", "ntc", "no_template", "no template")):
        return "NEGATIVE_CONTROL"
    if "pos" in cat and "control" in cat:
        return "POSITIVE_CONTROL"

    # 4. default
    return "SAMPLE"


def ingest_project(conn, project_dir, rows, regen_concordance=True):
    """Ingest every enabled manifest row belonging to one project_dir."""
    proj_path = DATA_PROJECTS / project_dir
    kraken2_dir, wilder_dir = proj_path / "kraken2", proj_path / "wilder"
    project_name = next((r["project"] for r in rows if r.get("project")), project_dir)
    platform = next((r.get("platform") for r in rows if r.get("platform")), None)

    # Manifest-level sample_type override map (authoritative when the manifest's
    # optional `sample_type` column is set for a sample_id). Keyed by the raw and
    # normalized sample_id so FAIRe rows and report-only samples both resolve.
    manifest_stype = {}
    for _r in rows:
        _st = str(_r.get("sample_type") or "").strip().upper().replace(" ", "_")
        if _st in ("SAMPLE", "POSITIVE_CONTROL", "NEGATIVE_CONTROL", "OTHER"):
            _sid = str(_r.get("sample_id") or "").strip()
            if _sid:
                manifest_stype[_sid] = _st
                try:
                    manifest_stype[normalize_mbon_id(_sid)] = _st
                except Exception:
                    pass

    log.info(f"=== Project '{project_name}'  ({ec.rel(proj_path)}) ===")
    cur = conn.cursor()
    user_id = get_or_create_user(cur, DEFAULT_USER)
    instrument_id = get_or_create_instrument(cur, platform) if platform else None
    project_id = get_or_create_project(cur, project_name, user_id, project_name)
    conn.commit()

    # ---- concordance (auto-generate into meta/) ----
    faire_files = collect_faire_files(project_dir, rows)
    barcode_files = collect_barcode_files(project_dir, rows)
    if not faire_files:
        log.warning("project %s: no FAIRe checklist found (manifest 'metadata' column or "
                    "faire/*.xlsx) — samples will only be created from reports", project_dir)
    if regen_concordance:
        generate_concordance(project_dir, faire_files, barcode_files=barcode_files, write=True)
    concordance = load_concordance(project_dir) or {"kraken2": {}, "wilder": {}}

    # ---- FAIRe samples -> sample + mbon_samples + data_management + bioinformatics ----
    mbon_cols = table_columns(cur, "mbon_samples")
    mbon_checks = check_allowed_values(cur, "mbon_samples")

    id_to_sample, material_to_sample, name_to_sample, norm_to_sample = {}, {}, {}, {}
    sample_to_mbon, sample_to_bioinfo = {}, {}

    # run label shared by this project's samples (each sample gets its own run row)
    run_label = next((str(r["run"]).strip() for r in rows if r.get("run")), project_name)
    cruise = next((r.get("cruise") for r in rows if r.get("cruise") not in (None, "")), None)
    # coordinates that may live on samplesheet_v1 (e.g. OceanX) keyed by id/suffix
    faire_coords_by_id, faire_coords_by_suffix = load_faire_coords(faire_files)

    for fpath in faire_files:
        try:
            df = load_faire_samples(fpath)
        except Exception as e:
            log.error(f"FAIRe load failed {fpath.name}: {e}")
            continue
        mcol, ncol = faire_material_col(df), faire_name_col(df)
        # each FAIRe checklist becomes a collection (a sampling batch / cruise)
        coll_name = f"{project_name} {cruise}".strip() if cruise else fpath.stem
        collection_id = get_or_create_collection(
            cur, project_id, collection_name=coll_name,
            collection_type="sampling_batch",
            collected_at=parse_event_date_to_noon(cruise),
            description=f"FAIRe checklist: {fpath.name}")
        conn.commit()
        n_samp = 0
        for _, r in df.iterrows():
            sid_key = r["_samp_id"]
            sample_name = str(r[mcol]).strip() if (mcol and not _is_missing(r.get(mcol))) else sid_key
            row_dict = {c: r[c] for c in df.columns}
            collected_at = parse_event_date_to_noon(
                row_dict.get("eventDate") or row_dict.get("event_date")
                or row_dict.get("CollectionDate"))

            # location: prefer the row's own coords, else samplesheet_v1 by id/suffix
            lat = _to_float_or_none(row_dict.get("decimalLatitude") or row_dict.get("latitude"))
            lon = _to_float_or_none(row_dict.get("decimalLongitude") or row_dict.get("longitude"))
            if lat is None or lon is None:
                c = (faire_coords_by_id.get(sample_name) or faire_coords_by_id.get(sid_key)
                     or faire_coords_by_suffix.get(_trailing_int(sid_key)))
                if c:
                    lat, lon = c.get("lat"), c.get("lon")
                    if c.get("station") and _is_missing(row_dict.get("Station")):
                        row_dict["Station"] = c["station"]
                    if c.get("geo_loc_name") and _is_missing(row_dict.get("geo_loc_name")):
                        row_dict["geo_loc_name"] = c["geo_loc_name"]
            # alias must be unique per point (location_alias is UNIQUE); fall back
            # to the sample name so distinct samples get distinct map markers
            alias = (row_dict.get("Station") or row_dict.get("station")
                     or _clean_text(row_dict.get("locality")) or sample_name)
            loc_id = ensure_location(cur, conn, alias, lat, lon, project_id)

            # Manifest override (if any) wins over FAIRe-derived classification.
            _stype = (manifest_stype.get(str(sample_name).strip())
                      or manifest_stype.get(sid_key)
                      or _sample_type_from_faire(row_dict))
            sample_id = get_or_create_sample(
                cur, collection_id, sample_name, submitted_by=user_id,
                sample_type=_stype,
                collected_at=collected_at, directory=ec.rel(fpath),
                instrument_id=instrument_id, location_id=loc_id)

            # if coords were derived from samplesheet (row had none), record them
            if (lat is not None and lon is not None
                    and _is_missing(row_dict.get("decimalLatitude"))
                    and _is_missing(row_dict.get("latitude"))):
                row_dict["latitude"], row_dict["longitude"] = lat, lon
            mbon_data_id = insert_mbon_sample(cur, conn, sample_id, row_dict,
                                              mbon_cols, mbon_checks)
            # one run + data_management + bioinformatics per sample
            run_id = get_or_create_run(cur, sample_id, directory=run_label,
                                       entered_by=user_id, instrument_id=instrument_id,
                                       comments=sample_name)
            dm_id = get_or_create_data_management(cur, run_id, associated_resource=fpath.name)
            bio_id = get_or_create_bioinformatics(cur, dm_id)

            id_to_sample[sid_key] = sample_id
            norm_to_sample[r["_samp_norm"]] = sample_id
            if mcol and not _is_missing(r.get(mcol)):
                material_to_sample[str(r[mcol]).strip()] = sample_id
            if ncol and not _is_missing(r.get(ncol)):
                name_to_sample.setdefault(str(r[ncol]).strip(), sample_id)
            if mbon_data_id:
                sample_to_mbon[sample_id] = mbon_data_id
            sample_to_bioinfo[sample_id] = bio_id
            n_samp += 1
        conn.commit()
        log.info(f"FAIRe {fpath.name}: collection '{coll_name}' -> {n_samp} samples "
              f"(sample/mbon_samples/run/bioinformatics)")

    # ---- taxonomy lookup ----
    cur.execute("SELECT organism_id, taxonomic_id FROM taxonomy;")
    taxid_to_taxonomy = {row[1]: row[0] for row in cur.fetchall()}

    # ---- kraken2 ingest via concordance ----
    # Resolve each concordance report -> sample -> bioinfo_id.  If a report's
    # sample id is not present in the FAIRe checklist (e.g. Oxford submission
    # ids that don't appear in sampleMetadata), auto-create a lightweight sample
    # so the report still links.  MBON is unaffected (all its ids match FAIRe).
    stem_to_bioinfo = {}
    report_coll = [None]

    def _ensure_report_sample(samp):
        sid = id_to_sample.get(samp) or norm_to_sample.get(normalize_mbon_id(str(samp)))
        if sid is None and samp:
            if report_coll[0] is None:
                report_coll[0] = get_or_create_collection(
                    cur, project_id, collection_name=f"{project_name} reports",
                    collection_type="reports",
                    description="samples auto-created from kraken2 concordance")
            _rstype = (manifest_stype.get(str(samp).strip())
                       or manifest_stype.get(normalize_mbon_id(str(samp))) or "SAMPLE")
            sid = get_or_create_sample(cur, report_coll[0], str(samp), submitted_by=user_id,
                                       sample_type=_rstype,
                                       instrument_id=instrument_id, directory=ec.rel(kraken2_dir))
            run_id = get_or_create_run(cur, sid, directory=run_label, entered_by=user_id,
                                       instrument_id=instrument_id, comments=str(samp))
            dm = get_or_create_data_management(cur, run_id, associated_resource="kraken2 concordance")
            bio = get_or_create_bioinformatics(cur, dm)
            id_to_sample[samp] = sid
            sample_to_bioinfo[sid] = bio
        return sid

    for rel, info in concordance.get("kraken2", {}).items():
        sid = _ensure_report_sample(info.get("samp_number"))
        if sid and sid in sample_to_bioinfo:
            stem_to_bioinfo[info["stem"]] = sample_to_bioinfo[sid]
    conn.commit()

    k2_files, seen = [], set()
    for r in rows:
        if _truthy(r.get("disable")):
            continue
        for fp in find_kraken2_report_files(kraken2_dir, r.get("k2_report")):
            if fp not in seen:
                seen.add(fp)
                k2_files.append(fp)
    if k2_files:
        db_name = f"kraken2_{project_dir}".lower()
        res = ingest_kraken2(cur, conn, k2_files, stem_to_bioinfo, database=db_name)
        log.info(f"kraken2: {res['reports']} reports, {res['lcas']} lca rows"
              f"{'; unmatched: ' + str(len(res['unmatched'])) if res['unmatched'] else ''}")

    # ---- wilder ingest ----
    # client -> sample_id (via concordance samp_number, then id_to_sample)
    client_to_sample = {}
    for key, info in concordance.get("wilder", {}).items():
        client = info.get("client_sample_id")
        sid = id_to_sample.get(info.get("samp_number")) \
            or norm_to_sample.get(normalize_mbon_id(str(client))) \
            or name_to_sample.get(client) or material_to_sample.get(client)
        if client and sid:
            client_to_sample[client] = sid

    wilder_files, seenw = [], set()
    for r in rows:
        if _truthy(r.get("disable")) or _is_missing(r.get("asv_report")):
            continue
        for name in str(r["asv_report"]).split(";"):
            name = name.strip()
            if not name:
                continue
            fp = wilder_dir / name
            # Tolerant resolution: manifest entries sometimes omit the .xlsx
            # extension (e.g. "WLM611810_260307"), which previously caused the
            # job to be silently skipped and missing from the dashboard.
            if not fp.is_file() and not name.lower().endswith((".xlsx", ".xls")):
                alt = wilder_dir / (name + ".xlsx")
                if alt.is_file():
                    fp = alt
            if not fp.is_file():
                hits = sorted(wilder_dir.glob(name + "*.xlsx"))
                if hits:
                    fp = hits[0]
            if fp.is_file() and fp not in seenw:
                seenw.add(fp)
                wilder_files.append(fp)
    # Auto-discover any Wilder report present in the folder that the manifest did
    # not explicitly list, so multiple Wilder jobs are never silently missed.
    if wilder_dir.is_dir():
        for fp in sorted(wilder_dir.glob("*.xlsx")):
            if fp.name.startswith("~") or fp in seenw:
                continue
            seenw.add(fp)
            wilder_files.append(fp)
    for fp in wilder_files:
        job_id = fp.stem.split("_")[0]
        res = ingest_wilder(cur, conn, fp, job_id, client_to_sample,
                            sample_to_mbon, sample_to_bioinfo, taxid_to_taxonomy,
                            id_to_sample=id_to_sample, norm_to_sample=norm_to_sample)
        log.info(f"wilder {fp.name}: {res['inserted']} results"
              f"{'; unresolved UIDs: ' + str(res['unresolved_uids']) if res['unresolved_uids'] else ''}")

    cur.close()
    log.info(f"done: {len(id_to_sample)} samples linked in project '{project_name}'")


def import_rows(conn, rows, regen_concordance=True):
    """Group rows by project_dir and ingest each project."""
    groups = {}
    for r in rows:
        pdir = r.get("project_dir") or r.get("project")
        if not pdir:
            continue
        groups.setdefault(str(pdir).strip(), []).append(r)
    ok, failed = [], []
    for project_dir, grp in groups.items():
        if not (DATA_PROJECTS / project_dir).is_dir():
            log.error(f"skipping '{project_dir}': project folder {ec.rel(DATA_PROJECTS / project_dir)} not found")
            failed.append(project_dir)
            continue
        try:
            ingest_project(conn, project_dir, grp, regen_concordance=regen_concordance)
            ok.append(project_dir)
        except Exception:
            # One broken project must not abort the others: roll back its open
            # transaction, log the full traceback to the log file, carry on.
            conn.rollback()
            log.exception("project '%s' failed — its uncommitted changes were rolled back",
                          project_dir)
            failed.append(project_dir)
    log.info("import summary: %d project(s) ok%s", len(ok),
             f", {len(failed)} failed ({', '.join(failed)})" if failed else "")
    return ok, failed


# ===========================================================================
# 10. Delete operations (cascade-aware, honoring FK chain)
# ===========================================================================
def delete_sample(conn, sample_id_or_name):
    """Delete a sample and everything hanging off it."""
    cur = conn.cursor()
    if str(sample_id_or_name).isdigit():
        cur.execute("SELECT sample_id FROM sample WHERE sample_id=%s", (int(sample_id_or_name),))
    else:
        cur.execute("SELECT sample_id FROM sample WHERE sample_name=%s", (str(sample_id_or_name),))
    ids = [r[0] for r in cur.fetchall()]
    for sid in ids:
        _delete_sample_cascade(cur, sid)
    conn.commit()
    log.info(f"deleted {len(ids)} sample(s) matching '{sample_id_or_name}'")
    cur.close()


def _delete_run_cascade(cur, rid):
    """Delete a run and its analysis artifacts (data_management -> bioinformatics
    -> kraken2/wilder). Leaves the parent sample intact."""
    cur.execute("SELECT id FROM data_management WHERE run_id=%s", (rid,))
    for (dm,) in cur.fetchall():
        cur.execute("SELECT id FROM bioinformatics WHERE data_management_id=%s", (dm,))
        for (bio,) in cur.fetchall():
            cur.execute("SELECT id FROM kraken2_report WHERE bioinfo_id=%s", (bio,))
            for (rep,) in cur.fetchall():
                cur.execute("DELETE FROM kraken2_lcas WHERE kraken2_report_id=%s", (rep,))
            cur.execute("DELETE FROM kraken2_report WHERE bioinfo_id=%s", (bio,))
            cur.execute("DELETE FROM wilder_results WHERE bioinfo_id=%s", (bio,))
        cur.execute("DELETE FROM bioinformatics WHERE data_management_id=%s", (dm,))
    cur.execute("DELETE FROM data_management WHERE run_id=%s", (rid,))
    cur.execute("DELETE FROM basecall_run WHERE run_id=%s", (rid,))
    cur.execute("DELETE FROM run_summary WHERE run_id=%s", (rid,))
    cur.execute("DELETE FROM run WHERE run_id=%s", (rid,))


def _delete_sample_cascade(cur, sid):
    cur.execute("SELECT run_id FROM run WHERE sample_id=%s", (sid,))
    for (rid,) in cur.fetchall():
        _delete_run_cascade(cur, rid)
    cur.execute("SELECT mbon_data_id FROM mbon_samples WHERE sample_id=%s", (sid,))
    for (mid,) in cur.fetchall():
        cur.execute("DELETE FROM wilder_results WHERE mbon_data_id=%s", (mid,))
    cur.execute("DELETE FROM mbon_samples WHERE sample_id=%s", (sid,))
    cur.execute("DELETE FROM sample_summary WHERE sample_id=%s", (sid,))
    cur.execute("DELETE FROM sample WHERE sample_id=%s", (sid,))


def delete_run(conn, run_directory):
    cur = conn.cursor()
    cur.execute("SELECT run_id FROM run WHERE directory=%s OR comments LIKE %s",
                (run_directory, f"%{run_directory}%"))
    run_ids = [r[0] for r in cur.fetchall()]
    for rid in run_ids:
        _delete_run_cascade(cur, rid)
    conn.commit()
    log.info(f"deleted {len(run_ids)} run(s) matching '{run_directory}' (samples kept)")
    cur.close()


def delete_project(conn, project_name):
    cur = conn.cursor()
    cur.execute("SELECT project_id FROM project WHERE project_name=%s", (project_name,))
    row = cur.fetchone()
    if not row:
        log.info(f"no project named '{project_name}'")
        cur.close()
        return
    project_id = row[0]
    cur.execute("SELECT collection_id FROM collection WHERE project_id=%s", (project_id,))
    coll_ids = [r[0] for r in cur.fetchall()]
    n_samp = 0
    for cid in coll_ids:
        cur.execute("SELECT sample_id FROM sample WHERE collection_id=%s", (cid,))
        for (sid,) in cur.fetchall():
            _delete_sample_cascade(cur, sid)
            n_samp += 1
    cur.execute("DELETE FROM collection WHERE project_id=%s", (project_id,))
    cur.execute("DELETE FROM location WHERE project_id=%s", (project_id,))
    cur.execute("DELETE FROM project WHERE project_id=%s", (project_id,))
    conn.commit()
    log.info(f"deleted project '{project_name}' (project_id={project_id}): "
          f"{len(coll_ids)} collection(s), {n_samp} sample(s)")
    cur.close()


# ===========================================================================
# 11. CLI
# ===========================================================================
def _select_rows(rows, args):
    if getattr(args, "all", False):
        return [r for r in rows if not _truthy(r.get("disable"))]
    sel = []
    for r in rows:
        if _truthy(r.get("disable")):
            continue
        if args.project and str(r.get("project")).strip() == args.project:
            sel.append(r)
        elif args.project_dir and str(r.get("project_dir")).strip() == args.project_dir:
            sel.append(r)
        elif args.sample_id and str(r.get("sample_id")).strip() == args.sample_id:
            sel.append(r)
        elif args.run and str(r.get("run")).strip() == args.run:
            sel.append(r)
    return sel


def build_parser():
    p = argparse.ArgumentParser(
        prog="ingest.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="MBON / eDNA project ingest (report_manifest.xlsx driven).",
        epilog="Run `python ingest.py validate` first: it checks every input without "
               "touching the database.")
    p.add_argument("--projects-dir", dest="projects_dir", default=None,
                   help="projects folder (default: data/projects inside this repo; env "
                        "EDNA_PROJECTS_DIR). Relative paths resolve against the repo root.")
    p.add_argument("--manifest", default=None,
                   help="report manifest (.xlsx/.csv). Default: <projects-dir>/report_manifest.xlsx")
    p.add_argument("--schema", default=None,
                   help=f"DDL file for recreate-all (default: {ec.rel(CREATE_TABLES_SQL)})")
    p.add_argument("--example", action="store_true",
                   help=f"use the bundled synthetic demo data ({ec.rel(ec.EXAMPLE_PROJECTS_DIR)})")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ec.add_logging_args(p)
    sub = p.add_subparsers(dest="command", required=True)

    def add_selectors(sp):
        sp.add_argument("--all", action="store_true", help="all enabled manifest rows")
        sp.add_argument("--project", help="project_name to ingest")
        sp.add_argument("--project-dir", dest="project_dir", help="folder under data/projects/")
        sp.add_argument("--sample-id", dest="sample_id", help="single manifest sample_id")
        sp.add_argument("--run", help="run name")
        sp.add_argument("--no-concordance", dest="no_concordance", action="store_true",
                        help="do not regenerate meta/ concordance before ingest")
        sp.add_argument("--dry-run", action="store_true",
                        help="validate the selected rows and stop before touching the database")

    sp = sub.add_parser("recreate-all", help="DROP+CREATE all tables, then bulk import all rows")
    sp.add_argument("--no-import", action="store_true", help="only recreate schema, skip import")
    sp.add_argument("--no-concordance", dest="no_concordance", action="store_true")
    sp.add_argument("--terminate-sessions", action="store_true",
                    help="kill other client sessions (e.g. Metabase) holding locks before the DROP")
    sp.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")

    add_selectors(sub.add_parser("import", help="import manifest rows (no schema drop)"))
    add_selectors(sub.add_parser("add", help="alias of import for one project/run/sample"))

    dp = sub.add_parser("delete", help="delete a project / run / sample (cascade)")
    dp.add_argument("--project")
    dp.add_argument("--run")
    dp.add_argument("--sample-id", dest="sample_id")
    dp.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")

    cp = sub.add_parser("concordance", help="(re)generate meta/ concordance files (no DB)")
    cp.add_argument("--project", help="project_name")
    cp.add_argument("--project-dir", dest="project_dir", help="folder under data/projects/")
    cp.add_argument("--all", action="store_true", help="all projects in the manifest")

    vp = sub.add_parser("validate", help="check manifest + all referenced files (no DB)")
    vp.add_argument("--project", help="only rows of this project")
    vp.add_argument("--strict", action="store_true", help="treat warnings as errors")

    sub.add_parser("inspect-schema", help="print live tables + FK layout")
    return p


def _project_dirs_for(rows, args):
    if getattr(args, "all", False):
        return {str(r.get("project_dir") or r.get("project")).strip()
                for r in rows if (r.get("project_dir") or r.get("project"))}
    if getattr(args, "project_dir", None):
        return {args.project_dir}
    if getattr(args, "project", None):
        return {str(r.get("project_dir") or r.get("project")).strip()
                for r in rows if str(r.get("project")).strip() == args.project}
    return set()


def cmd_concordance(args):
    rows = read_manifest()
    dirs = _project_dirs_for(rows, args)
    if not dirs:
        log.error("concordance: pass --all, --project or --project-dir")
        return 2
    rc = 0
    for d in sorted(dirs):
        proj = DATA_PROJECTS / d
        if not proj.is_dir():
            log.error(f"{d}: project folder {ec.rel(proj)} not found")
            rc = 1
            continue
        grp = [r for r in rows if str(r.get("project_dir") or r.get("project")).strip() == d]
        faire_files = collect_faire_files(d, grp)
        barcode_files = collect_barcode_files(d, grp)
        log.info(f"[{d}]")
        try:
            generate_concordance(d, faire_files, barcode_files=barcode_files, write=True)
        except Exception:
            log.exception("concordance for %s failed", d)
            rc = 1
    return rc


def validate(rows, project=None):
    """Check every input an import would use, without a database. Returns an
    ec.ValidationReport listing ALL problems found (not just the first)."""
    rep = ec.ValidationReport()
    if project:
        rows = [r for r in rows if str(r.get("project")).strip() == project
                or str(r.get("project_dir")).strip() == project]
        if not rows:
            rep.error(f"no manifest rows for project '{project}'")
    enabled = [r for r in rows if not _truthy(r.get("disable"))]
    seen = {}
    for r in enabled:
        key = (str(r.get("sample_id")).strip(), str(r.get("run") or "").strip())
        if key in seen:
            rep.warn(f"manifest rows {seen[key]} and {r['_row']}: duplicate sample_id/run {key}")
        seen[key] = r["_row"]
    groups = {}
    for r in enabled:
        pdir = r.get("project_dir") or r.get("project")
        if _is_missing(pdir):
            rep.error(f"manifest row {r['_row']}: no project_dir / project")
            continue
        groups.setdefault(str(pdir).strip(), []).append(r)
    for pdir, grp in groups.items():
        proj = DATA_PROJECTS / pdir
        rep.checked += 1
        if not proj.is_dir():
            rep.error(f"project '{pdir}': folder {ec.rel(proj)} not found")
            continue
        for sub in ("faire", "kraken2", "wilder"):
            if not (proj / sub).is_dir():
                rep.warn(f"project '{pdir}': no {sub}/ subfolder")
        faire_files = collect_faire_files(pdir, grp)
        if not faire_files:
            rep.warn(f"project '{pdir}': no FAIRe checklist found")
        for f in faire_files:
            rep.checked += 1
            try:
                df = load_faire_samples(f)
                cols = {_norm_col(c) for c in df.columns}
                if not {"decimallatitude", "decimallongitude"} <= cols:
                    rep.warn(f"{f.name}: no decimalLatitude/decimalLongitude columns — "
                             f"samples cannot be mapped")
                log.info("  FAIRe %s: %d sample(s), key column '%s'",
                         f.name, len(df), df.attrs.get("id_col"))
            except Exception as e:
                rep.error(f"{f.name}: {e}")
        for bf in collect_barcode_files(pdir, grp):
            rep.checked += 1
            if not load_barcode_maps([bf]):
                rep.warn(f"{bf.name}: no sheet with Barcode + sample_ID columns")
        for r in grp:
            k2 = r.get("k2_report")
            if not _is_missing(k2):
                rep.checked += 1
                files = find_kraken2_report_files(proj / "kraken2", k2)
                if not files:
                    rep.error(f"manifest row {r['_row']} ({r.get('sample_id')}): k2_report "
                              f"'{k2}' matched no report under {ec.rel(proj / 'kraken2')}")
                for fp in files[:200]:
                    try:
                        ec.require_file(fp, "Kraken2 report")
                        if not any(True for _ in parse_kraken2_report(fp)):
                            rep.error(f"{ec.rel(fp)}: no parsable Kraken2 lines")
                    except InputError as e:
                        rep.error(str(e))
            asv = r.get("asv_report")
            if not _is_missing(asv):
                for name in re.split(r"[;,]", str(asv)):
                    name = name.strip()
                    if not name:
                        continue
                    rep.checked += 1
                    fp = proj / "wilder" / name
                    if not fp.is_file() and not name.lower().endswith((".xlsx", ".xls")):
                        fp = proj / "wilder" / (name + ".xlsx")
                    try:
                        ec.require_file(fp, "Wilder workbook", ec.SPREADSHEET_EXT)
                        ec.require_sheets(fp, ["metadata", "aggregated"], "Wilder workbook")
                    except InputError as e:
                        rep.error(f"manifest row {r['_row']} ({r.get('sample_id')}): {e}")
    return rep


def _confirm(prompt, assume_yes):
    """Ask before a destructive action. Non-interactive runs must pass --yes."""
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        log.error("%s — refusing to continue non-interactively without --yes", prompt)
        return False
    try:
        ans = input(f"{prompt} Type 'yes' to continue: ").strip().lower()
    except EOFError:
        ans = ""
    if ans != "yes":
        log.warning("aborted by user")
        return False
    return True


def main(argv=None):
    global DATA_PROJECTS, MANIFEST_PATH, CREATE_TABLES_SQL
    args = build_parser().parse_args(argv)
    ec.setup_logging("ingest", args.log_level, args.log_file, args.quiet)
    ec.load_dotenv_if_present()
    log.info("ingest %s — command: %s", __version__, args.command)

    try:
        if args.example:
            DATA_PROJECTS = ec.EXAMPLE_PROJECTS_DIR
        elif args.projects_dir or os.getenv("EDNA_PROJECTS_DIR"):
            DATA_PROJECTS = ec.resolve_path(args.projects_dir or os.getenv("EDNA_PROJECTS_DIR"))
        MANIFEST_PATH = (ec.resolve_path(args.manifest) if args.manifest
                         else DATA_PROJECTS / ec.MANIFEST_NAME)
        if args.schema:
            CREATE_TABLES_SQL = ec.resolve_path(args.schema)
        ec.require_dir(DATA_PROJECTS, "projects folder")
        log.info("projects dir: %s | manifest: %s", ec.rel(DATA_PROJECTS), ec.rel(MANIFEST_PATH))

        if args.command == "concordance":
            return cmd_concordance(args) or ec.log_summary(log)

        if args.command == "validate":
            rep = validate(read_manifest(), project=args.project)
            if args.strict and rep.warnings:
                rep.errors.extend(f"(strict) {w}" for w in rep.warnings)
            return rep.emit(log, "manifest validation")

        # Everything below needs a database. Read + check the manifest first so a
        # typo fails fast instead of after a DROP.
        rows = None
        if args.command in ("import", "add") or (args.command == "recreate-all"
                                                 and not args.no_import):
            rows = read_manifest()

        if args.command in ("import", "add"):
            selected = _select_rows(rows, args)
            if not selected:
                log.error("No matching manifest rows (use --all, --project, --project-dir, "
                          "--sample-id or --run).")
                return 2
            rep = validate(selected)
            rep.emit(log, "pre-import validation")
            if args.dry_run:
                return 0 if rep.ok() else 1
            if not rep.ok():
                log.warning("continuing despite validation errors — affected files will be skipped")

        if args.command == "recreate-all":
            ec.require_file(CREATE_TABLES_SQL, "schema DDL", (".sql",))
            if not _confirm("recreate-all will DROP and re-CREATE every MBON table "
                            "(all data in them is lost).", args.yes):
                return 1
        if args.command == "delete":
            target = args.project or args.run or args.sample_id
            if not target:
                log.error("delete requires --project, --run or --sample-id")
                return 2
            if not _confirm(f"delete will permanently remove '{target}' and everything "
                            f"linked to it.", args.yes):
                return 1

        conn = connect()
        try:
            if args.command == "inspect-schema":
                inspect_schema(conn)
            elif args.command == "recreate-all":
                recreate_schema(conn, force=args.terminate_sessions)
                if not args.no_import:
                    import_rows(conn, [r for r in rows if not _truthy(r.get("disable"))],
                                regen_concordance=not args.no_concordance)
            elif args.command in ("import", "add"):
                import_rows(conn, selected, regen_concordance=not args.no_concordance)
            elif args.command == "delete":
                if args.project:
                    delete_project(conn, args.project)
                elif args.run:
                    delete_run(conn, args.run)
                else:
                    delete_sample(conn, args.sample_id)
        finally:
            conn.close()
        return ec.log_summary(log)
    except InputError as e:
        log.error("%s", e)
        return 2
    except SystemExit as e:
        if isinstance(e.code, str):
            log.error("%s", e.code)
            return 2
        raise
    except KeyboardInterrupt:
        log.error("interrupted — the current project's uncommitted changes were not saved")
        return 130
    except Exception:
        log.exception("unexpected failure — see the log file for the full traceback")
        return 1


if __name__ == "__main__":
    sys.exit(main())
