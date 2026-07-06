"""In-process replacements for App Engine's task queue and cron.

Started once from ``wsgi_local`` with the Flask app. Runs:
  * one worker thread draining the task FIFO strictly serially (mirrors the
    GAE ``default`` queue's ``max_concurrent_requests: 1`` that the upload math
    relies on),
  * one scheduler thread firing the ``cron.yaml`` jobs on their intervals.

Both dispatch by building a Flask request context and calling the handler
directly (no real HTTP). ``drain()`` and ``tick()`` let the test suite advance
the system deterministically instead of sleeping.
"""
import logging
import threading
import time

import gae_shim
import BatchRankings
import FetchParseFlags
import HandleQueuedResults
import RumbleSelect
import RumbleStats

_log = logging.getLogger("gunicorn.error")  # INFO-level, wired to journald

_app = None
_started = False

# task url -> handler (raw functions; the decorators' header checks are bypassed
# because we invoke in-process and set X-Appengine-TaskName ourselves)
_TASK_HANDLERS = {
    "/HandleQueuedResults": HandleQueuedResults.handle_queued_results,
    "/BatchRankings": BatchRankings.batch_rankings,
}

# scheduled jobs: (interval_seconds, path, handler). Path carries query args the
# handler reads via request.query_string. A single scheduler thread runs these
# serially, so no cross-job staggering is needed.
_SCHEDULE = [
    (60, "/", RumbleSelect.rumble_select),
    (60, "/?theme=dark", RumbleSelect.rumble_select),
    (600, "/RumbleStats?regen=1", RumbleStats.rumble_stats),
    (600, "/RumbleStats?regen=1&theme=dark", RumbleStats.rumble_stats),
    (7200, "/FetchParseFlags", FetchParseFlags.fetch_parse_flags),
    (3600, "/QueueHourlyBatchRankings", BatchRankings.queue_hourly_batch_rankings),
]
_JOBS_BY_PATH = {path: fn for _, path, fn in _SCHEDULE}


def _run(path, fn, method="GET", data=None, headers=None):
    with _app.test_request_context(path=path, method=method, data=data,
                                   headers=headers or {}):
        return fn()


def _worker():
    q = gae_shim._task_queue
    while True:
        url, payload, headers = q.get()
        start = time.monotonic()
        status = "ok"
        try:
            fn = _TASK_HANDLERS.get(url)
            if fn is None:
                _log.error("no local handler for task url " + url)
                status = "no-handler"
            else:
                headers = {**headers, "X-Appengine-TaskName": "local"}
                status = "error"
                for attempt in range(3):
                    try:
                        _run(url, fn, method="POST", data=payload, headers=headers)
                        status = "ok"
                        break
                    except Exception:
                        _log.exception("task " + url + " failed (attempt " + str(attempt + 1) + ")")
        finally:
            gae_shim._record_execution()
            q.task_done()
            _log.info("task %s %s %.0fms qlen=%d", url, status,
                      (time.monotonic() - start) * 1000, q.qsize())


def _scheduler():
    next_run = {path: time.monotonic() for _, path, _ in _SCHEDULE}  # first run at startup
    while True:
        now = time.monotonic()
        for interval, path, fn in _SCHEDULE:
            if now >= next_run[path]:
                next_run[path] = now + interval
                try:
                    _run(path, fn)
                except Exception:
                    logging.exception("scheduled job " + path + " failed")
        time.sleep(1)


def start(app):
    """Start the worker + scheduler once. Idempotent."""
    global _app, _started
    _app = app
    if _started:
        return
    _started = True
    threading.Thread(target=_worker, name="taskqueue-worker", daemon=True).start()
    threading.Thread(target=_scheduler, name="cron-scheduler", daemon=True).start()


# deterministic hooks for tests
def drain(timeout=30):
    """Block until the task queue is empty and the worker is idle."""
    q = gae_shim._task_queue
    deadline = time.time() + timeout
    while q.unfinished_tasks and time.time() < deadline:
        time.sleep(0.01)
    if q.unfinished_tasks:
        raise TimeoutError("task queue did not drain within " + str(timeout) + "s")


def tick(path):
    """Run a single scheduled job synchronously, by its path."""
    fn = _JOBS_BY_PATH.get(path)
    if fn is None:
        raise KeyError("no scheduled job for " + path)
    return _run(path, fn)
