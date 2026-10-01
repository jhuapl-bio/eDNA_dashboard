"""
Browser-side data imports (xlsx / csv) persisted server-side.

The frontend Import panel parses files entirely in the browser, then POSTs the
normalized, role-tagged datasets here so they survive reloads and can be served
back to any client.  This store is deliberately INDEPENDENT of Postgres: it
keeps working even when no database connection is configured, and never touches
the canonical schema.

Stored shape (imports.json):
    { "sources": [
        { "id", "name", "fileName", "kind", "enabled",
          "datasets": [ { "role", "sheetName", "headers", "records": [...] } ],
          "csvMeta": {...}            # optional, for raw-CSV sources
        }, ...
    ] }
"""

import logging
import os
import json
import threading

from flask import Blueprint, jsonify, request

imports_bp = Blueprint("imports", __name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
# Override with MBON_IMPORTS_FILE (relative paths resolve against api/).
_STORE = os.path.join(_HERE, os.getenv("MBON_IMPORTS_FILE", "imports.json"))
log = logging.getLogger("mbon.imports")
MAX_SOURCES = int(os.getenv("MBON_MAX_IMPORT_SOURCES", "200"))
MAX_ROWS_PER_DATASET = int(os.getenv("MBON_MAX_IMPORT_ROWS", "500000"))
_LOCK = threading.Lock()

# roles the dashboard understands (others are still stored, just not merged)
ROLES = ["samples", "cruise", "runs", "bioinformatics", "wilder", "kraken2", "taxtriage"]


def _read():
    if not os.path.exists(_STORE):
        return {"sources": []}
    try:
        with open(_STORE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or "sources" not in data:
            log.error("%s has an unexpected structure — ignoring it", _STORE)
            return {"sources": []}
        return data
    except Exception as e:
        log.error("cannot read %s: %s", _STORE, e)
        return {"sources": []}


def _write(data):
    tmp = _STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, _STORE)


def _merged(data, enabled_only=True):
    """Concatenate records across sources, grouped by role."""
    out = {r: [] for r in ROLES}
    for src in data.get("sources", []):
        if enabled_only and not src.get("enabled", True):
            continue
        for ds in src.get("datasets", []):
            role = ds.get("role")
            if role in out:
                out[role].extend(ds.get("records", []))
    return out


def merged_records(role):
    """Public helper other endpoints can use to fold imported rows in."""
    with _LOCK:
        return _merged(_read()).get(role, [])


def _summary(data):
    srcs = []
    for s in data.get("sources", []):
        srcs.append({
            "id": s.get("id"),
            "name": s.get("name"),
            "fileName": s.get("fileName"),
            "kind": s.get("kind"),
            "enabled": s.get("enabled", True),
            "datasets": [
                {"role": d.get("role"), "sheetName": d.get("sheetName"),
                 "rows": len(d.get("records", [])), "cols": len(d.get("headers", []))}
                for d in s.get("datasets", [])
            ],
            "csvMeta": s.get("csvMeta"),
        })
    return srcs


@imports_bp.route("/api/import", methods=["GET"])
def import_list():
    """Return source summaries + merged records by role + counts."""
    with _LOCK:
        data = _read()
    merged = _merged(data)
    return jsonify({
        "sources": _summary(data),
        "merged": merged,
        "counts": {r: len(v) for r, v in merged.items()},
    })


@imports_bp.route("/api/import", methods=["POST"])
def import_save():
    """Replace the whole stored set with the posted sources (idempotent sync)."""
    body = request.get_json(force=True, silent=True) or {}
    sources = body.get("sources")
    if not isinstance(sources, list):
        return jsonify({"error": "Expected { sources: [...] }"}), 400
    if len(sources) > MAX_SOURCES:
        return jsonify({"error": f"Too many sources ({len(sources)} > {MAX_SOURCES})"}), 413
    # light sanitation — keep only known keys, validate shapes
    clean = []
    for i, s in enumerate(sources):
        if not isinstance(s, dict):
            return jsonify({"error": f"sources[{i}] must be an object"}), 400
        for d in s.get("datasets", []) or []:
            if not isinstance(d, dict) or not isinstance(d.get("records", []), list):
                return jsonify({"error": f"sources[{i}].datasets: each needs a records list"}), 400
            if len(d.get("records", [])) > MAX_ROWS_PER_DATASET:
                return jsonify({"error": f"sources[{i}]: dataset exceeds "
                                         f"{MAX_ROWS_PER_DATASET} rows"}), 413
            if d.get("role") and d.get("role") not in ROLES:
                log.warning("import: unknown role %r in source %r (stored, not merged)",
                            d.get("role"), s.get("name"))
        clean.append({
            "id": s.get("id"),
            "name": s.get("name"),
            "fileName": s.get("fileName"),
            "kind": s.get("kind"),
            "enabled": bool(s.get("enabled", True)),
            "datasets": [
                {"role": d.get("role"), "sheetName": d.get("sheetName"),
                 "headers": d.get("headers", []), "records": d.get("records", [])}
                for d in s.get("datasets", []) if d.get("role")
            ],
            "csvMeta": s.get("csvMeta"),
        })
    data = {"sources": clean}
    with _LOCK:
        _write(data)
    log.info("import store updated: %d source(s), %d dataset(s)", len(clean),
             sum(len(c["datasets"]) for c in clean))
    merged = _merged(data)
    return jsonify({"ok": True, "sources": _summary(data),
                    "counts": {r: len(v) for r, v in merged.items()}})


@imports_bp.route("/api/import/<src_id>", methods=["DELETE"])
def import_delete(src_id):
    with _LOCK:
        data = _read()
        before = len(data["sources"])
        data["sources"] = [s for s in data["sources"] if s.get("id") != src_id]
        _write(data)
    return jsonify({"ok": True, "removed": before - len(data["sources"])})


@imports_bp.route("/api/import", methods=["DELETE"])
def import_clear():
    with _LOCK:
        _write({"sources": []})
    return jsonify({"ok": True})
