#!/usr/bin/env python
import time

from flask import request

import store
import structures
from structures import load_blob


def rumble_select():
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
    if timing:
        extraArgs += "&amp;timing=1"
    if dark:
        extraArgs += "&amp;theme=dark"

    outstr = None
    if outstr is None:
        out = []
        out.append(structures.header("Home", "LiteRumble - Home", dark))

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
            for r in rumbs:
                scoresdicts = store.load_scores(r.Name)
                entries = len(scoresdicts) if hasattr(scoresdicts, "__len__") else len(r.Participants)
                r.__dict__["entries"] = entries
            rumbs.sort(key=lambda r: -r.__dict__["entries"])

            out.append("<table class=\"rumble\">\n<tr>")
            out.append("\n<th colspan=\"2\">" + cat + "</th>\n<th>Participants</th>\n</tr>")

            for i, r in enumerate(rumbs):
                game = r.Name
                gameHref = "<a href=\"Rankings?game=" + game + extraArgs + "\" >" + game + "</a>"
                topHref = "<a href=\"Rankings?game=" + game + "&amp;limit=50" + extraArgs + "\" >top 50</a>"
                out.append("\n<tr>\n<td>" + gameHref + "</td>\n<td>" + topHref + "</td>\n<td>")
                out.append(str(r.__dict__["entries"]) + "</td>\n</tr>")
                r.__dict__.pop("entries", 1)

            out.append("</table>")

        # extraArgs is prefixed with "&amp;" to chain onto an existing query string;
        # these links have no base param, so start a fresh "?" query instead.
        baseArgs = ("?" + extraArgs.replace("&amp;", "", 1)) if extraArgs else ""
        out.append("<table><tr><td><b><a href=\"RumbleStats" + baseArgs + "\">LiteRumble Statistics</a></b></td></tr>")
        out.append("<tr><td><b><a href=\"ScoreExplanation\">Score Explanation</a></b></td></tr></table>")
        out.append("<br>Learn more about Robocode at <a href=\"http://robowiki.net\">Robowiki.net</a>")

        if dark:
            out.append("<br><a href=\"?theme=light\">Light mode</a>")
        else:
            out.append("<br><a href=\"?theme=dark\">Dark mode</a>")

        outstr = "".join(out)

    elapsed = time.time() - starttime
    if timing:
        outstr += "<br>\n Page served in " + str(int(round(elapsed * 1000))) + "ms."

    outstr += "</body></html>"
    return outstr
