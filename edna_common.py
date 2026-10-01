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
edna_common — shared logging, path and input-validation helpers.

Used by ``ingest.py`` and ``build_mbon_html.py`` so both tools:

* resolve every default path *relative to the repository root* (the folder
  that contains this file), never to a machine-specific absolute path;
* log to the console AND to a rotating file under ``logs/`` (configurable);
* validate inputs (files exist, are readable, have the expected extension,
  sheets and columns) before doing any work, and report problems clearly
  instead of crashing half-way through.

Nothing in here touches the database or the network.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

# ---------------------------------------------------------------------------
# Repository-relative defaults
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent

DATA_DIR = REPO_ROOT / "data"                     # real data (git-ignored)
PROJECTS_DIR = DATA_DIR / "projects"              # data/projects/<project>/{faire,kraken2,wilder,meta}
MANIFEST_NAME = "report_manifest.xlsx"            # lives inside the projects dir
EXAMPLE_PROJECTS_DIR = DATA_DIR / "example" / "projects"   # synthetic demo data (committed)
TEMPLATES_DIR = REPO_ROOT / "templates"
REPORTS_DIR = REPO_ROOT / "reports"               # generated HTML (git-ignored)
LOGS_DIR = REPO_ROOT / "logs"                     # log files (git-ignored)
SQL_DIR = REPO_ROOT / "sql"

SPREADSHEET_EXT = (".xlsx", ".xlsm", ".xls")
TABULAR_EXT = SPREADSHEET_EXT + (".csv", ".tsv", ".txt")

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
CONSOLE_FORMAT = "%(levelname)-7s %(message)s"


def resolve_path(p: str | os.PathLike | None, base: Path = REPO_ROOT) -> Path | None:
    """Expand ``~`` / env vars and make a relative path relative to ``base``
    (the repo root by default) — *not* to the current working directory — so
    the scripts behave the same no matter where they are launched from.
    Absolute paths are returned unchanged."""
    if p is None or str(p).strip() == "":
        return None
    q = Path(os.path.expandvars(os.path.expanduser(str(p).strip())))
    if q.is_absolute():
        return q
    # A path that already exists relative to the CWD wins (user typed it there).
    if (Path.cwd() / q).exists():
        return (Path.cwd() / q).resolve()
    return (base / q).resolve()


def rel(p: Path | str | None) -> str:
    """Pretty, repo-relative string for log messages (falls back to the name)."""
    if p is None:
        return "<none>"
    p = Path(p)
    try:
        return str(p.resolve().relative_to(REPO_ROOT))
    except (ValueError, OSError):
        return str(p)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
