"""
MBON Dashboard - Flask REST API
Metagenomics sample tracking for the Marine Biodiversity Observation Network

Configuration is entirely environment-driven (see api/.env.example):

    PGHOST / PGPORT / PGDATABASE / PGUSER / PGPASSWORD   default Postgres source
    API_HOST (127.0.0.1)  API_PORT (5050)  FLASK_DEBUG (0)
    CORS_ORIGINS          extra comma-separated allowed origins
    TAXDUMP_DIR           NCBI taxdump folder (default: <repo>/taxdump)
    LOG_LEVEL (INFO)      LOG_FILE (default: <repo>/logs/api.log, '-' disables)
"""

import os
import json
import logging
import logging.handlers
import threading
import time
from datetime import datetime, date
from decimal import Decimal

import psycopg2
import psycopg2.extras
from flask import Flask, jsonify, request, abort, g
from flask_cors import CORS
from dotenv import load_dotenv
from werkzeug.exceptions import HTTPException

_API_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(_API_DIR)

# api/.env first, then the repo-root .env; real env vars always win.
load_dotenv(os.path.join(_API_DIR, ".env"))
load_dotenv(os.path.join(REPO_ROOT, ".env"))

import connections as conn_store  # noqa: E402  (needs env loaded first)
from imports import imports_bp    # noqa: E402


# ── Logging ──────────────────────────────────────────────────────────────────

def _setup_logging():
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    log_file = os.getenv("LOG_FILE", os.path.join(REPO_ROOT, "logs", "api.log"))
    if log_file and log_file.lower() not in ("-", "none", "off"):
        try:
            os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=5_000_000,
                                                      backupCount=3, encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError as e:
            root.warning("cannot write log file %s: %s", log_file, e)
    return logging.getLogger("mbon.api")


log = _setup_logging()

default_origins = ["http://localhost:8080",
                   "http://127.0.0.1:8080",
                   "http://localhost:8280",
                   "http://127.0.0.1:8280",
                   "http://[::1]:8280",
                   "null"]
app = Flask(__name__)
# Reject absurdly large request bodies (imports are JSON, not raw files).
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_CONTENT_MB", "64")) * 1024 * 1024

# Comma-separated env var, for example:
# export CORS_ORIGINS="http://localhost:8280,http://my-server.example.org:8280"
cors_origins_env = os.getenv("CORS_ORIGINS")

if cors_origins_env:
    cors_origins_split = [origin.strip() for origin in cors_origins_env.split(",") if origin.strip()]
    cors_origins = default_origins + cors_origins_split
else:
    cors_origins = default_origins
log.info("CORS origins: %s", ", ".join(cors_origins))
CORS(app, origins=cors_origins)


# ── Request logging + JSON error handling ───────────────────────────────────

@app.before_request
def _start_timer():
    g._t0 = time.perf_counter()


@app.after_request
def _log_request(resp):
    try:
        ms = (time.perf_counter() - g._t0) * 1000
    except AttributeError:
        ms = -1
    lvl = logging.WARNING if resp.status_code >= 400 else logging.DEBUG
    log.log(lvl, "%s %s -> %s (%.0f ms)", request.method, request.full_path.rstrip("?"),
            resp.status_code, ms)
    return resp


@app.errorhandler(HTTPException)
def _http_error(e):
    return jsonify({"error": e.name, "detail": e.description}), e.code


@app.errorhandler(ValueError)
def _bad_value(e):
    # e.g. ?limit=abc  -> int() ValueError: a client error, not a crash
    log.warning("bad request %s: %s", request.path, e)
    return jsonify({"error": "Bad request", "detail": str(e)}), 400


@app.errorhandler(psycopg2.OperationalError)
def _db_down(e):
    log.error("database unavailable for %s: %s", request.path, str(e).strip())
    return jsonify({"error": "Database unavailable",
                    "detail": "Could not connect to the active Postgres source. Check the "
                              "connection in the Sources tab or the PG* settings in .env."}), 503


@app.errorhandler(psycopg2.Error)
def _db_error(e):
    log.error("database error for %s: %s", request.path, str(e).strip())
    return jsonify({"error": "Database error", "detail": str(e).strip().splitlines()[0]}), 500


@app.errorhandler(Exception)
def _unhandled(e):
    log.exception("unhandled error on %s %s", request.method, request.path)
    return jsonify({"error": "Internal server error",
                    "detail": "See the API log for details."}), 500

# Browser-side imports (xlsx/csv) — self-contained JSON store, no DB needed.
app.register_blueprint(imports_bp)


# ── Database helpers ─────────────────────────────────────────────────────────

def get_db():
    """Open a fresh psycopg2 connection for each request.

    Credentials come from the active connection profile when it is a Postgres
    source; otherwise they fall back to the legacy environment variables.  This
    keeps every existing schema endpoint working while letting analysts switch
    the active Postgres connection from the UI.
    """
    cfg = {}
    try:
        cfg = conn_store.active_postgres_config() or {}
    except Exception:
        cfg = {}
    return psycopg2.connect(
        dbname=cfg.get("dbname", os.getenv("PGDATABASE", "postgres")),
        user=cfg.get("user", os.getenv("PGUSER", "postgres")),
        password=cfg.get("password", os.getenv("PGPASSWORD", "")),
        host=cfg.get("host", os.getenv("PGHOST", "127.0.0.1")),
        port=cfg.get("port", os.getenv("PGPORT", "5432")),
        connect_timeout=int(cfg.get("connect_timeout", 6)),
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def query(sql, params=None):
    """Execute a SELECT and return all rows as a list of dicts."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def query_one(sql, params=None):
    """Execute a SELECT and return a single row dict, or None."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


class _Encoder(json.JSONEncoder):
    """Handle types that Flask's default encoder misses."""
    def default(self, obj):
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)

app.json_encoder = _Encoder


def paginate(request):
    """Return (limit, offset) from query-string params."""
    try:
        limit = max(1, min(int(request.args.get("limit", 100)), 1000))
        page  = max(1, int(request.args.get("page", 1)))
    except ValueError:
        limit, page = 100, 1
    return limit, (page - 1) * limit


# ── Taxdump background loader ────────────────────────────────────────────────

_TAX_NAMES = {}   # taxid(int) -> scientific name str
_TAX_NODES = {}   # taxid(int) -> (parent_taxid int, rank str)
_taxdump_status = {'loaded': False, 'loading': False, 'names': 0, 'nodes': 0, 'error': None}
RANK_ORDER = ['D', 'K', 'P', 'C', 'O', 'F', 'G', 'S', 'U']


def rank_sort_key(rank):
    try:
        return (0, RANK_ORDER.index(rank))
    except ValueError:
        return (1, str(rank or ''))

def _load_taxdump():
    global _TAX_NAMES, _TAX_NODES
    _taxdump_status['loading'] = True
    base = os.getenv('TAXDUMP_DIR') or os.path.join(REPO_ROOT, 'taxdump')
    if not os.path.isabs(base):
        base = os.path.join(REPO_ROOT, base)
    names_path = os.path.join(base, 'names.dmp')
    nodes_path = os.path.join(base, 'nodes.dmp')
    if not (os.path.exists(names_path) and os.path.exists(nodes_path)):
        log.warning("NCBI taxdump not found in %s (names.dmp / nodes.dmp) — taxonomy "
                    "lineage endpoints will return empty results. Set TAXDUMP_DIR or "
                    "download https://ftp.ncbi.nlm.nih.gov/pub/taxonomy/taxdump.tar.gz", base)
    try:
        names = {}
        if os.path.exists(names_path):
            with open(names_path, encoding='utf-8', errors='replace') as f:
                for line in f:
                    parts = line.rstrip('\n').split('\t|\t')
                    if len(parts) >= 4 and parts[3].rstrip('\t|\n ') == 'scientific name':
                        try:
                            names[int(parts[0].strip())] = parts[1].strip()
                        except ValueError:
                            pass
        _TAX_NAMES = names
        _taxdump_status['names'] = len(names)

        nodes = {}
        if os.path.exists(nodes_path):
            with open(nodes_path, encoding='utf-8', errors='replace') as f:
                for line in f:
                    parts = line.rstrip('\n').split('\t|\t')
                    if len(parts) >= 3:
                        try:
                            taxid  = int(parts[0].strip())
                            parent = int(parts[1].strip())
                            rank   = parts[2].strip().rstrip('\t| ')
                            nodes[taxid] = (parent, rank)
                        except ValueError:
                            pass
        _TAX_NODES = nodes
        _taxdump_status['nodes'] = len(nodes)
        _taxdump_status['loaded'] = True
        log.info("taxdump loaded: %d names, %d nodes", len(_TAX_NAMES), len(_TAX_NODES))
    except Exception as e:
        _taxdump_status['error'] = str(e)
        log.exception("taxdump load failed")
    finally:
        _taxdump_status['loading'] = False

threading.Thread(target=_load_taxdump, daemon=True, name='taxdump-loader').start()


# ── Kraken2 "organism group" from taxonomy lineage ───────────────────────────
# Kraken2 rows carry an NCBI taxonomy_id but no organism_group. To mirror the
# static build (build_mbon_html.parse_kraken2_report), a taxon's *group* is the
# name of its nearest ancestor at Class, else Phylum, else Kingdom, else Domain
# (superkingdom). We resolve this by walking the taxdump lineage (_TAX_NODES).
_K2_GROUP_CACHE = {}   # taxid(int) -> group name (or None)
_K2_RANK_TO_LETTER = {'class': 'C', 'phylum': 'P', 'kingdom': 'K',
                      'superkingdom': 'D', 'domain': 'D'}


