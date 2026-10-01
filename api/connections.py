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
MBON Dashboard — connection registry.

Stores the set of data-source *profiles* the dashboard can switch between, and
which one is currently active.  Persisted to api/connections.json.

Security model (hosted, few trusted analysts):
  * Secrets (Postgres passwords) are stored server-side only and NEVER returned
    to the browser — `public()` masks them.
  * Built `DataSource` objects are cached so we don't reconnect / re-read a
    spreadsheet on every request; spreadsheet caches expire after TTL so edits
    to a linked workbook/sheet are eventually picked up (or via explicit reload).
"""

from __future__ import annotations

import os
import json
import time
import uuid
import threading
from typing import Optional

from datasources import build_source, DataSource

import logging

log = logging.getLogger("mbon.connections")

_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(_DIR)
# Override with MBON_CONNECTIONS_FILE (relative paths resolve against api/).
STORE_PATH = os.path.join(_DIR, os.getenv("MBON_CONNECTIONS_FILE", "connections.json"))
# Synthetic demo workbook (see make_example_data.py) offered as a ready-made source.
EXAMPLE_XLSX = os.path.join("data", "example", "dashboard_example.xlsx")

_SECRET_KEYS = ("password", "passwd", "secret", "token", "api_key")
_SPREADSHEET_TTL = 300  # seconds before a cached workbook/sheet is reloaded

_lock = threading.RLock()
_built: dict[str, tuple[float, DataSource]] = {}   # id -> (built_at, source)


# ── persistence ──────────────────────────────────────────────────────────────

def _default_store() -> dict:
    """Seed a Postgres profile from environment variables, plus the bundled
    example workbook. The example source is active when no Postgres host has
    been configured, so a fresh checkout shows data immediately."""
    pg = {
        "id": "default-postgres",
        "name": "Local Postgres",
        "kind": "postgres",
        "readonly": True,
        "config": {
            "host": os.getenv("PGHOST", "127.0.0.1"),
            "port": os.getenv("PGPORT", "5432"),
            "dbname": os.getenv("PGDATABASE", "postgres"),
            "user": os.getenv("PGUSER", "postgres"),
            "password": os.getenv("PGPASSWORD", ""),
        },
        "created_at": time.time(),
    }
    example = {
        "id": "example-xlsx",
        "name": "Example data (synthetic workbook)",
        "kind": "xlsx",
        "readonly": True,
        "config": {"path": EXAMPLE_XLSX},
        "created_at": time.time(),
    }
    conns = [pg]
    if os.path.exists(os.path.join(REPO_ROOT, EXAMPLE_XLSX)):
        conns.append(example)
    active = "default-postgres" if (os.getenv("PGHOST") or len(conns) == 1) else "example-xlsx"
    log.info("created connection store %s (active: %s)", STORE_PATH, active)
    return {"active": active, "connections": conns}


def _read() -> dict:
    if not os.path.exists(STORE_PATH):
        store = _default_store()
        _write(store)
        return store
    try:
        with open(STORE_PATH, encoding="utf-8") as fh:
            store = json.load(fh)
        if not isinstance(store, dict) or not isinstance(store.get("connections"), list):
            raise ValueError("unexpected structure")
        return store
    except Exception as e:
        # Keep the broken file for inspection instead of silently overwriting it.
        log.error("connection store %s is unreadable (%s) — using defaults; the file "
                  "was left untouched", STORE_PATH, e)
        return _default_store()


def _write(store: dict):
    tmp = STORE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(store, fh, indent=2, default=str)
    os.replace(tmp, STORE_PATH)


# ── masking ──────────────────────────────────────────────────────────────────

def public(profile: dict) -> dict:
    """Return a copy safe to send to the browser (secrets masked)."""
    p = json.loads(json.dumps(profile, default=str))
    cfg = p.get("config", {})
    for k in list(cfg.keys()):
        if k.lower() in _SECRET_KEYS and cfg[k]:
            cfg[k] = "********"
    return p


# ── public API ───────────────────────────────────────────────────────────────

def list_profiles() -> list[dict]:
    with _lock:
        store = _read()
        active = store.get("active")
        out = []
        for p in store["connections"]:
            pp = public(p)
            pp["active"] = (p["id"] == active)
            out.append(pp)
        return out


def get_profile(cid: str) -> Optional[dict]:
    with _lock:
        for p in _read()["connections"]:
            if p["id"] == cid:
                return p
        return None


def active_id() -> Optional[str]:
    return _read().get("active")


def add_profile(data: dict) -> dict:
    with _lock:
        store = _read()
        cid = data.get("id") or f"{data.get('kind', 'src')}-{uuid.uuid4().hex[:8]}"
        profile = {
            "id": cid,
            "name": data.get("name") or cid,
            "kind": data["kind"],
            "readonly": bool(data.get("readonly", True)),
            "config": data.get("config") or {},
            "created_at": time.time(),
        }
        store["connections"].append(profile)
        if data.get("activate"):
            store["active"] = cid
        _write(store)
        _built.pop(cid, None)
        return public(profile)


def update_profile(cid: str, data: dict) -> Optional[dict]:
    with _lock:
        store = _read()
        for p in store["connections"]:
            if p["id"] == cid:
                if "name" in data:
                    p["name"] = data["name"]
                if "readonly" in data:
                    p["readonly"] = bool(data["readonly"])
                if "config" in data:
                    incoming = data["config"] or {}
                    # Don't overwrite a stored secret with the "********" mask.
                    for k, v in incoming.items():
                        if k.lower() in _SECRET_KEYS and v == "********":
                            continue
                        p.setdefault("config", {})[k] = v
                _write(store)
                _built.pop(cid, None)
                return public(p)
        return None


def delete_profile(cid: str) -> bool:
    with _lock:
        store = _read()
        before = len(store["connections"])
        store["connections"] = [p for p in store["connections"] if p["id"] != cid]
        if store.get("active") == cid:
            store["active"] = (store["connections"][0]["id"]
                               if store["connections"] else None)
        _write(store)
        _built.pop(cid, None)
        return len(store["connections"]) < before


def set_active(cid: str) -> bool:
    with _lock:
        store = _read()
        if any(p["id"] == cid for p in store["connections"]):
            store["active"] = cid
            _write(store)
            return True
        return False


# ── source building (cached) ─────────────────────────────────────────────────

def _source_for(profile: dict, force: bool = False) -> DataSource:
    cid = profile["id"]
    now = time.time()
    with _lock:
        cached = _built.get(cid)
        if cached and not force:
            built_at, src = cached
            is_sheet = profile["kind"] in ("xlsx", "gsheet")
            if not is_sheet or (now - built_at) < _SPREADSHEET_TTL:
                return src
        src = build_source(profile)
        _built[cid] = (now, src)
        return src


def active_source(force: bool = False) -> DataSource:
    profile = get_profile(active_id())
    if not profile:
        raise RuntimeError("no active data source configured")
    return _source_for(profile, force=force)


def source_by_id(cid: str, force: bool = False) -> DataSource:
    profile = get_profile(cid)
    if not profile:
        raise KeyError(cid)
    return _source_for(profile, force=force)


def test_profile(cid: str) -> dict:
    profile = get_profile(cid)
    if not profile:
        return {"ok": False, "error": "not found"}
    try:
        return _source_for(profile, force=True).test()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def invalidate(cid: Optional[str] = None):
    with _lock:
        if cid:
            _built.pop(cid, None)
        else:
            _built.clear()


def active_postgres_config() -> Optional[dict]:
    """Config of the active source if it is Postgres, else None."""
    profile = get_profile(active_id())
    if profile and profile.get("kind") == "postgres":
        return profile.get("config") or {}
    return None
