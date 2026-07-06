import os
import urllib.error
import urllib.request

from flask import Flask, abort, redirect, request
from flask_compress import Compress
from google.appengine.api import wrap_wsgi_app

import BatchRankings
import BotCompare
import BotDetails
import FetchParseFlags
import HandleQueuedResults
import Rankings
import RatingsFile
import RemoveOldParticipant
import RumbleSelect
import RumbleStats
import UploadedResults
from auth import require_admin, require_cron, require_task

app = Flask(__name__)
app.wsgi_app = wrap_wsgi_app(app.wsgi_app)

Compress(app)

# On App Engine (REDIRECT_TO set in app.yaml) send everything to the new host.
# Unset on the SQLite server, so the same code is a no-op there -- no loop.
# POST uploads can't follow a 302 (Java HttpURLConnection won't redirect POST),
# so those paths are proxied through to the new host instead.
_REDIRECT_TO = os.environ.get("REDIRECT_TO")
_PROXY_PATHS = {"/UploadedResults", "/RemoveOldParticipant"}


@app.before_request
def redirect_or_proxy():
    if not _REDIRECT_TO:
        return None
    target = _REDIRECT_TO + request.path
    if request.query_string:
        target += "?" + request.query_string.decode()
    if request.path not in _PROXY_PATHS:
        return redirect(target, code=302)
    headers = {}
    if request.content_type:
        headers["Content-Type"] = request.content_type
    req = urllib.request.Request(
        target, data=request.get_data(), method=request.method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read(), r.status, {"Content-Type": r.headers.get_content_type()}
    except urllib.error.HTTPError as e:
        return e.read(), e.code
    except urllib.error.URLError:
        return b"upstream unavailable", 502

BLOCKED_USER_AGENTS = (
    "meta-externalagent",
    "meta-externalfetcher",
    "facebookexternalhit",
    "gptbot",
    "oai-searchbot",
    "chatgpt-user",
    "claudebot",
    "anthropic-ai",
    "ccbot",
    "bytespider",
    "amazonbot",
    "petalbot",
)


@app.before_request
def block_scrapers():
    ua = (request.headers.get("User-Agent") or "").lower()
    if any(bot in ua for bot in BLOCKED_USER_AGENTS):
        abort(410)
    # BotCompare is O(n^2) and disallowed in robots.txt
    if request.path == "/BotCompare" and "bot" in ua:
        abort(410)


app.add_url_rule("/", view_func=RumbleSelect.rumble_select, methods=["GET"])
app.add_url_rule("/RumbleSelect", view_func=RumbleSelect.rumble_select, methods=["GET"])
app.add_url_rule("/Rankings", view_func=Rankings.rankings, methods=["GET"])
app.add_url_rule("/RatingsFile", view_func=RatingsFile.ratings_file, methods=["GET"])
app.add_url_rule("/BotDetails", view_func=BotDetails.bot_details, methods=["GET"])
app.add_url_rule("/BotCompare", view_func=BotCompare.bot_compare, methods=["GET"])
app.add_url_rule("/RumbleStats", view_func=RumbleStats.rumble_stats, methods=["GET"])
app.add_url_rule("/UploadedResults", view_func=UploadedResults.uploaded_results, methods=["POST"])

app.add_url_rule(
    "/FetchParseFlags",
    view_func=require_cron(FetchParseFlags.fetch_parse_flags),
    methods=["GET"],
)
app.add_url_rule(
    "/HandleQueuedResults",
    view_func=require_task(HandleQueuedResults.handle_queued_results),
    methods=["POST"],
)
app.add_url_rule(
    "/RemoveOldParticipant",
    view_func=RemoveOldParticipant.remove_old_participant,
    methods=["GET", "POST"],
)
app.add_url_rule(
    "/QueueBatchRankings",
    view_func=require_admin(BatchRankings.queue_batch_rankings),
    methods=["GET"],
)
app.add_url_rule(
    "/QueueHourlyBatchRankings",
    view_func=require_cron(BatchRankings.queue_hourly_batch_rankings),
    methods=["GET"],
)
