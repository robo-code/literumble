#!/usr/bin/env python
import datetime
import time

from flask import request
from google.appengine.api import taskqueue

import store
import structures
from structures import load_blob


def formatSecs(secs):
    mins = int(round(secs / 60.0 - 0.49999))
    secs = int(round(secs % 60))
    hours = int(round(mins / 60.0 - 0.49999))
    mins = int(round(mins % 60))
    days = int(round(hours / 24.0 - 0.49999))
    timeSince = ""
    if days > 0:
        timeSince = str(days) + " day"
        if days != 1:
            timeSince += "s"
    elif hours > 0:
        timeSince = str(hours) + " hour"
        if hours != 1:
            timeSince += "s"
    else:
        timeSince = str(mins) + " minute"
        if mins != 1:
            timeSince += "s"
    return timeSince


def timeSince(timestring):
    t = datetime.datetime.strptime(timestring, "%Y-%m-%d %H:%M:%S")
    secs = (datetime.datetime.now() - t).total_seconds()
    return formatSecs(secs)


def rumble_stats():
    starttime = time.time()
    query = request.query_string.decode('utf-8').replace("%20", " ")
    parts = query.split("&")
    requests = {}
    if parts[0] != "":
        for pair in parts:
            ab = pair.split('=', 1)
            requests[ab[0]] = ab[1] if len(ab) > 1 else ""

    timing = bool(requests.get("timing", False))
    dark = requests.get("theme", "") == "dark"

    extraArgs = ""
    if dark:
        extraArgs += "&amp;theme=dark"

    outstr = None
    if outstr is None:
        tq = taskqueue.Queue()
        tqs_r = tq.fetch_statistics_async()
        out = []
        out.append(structures.header("Statistics", structures.home_link("LiteRumble", dark) + " Statistics", dark))
        out.append("\nStats generated: " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " UTC")
        out.append("\nAllowed Robocode versions: " + ", ".join(structures.allowed_clients) + "\n<br><br>\n")
        q = structures.Rumble.all()
        rumbles = [[], [], []]
        categories = ["1v1", "Melee", "Teams"]

        for r in q.run():
            if r.Melee:
                rumbles[1].append(r)
            elif r.Teams:
                rumbles[2].append(r)
            else:
                rumbles[0].append(r)

        for cat, rumbs in zip(categories, rumbles):
            entries_by_id = {}
            for r in rumbs:
                scores = store.load_scores(r.Name)
                if not isinstance(scores, dict):
                    scores = {}
                entries = len(scores)
                if r.LastUpload is None:
                    latest = None
                    for s in scores.values():
                        t = s.LastUpload
                        if latest is None or t > latest:
                            latest = t
                    r.LastUpload = latest

                entries_by_id[id(r)] = entries
            rumbs.sort(key=lambda r: -entries_by_id[id(r)])

            out.append("<table class=\"rumble\">\n<tr>")
            out.append("<th>" + cat + "</th>\n<th>Participants</th>\n<th>Total Uploads</th>\n<th>Last Upload</th></tr>")

            for i, r in enumerate(rumbs):
                game = r.Name
                gameHref = "<a href=\"Rankings?game=" + game + extraArgs + "\" ><b>" + game + "</b></a>"

                lastTimeSince = timeSince(r.LastUpload) + " ago"
                out.append("\n<tr>\n<td>" + gameHref + "</td>\n<th>" + str(entries_by_id[id(r)]) + "</th>\n<th>")
                out.append(str(r.TotalUploads) + "</th><th>" + lastTimeSince + "</th>\n</tr>")
                uploaders = load_blob(r.Uploaders, {})
                if not isinstance(uploaders, dict) or len(uploaders) == 0:
                    uploaders = {}

                uv = list(uploaders.values())
                cutoff = datetime.datetime.now() - datetime.timedelta(31)
                uv = [u for u in uv if datetime.datetime.strptime(u.latest, "%Y-%m-%d %H:%M:%S") > cutoff]
                uv.sort(key=lambda u: u.latest, reverse=True)
                for j, u in enumerate(uv):
                    out.append("\n<tr><td></td><td>")
                    name = u.name
                    if name == "Put_Your_Name_Here":
                        name = "Anonymous"

                    out.append(name)
                    out.append("</td><td>")
                    out.append(str(u.total))
                    out.append("</td><td>")
                    out.append(timeSince(u.latest) + " ago")
                    out.append("</td></tr>")

            for r in rumbs:
                r.__dict__.pop("entries", 1)
            out.append("\n</table>")
        tqs = tqs_r.get_result()
        tasks = tqs.tasks
        last_min = tqs.executed_last_minute
        if last_min is None or last_min == 0:
            last_min = 1
        if tasks is None:
            tasks = 0
        backlog = float(tasks) * 60.0 / last_min

        tq_string = "<table>\n<tr><th colspan=\"2\">Upload Queue</th></tr>\n<tr><td>Projected Processing Time</td><td>" + formatSecs(backlog) + "</td></tr>"
        tq_string += "\n<tr><td>Current Size</td><td>" + str(tasks) + " pairing"
        if tasks != 1:
            tq_string += "s"
        tq_string += "</td></tr>\n</table>\n<br>"
        out.insert(2, tq_string)
        outstr = "".join(out)

    elapsed = time.time() - starttime
    if timing:
        outstr += "<br>\n Page served in " + str(int(round(elapsed * 1000))) + "ms."
    outstr += "</body></html>"

    return outstr
