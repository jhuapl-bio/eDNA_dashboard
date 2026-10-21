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
Ocean DNA Explorer bridge for the Flask API: mounts ode_client.BridgeAPI at
/api/ode/* so the report's "Import from Ocean DNA Explorer" dialog can use
`bash api/run.sh` (port 5050) as well as the standalone
`python ode_client.py serve` (port 5051). See ode_client.py for the routes.
"""

import logging
import os
import sys

from flask import Blueprint, Response, request

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import ode_client as oc  # noqa: E402

log = logging.getLogger("mbon.api.ode")
ode_bp = Blueprint("ode", __name__, url_prefix="/api/ode")
_api = None


def _bridge():
    global _api
    if _api is None:
        _api = oc.BridgeAPI(oc.ODEClient(), oc.DEFAULT_PROJECTS)
        log.info("Ocean DNA Explorer bridge ready (upstream %s, cache %s)",
                 _api.client.base, _api.client.cache_dir)
    return _api


@ode_bp.route("", defaults={"sub": ""}, methods=["GET", "POST", "DELETE"])
@ode_bp.route("/<path:sub>", methods=["GET", "POST", "DELETE"])
def ode_bridge(sub):
    status, ctype, data = _bridge().handle(request.method, "/api/ode/" + sub,
                                           request.args.to_dict(flat=False),
                                           request.get_data(), request.headers.get("Origin"))
    return Response(data, status=status, content_type=ctype)


@ode_bp.after_request
def _private_network(resp):
    # Chrome's Private Network Access preflight (a hosted report calling 127.0.0.1)
    resp.headers["Access-Control-Allow-Private-Network"] = "true"
    return resp