def k2_group(taxid):
    """Ancestor group name (Class→Phylum→Kingdom→Domain) for a Kraken2 taxid,
    using the taxdump lineage. Returns None if unresolved or taxdump not ready."""
    if not taxid or not _TAX_NODES:
        return None
    try:
        taxid = int(taxid)
    except (TypeError, ValueError):
        return None
    if taxid in _K2_GROUP_CACHE:
        return _K2_GROUP_CACHE[taxid]
    anc = {}
    cur, seen, steps = taxid, set(), 0
    while cur and cur not in seen and steps < 80:
        seen.add(cur); steps += 1
        node = _TAX_NODES.get(cur)
        if not node:
            break
        parent, rank = node
        letter = _K2_RANK_TO_LETTER.get((rank or '').lower())
        if letter and letter not in anc:
            anc[letter] = _TAX_NAMES.get(cur)
        if not parent or parent == cur:
            break
        cur = parent
    group = anc.get('C') or anc.get('P') or anc.get('K') or anc.get('D')
    # Only cache once the taxdump is fully loaded, so early-startup misses aren't
    # frozen in as None for the rest of the process lifetime.
    if _taxdump_status.get('loaded'):
        _K2_GROUP_CACHE[taxid] = group
    return group


# ── Health check ─────────────────────────────────────────────────────────────

@app.route("/api/health")
def health():
    try:
        query("SELECT 1")
        return jsonify({"status": "ok", "db": "connected"})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


# ── Collections ──────────────────────────────────────────────────────────────

@app.route("/api/collections")
def list_collections():
    """
    List collections (cruises / outings / batches).
    Optional filters: project_id
    """
    limit, offset = paginate(request)
    where, params = [], []

    if request.args.get("project_id"):
        where.append("c.project_id = %s")
        params.append(int(request.args["project_id"]))

    clause = "WHERE " + " AND ".join(where) if where else ""
    sql = f"""
        SELECT
            c.collection_id,
            c.collection_name,
            c.collection_type,
            c.collected_at,
            c.description,
            c.comments,
            c.created_at,
            p.project_name,
            COUNT(DISTINCT s.sample_id) AS sample_count,
            COUNT(DISTINCT r.run_id)    AS run_count
        FROM collection c
        LEFT JOIN project p ON p.project_id = c.project_id
        LEFT JOIN sample  s ON s.collection_id = c.collection_id
        LEFT JOIN run     r ON r.sample_id = s.sample_id
        {clause}
        GROUP BY c.collection_id, p.project_name
        ORDER BY c.created_at DESC
        LIMIT %s OFFSET %s
    """
    params += [limit, offset]
    rows = query(sql, params)
    return jsonify(rows)


@app.route("/api/collections/<int:collection_id>")
def get_collection(collection_id):
    row = query_one("""
        SELECT
            c.*,
            p.project_name,
            COUNT(DISTINCT s.sample_id) AS sample_count,
            COUNT(DISTINCT r.run_id)    AS run_count
        FROM collection c
        LEFT JOIN project p ON p.project_id    = c.project_id
        LEFT JOIN sample  s ON s.collection_id = c.collection_id
        LEFT JOIN run     r ON r.sample_id     = s.sample_id
        WHERE c.collection_id = %s
        GROUP BY c.collection_id, p.project_name
    """, (collection_id,))
    if not row:
        abort(404, description=f"Collection {collection_id} not found")
    return jsonify(row)


# ── Runs ─────────────────────────────────────────────────────────────────────

@app.route("/api/runs")
def list_runs():
    """
    List sequencing runs.
    Optional filters: project_id, sample_id, collection_id, instrument_id
    """
    limit, offset = paginate(request)
    where, params = [], []

    if request.args.get("project_id"):
        where.append("c.project_id = %s")
        params.append(int(request.args["project_id"]))

    if request.args.get("sample_id"):
        where.append("r.sample_id = %s")
        params.append(int(request.args["sample_id"]))

    if request.args.get("collection_id"):
        where.append("s.collection_id = %s")
        params.append(int(request.args["collection_id"]))

    if request.args.get("instrument_id"):
        where.append("r.instrument_id = %s")
        params.append(int(request.args["instrument_id"]))

    clause = "WHERE " + " AND ".join(where) if where else ""
    sql = f"""
        SELECT
            r.run_id,
            r.directory,
            r.created_at,
            r.updated_at,
            r.comments,
            r.sample_id,
            s.sample_name,
            c.collection_id,
            c.collection_name,
            p.project_name,
            p.assay_type,
            i.platform AS instrument_platform,
            i.instrument_name,
            u.username  AS entered_by
        FROM run r
        LEFT JOIN sample     s ON s.sample_id     = r.sample_id
        LEFT JOIN collection c ON c.collection_id = s.collection_id
        LEFT JOIN project    p ON p.project_id    = c.project_id
        LEFT JOIN instrument i ON i.instrument_id = r.instrument_id
        LEFT JOIN users      u ON u.id            = r.entered_by
        {clause}
        ORDER BY r.created_at DESC
        LIMIT %s OFFSET %s
    """
    params += [limit, offset]
    rows = query(sql, params)
    return jsonify(rows)


@app.route("/api/runs/<int:run_id>")
def get_run(run_id):
    row = query_one("""
        SELECT
            r.*,
            s.sample_name,
            c.collection_id, c.collection_name,
            p.project_name, p.assay_type,
            i.platform AS instrument_platform, i.instrument_name,
            u.username AS entered_by_username
        FROM run r
        LEFT JOIN sample     s ON s.sample_id     = r.sample_id
        LEFT JOIN collection c ON c.collection_id = s.collection_id
        LEFT JOIN project    p ON p.project_id    = c.project_id
        LEFT JOIN instrument i ON i.instrument_id = r.instrument_id
        LEFT JOIN users      u ON u.id            = r.entered_by
        WHERE r.run_id = %s
    """, (run_id,))
    if not row:
        abort(404, description=f"Run {run_id} not found")
    return jsonify(row)


# ── Samples ───────────────────────────────────────────────────────────────────

@app.route("/api/samples")
def list_samples():
    """
    List samples.
    Optional filters: collection_id, project_id, sample_type, location_id, search (name)
    """
    limit, offset = paginate(request)
    where, params = [], []

    if request.args.get("collection_id"):
        where.append("s.collection_id = %s")
        params.append(int(request.args["collection_id"]))

    if request.args.get("project_id"):
        where.append("c.project_id = %s")
        params.append(int(request.args["project_id"]))

    if request.args.get("sample_type"):
        where.append("s.sample_type = %s")
        params.append(request.args["sample_type"])

    if request.args.get("location_id"):
        where.append("s.location_id = %s")
        params.append(int(request.args["location_id"]))

    if request.args.get("search"):
        where.append("s.sample_name ILIKE %s")
        params.append(f"%{request.args['search']}%")

    clause = "WHERE " + " AND ".join(where) if where else ""
    sql = f"""
        SELECT
            s.sample_id,
            s.sample_name,
            s.sample_type,
            s.collected_at,
            s.completed_at,
            s.directory,
            s.filepath,
            s.collection_id,
            c.collection_name,
            c.collection_type,
            p.project_name,
            l.location_alias,
            l.latitude,
            l.longitude,
            u.username AS submitted_by,
            COUNT(r.run_id) AS run_count
        FROM sample s
        LEFT JOIN collection c ON c.collection_id = s.collection_id
        LEFT JOIN project    p ON p.project_id    = c.project_id
        LEFT JOIN location   l ON l.location_id   = s.location_id
        LEFT JOIN users      u ON u.id            = s.submitted_by
        LEFT JOIN run        r ON r.sample_id     = s.sample_id
        {clause}
        GROUP BY s.sample_id, c.collection_id, c.collection_name, c.collection_type,
                 p.project_name, l.location_alias, l.latitude, l.longitude, u.username
        ORDER BY s.sample_id DESC
        LIMIT %s OFFSET %s
    """
    params += [limit, offset]
    rows = query(sql, params)
    return jsonify(rows)


@app.route("/api/samples/<int:sample_id>")
def get_sample(sample_id):
    row = query_one("""
        SELECT
            s.*,
            c.collection_name, c.collection_type,
            p.project_name, p.assay_type,
            l.location_alias, l.latitude, l.longitude,
            u.username AS submitted_by_username
        FROM sample s
        LEFT JOIN collection c ON c.collection_id = s.collection_id
        LEFT JOIN project    p ON p.project_id    = c.project_id
        LEFT JOIN location   l ON l.location_id   = s.location_id
        LEFT JOIN users      u ON u.id            = s.submitted_by
        WHERE s.sample_id = %s
    """, (sample_id,))
    if not row:
        abort(404, description=f"Sample {sample_id} not found")
    return jsonify(row)


@app.route("/api/samples/<int:sample_id>/summary")
def get_sample_summary(sample_id):
    row = query_one("""
        SELECT * FROM sample_summary WHERE sample_id = %s
    """, (sample_id,))
    return jsonify(row or {})


# ── Projects ──────────────────────────────────────────────────────────────────

@app.route("/api/projects")
def list_projects():
    rows = query("""
        SELECT
            p.*,
            u.username AS recorded_by_username,
            inst.institution_name,
            COUNT(DISTINCT c.collection_id) AS collection_count,
            COUNT(DISTINCT s.sample_id)     AS sample_count,
            COUNT(DISTINCT r.run_id)        AS run_count
        FROM project p
        LEFT JOIN users       u    ON u.id                = p.recorded_by
        LEFT JOIN institution inst ON inst.institution_id = p.institution_id
        LEFT JOIN collection  c    ON c.project_id        = p.project_id
        LEFT JOIN sample      s    ON s.collection_id     = c.collection_id
        LEFT JOIN run         r    ON r.sample_id         = s.sample_id
        GROUP BY p.project_id, u.username, inst.institution_name
        ORDER BY p.project_id
    """)
    return jsonify(rows)


# ── Kraken2 ───────────────────────────────────────────────────────────────────

