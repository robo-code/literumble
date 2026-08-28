"""Gunicorn entrypoint for the non-App-Engine (SQLite) deploy.

    gunicorn wsgi_local:app -b 127.0.0.1:8081 --workers 1 --threads 16

MUST run with a single worker: the memcache dict, structures.global_dict, the
task-queue worker and the scheduler are all in-process singletons.
"""
import os

import gae_shim  # noqa: F401  MUST be first -- registers google.appengine.* in sys.modules

import main
import BatchRankings
import background

from flask import send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

app = main.app
_ROOT = os.path.dirname(os.path.abspath(__file__))

# Trust one proxy hop so remote_addr is the real client, not 127.0.0.1 -- otherwise
# is_current_user_admin() would treat every proxied request as admin. Safe only
# because nginx *overwrites* X-Forwarded-For (unspoofable).
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)


@app.before_request
def _drop_stale_snapshot():
    gae_shim.reset()


# /BatchRankings runs in the batchratings service on GAE (dispatch.yaml); here it
# is the same process, so the task worker can dispatch to it.
app.add_url_rule("/BatchRankings", "BatchRankings",
                 BatchRankings.batch_rankings, methods=["POST"])


# Static files App Engine served via app.yaml handlers. In production nginx
# serves these directly; these routes are the fallback for a plain local run.
_STATIC = {
    "/style.css": "style.css",
    "/robots.txt": "robots.txt",
    "/favicon.ico": "favicon.ico",
    "/ScoreExplanation": "ScoreExplanation.html",
    "/googlea1fa86d6b3ddcb15.html": "googlea1fa86d6b3ddcb15.html",
}
for _route, _fname in _STATIC.items():
    app.add_url_rule(_route, _route,
                     lambda fname=_fname: send_from_directory(_ROOT, fname))


@app.route("/flags/<path:filename>")
def _flags(filename):
    return send_from_directory(os.path.join(_ROOT, "flags"), filename)


background.start(app)