class _CountingHandler(logging.Handler):
    """Counts WARNING / ERROR records so a run can end with a summary and a
    meaningful exit code."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.warnings = 0
        self.errors = 0

    def emit(self, record):
        if record.levelno >= logging.ERROR:
            self.errors += 1
        elif record.levelno >= logging.WARNING:
            self.warnings += 1


_COUNTER = _CountingHandler()


def setup_logging(name: str, level: str | int = "INFO", log_file: str | Path | None = None,
                  quiet: bool = False) -> logging.Logger:
    """Configure the root logger once: console (stderr) + rotating file.

    ``log_file`` defaults to ``logs/<name>.log`` under the repo root. Pass
    ``"-"`` / ``"none"`` to disable file logging. The env vars
    ``EDNA_LOG_LEVEL`` and ``EDNA_LOG_FILE`` override the defaults.
    """
    level = os.getenv("EDNA_LOG_LEVEL", level)
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    log_file = os.getenv("EDNA_LOG_FILE", log_file) if log_file is None else log_file

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(logging.DEBUG)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.WARNING if quiet else level)
    console.setFormatter(logging.Formatter(CONSOLE_FORMAT))
    root.addHandler(console)

    if str(log_file).lower() not in ("-", "none", "off", "false"):
        path = resolve_path(log_file) if log_file else LOGS_DIR / f"{name}.log"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3,
                                                      encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(logging.Formatter(LOG_FORMAT))
            root.addHandler(fh)
        except OSError as e:  # read-only checkout etc. — never fatal
            console.handle(logging.makeLogRecord({
                "levelno": logging.WARNING, "levelname": "WARNING",
                "msg": f"could not open log file {path}: {e} (console logging only)"}))
            path = None
    else:
        path = None

    _COUNTER.warnings = _COUNTER.errors = 0
    root.addHandler(_COUNTER)
    for noisy in ("openpyxl", "urllib3", "matplotlib", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    log = logging.getLogger(name)
    if path:
        log.debug("logging to %s", path)
    return log


def log_summary(log: logging.Logger) -> int:
    """Log a one-line warning/error tally and return a process exit code
    (0 = clean, 0 = warnings only, 1 = errors)."""
    if _COUNTER.errors or _COUNTER.warnings:
        log.info("finished with %d error(s), %d warning(s) — see the log for details",
                 _COUNTER.errors, _COUNTER.warnings)
    else:
        log.info("finished cleanly")
    return 1 if _COUNTER.errors else 0


def add_logging_args(parser) -> None:
    """Standard ``--log-level / --log-file / --quiet`` flags for an argparse parser."""
    g = parser.add_argument_group("logging")
    g.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                   help="console verbosity (file log always captures DEBUG). Default: INFO")
    g.add_argument("--log-file", default=None,
                   help="log file path (default: logs/<tool>.log; '-' disables file logging)")
    g.add_argument("-q", "--quiet", action="store_true", help="only print warnings/errors")


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------
class InputError(Exception):
    """An input file/folder is missing or malformed. Raised by the ``require_*``
    helpers; callers decide whether it is fatal (CLI) or skippable (one file
    among many)."""


def is_temp_file(p: Path) -> bool:
    """Office lock files (``~$Book.xlsx``), macOS cruft and hidden files."""
    n = p.name
    return n.startswith("~$") or n.startswith("._") or n == ".DS_Store" or n.startswith(".")


def require_dir(p: Path | str | None, what: str = "directory") -> Path:
    if p is None:
        raise InputError(f"{what}: no path given")
    p = Path(p)
    if not p.exists():
        raise InputError(f"{what} not found: {p}")
    if not p.is_dir():
        raise InputError(f"{what} is not a directory: {p}")
    return p


def require_file(p: Path | str | None, what: str = "file",
                 extensions: Sequence[str] | None = None, allow_empty: bool = False) -> Path:
    """Raise ``InputError`` unless ``p`` is an existing, readable, non-empty
    file (optionally with one of ``extensions``)."""
    if p is None:
        raise InputError(f"{what}: no path given")
    p = Path(p)
    if not p.exists():
        raise InputError(f"{what} not found: {p}")
    if not p.is_file():
        raise InputError(f"{what} is not a file: {p}")
    if is_temp_file(p):
        raise InputError(f"{what} looks like an Office lock/temp file, not data: {p.name}")
    if extensions and not p.name.lower().endswith(tuple(e.lower() for e in extensions)):
        raise InputError(f"{what} has unexpected extension ({p.suffix or 'none'}); "
                         f"expected one of {', '.join(extensions)}: {p.name}")
    if not os.access(p, os.R_OK):
        raise InputError(f"{what} is not readable (permissions): {p}")
    if not allow_empty and p.stat().st_size == 0:
        raise InputError(f"{what} is empty (0 bytes): {p}")
    return p


def check_file(p, what="file", extensions=None, log: logging.Logger | None = None,
               level=logging.WARNING) -> Path | None:
    """Non-raising variant of :func:`require_file`: logs and returns ``None``."""
    try:
        return require_file(p, what, extensions)
    except InputError as e:
        (log or logging.getLogger("edna")).log(level, "%s", e)
        return None


def norm_col(v) -> str:
    """Header normalisation used everywhere: lowercase alphanumerics only."""
    return re.sub(r"[^a-z0-9]+", "", str(v).strip().lower())


def excel_sheet_names(path: Path) -> list[str]:
    """Sheet names of a workbook, raising ``InputError`` (not a zip/openpyxl
    traceback) when the file is corrupt, password-protected or not xlsx."""
    import openpyxl
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            return list(wb.sheetnames)
        finally:
            wb.close()
    except Exception as e:  # zipfile.BadZipFile, InvalidFileException, KeyError ...
        raise InputError(f"cannot open workbook {Path(path).name}: {type(e).__name__}: {e}") from e


def require_sheets(path: Path, sheets: Iterable[str], what: str = "workbook") -> list[str]:
    """Ensure every name in ``sheets`` exists (case-insensitive). Returns the
    workbook's sheet names."""
    names = excel_sheet_names(path)
    have = {n.lower() for n in names}
    missing = [s for s in sheets if s.lower() not in have]
    if missing:
        raise InputError(f"{what} {Path(path).name} is missing sheet(s) {missing}; "
                         f"found {names}")
    return names