@app.route("/api/kraken2/reports")
def list_kraken2_reports():
    limit, offset = paginate(request)
    where, params = [], []

    if request.args.get("search"):
        where.append("(kr.database ILIKE %s OR kr.report_location ILIKE %s)")
        search_term = f"%{request.args['search']}%"
        params.append(search_term)
        params.append(search_term)

    clause = "WHERE " + " AND ".join(where) if where else ""
    rows = query(f"""
        SELECT
            kr.id,
            kr.database,
            kr.report_date,
            kr.krona_location,
            kr.report_location,
            b.sop_bioinformatics,
            b.version_pipeline,
            s.sample_id,
            ms.mbon_samp_alias,
            ms.station,
            ms.cruise_number
        FROM kraken2_report kr
        LEFT JOIN bioinformatics b   ON b.id              = kr.bioinfo_id
        LEFT JOIN data_management dm ON dm.id             = b.data_management_id
        LEFT JOIN run r              ON r.run_id          = dm.run_id
        LEFT JOIN sample s           ON s.sample_id       = r.sample_id
        LEFT JOIN mbon_samples ms    ON ms.sample_id      = s.sample_id
        {clause}
        ORDER BY kr.report_date DESC
        LIMIT %s OFFSET %s
    """, params + [limit, offset])
    return jsonify(rows)


@app.route("/api/kraken2/reports/<int:report_id>")
def get_kraken2_report(report_id):
    report = query_one("SELECT * FROM kraken2_report WHERE id = %s", (report_id,))
    if not report:
        abort(404)
    lcas = query("""
        SELECT * FROM kraken2_lcas
        WHERE kraken2_report_id = %s
        ORDER BY abundance DESC
        LIMIT 500
    """, (report_id,))
    return jsonify({"report": report, "lcas": lcas})


@app.route("/api/kraken2/lcas")
def list_kraken2_lcas():
    """
    Get LCA rows for a specific report.
    Required: report_id
    Optional filters: rank, min_abundance
    """
    report_id = request.args.get("report_id")
    if not report_id:
        abort(400, description="report_id is required")

    where = ["kraken2_report_id = %s"]
    params = [int(report_id)]

    if request.args.get("rank"):
        where.append("rank = %s")
        params.append(request.args["rank"])

    if request.args.get("min_abundance"):
        where.append("abundance >= %s")
        params.append(float(request.args["min_abundance"]))

    rows = query(f"""
        SELECT * FROM kraken2_lcas
        WHERE {' AND '.join(where)}
        ORDER BY abundance DESC
        LIMIT 1000
    """, params)
    return jsonify(rows)


@app.route("/api/kraken2/ranks")
def kraken2_ranks():
    """Return only Kraken2 ranks that are present, optionally for one report."""
    where, params = ["rank IS NOT NULL", "rank <> ''"], []
    if request.args.get("report_id"):
        where.append("kraken2_report_id = %s")
        params.append(int(request.args["report_id"]))
    rows = query(f"""
        SELECT rank, COUNT(*) AS taxa_count, SUM(count) AS total_count
        FROM kraken2_lcas
        WHERE {' AND '.join(where)}
        GROUP BY rank
    """, params if params else None)
    rows.sort(key=lambda r: rank_sort_key(r.get("rank")))
    return jsonify(rows)


@app.route("/api/kraken2/top-taxa")
def kraken2_top_taxa():
    """
    Aggregate top taxa across all kraken2 reports (or a single report).
    Optional: report_id, rank, limit (default 20)
    """
    top_n = min(int(request.args.get("limit", 20)), 100)
    where, params = [], []

    if request.args.get("report_id"):
        where.append("kraken2_report_id = %s")
        params.append(int(request.args["report_id"]))

    if request.args.get("rank"):
        where.append("rank = %s")
        params.append(request.args["rank"])

    clause = "WHERE " + " AND ".join(where) if where else ""
    rows = query(f"""
        SELECT name, rank, AVG(abundance) AS avg_abundance, SUM(count) AS total_count
        FROM kraken2_lcas
        {clause}
        GROUP BY name, rank
        ORDER BY avg_abundance DESC
        LIMIT %s
    """, params + [top_n])
    return jsonify(rows)


# ── MBON samples ──────────────────────────────────────────────────────────────

@app.route("/api/mbon/samples")
def list_mbon_samples():
    """
    List MBON-specific sample metadata.
    Optional filters: cruise_number, station, search (mbon_samp_alias)
    """
    limit, offset = paginate(request)
    where, params = [], []

    if request.args.get("cruise_number"):
        where.append("ms.cruise_number = %s")
        params.append(request.args["cruise_number"])

    if request.args.get("station"):
        where.append("ms.station = %s")
        params.append(request.args["station"])

    if request.args.get("search"):
        where.append("ms.mbon_samp_alias ILIKE %s")
        params.append(f"%{request.args['search']}%")

    clause = "WHERE " + " AND ".join(where) if where else ""
    sql = f"""
        SELECT
            ms.mbon_data_id,
            ms.mbon_samp_alias,
            ms.cruise_number,
            ms.station,
            ms.station_id,
            ms.geo_loc_name,
            ms.latitude,
            ms.longitude,
            ms.sample_depth,
            ms.sea_surface_temperature,
            ms.salinity,
            ms.temp,
            ms.water_volume_filtered,
            ms.filter_pore_size,
            ms.env_broad_scale,
            ms.samp_category,
            ms.neg_cont_type,
            ms.sampling_method,
            ms.extraction_method,
            s.sample_name,
            s.sample_type,
            s.sample_id,
            l.location_alias
        FROM mbon_samples ms
        LEFT JOIN sample   s ON s.sample_id    = ms.sample_id
        LEFT JOIN location l ON l.location_id  = ms.location_id
        {clause}
        ORDER BY ms.mbon_data_id DESC
        LIMIT %s OFFSET %s
    """
    params += [limit, offset]
    rows = query(sql, params)
    return jsonify(rows)


@app.route("/api/mbon/samples/<int:mbon_data_id>")
def get_mbon_sample(mbon_data_id):
    row = query_one("""
        SELECT ms.*, s.sample_name, s.sample_type, s.filepath,
               l.location_alias, l.latitude AS loc_latitude, l.longitude AS loc_longitude
        FROM mbon_samples ms
        LEFT JOIN sample   s ON s.sample_id   = ms.sample_id
        LEFT JOIN location l ON l.location_id = ms.location_id
        WHERE ms.mbon_data_id = %s
    """, (mbon_data_id,))
    if not row:
        abort(404)
    return jsonify(row)


@app.route("/api/mbon/cruises")
def list_mbon_cruises():
    rows = query("""
        SELECT DISTINCT cruise_number, cruise_id
        FROM mbon_samples
        WHERE cruise_number IS NOT NULL
        ORDER BY cruise_number
    """)
    return jsonify(rows)


@app.route("/api/mbon/stations")
def list_mbon_stations():
    rows = query("""
        SELECT DISTINCT station, station_id, geo_loc_name,
               AVG(latitude)  AS latitude,
               AVG(longitude) AS longitude
        FROM mbon_samples
        WHERE station IS NOT NULL
        GROUP BY station, station_id, geo_loc_name
        ORDER BY station
    """)
    return jsonify(rows)


# ── Wilderlab results ─────────────────────────────────────────────────────────

@app.route("/api/wilder/results")
def list_wilder_results():
    """
    Get Wilderlab results.
    Optional filters: mbon_data_id, wilder_job_id, organism_group, min_reads
    """
    limit, offset = paginate(request)
    where, params = [], []

    if request.args.get("mbon_data_id"):
        where.append("wr.mbon_data_id = %s")
        params.append(int(request.args["mbon_data_id"]))

    if request.args.get("wilder_job_id"):
        where.append("wr.wilder_job_id = %s")
        params.append(request.args["wilder_job_id"])

    if request.args.get("organism_group"):
        where.append("wr.organism_group = %s")
        params.append(request.args["organism_group"])

    if request.args.get("min_reads"):
        where.append("wr.read_count >= %s")
        params.append(int(request.args["min_reads"]))

    if request.args.get("names"):
        name_list = [n.strip() for n in request.args["names"].split(",") if n.strip()]
        if name_list:
            where.append("wr.scientific_name = ANY(%s)")
            params.append(name_list)

    if request.args.get("search"):
        where.append(
            "(wr.scientific_name ILIKE %s OR wr.common_name ILIKE %s OR ms.mbon_samp_alias ILIKE %s)"
        )
        term = f"%{request.args['search']}%"
        params += [term, term, term]

    clause = "WHERE " + " AND ".join(where) if where else ""
    sql = f"""
        SELECT
            wr.wilder_result_id,
            wr.mbon_data_id,
            wr.wilder_job_id,
            wr.wilder_uid,
            wr.scientific_name,
            wr.taxonomic_rank,
            wr.tax_id,
            wr.common_name,
            wr.organism_group,
            wr.read_count,
            wr.created_at,
            ms.mbon_samp_alias,
            ms.station
        FROM wilder_results wr
        LEFT JOIN mbon_samples ms ON ms.mbon_data_id = wr.mbon_data_id
        {clause}
        ORDER BY wr.read_count DESC
        LIMIT %s OFFSET %s
    """
    params += [limit, offset]
    rows = query(sql, params)
    return jsonify(rows)


@app.route("/api/wilder/jobs")
def list_wilder_jobs():
    rows = query("""
        SELECT
            wilder_job_id,
            COUNT(*)                  AS result_count,
            COUNT(DISTINCT mbon_data_id) AS sample_count,
            SUM(read_count)           AS total_reads,
            MIN(created_at)           AS first_created
        FROM wilder_results
        GROUP BY wilder_job_id
        ORDER BY first_created DESC
    """)
    return jsonify(rows)


@app.route("/api/wilder/organism-groups")
def list_wilder_organism_groups():
    where, params = ["organism_group IS NOT NULL"], []
    if request.args.get("job"):
        where.append("wilder_job_id = %s")
        params.append(request.args["job"])
    rows = query(f"""
        SELECT
            organism_group,
            COUNT(DISTINCT scientific_name) AS species_count,
            SUM(read_count)                 AS total_reads
        FROM wilder_results
        WHERE {' AND '.join(where)}
        GROUP BY organism_group
        ORDER BY total_reads DESC
    """, params if params else None)
    return jsonify(rows)


