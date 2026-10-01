#!/usr/bin/env bash
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
# Start the MBON Dashboard Flask API.
#   bash api/run.sh            (from anywhere)
# Settings come from api/.env (created from api/.env.example on first run) and
# the repo-root .env. With no database configured the API still serves the
# bundled example workbook (Sources tab -> "Example data").
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

command -v python3 >/dev/null 2>&1 || { echo "✖ python3 not found on PATH" >&2; exit 1; }
python3 - <<'PY' || { echo "✖ Python 3.9+ is required" >&2; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)
PY

if [ ! -f .env ]; then
  cp .env.example .env
  echo "ℹ  Created api/.env from api/.env.example — edit it to point at your Postgres."
fi

if [ ! -x venv/bin/python ]; then
  echo "→ Creating virtual environment (api/venv)…"
  python3 -m venv venv
fi

echo "→ Installing/upgrading dependencies…"
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt

PORT="$(grep -E '^API_PORT=' .env 2>/dev/null | cut -d= -f2- || true)"
echo "→ Starting MBON API on http://127.0.0.1:${PORT:-5050}  (logs: ../logs/api.log)"
exec ./venv/bin/python app.py