def require_columns(columns: Iterable, required: Iterable[str], what: str = "table",
                    aliases: dict[str, Sequence[str]] | None = None) -> dict[str, str]:
    """Match ``required`` logical column names against ``columns`` using
    :func:`norm_col` (plus optional aliases). Returns {logical: actual}.
    Raises ``InputError`` listing every missing column."""
    norm = {norm_col(c): c for c in columns if c is not None}
    found, missing = {}, []
    for key in required:
        cands = [key] + list((aliases or {}).get(key, []))
        hit = next((norm[norm_col(c)] for c in cands if norm_col(c) in norm), None)
        if hit is None:
            missing.append(key)
        else:
            found[key] = hit
    if missing:
        raise InputError(f"{what}: missing required column(s) {missing}; "
                         f"columns present: {[str(c) for c in columns][:25]}")
    return found


@dataclass
class ValidationReport:
    """Collects problems found while checking inputs, so a ``validate`` run can
    list *everything* wrong in one pass instead of stopping at the first."""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checked: int = 0

    def error(self, msg: str):
        self.errors.append(msg)

    def warn(self, msg: str):
        self.warnings.append(msg)

    def ok(self) -> bool:
        return not self.errors

    def emit(self, log: logging.Logger, title: str = "validation") -> int:
        for w in self.warnings:
            log.warning("%s", w)
        for e in self.errors:
            log.error("%s", e)
        log.info("%s: %d item(s) checked, %d error(s), %d warning(s)",
                 title, self.checked, len(self.errors), len(self.warnings))
        return 0 if self.ok() else 1


def split_list(value: str | None) -> set[str]:
    """Comma-separated CLI value -> set of stripped, non-empty strings."""
    if not value:
        return set()
    return {x.strip() for x in str(value).split(",") if x.strip()}


def load_dotenv_if_present(path: Path | None = None) -> bool:
    """Load ``.env`` (repo root by default) if python-dotenv is installed.
    Real environment variables always win (override=False)."""
    path = path or REPO_ROOT / ".env"
    try:
        from dotenv import load_dotenv
    except ImportError:
        if path.exists():
            logging.getLogger("edna").warning(
                "%s exists but python-dotenv is not installed — it will be ignored "
                "(pip install python-dotenv)", rel(path))
        return False
    return bool(load_dotenv(path, override=False))


def pg_settings(log: logging.Logger | None = None) -> dict:
    """Postgres connection settings from PG* env vars. Defaults match
    ``deploy/compose.yml`` for local development only; a warning is logged when
    the default password is used so it is never silently relied on."""
    log = log or logging.getLogger("edna")
    cfg = {
        "dbname": os.getenv("PGDATABASE", "postgres"),
        "user": os.getenv("PGUSER", "postgres"),
        "password": os.getenv("PGPASSWORD"),
        "host": os.getenv("PGHOST", "127.0.0.1"),
        "port": os.getenv("PGPORT", "5432"),
        "connect_timeout": int(os.getenv("PGCONNECT_TIMEOUT", "10")),
    }
    if cfg["password"] is None:
        log.warning("PGPASSWORD is not set — falling back to the local docker-compose "
                    "default. Set it in .env for any shared database.")
        cfg["password"] = "postgres"
    try:
        int(cfg["port"])
    except ValueError:
        raise InputError(f"PGPORT must be an integer, got {cfg['port']!r}")
    return cfg