@app.route("/api/wilder/samples")
def list_wilder_samples():
    """Distinct MBON samples that have Wilder results, with alias."""
    rows = query("""
        SELECT DISTINCT wr.mbon_data_id, ms.mbon_samp_alias, wr.wilder_job_id
        FROM wilder_results wr
        LEFT JOIN mbon_samples ms ON ms.mbon_data_id = wr.mbon_data_id
        ORDER BY ms.mbon_samp_alias NULLS LAST, wr.mbon_data_id
    """)
    log.debug("wilder samples: %d rows", len(rows))
    return jsonify(rows)


@app.route("/api/wilder/top-taxa")
def wilder_top_taxa():
    """Top taxa by read count. Optional: organism_group, wilder_job_id, mbon_data_id"""
    top_n = min(int(request.args.get("limit", 20)), 500)
    where, params = [], []

    if request.args.get("organism_group"):
        where.append("organism_group = %s")
        params.append(request.args["organism_group"])

    if request.args.get("wilder_job_id"):
        where.append("wilder_job_id = %s")
        params.append(request.args["wilder_job_id"])

    if request.args.get("mbon_data_id"):
        where.append("mbon_data_id = %s")
        params.append(int(request.args["mbon_data_id"]))

    clause = "WHERE " + " AND ".join(where) if where else ""
    rows = query(f"""
        SELECT
            scientific_name,
            common_name,
            organism_group,
            taxonomic_rank,
            SUM(read_count)                 AS total_reads,
            COUNT(DISTINCT mbon_data_id)    AS sample_count
        FROM wilder_results
        {clause}
        GROUP BY scientific_name, common_name, organism_group, taxonomic_rank
        ORDER BY total_reads DESC
        LIMIT %s
    """, params + [top_n])
    return jsonify(rows)


# ── Locations ─────────────────────────────────────────────────────────────────

@app.route("/api/locations")
def list_locations():
    rows = query("""
        SELECT l.*, p.project_name
        FROM location l
        LEFT JOIN project p ON p.project_id = l.project_id
        ORDER BY l.location_alias
    """)
    return jsonify(rows)


# ── Taxonomy ─────────────────────────────────────────────────────────────────

@app.route("/api/taxonomy")
def list_taxonomy():
    limit, offset = paginate(request)
    search = request.args.get("search", "")
    params = []
    where = ""
    if search:
        where = "WHERE organism_name ILIKE %s"
        params.append(f"%{search}%")
    rows = query(f"""
        SELECT * FROM taxonomy
        {where}
        ORDER BY organism_name
        LIMIT %s OFFSET %s
    """, params + [limit, offset])
    return jsonify(rows)


# ── Stats / Dashboard summary ────────────────────────────────────────────────

@app.route("/api/stats")
def dashboard_stats():
    """High-level counts for the dashboard landing page."""
    stats = query_one("""
        SELECT
            (SELECT COUNT(*) FROM project)        AS project_count,
            (SELECT COUNT(*) FROM collection)     AS collection_count,
            (SELECT COUNT(*) FROM sample)         AS sample_count,
            (SELECT COUNT(*) FROM run)            AS run_count,
            (SELECT COUNT(*) FROM mbon_samples)   AS mbon_sample_count,
            (SELECT COUNT(*) FROM kraken2_report) AS kraken2_report_count,
            (SELECT COUNT(*) FROM kraken2_lcas)   AS kraken2_lca_count,
            (SELECT COUNT(*) FROM wilder_results) AS wilder_result_count,
            (SELECT COUNT(DISTINCT scientific_name) FROM wilder_results) AS wilder_species_count,
            (SELECT COUNT(*) FROM taxonomy)       AS taxonomy_count
    """)
    return jsonify(stats)


# ── Taxonomy search and lineage routes ───────────────────────────────────────

@app.route("/api/taxonomy/status")
def taxonomy_status():
    return jsonify(_taxdump_status)


@app.route("/api/taxonomy/search")
def taxonomy_search():
    q = request.args.get("q", "").strip().lower()
    if not q or len(q) < 2:
        return jsonify([])
    limit = min(int(request.args.get("limit", 50)), 200)
    results = []
    for taxid, name in _TAX_NAMES.items():
        if q in name.lower():
            node = _TAX_NODES.get(taxid, (1, 'no rank'))
            results.append({"taxid": taxid, "name": name, "rank": node[1], "parent": node[0]})
            if len(results) >= limit:
                break
    return jsonify(sorted(results, key=lambda x: len(x["name"])))


@app.route("/api/taxonomy/lineage/<int:taxid>")
def get_lineage(taxid):
    lineage = []
    current = taxid
    visited = set()
    while current and current not in visited:
        visited.add(current)
        name = _TAX_NAMES.get(current, f"taxid:{current}")
        node = _TAX_NODES.get(current)
        if not node:
            lineage.append({"taxid": current, "name": name, "rank": "no rank", "parent": None})
            break
        parent, rank = node
        lineage.append({"taxid": current, "name": name, "rank": rank, "parent": parent})
        if current == parent or current == 1:
            break
        current = parent
    return jsonify({"taxid": taxid, "lineage": list(reversed(lineage))})


# ── Kraken2 sunburst and comparison routes ───────────────────────────────────

@app.route("/api/kraken2/sunburst/<int:report_id>")
def kraken2_sunburst(report_id):
    """
    Return LCA data for a report formatted for D3 sunburst (matching Sunburst.vue inputdata format).
    Fields per node: taxid, parenttaxid, target, rank_code, value (%), num_fragments_clade,
    num_fragments_assigned, depth, full.
    A virtual root node (taxid=0) is created so d3.stratify always has a single root.
    """
    rows = query("""
        SELECT taxonomy_id, parent_taxonomy_id, rank, abundance, count, name
        FROM kraken2_lcas
        WHERE kraken2_report_id = %s
        ORDER BY count DESC
    """, (report_id,))
    if not rows:
        return jsonify([])

    total = sum(r['count'] for r in rows) or 1
    by_id = {r['taxonomy_id']: r for r in rows}

    # Build children map using the data's own parent links
    children_map = {}   # parent_id -> [child_id, ...]
    for r in rows:
        children_map.setdefault(r['parent_taxonomy_id'], []).append(r['taxonomy_id'])

    # Nodes whose parent is not in the dataset (or self-referential) -> orphans, parented to virtual root 0
    VROOT = 0
    orphans = [r['taxonomy_id'] for r in rows
               if r['parent_taxonomy_id'] not in by_id or r['parent_taxonomy_id'] == r['taxonomy_id']]
    for oid in orphans:
        orig_parent = by_id[oid]['parent_taxonomy_id']
        if orig_parent in children_map and oid in children_map[orig_parent]:
            children_map[orig_parent].remove(oid)
        children_map.setdefault(VROOT, []).append(oid)

    # BFS from VROOT to assign depths
    result = [{
        'taxid': VROOT, 'parenttaxid': None,
        'target': 'root', 'rank_code': 'base',
        'value': 100.0, 'num_fragments_clade': total,
        'num_fragments_assigned': 0, 'depth': 0, 'full': 'root',
    }]
    visited = {VROOT}
    queue = [(VROOT, 0)]
    while queue:
        curr, depth = queue.pop(0)
        for cid in children_map.get(curr, []):
            if cid in visited or cid not in by_id:
                continue
            visited.add(cid)
            r = by_id[cid]
            # Enrich name from taxdump if available
            tax_name = _TAX_NAMES.get(cid, r['name']) if _taxdump_status['loaded'] else r['name']
            # Map rank to single letter; keep as-is if already short
            rk = (r['rank'] or 'U').strip()
            result.append({
                'taxid': cid,
                'parenttaxid': curr,
                'target': r['name'],
                'rank_code': rk,
                'value': float(r['abundance']),
                'num_fragments_clade': int(r['count']),
                'num_fragments_assigned': int(r['count']),
                'depth': depth + 1,
                'full': tax_name,
            })
            queue.append((cid, depth + 1))

    return jsonify(result)


@app.route("/api/kraken2/taxon-compare")
def kraken2_taxon_compare():
    """
    Compare a single taxon across all reports.
    ?name=Homo+sapiens  (or &taxid=9606)
    Returns list of {report_id, database, report_date, abundance, count}
    """
    name = request.args.get("name", "").strip()
    taxid = request.args.get("taxid")
    if not name and not taxid:
        abort(400, description="name or taxid required")

    where, params = [], []
    if name:
        where.append("l.name ILIKE %s")
        params.append(name)
    if taxid:
        where.append("l.taxonomy_id = %s")
        params.append(int(taxid))

    rows = query(f"""
        SELECT
            r.id AS report_id,
            r.database,
            r.report_date,
            r.report_location,
            l.name,
            l.rank,
            l.abundance,
            l.count,
            ms.mbon_samp_alias,
            ms.station,
            ms.cruise_number
        FROM kraken2_lcas l
        JOIN kraken2_report r ON r.id = l.kraken2_report_id
        LEFT JOIN bioinformatics b   ON b.id        = r.bioinfo_id
        LEFT JOIN data_management dm ON dm.id       = b.data_management_id
        LEFT JOIN run rn             ON rn.run_id   = dm.run_id
        LEFT JOIN sample s           ON s.sample_id = rn.sample_id
        LEFT JOIN mbon_samples ms    ON ms.sample_id= s.sample_id
        WHERE {' AND '.join(where)}
        ORDER BY l.abundance DESC
    """, params)
    return jsonify(rows)


