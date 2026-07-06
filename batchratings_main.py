from flask import Flask
from google.appengine.api import wrap_wsgi_app

import BatchRankings
from auth import require_task

app = Flask(__name__)
app.wsgi_app = wrap_wsgi_app(app.wsgi_app)

app.add_url_rule(
    "/BatchRankings",
    view_func=require_task(BatchRankings.batch_rankings),
    methods=["POST"],
)
app.add_url_rule("/_ah/start", view_func=BatchRankings.start_backend, methods=["GET"])
app.add_url_rule("/_ah/warmup", view_func=BatchRankings.start_backend, methods=["GET"])
