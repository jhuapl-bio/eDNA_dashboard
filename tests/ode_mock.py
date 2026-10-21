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
"""Offline stand-in for the Ocean DNA Explorer API + its file store, serving
the SYNTHETIC projects in tests/fixtures/ode/ (written by tests/make_ode_fixtures.py). Used by test_ode_client.py and
by the headless UI check:

    python tests/ode_mock.py --port 5099        # http://127.0.0.1:5099/api/project ...
"""
import json
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FIX = Path(__file__).resolve().parent / "fixtures" / "ode"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ode_client as oc  # noqa: E402


class MockODE:
    def __init__(self, port=0, slow=0.0):
        data = json.loads((FIX / "api.json").read_text(encoding="utf-8"))
        self.projects, self.analyses, self.fmap = data["projects"], data["analyses"], data["files"]
        self.fail_files = set()          # file basenames answered with 404
        self.fail_api = False            # every /api call -> 500
        self.slow = slow                 # seconds per 16 KB chunk (spinner testing)
        self.hits = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self._samples = None

    # ---- data
    def url(self, u):
        return u.replace("{FILES}", self.base + "/files")

    def _rec(self, r):
        return {k: (self.url(v) if isinstance(v, str) else v) for k, v in r.items()}

    def sample_rows(self):
        if self._samples is None:
            out = []
            for p in self.projects:
                name = p["sampleMetadataFileUrl_ODE"].rsplit("/", 1)[-1]
                txt = (FIX / "files" / self.fmap[name]).read_text(encoding="utf-8")
                for r in oc.records(txt):
                    out.append({"id": len(out) + 1, "project_id": p["project_id"], "samp_name": r.get("samp_name"),
                                "decimalLatitude": oc._float(r.get("decimalLatitude")),
                                "decimalLongitude": oc._float(r.get("decimalLongitude")),
                                "eventDate": r.get("eventDate"), "station_id": r.get("station_id") or None,
                                "geo_loc_name": r.get("geo_loc_name"), "samp_category": r.get("samp_category"),
                                "expedition_id": r.get("expedition_id")})
            self._samples = out
        return self._samples

    def api(self, table, q):
        g = lambda k: (q.get(k) or [None])[0]
        if table == "tables":
            return ["Project", "Sample", "Library", "Analysis", "Occurrence", "Assignment", "Feature", "Taxonomy"]
        if table == "project":
            rows = [self._rec(p) for p in self.projects]
            if g("project_id"):
                rows = [p for p in rows if g("project_id").lower() in p["project_id"].lower()]
            return rows
        if table == "analysis":
            rows = [self._rec(a) for a in self.analyses]
            if g("project_id"):
                rows = [a for a in rows if g("project_id").lower() in a["project_id"].lower()]
            return rows
        if table == "sample":
            rows = list(self.sample_rows())
            if g("search"):
                if any(k for k in q if k not in ("search", "fields", "limit", "orderBy", "page", "trusted")):
                    raise ValueError("Search may not include other filter parameters.")
                s = g("search").lower()
                rows = [r for r in rows if any(s in str(v).lower() for v in r.values() if isinstance(v, str))]
            if g("advanced"):
                adv = json.loads(g("advanced"))
                for cond in adv:
                    conds = cond if isinstance(cond[0], list) else [cond]
                    rows = [r for r in rows if any(
                        (c[1] == "equals" and str(r.get(c[0])) == c[2]) or
                        (c[1] == "contains" and str(c[2]).lower() in str(r.get(c[0]) or "").lower())
                        for c in conds)]
            if g("project_id"):
                rows = [r for r in rows if g("project_id").lower() in r["project_id"].lower()]
            lim, page = int(g("limit") or 10 ** 6), int(g("page") or 1)
            return rows[(page - 1) * lim: page * lim]
        raise KeyError(table)

    # ---- http
    def _handler(self):
        mock = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, st, body, ctype="application/json", cors=False):
                self.send_response(st)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                if cors:
                    self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                if mock.slow and len(body) > 1 << 14:
                    for i in range(0, len(body), 1 << 14):
                        self.wfile.write(body[i:i + (1 << 14)])
                        self.wfile.flush()
                        time.sleep(mock.slow)
                else:
                    self.wfile.write(body)

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                mock.hits.append(u.path)
                if u.path.startswith("/__fail/"):            # test control: 404 this file from now on
                    mock.fail_files.add(u.path[len("/__fail/"):])
                    return self._send(200, b"ok", "text/plain", cors=True)
                if u.path == "/__reset":
                    mock.fail_files.clear()
                    mock.fail_api = False
                    return self._send(200, b"ok", "text/plain", cors=True)
                if u.path.startswith("/files/"):
                    name = u.path[len("/files/"):]
                    safe = mock.fmap.get(name) or mock.fmap.get(urllib.parse.unquote(name))
                    if not safe or name in mock.fail_files:
                        return self._send(404, b"The requested blob does not exist", "text/plain", cors=True)
                    return self._send(200, (FIX / "files" / safe).read_bytes(),
                                      "text/tab-separated-values", cors=True)
                m = re.match(r"^/api/(\w+)$", u.path)
                if not m:
                    return self._send(404, b'{"statusMessage":"error","error":"not found"}')
                if mock.fail_api:
                    return self._send(500, b'{"statusMessage":"error","error":"Server error"}')
                try:
                    res = mock.api(m.group(1), urllib.parse.parse_qs(u.query))
                except ValueError as e:
                    return self._send(400, json.dumps({"statusMessage": "error", "error": str(e)}).encode())
                except KeyError:
                    return self._send(400, b'{"statusMessage":"error","error":"Invalid table"}')
                return self._send(200, json.dumps({"statusMessage": "success", "result": res}).encode())
        return H

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5099)
    ap.add_argument("--slow", type=float, default=0.0)
    a = ap.parse_args()
    m = MockODE(a.port, a.slow)
    print(f"mock ODE on {m.base}", flush=True)
    m.httpd.serve_forever()