@app.route("/api/kraken2/compare")
def compare_k2():
    """
    Compare top taxa across multiple reports.
    ?reports=1,2,3&rank=S&top=25
    Returns: { report_ids, rank, reports_meta, matrix:[{name, rank, r_<id>_abundance, r_<id>_count}] }
    """
    ids_str = request.args.get("reports", "")
    if not ids_str:
        abort(400, description="reports param required (comma-separated IDs)")
    try:
        report_ids = [int(x) for x in ids_str.split(",") if x.strip()]
    except ValueError:
        abort(400, description="invalid report IDs")
    rank  = request.args.get("rank", "S")
    top_n = min(int(request.args.get("top", 25)), 100)

    by_report = {}
    meta = {}
    for rid in report_ids:
        rows = query("""
            SELECT kl.name, kl.rank, kl.abundance, kl.count,
                   kr.database, kr.report_date,
                   ms.mbon_samp_alias, ms.station, ms.cruise_number
            FROM kraken2_lcas kl
            JOIN kraken2_report kr ON kr.id = kl.kraken2_report_id
            LEFT JOIN bioinformatics b   ON b.id        = kr.bioinfo_id
            LEFT JOIN data_management dm ON dm.id       = b.data_management_id
            LEFT JOIN run rn             ON rn.run_id   = dm.run_id
            LEFT JOIN sample s           ON s.sample_id = rn.sample_id
            LEFT JOIN mbon_samples ms    ON ms.sample_id= s.sample_id
            WHERE kl.kraken2_report_id = %s AND kl.rank = %s
            ORDER BY kl.abundance DESC LIMIT %s
        """, (rid, rank, top_n))
        by_report[rid] = {r['name']: r for r in rows}
        if rows:
            meta[rid] = {
                'database': rows[0]['database'],
                'report_date': rows[0]['report_date'],
                'sample_alias': rows[0].get('mbon_samp_alias'),
                'station': rows[0].get('station'),
                'cruise_number': rows[0].get('cruise_number'),
            }

    all_names = set()
    for d in by_report.values():
        all_names.update(d.keys())

    # Sort by max abundance across any report
    sorted_names = sorted(all_names,
        key=lambda n: max((by_report[rid].get(n, {}).get('abundance', 0) or 0) for rid in report_ids),
        reverse=True)[:top_n]

    matrix = []
    for name in sorted_names:
        row = {'name': name}
        for rid in report_ids:
            entry = by_report[rid].get(name, {})
            row[f'r{rid}_abundance'] = float(entry.get('abundance', 0) or 0)
            row[f'r{rid}_count']     = int(entry.get('count', 0) or 0)
        matrix.append(row)

    return jsonify({'report_ids': report_ids, 'rank': rank, 'meta': {str(k): v for k, v in meta.items()}, 'matrix': matrix})


@app.route("/api/wilder/compare")
def compare_wilder():
    """
    Compare taxa across multiple mbon_data_ids.
    ?samples=1,2,3&group=Fish&top=25
    Returns: { sample_ids, aliases, matrix:[{name, group, s_<id>}] }
    """
    ids_str = request.args.get("samples", "")
    if not ids_str:
        abort(400, description="samples param required")
    try:
        sample_ids = [int(x) for x in ids_str.split(",") if x.strip()]
    except ValueError:
        abort(400, description="invalid sample IDs")
    top_n = min(int(request.args.get("top", 25)), 100)
    group = request.args.get("group", "")

    where = ["wr.mbon_data_id = ANY(%s)"]
    params: list = [sample_ids]
    if group:
        where.append("wr.organism_group = %s")
        params.append(group)

    rows = query(f"""
        SELECT wr.mbon_data_id, ms.mbon_samp_alias, wr.scientific_name,
               wr.organism_group, wr.read_count
        FROM wilder_results wr
        JOIN mbon_samples ms ON ms.mbon_data_id = wr.mbon_data_id
        WHERE {' AND '.join(where)}
    """, params)

    by_sample = {}
    aliases = {}
    all_taxa = {}
    for r in rows:
        sid = r['mbon_data_id']
        aliases[sid] = r['mbon_samp_alias']
        by_sample.setdefault(sid, {})[r['scientific_name']] = r['read_count']
        all_taxa[r['scientific_name']] = r['organism_group']

    taxa_totals = {t: sum(by_sample.get(s, {}).get(t, 0) for s in sample_ids) for t in all_taxa}
    top_taxa = sorted(taxa_totals.keys(), key=lambda t: taxa_totals[t], reverse=True)[:top_n]

    matrix = [
        {'name': t, 'group': all_taxa[t], 'total': taxa_totals[t],
         **{f's{sid}': by_sample.get(sid, {}).get(t, 0) for sid in sample_ids}}
        for t in top_taxa
    ]
    return jsonify({'sample_ids': sample_ids,
                    'aliases': {str(k): v for k, v in aliases.items()},
                    'matrix': matrix})


# ── Map station aggregates ────────────────────────────────────────────────────

@app.route("/api/map/stations/wilder")
def map_stations_wilder():
    """
    Per-station aggregate for the Wilder map view.
    Returns one row per station that has lat/lon, with the top species and
    total reads so the frontend can size markers and render popup content.
    Optional: wilder_job_id, organism_group
    """
    where, params = [], []
    if request.args.get("wilder_job_id"):
        where.append("wr.wilder_job_id = %s")
        params.append(request.args["wilder_job_id"])
    if request.args.get("organism_group"):
        where.append("wr.organism_group = %s")
        params.append(request.args["organism_group"])

    clause = "AND " + " AND ".join(where) if where else ""

    # Station-level totals
    stations = query(f"""
        SELECT
            ms.station,
            ms.station_id,
            ms.geo_loc_name,
            AVG(ms.latitude)   AS latitude,
            AVG(ms.longitude)  AS longitude,
            SUM(wr.read_count) AS total_reads,
            COUNT(DISTINCT wr.scientific_name) AS species_count,
            COUNT(DISTINCT ms.mbon_data_id)    AS sample_count
        FROM wilder_results wr
        JOIN mbon_samples ms ON ms.mbon_data_id = wr.mbon_data_id
        WHERE ms.latitude IS NOT NULL AND ms.longitude IS NOT NULL
          {clause}
        GROUP BY ms.station, ms.station_id, ms.geo_loc_name
        ORDER BY total_reads DESC
    """, params)

    # Top species per station (top 50 for popup table/chart)
    species_rows = query(f"""
        SELECT
            ms.station,
            wr.scientific_name,
            wr.common_name,
            wr.organism_group,
            wr.taxonomic_rank,
            SUM(wr.read_count) AS total_reads,
            COUNT(DISTINCT ms.mbon_data_id) AS sample_count
        FROM wilder_results wr
        JOIN mbon_samples ms ON ms.mbon_data_id = wr.mbon_data_id
        WHERE ms.latitude IS NOT NULL AND ms.longitude IS NOT NULL
          {clause}
        GROUP BY ms.station, wr.scientific_name, wr.common_name, wr.organism_group, wr.taxonomic_rank
        ORDER BY ms.station, total_reads DESC
    """, params)

    # Group species rows by station
    by_station = {}
    for row in species_rows:
        s = row["station"]
        by_station.setdefault(s, []).append(row)

    for stn in stations:
        stn["top_species"] = by_station.get(stn["station"], [])[:50]

    # Add samples list to each station (with per-sample total reads)
    samples_rows = query(f"""
        SELECT
            ms.station,
            ms.mbon_data_id,
            ms.mbon_samp_alias,
            ms.sample_depth,
            ms.latitude,
            ms.longitude,
            ms.temp,
            ms.salinity,
                        MAX(p.project_name)   AS project_name,
                        MIN(rn.run_id)        AS run_id,
                        MAX(rn.directory)     AS run_directory,
            MAX(wr.wilder_uid)    AS wilder_uid,
            MAX(wr.wilder_job_id) AS wilder_job_id,
            SUM(wr.read_count)    AS total_reads
        FROM wilder_results wr
        JOIN mbon_samples ms ON ms.mbon_data_id = wr.mbon_data_id
                LEFT JOIN bioinformatics  b  ON b.id                = wr.bioinfo_id
                LEFT JOIN data_management dm ON dm.id               = b.data_management_id
                LEFT JOIN run             rn ON rn.run_id           = dm.run_id
                LEFT JOIN sample          s  ON s.sample_id         = rn.sample_id
                LEFT JOIN collection      c  ON c.collection_id     = s.collection_id
                LEFT JOIN project         p  ON p.project_id        = c.project_id
        WHERE ms.latitude IS NOT NULL AND ms.longitude IS NOT NULL
          {clause}
        GROUP BY ms.station, ms.mbon_data_id, ms.mbon_samp_alias,
                 ms.sample_depth, ms.latitude, ms.longitude, ms.temp, ms.salinity
        ORDER BY ms.station, total_reads DESC
    """, params if params else None)

    # Per-sample top species (for popup "Samples" view and compare per-sample)
    sample_species_rows = query(f"""
        SELECT
            ms.mbon_data_id,
            wr.scientific_name,
            wr.common_name,
            wr.organism_group,
            wr.taxonomic_rank,
            SUM(wr.read_count) AS total_reads
        FROM wilder_results wr
        JOIN mbon_samples ms ON ms.mbon_data_id = wr.mbon_data_id
        WHERE ms.latitude IS NOT NULL AND ms.longitude IS NOT NULL
          {clause}
        GROUP BY ms.mbon_data_id, wr.scientific_name, wr.common_name, wr.organism_group, wr.taxonomic_rank
        ORDER BY ms.mbon_data_id, total_reads DESC
    """, params if params else None)
    by_sample_sp = {}
    for row in sample_species_rows:
        by_sample_sp.setdefault(row["mbon_data_id"], []).append(row)

    samp_by_station = {}
    for row in samples_rows:
        sid = row["mbon_data_id"]
        samp_by_station.setdefault(row["station"], []).append({
            "mbon_data_id": sid,
            "alias": row["mbon_samp_alias"] or row["wilder_uid"] or str(sid),
            "job_id": row["wilder_job_id"],
            "total_reads": int(row["total_reads"] or 0),
            "sample_depth": row.get("sample_depth"),
            "latitude": row.get("latitude"),
            "longitude": row.get("longitude"),
            "temp": row.get("temp"),
            "salinity": row.get("salinity"),
            "project_name": row.get("project_name"),
            "run_id": row.get("run_id"),
            "run_directory": row.get("run_directory"),
            "species": by_sample_sp.get(sid, [])[:30],
        })
    for stn in stations:
        stn["samples"] = samp_by_station.get(stn["station"], [])

    return jsonify(stations)


@app.route("/api/map/stations/kraken2")
def map_stations_kraken2():
    """
    Per-station aggregate for the Kraken2 map view.
    Joins kraken2 reports -> bioinformatics -> data_management -> run -> sample -> location -> mbon_samples.
    Optional: rank, group (organism group derived from taxonomy lineage)
    """
    rank_filter = request.args.get("rank", "")
    group_filter = request.args.get("group", "")

    rank_clause = "AND kl.rank = %s" if rank_filter else ""
    rank_params = [rank_filter] if rank_filter else []

    # Station-level totals via mbon_samples (has lat/lon at station level)
    stations = query(f"""
        SELECT
            ms.station,
            ms.station_id,
            ms.geo_loc_name,
            AVG(ms.latitude)   AS latitude,
            AVG(ms.longitude)  AS longitude,
            SUM(kl.count)      AS total_reads,
            COUNT(DISTINCT kl.name) AS species_count,
            COUNT(DISTINCT ms.mbon_data_id) AS sample_count
        FROM kraken2_lcas kl
        JOIN kraken2_report kr  ON kr.id               = kl.kraken2_report_id
        JOIN bioinformatics  b  ON b.id                = kr.bioinfo_id
        JOIN data_management dm ON dm.id               = b.data_management_id
        JOIN run             rn ON rn.run_id           = dm.run_id
        JOIN sample          s  ON s.sample_id         = rn.sample_id
        JOIN mbon_samples    ms ON ms.sample_id        = s.sample_id
        WHERE ms.latitude IS NOT NULL AND ms.longitude IS NOT NULL
          {rank_clause}
        GROUP BY ms.station, ms.station_id, ms.geo_loc_name
        ORDER BY total_reads DESC
    """, rank_params)

    # Top taxa per station (taxonomy_id lets us resolve the organism group)
    taxa_rows = query(f"""
        SELECT
            ms.station,
            kl.name,
            kl.rank,
            MIN(kl.taxonomy_id)  AS taxonomy_id,
            SUM(kl.count)        AS total_reads,
            AVG(kl.abundance)    AS avg_abundance
        FROM kraken2_lcas kl
        JOIN kraken2_report kr  ON kr.id               = kl.kraken2_report_id
        JOIN bioinformatics  b  ON b.id                = kr.bioinfo_id
        JOIN data_management dm ON dm.id               = b.data_management_id
        JOIN run             rn ON rn.run_id           = dm.run_id
        JOIN sample          s  ON s.sample_id         = rn.sample_id
        JOIN mbon_samples    ms ON ms.sample_id        = s.sample_id
        WHERE ms.latitude IS NOT NULL AND ms.longitude IS NOT NULL
          {rank_clause}
        GROUP BY ms.station, kl.name, kl.rank
        ORDER BY ms.station, total_reads DESC
    """, rank_params)

    # Attach the derived organism group to every taxon row.
    for row in taxa_rows:
        row["organism_group"] = k2_group(row.get("taxonomy_id"))

    by_station = {}
    for row in taxa_rows:
        by_station.setdefault(row["station"], []).append(row)

    # Optional group filter: keep only taxa in the requested group and recompute
    # station-level totals from the filtered set (mirrors the wilder group filter).
    if group_filter:
        kept = []
        for stn in stations:
            gt = [r for r in by_station.get(stn["station"], [])
                  if r.get("organism_group") == group_filter]
            if not gt:
                continue
            stn["top_taxa"] = gt[:200]
            stn["total_reads"] = sum(int(r["total_reads"] or 0) for r in gt)
            stn["species_count"] = len({r["name"] for r in gt})
            stn["rank_options"] = sorted(
                {r.get("rank") for r in gt if r.get("rank")}, key=rank_sort_key)
            kept.append(stn)
        stations = kept
    else:
        for stn in stations:
            stn["top_taxa"] = by_station.get(stn["station"], [])[:200]
            stn["rank_options"] = sorted(
                {row.get("rank") for row in stn["top_taxa"] if row.get("rank")},
                key=rank_sort_key,
            )

    # Add samples list to each station
    samp_rows_k2 = query(f"""
        SELECT
            ms.station,
            ms.mbon_data_id,
            ms.mbon_samp_alias,
            ms.sample_depth,
            ms.latitude,
            ms.longitude,
            ms.temp,
            ms.salinity,
            MAX(p.project_name) AS project_name,
            MIN(rn.run_id)      AS run_id,
            MAX(rn.directory)   AS run_directory,
            MIN(kr.id) AS report_id,
            SUM(kl.count) AS total_reads
        FROM kraken2_lcas kl
        JOIN kraken2_report kr  ON kr.id               = kl.kraken2_report_id
        JOIN bioinformatics  b  ON b.id                = kr.bioinfo_id
        JOIN data_management dm ON dm.id               = b.data_management_id
        JOIN run             rn ON rn.run_id           = dm.run_id
        JOIN sample          s  ON s.sample_id         = rn.sample_id
        LEFT JOIN collection c  ON c.collection_id     = s.collection_id
        LEFT JOIN project    p  ON p.project_id        = c.project_id
        JOIN mbon_samples    ms ON ms.sample_id        = s.sample_id
        WHERE ms.latitude IS NOT NULL AND ms.longitude IS NOT NULL
                    {rank_clause}
                GROUP BY ms.station, ms.mbon_data_id, ms.mbon_samp_alias,
                                 ms.sample_depth, ms.latitude, ms.longitude, ms.temp, ms.salinity
        ORDER BY ms.station, ms.mbon_data_id
    """, rank_params if rank_filter else None)
    k2samp_by_station = {}
    for row in samp_rows_k2:
        k2samp_by_station.setdefault(row["station"], []).append({
            "mbon_data_id": row["mbon_data_id"],
            "alias": row["mbon_samp_alias"] or f"Report #{row['report_id']}",
            "report_id": row["report_id"],
            "total_reads": int(row["total_reads"] or 0),
            "sample_depth": row.get("sample_depth"),
            "latitude": row.get("latitude"),
            "longitude": row.get("longitude"),
            "temp": row.get("temp"),
            "salinity": row.get("salinity"),
            "project_name": row.get("project_name"),
            "run_id": row.get("run_id"),
            "run_directory": row.get("run_directory"),
        })
    for stn in stations:
        stn["samples"] = k2samp_by_station.get(stn["station"], [])

    return jsonify(stations)


@app.route("/api/map/groups/kraken2")
def map_groups_kraken2():
    """Distinct Kraken2 organism groups (Class→Phylum→Kingdom→Domain names),
    resolved from the taxonomy lineage, with a taxon count each."""
    try:
        rows = query("SELECT DISTINCT taxonomy_id FROM kraken2_lcas "
                     "WHERE taxonomy_id IS NOT NULL")
    except Exception:
        rows = []
    counts = {}
    for r in rows:
        g = k2_group(r.get("taxonomy_id"))
        if g:
            counts[g] = counts.get(g, 0) + 1
    out = sorted(({"organism_group": g, "taxa_count": c} for g, c in counts.items()),
                 key=lambda x: (-x["taxa_count"], x["organism_group"]))
    return jsonify(out)


def _xsample_matrix(meta_rows, per_sample_rows, taxa, key_field):
    """Build the per-sample list + organism x sample read matrix used by the
    Cross-Sample occupancy bar and heatmap.

    meta_rows        : [{mbon_data_id, mbon_samp_alias, sample_depth, [total_reads]}]
    per_sample_rows  : [{mbon_data_id, <key_field or scientific_name/name>, reads}]
    taxa             : the (already top-N truncated) combined taxa list; the
                       matrix is limited to these taxa to bound response size.
    key_field        : 'scientific_name' (wilder) or 'name' (kraken2)

    Returns {"samples": [...], "matrix": [{taxon, per_sample:{id:reads}, total}]}.
    """
    # Per-sample totals (fallback when meta lacks precomputed totals).
    sample_totals = {}
    for r in per_sample_rows:
        sid = r["mbon_data_id"]
        sample_totals[sid] = sample_totals.get(sid, 0) + int(r["reads"] or 0)

    samples = []
    for m in meta_rows:
        sid = m["mbon_data_id"]
        samples.append({
            "mbon_data_id": sid,
            "alias": m.get("mbon_samp_alias") or f"#{sid}",
            "sample_depth": m.get("sample_depth"),
            "total_reads": int(m.get("total_reads") if m.get("total_reads") is not None
                               else sample_totals.get(sid, 0)),
        })
    # Keep a stable, reads-descending sample order.
    samples.sort(key=lambda s: -s["total_reads"])

    wanted = {t.get(key_field) for t in taxa}
    lookup = {}
    for r in per_sample_rows:
        name = r.get(key_field) if key_field in r else (r.get("scientific_name") or r.get("name"))
        if name not in wanted:
            continue
        lookup.setdefault(name, {})[r["mbon_data_id"]] = int(r["reads"] or 0)

    matrix = []
    for t in taxa:
        name = t.get(key_field)
        per = lookup.get(name, {})
        matrix.append({
            "taxon": name,
            "organism_group": t.get("organism_group"),
            "rank": t.get("rank"),
            "per_sample": {str(k): v for k, v in per.items()},
            "total": t.get("total_reads", 0),
            "sample_count": t.get("sample_count", 0),
        })
    return {"samples": samples, "matrix": matrix}


@app.route("/api/map/merged-distribution", methods=["POST"])
def map_merged_distribution():
    """Merge a set of samples into one combined taxonomic distribution.

    This backs the map "sample merging (with numbers)" feature ported from the
    build_mbon_html.py template: the caller passes a list of samples and the
    server returns the combined per-taxon read counts across those samples,
    plus the number of samples merged and, per taxon, how many of the merged
    samples it was detected in (occupancy).

    Request JSON:
        {
          "type":       "wilder" | "kraken2",
          "sample_ids": [<mbon_data_id>, ...],   # samples to merge
          "rank":       "S"        (optional; kraken2 rank filter),
          "top_n":      50         (optional; cap on returned taxa rows)
        }

    Response JSON:
        {
          "type": "wilder",
          "requested_samples": 3,      # ids sent in
          "sample_count": 3,           # ids that actually had data (merged)
          "sample_ids": [...],
          "total_reads": 123456,
          "taxa":   [ { name/scientific_name, common_name, organism_group,
                        rank/taxonomic_rank, total_reads, sample_count,
                        abundance } ... ],
          "groups": [ { organism_group, total_reads, species_count } ]  # wilder
        }
    """
    body = request.get_json(silent=True) or {}
    dtype = (body.get("type") or "wilder").lower()
    raw_ids = body.get("sample_ids") or []
    rank_filter = body.get("rank") or ""
    try:
        top_n = int(body.get("top_n") or 0)
    except (TypeError, ValueError):
        top_n = 0

    # Coerce ids to ints, dropping anything non-numeric.
    sample_ids = []
    for v in raw_ids:
        try:
            sample_ids.append(int(v))
        except (TypeError, ValueError):
            continue
    sample_ids = sorted(set(sample_ids))

    if not sample_ids:
        return jsonify({"error": "sample_ids is required and must be non-empty"}), 400
    if dtype not in ("wilder", "kraken2"):
        return jsonify({"error": "type must be 'wilder' or 'kraken2'"}), 400

    if dtype == "wilder":
        rows = query("""
            SELECT
                wr.scientific_name,
                wr.common_name,
                wr.organism_group,
                wr.taxonomic_rank,
                SUM(wr.read_count)                 AS total_reads,
                COUNT(DISTINCT wr.mbon_data_id)    AS sample_count
            FROM wilder_results wr
            WHERE wr.mbon_data_id = ANY(%s)
            GROUP BY wr.scientific_name, wr.common_name,
                     wr.organism_group, wr.taxonomic_rank
            ORDER BY total_reads DESC
        """, (sample_ids,))

        total_reads = sum(int(r["total_reads"] or 0) for r in rows)
        merged = query(
            "SELECT COUNT(DISTINCT mbon_data_id) AS n "
            "FROM wilder_results WHERE mbon_data_id = ANY(%s)",
            (sample_ids,),
        )
        merged_count = int(merged[0]["n"]) if merged else 0

        taxa = []
        groups = {}
        for r in rows:
            reads = int(r["total_reads"] or 0)
            pct = (reads / total_reads * 100) if total_reads else 0
            taxa.append({
                "scientific_name": r["scientific_name"],
                "common_name": r["common_name"],
                "organism_group": r["organism_group"],
                "taxonomic_rank": r["taxonomic_rank"],
                "total_reads": reads,
                "sample_count": int(r["sample_count"] or 0),
                "abundance": round(pct, 4),
            })
            g = r["organism_group"] or "Unassigned"
            gg = groups.setdefault(g, {"organism_group": g, "total_reads": 0, "species_count": 0})
            gg["total_reads"] += reads
            gg["species_count"] += 1

        if top_n and top_n > 0:
            taxa = taxa[:top_n]
        group_list = sorted(groups.values(), key=lambda x: -x["total_reads"])

        # Per-sample metadata (alias, depth) and the organism x sample matrix
        # that back the Cross-Sample occupancy bar + heatmap.
        meta_rows = query("""
            SELECT ms.mbon_data_id, ms.mbon_samp_alias, ms.sample_depth,
                   COALESCE(SUM(wr.read_count), 0) AS total_reads
            FROM mbon_samples ms
            LEFT JOIN wilder_results wr ON wr.mbon_data_id = ms.mbon_data_id
            WHERE ms.mbon_data_id = ANY(%s)
            GROUP BY ms.mbon_data_id, ms.mbon_samp_alias, ms.sample_depth
            ORDER BY total_reads DESC
        """, (sample_ids,))
        ps_rows = query("""
            SELECT wr.mbon_data_id, wr.scientific_name,
                   SUM(wr.read_count) AS reads
            FROM wilder_results wr
            WHERE wr.mbon_data_id = ANY(%s)
            GROUP BY wr.mbon_data_id, wr.scientific_name
        """, (sample_ids,))
        payload = _xsample_matrix(meta_rows, ps_rows, taxa, "scientific_name")

        return jsonify({
            "type": "wilder",
            "requested_samples": len(sample_ids),
            "sample_count": merged_count,
            "sample_ids": sample_ids,
            "total_reads": total_reads,
            "taxa": taxa,
            "groups": group_list,
            "samples": payload["samples"],
            "matrix": payload["matrix"],
        })

    # kraken2
    rank_clause = "AND kl.rank = %s" if rank_filter else ""
    params = [sample_ids]
    if rank_filter:
        params.append(rank_filter)

    rows = query(f"""
        SELECT
            kl.name,
            kl.rank,
            SUM(kl.count)                    AS total_reads,
            AVG(kl.abundance)                AS avg_abundance,
            COUNT(DISTINCT ms.mbon_data_id)  AS sample_count
        FROM kraken2_lcas kl
        JOIN kraken2_report kr  ON kr.id        = kl.kraken2_report_id
        JOIN bioinformatics  b  ON b.id         = kr.bioinfo_id
        JOIN data_management dm ON dm.id        = b.data_management_id
        JOIN run             rn ON rn.run_id    = dm.run_id
        JOIN sample          s  ON s.sample_id  = rn.sample_id
        JOIN mbon_samples    ms ON ms.sample_id = s.sample_id
        WHERE ms.mbon_data_id = ANY(%s)
          {rank_clause}
        GROUP BY kl.name, kl.rank
        ORDER BY total_reads DESC
    """, params)

    total_reads = sum(int(r["total_reads"] or 0) for r in rows)
    merged = query("""
        SELECT COUNT(DISTINCT ms.mbon_data_id) AS n
        FROM kraken2_lcas kl
        JOIN kraken2_report kr  ON kr.id        = kl.kraken2_report_id
        JOIN bioinformatics  b  ON b.id         = kr.bioinfo_id
        JOIN data_management dm ON dm.id        = b.data_management_id
        JOIN run             rn ON rn.run_id    = dm.run_id
        JOIN sample          s  ON s.sample_id  = rn.sample_id
        JOIN mbon_samples    ms ON ms.sample_id = s.sample_id
        WHERE ms.mbon_data_id = ANY(%s)
    """, (sample_ids,))
    merged_count = int(merged[0]["n"]) if merged else 0

    taxa = []
    for r in rows:
        reads = int(r["total_reads"] or 0)
        pct = (reads / total_reads * 100) if total_reads else 0
        taxa.append({
            "name": r["name"],
            "rank": r["rank"],
            "total_reads": reads,
            "avg_abundance": round(float(r["avg_abundance"] or 0), 4),
            "sample_count": int(r["sample_count"] or 0),
            "abundance": round(pct, 4),
        })
    if top_n and top_n > 0:
        taxa = taxa[:top_n]

    # Per-sample metadata + organism x sample matrix for Cross-Sample plots.
    meta_rows = query("""
        SELECT ms.mbon_data_id, ms.mbon_samp_alias, ms.sample_depth
        FROM mbon_samples ms
        WHERE ms.mbon_data_id = ANY(%s)
    """, (sample_ids,))
    ps_params = [sample_ids]
    if rank_filter:
        ps_params.append(rank_filter)
    ps_rows = query(f"""
        SELECT ms.mbon_data_id, kl.name, SUM(kl.count) AS reads
        FROM kraken2_lcas kl
        JOIN kraken2_report kr  ON kr.id        = kl.kraken2_report_id
        JOIN bioinformatics  b  ON b.id         = kr.bioinfo_id
        JOIN data_management dm ON dm.id        = b.data_management_id
        JOIN run             rn ON rn.run_id    = dm.run_id
        JOIN sample          s  ON s.sample_id  = rn.sample_id
        JOIN mbon_samples    ms ON ms.sample_id = s.sample_id
        WHERE ms.mbon_data_id = ANY(%s)
          {rank_clause}
        GROUP BY ms.mbon_data_id, kl.name
    """, ps_params)
    payload = _xsample_matrix(meta_rows, ps_rows, taxa, "name")

    return jsonify({
        "type": "kraken2",
        "requested_samples": len(sample_ids),
        "sample_count": merged_count,
        "sample_ids": sample_ids,
        "total_reads": total_reads,
        "taxa": taxa,
        "samples": payload["samples"],
        "matrix": payload["matrix"],
    })


@app.route("/api/map/available-types")
def map_available_types():
    """Detect which map station types have geo-located data in the DB."""
    types = []
    r_w = query("""
        SELECT COUNT(*) AS n FROM wilder_results wr
        JOIN mbon_samples ms ON ms.mbon_data_id = wr.mbon_data_id
        WHERE ms.latitude IS NOT NULL
    """)
    if r_w and int(r_w[0]["n"]) > 0:
        types.append("wilder")
    r_k = query("""
        SELECT COUNT(*) AS n FROM kraken2_lcas kl
        JOIN kraken2_report kr  ON kr.id = kl.kraken2_report_id
        JOIN bioinformatics  b  ON b.id = kr.bioinfo_id
        JOIN data_management dm ON dm.id = b.data_management_id
        JOIN run             rn ON rn.run_id = dm.run_id
        JOIN sample          s  ON s.sample_id = rn.sample_id
        JOIN mbon_samples    ms ON ms.sample_id = s.sample_id
        WHERE ms.latitude IS NOT NULL
    """)
    if r_k and int(r_k[0]["n"]) > 0:
        types.append("kraken2")
    return jsonify(types)


# ── Connection management ─────────────────────────────────────────────────────
# Server-side registry of data sources (Postgres, hosted .xlsx, Google Sheets).
# Secrets are never returned to the browser.

_ALLOWED_KINDS = {"postgres", "xlsx", "gsheet"}


@app.route("/api/connections")
def connections_list():
    return jsonify(conn_store.list_profiles())


@app.route("/api/connections/active")
def connections_active():
    cid = conn_store.active_id()
    prof = conn_store.get_profile(cid) if cid else None
    return jsonify(conn_store.public(prof) if prof else {})


@app.route("/api/connections", methods=["POST"])
def connections_create():
    data = request.get_json(force=True, silent=True) or {}
    if data.get("kind") not in _ALLOWED_KINDS:
        abort(400, description=f"kind must be one of {sorted(_ALLOWED_KINDS)}")
    return jsonify(conn_store.add_profile(data)), 201


@app.route("/api/connections/<cid>", methods=["PUT", "PATCH"])
def connections_update(cid):
    data = request.get_json(force=True, silent=True) or {}
    updated = conn_store.update_profile(cid, data)
    if not updated:
        abort(404)
    return jsonify(updated)


@app.route("/api/connections/<cid>", methods=["DELETE"])
def connections_delete(cid):
    if not conn_store.delete_profile(cid):
        abort(404)
    return jsonify({"deleted": cid})


@app.route("/api/connections/<cid>/test", methods=["POST", "GET"])
def connections_test(cid):
    return jsonify(conn_store.test_profile(cid))


@app.route("/api/connections/<cid>/activate", methods=["POST"])
def connections_activate(cid):
    if not conn_store.set_active(cid):
        abort(404)
    return jsonify({"active": cid})


@app.route("/api/connections/<cid>/reload", methods=["POST"])
def connections_reload(cid):
    """Force a spreadsheet source to re-read its workbook / sheet."""
    conn_store.invalidate(cid)
    try:
        return jsonify(conn_store.source_by_id(cid, force=True).test())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Explore (env-range + species cross-filter) ────────────────────────────────

@app.route("/api/explore/schema")
def explore_schema():
    """Field metadata for the active source: env ranges, groups, ranks, bbox."""
    try:
        src = conn_store.active_source()
        schema = src.explore_schema()
        cid = conn_store.active_id()
        prof = conn_store.get_profile(cid) or {}
        schema["source"] = {"id": cid, "name": prof.get("name"), "kind": prof.get("kind")}
        return jsonify(schema)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _parse_range(args, key):
    lo = args.get(f"{key}_min")
    hi = args.get(f"{key}_max")
    if lo is None and hi is None:
        return None
    def f(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None
    return [f(lo), f(hi)]


@app.route("/api/explore/query", methods=["GET", "POST"])
def explore_query():
    """
    Env-range + species cross-filter against the active source.

    Accepts JSON body (POST) or query params (GET):
        depth_min/max, salinity_min/max, temp_min/max, sst_min/max
        bbox = {s,n,w,e}      (POST) or  s,n,w,e params (GET)
        groups[], ranks[], species[], stations[]   (CSV in GET)
        search, min_reads, top, date_from, date_to
    """
    if request.method == "POST":
        f = request.get_json(force=True, silent=True) or {}
    else:
        a = request.args
        def csv(key):
            v = a.get(key)
            return [x.strip() for x in v.split(",") if x.strip()] if v else None
        f = {
            "depth":    _parse_range(a, "depth"),
            "salinity": _parse_range(a, "salinity"),
            "temp":     _parse_range(a, "temp"),
            "sst":      _parse_range(a, "sst"),
            "groups":   csv("groups"),
            "ranks":    csv("ranks"),
            "species":  csv("species"),
            "stations": csv("stations"),
            "search":   a.get("search"),
            "min_reads": a.get("min_reads"),
            "top":      a.get("top", 50),
            "date_from": a.get("date_from"),
            "date_to":  a.get("date_to"),
        }
        if any(a.get(k) is not None for k in ("s", "n", "w", "e")):
            def fl(x):
                try:
                    return float(x)
                except (TypeError, ValueError):
                    return None
            f["bbox"] = {k: fl(a.get(k)) for k in ("s", "n", "w", "e")}
    # drop empties
    f = {k: v for k, v in f.items() if v not in (None, "", [])}
    try:
        result = conn_store.active_source().explore_query(f)
        cid = conn_store.active_id()
        prof = conn_store.get_profile(cid) or {}
        result["source"] = {"id": cid, "name": prof.get("name"), "kind": prof.get("kind")}
        return jsonify(result)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ── Custom read-only SQL ──────────────────────────────────────────────────────
#
# A deliberately constrained SQL endpoint for trusted analysts: it accepts a
# single SELECT (or WITH … SELECT) statement, rejects anything that could
# mutate the database or run multiple statements, wraps the query in an outer
# LIMIT, and runs it with a short statement timeout.  Postgres sources only.

import re as _re

# Tokens that must never appear as statement keywords in a read-only query.
_SQL_FORBIDDEN = (
    "insert", "update", "delete", "drop", "truncate", "alter", "create",
    "grant", "revoke", "comment", "vacuum", "analyze", "reindex", "cluster",
    "copy", "call", "do", "merge", "refresh", "lock", "begin", "commit",
    "rollback", "savepoint", "set", "reset", "prepare", "execute", "listen",
    "notify", "discard", "explain",
)
SQL_MAX_ROWS = int(os.getenv("SQL_MAX_ROWS", "5000"))
SQL_TIMEOUT_MS = int(os.getenv("SQL_TIMEOUT_MS", "15000"))


def _strip_sql_comments(sql: str) -> str:
    """Remove -- line comments and /* */ block comments."""
    sql = _re.sub(r"/\*.*?\*/", " ", sql, flags=_re.S)
    sql = _re.sub(r"--[^\n]*", " ", sql)
    return sql


def _validate_select(sql: str) -> str:
    """Return cleaned SQL if it is a single read-only SELECT, else raise ValueError."""
    if not sql or not sql.strip():
        raise ValueError("Empty query")
    cleaned = _strip_sql_comments(sql).strip().rstrip(";").strip()
    if not cleaned:
        raise ValueError("Empty query")
    # Reject multiple statements (any remaining semicolon separates statements).
    if ";" in cleaned:
        raise ValueError("Only a single statement is allowed")
    low = cleaned.lower()
    # Must begin with SELECT or WITH.
    if not (low.startswith("select") or low.startswith("with")):
        raise ValueError("Only SELECT / WITH queries are allowed")
    # Reject forbidden keywords appearing as whole words.
    for kw in _SQL_FORBIDDEN:
        if _re.search(r"(?<![a-z_])" + kw + r"(?![a-z_])", low):
            raise ValueError(f"Keyword not allowed in read-only query: {kw.upper()}")
    # Disallow writeable CTEs (WITH … AS ( INSERT/UPDATE/DELETE … )) — already
    # covered by the keyword scan above, but keep the explicit guard for clarity.
    return cleaned


@app.route("/api/sql/query", methods=["POST"])
def sql_query():
    """Execute a single read-only SELECT against the active Postgres source."""
    body = request.get_json(silent=True) or {}
    raw = body.get("sql", "")
    try:
        limit = int(body.get("limit") or SQL_MAX_ROWS)
    except (TypeError, ValueError):
        limit = SQL_MAX_ROWS
    limit = max(1, min(limit, SQL_MAX_ROWS))

    # Only meaningful for Postgres sources.
    try:
        prof = conn_store.get_profile(conn_store.active_id()) or {}
    except Exception:
        prof = {}
    if prof.get("kind") not in (None, "postgres"):
        return jsonify({"error": "Custom SQL is only available for Postgres data sources."}), 400

    try:
        cleaned = _validate_select(raw)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    wrapped = f"SELECT * FROM (\n{cleaned}\n) AS _mbon_sub LIMIT {limit}"
    conn = None
    try:
        import time
        t0 = time.time()
        conn = get_db()
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {SQL_TIMEOUT_MS}")
            cur.execute(wrapped)
            rows = [dict(r) for r in cur.fetchall()]
            columns = [d[0] for d in cur.description] if cur.description else []
        ms = int((time.time() - t0) * 1000)
        return app.response_class(
            json.dumps({
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
                "truncated": len(rows) >= limit,
                "elapsed_ms": ms,
            }, cls=_Encoder),
            mimetype="application/json",
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        if conn is not None:
            conn.close()


@app.route("/api/sql/tables")
def sql_tables():
    """List user tables and their columns (for editor autocomplete)."""
    try:
        prof = conn_store.get_profile(conn_store.active_id()) or {}
    except Exception:
        prof = {}
    if prof.get("kind") not in (None, "postgres"):
        return jsonify({"tables": []})
    try:
        rows = query(
            """
            SELECT table_schema, table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
            ORDER BY table_schema, table_name, ordinal_position
            """
        )
    except Exception as e:
        return jsonify({"tables": [], "error": str(e)})
    tables = {}
    for r in rows:
        key = r["table_name"] if r["table_schema"] == "public" else f"{r['table_schema']}.{r['table_name']}"
        tables.setdefault(key, []).append({"name": r["column_name"], "type": r["data_type"]})
    return jsonify({"tables": [{"name": k, "columns": v} for k, v in tables.items()]})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    host = os.getenv("API_HOST", "127.0.0.1")
    port = int(os.getenv("API_PORT", "5050"))
    debug = os.getenv("FLASK_DEBUG", "0").lower() in ("1", "true", "yes")
    if debug and host not in ("127.0.0.1", "localhost"):
        log.warning("FLASK_DEBUG is on while listening on %s — the debugger allows code "
                    "execution; never expose this on a shared network", host)
    log.info("MBON API listening on http://%s:%d (debug=%s)", host, port, debug)
    app.run(debug=debug, host=host, port=port)
