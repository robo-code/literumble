#!/usr/bin/env python
import datetime
import time
from operator import attrgetter

from flask import Response, request

import store
import structures
from structures import global_dict, load_blob


_SORT_ATTRS = {"Latest Battle": "LastUpload", "Opponent APS": "OppAPS", "Opponent Survival": "OppSurvival"}


def _retired_score(game, name, pairings):
    """A LiteBot for a bot whose RumbleScore row is gone, or None if it never had
    one here. Every field is the same aggregate over live pairings that
    BatchRankings would have written; the batch-only figures (ANPP, Vote, APS_CI)
    have no pairing to come from and stay at their unset -1."""
    if not pairings:
        return None
    apss = [float(p.APS) for p in pairings]
    return structures.LiteBot(loadDict={
        "Name": name,
        "Rumble": game,
        "Active": False,
        "Pairings": len(pairings),
        "Battles": sum(int(p.Battles) for p in pairings),
        "APS": sum(apss) / len(apss),
        "Survival": sum(float(p.Survival) for p in pairings) / len(pairings),
        "PL": sum((a > 50.0) - (a < 50.0) for a in apss),
        "LastUpload": max(str(p.LastUpload) for p in pairings),
        "ANPP": -1.0,
        "VoteScore": -1.0,
        "APS_CI": -1.0,
        "Uploaders": [],
    })


def bot_details():
    starttime = time.time()
    query = request.query_string.decode('utf-8').replace("%20", " ")
    parts = query.split("&")
    requests = {}
    if parts[0] != "":
        for pair in parts:
            ab = pair.split('=', 1)
            requests[ab[0]] = ab[1] if len(ab) > 1 else ""

    game = requests.get("game")
    if game is None:
        return "ERROR: RUMBLE NOT SPECIFIED IN FORMAT game=____"

    name = requests.get("name", None)
    if name is None:
        return "ERROR: BOT NOT SPECIFIED IN FORMAT name=____"

    lim = int(requests.get("limit", "10000000"))
    order = requests.get("order", None)
    timing = bool(requests.get("timing", False))
    api = bool(requests.get("api", False))
    dark = requests.get("theme", "") == "dark"

    extraArgs = ""
    if timing:
        extraArgs += "&amp;timing=1"
    if dark:
        extraArgs += "&amp;theme=dark"
    reverseSort = True

    if api or order is None or order.replace(" ", "").lstrip("-") == "":
        order = "Name"
        reverseSort = False
    elif order[0] == "-":
        order = order[1:]
        reverseSort = False

    order = _SORT_ATTRS.get(order, order)

    parsetime = time.time() - starttime

    cached = False
    bot = store.load_score(game, name)

    # A retired bot keeps its Pairing rows but loses its RumbleScore row, so its
    # own columns have to come back off the pairings that outlived it. Loaded
    # here rather than below so the retired path doesn't read them twice.
    retiredPairings = None
    if bot is None:
        retiredPairings = store.load_pairings_alive(game, name)
        bot = _retired_score(game, name, retiredPairings)

    rumble = None
    if not api:
        rumble = global_dict.get(game, None)
        if rumble is None:
            rumble = structures.Rumble.get_by_key_name(game)
            if rumble is None:
                return "RUMBLE NOT FOUND"
            global_dict[game] = rumble

    if bot is None:
        return "ERROR. name/game combination does not exist: " + name + "/" + game

    flagmap = global_dict.get(structures.default_flag_map)
    if flagmap is None:
        flagmapholder = structures.FlagMap.get_by_key_name(structures.default_flag_map)
        if flagmapholder is not None:
            flagmap = flagmapholder.InternalMap
            global_dict[structures.default_flag_map] = flagmap

    flagmap = load_blob(flagmap, {})
    if not isinstance(flagmap, dict):
        flagmap = {}

    bots = None
    if lim > 0 or not api:
        # join to RumbleScore: only opponents still in the rumble (dead pairings are
        # kept for history but not shown), each carrying the opponent's OppAPS/OppSurvival.
        bots = (retiredPairings if retiredPairings is not None
                else store.load_pairings_alive(game, name))

        removes = []
        for b in bots:
            try:
                _ = b.LastUpload
            except AttributeError:
                b.LastUpload = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            package = b.Name.split(".")[0]
            if package in flagmap:
                b.Flag = flagmap[package]
            else:
                b.Flag = "NONE"
            try:
                b.APS = float(b.APS)
                b.KNNPBI = float(b.KNNPBI)
                b.NPP = float(b.NPP)
                b.Battles = int(b.Battles)
            except (TypeError, ValueError, AttributeError):
                removes.append(b)
        for b in removes:
            bots.pop(bots.index(b))

    package = bot.Name.split(".")[0]
    if package in flagmap:
        bot.Flag = flagmap[package]
    else:
        bot.Flag = "NONE"

    retrievetime = time.time() - parsetime - starttime

    if lim > 0:
        # load_pairings_alive already returns only live opponents
        if order == "APS CI":
            ci = structures.pairing_ci
            bots = sorted(bots, key=lambda b: ci(b) if ci(b) is not None else -1.0, reverse=reverseSort)
        else:
            if bots and order not in bots[0].__dict__:
                return Response("ERROR: UNKNOWN order=" + order, status=400, mimetype="text/plain")
            bots = sorted(bots, key=attrgetter(order), reverse=reverseSort)

    if api:
        outs = ["{"]
        outs.append("\n\"name\":\"")
        outs.append(name)
        outs.append("\",\n\"flag\":\"")
        outs.append(bot.Flag)
        outs.append("\",\n\"APS\":")
        outs.append(structures.fmt(bot.APS))
        outs.append(",\n\"APS_CI\":")
        outs.append(structures.fmt(getattr(bot, "APS_CI", -1.0)))
        outs.append(",\n\"PWIN\":")
        outs.append(structures.fmt(50.0 * float(bot.PL) / bot.Pairings + 50.0))
        outs.append(",\n\"ANPP\":")
        outs.append(structures.fmt(bot.ANPP))
        outs.append(",\n\"vote\":")
        outs.append(structures.fmt(bot.VoteScore))
        outs.append(",\n\"survival\":")
        outs.append(structures.fmt(bot.Survival))
        outs.append(",\n\"pairings\":")
        outs.append(str(bot.Pairings))
        outs.append(",\n\"battles\":")
        outs.append(str(bot.Battles))
        outs.append(",\n\"latest\":\"")
        outs.append(str(bot.LastUpload))
        outs.append("\"")
        if lim > 0:
            outs.append(",\n\"pairingsList\":[\n")
            headings = [
                "\"name\"",
                "\"flag\"",
                "\"rank\"",
                "\"APS\"",
                "\"APS_CI\"",
                "\"NPP\"",
                "\"survival\"",
                "\"KNNPBI\"",
                "\"battles\"",
                "\"latest\"",
                "\"oppAPS\"",
                "\"oppSurvival\""]
            escapes = ["\"", "\"", "", "", "", "", "", "", "", "\"", "", ""]
            count = 0
            for b in bots:
                count += 1
                if count > lim:
                    break

                bci = structures.pairing_ci(b)

                cells = [b.Name,
                         b.Flag,
                         count,
                         b.APS,
                         -1.0 if bci is None else bci,
                         b.NPP,
                         b.Survival,
                         b.KNNPBI,
                         b.Battles, b.LastUpload,
                         float(b.OppAPS), float(b.OppSurvival)]

                outs.append("    {")
                for i in range(len(cells)):
                    outs.append(headings[i])
                    outs.append(":")
                    outs.append(escapes[i])
                    outs.append(structures.fmt(cells[i]))
                    outs.append(escapes[i])
                    outs.append(",")
                outs[-1] = "},\n"
            outs[-1] = "}\n]"
        outs.append("\n}")
        return Response("".join(outs), mimetype="application/json")

    sorttime = time.time() - retrievetime - parsetime - starttime
    order = {v: k for k, v in _SORT_ATTRS.items()}.get(order, order)

    out = []

    gameHref = "<a href=\"Rankings?game=" + game + extraArgs + "\">" + game + "</a>"
    gameTitle = "Bot details of <b>" + name + "</b> in " + gameHref + " vs. " + str(len(bots)) + " bots."

    flagtag = "<img id='flag' src=\"/flags/" + bot.Flag + ".gif\">  " + structures.country_lookup[bot.Flag]
    endName = name.split(" ")[0].split(".")[-1]
    out.append(structures.header(endName + " in " + game, gameTitle, dark))
    out.append("<table>\n")
    wikiurl = "<a href=\"https://robowiki.net/wiki/{identifier}\">{name}</a>" \
        .format(identifier=name.split(" ")[0].split(".")[-1], name=name)
    out.append("<tr>\n<th>Name</th>\n<td>\n" + wikiurl + "</td>\n<th>APS</th><th>Survival</th><th>KNNPBI</th></tr>")
    out.append("<tr>\n<th>Flag</th>\n<td>\n" + flagtag + "</td>")
    out.append("<td rowspan=\"11\"><canvas id=\"distAPS\" width=\"231\" height=\"231\" style=\"border: black 1px solid;\"></canvas></td>")
    out.append("<td rowspan=\"11\"><canvas id=\"distSurvival\" width=\"231\" height=\"231\" style=\"border: black 1px solid;\"></canvas></td>")
    out.append("<td rowspan=\"11\"><canvas id=\"distKNNPBI\" width=\"231\" height=\"231\" style=\"border: black 1px solid;\"></canvas></td>")

    out.append("</td></tr>")
    out.append("<tr>\n<th>APS</th>\n<td>\n" + structures.fmt(bot.APS) + "</td></tr>")
    botCI = getattr(bot, "APS_CI", -1.0)
    botCIStr = "n/a" if (botCI is None or botCI < 0) else "&plusmn;" + structures.fmt(botCI)
    out.append("<tr>\n<th>APS CI</th>\n<td>\n" + botCIStr + "</td></tr>")
    out.append("<tr>\n<th>PWIN</th>\n<td>\n" + structures.fmt(50.0 * float(bot.PL) / bot.Pairings + 50.0) + "</td></tr>")
    out.append("<tr>\n<th>ANPP</th>\n<td>\n" + structures.fmt(bot.ANPP) + "</td></tr>")
    out.append("<tr>\n<th>Vote</th>\n<td>\n" + structures.fmt(bot.VoteScore) + "</td></tr>")
    out.append("<tr>\n<th>Survival</th>\n<td>\n" + structures.fmt(bot.Survival) + "</td></tr>")
    out.append("<tr>\n<th>Pairings</th>\n<td>\n" + str(bot.Pairings) + "</td></tr>")
    out.append("<tr>\n<th>Battles</th>\n<td>\n" + str(bot.Battles) + "</td></tr>")
    out.append("<tr>\n<th>Latest Battle</th>\n<td>\n" + str(bot.LastUpload) + " UTC</td></tr>")
    out.append("<tr>\n<td colspan=\"2\">")
    out.append("<form name=\"input\" action=\"BotCompare\" method=\"get\">")
    out.append("<input type=\"hidden\" name=\"game\" value=\"" + game + "\" />")
    out.append("<input type=\"hidden\" name=\"bota\" value=\"" + name + "\" />")
    out.append("<input type=\"text\" name=\"botb\" value=\"" + name + "\" />")
    if dark:
        out.append("<input type=\"hidden\" name=\"theme\" value=\"dark\" />")
    out.append("<input type=\"submit\" value=\"Compare\" /></form>")
    out.append("</td></tr></table>")

    if lim > 0:
        out.append("\n<table>\n<tr>\n")
        headings = ["  ",
                    "Flag",
                    "Name",
                    "",
                    "APS",
                    "APS CI",
                    "NPP",
                    "Survival",
                    "KNNPBI",
                    "Battles",
                    "Latest Battle",
                    "Opponent APS",
                    "Opponent Survival"]

        for heading in headings:
            sortedBy = (order == heading)
            headinglink = heading
            if sortedBy and reverseSort:
                heading = "-" + heading
                headinglink = heading
            elif not sortedBy:
                headinglink = "-" + headinglink

            orderHref = "<a href=\"BotDetails?game=" + game + "&amp;name=" + name.replace(" ", "%20") + "&amp;order=" + headinglink.replace(" ", "%20") + extraArgs + "\">" + heading + "</a>"
            if sortedBy:
                out.append("\n<th class=\"sortedby\">" + orderHref + "</th>")
            else:
                out.append("\n<th>" + orderHref + "</th>")
        out.append("\n</tr>")
        rank = 0
        highlightKey = [False, False, False, False, True, False, True, True, True, False, False, True, True]
        mins = [0, 0, 0, 0, 40, 0, 40, 40, -0.1, 0, 0, 40, 40]
        maxs = [0, 0, 0, 0, 60, 0, 70, 60, 0.1, 0, 0, 60, 60]
        for b in bots:
            rank += 1
            if rank > lim:
                break

            botName = b.Name
            botNameHref = "<a href=\"BotDetails?game=" + game + "&amp;name=" + botName.replace(" ", "%20") + extraArgs + "\">" + botName + " </a>"
            compareHref = "<a rel=\"nofollow\" href=\"BotCompare?game=" + game + "&amp;bota=" + name.replace(" ", "%20") + "&amp;botb=" + botName.replace(" ", "%20") + extraArgs + "\">compare</a>"
            ft = []
            ft.append("<img id='flag' src=\"/flags/")
            ft.append(b.Flag)
            ft.append(".gif\">")
            flagtag2 = "".join(ft)

            bci = structures.pairing_ci(b)
            bciStr = "n/a" if bci is None else "&plusmn;" + structures.fmt(bci)

            cells = [str(rank),
                     flagtag2,
                     botNameHref,
                     compareHref,
                     b.APS,
                     bciStr,
                     b.NPP,
                     b.Survival,
                     b.KNNPBI,
                     b.Battles,
                     b.LastUpload,
                     b.OppAPS,
                     b.OppSurvival]

            out.append("\n<tr>")
            for i, cell in enumerate(cells):
                if highlightKey[i]:
                    if cell < mins[i]:
                        out.append("\n<td class=\"red\">" + structures.fmt(cell) + "</td>")
                    elif cell > maxs[i]:
                        out.append("\n<td class=\"green\">" + structures.fmt(cell) + "</td>")
                    else:
                        out.append("\n<td>" + structures.fmt(cell) + "</td>")
                else:
                    out.append("\n<td>" + structures.fmt(cell) + "</td>")
            out.append("\n</tr>")

        out.append("</table>")
    htmltime = time.time() - sorttime - retrievetime - parsetime - starttime
    elapsed = time.time() - starttime
    if timing:
        out.append("<br />\n Page served in " + str(int(round(elapsed * 1000))) + "ms. Bot cached: " + str(cached))
        out.append("\n<br /> parsing: " + str(int(round(parsetime * 1000))))
        out.append("\n<br /> retrieve: " + str(int(round(retrievetime * 1000))))
        out.append("\n<br /> sort: " + str(int(round(sorttime * 1000))))
        out.append("\n<br /> html generation: " + str(int(round(htmltime * 1000))))

    out.append("""<div id="tip" style="position:fixed;padding:1px 4px;font-size:12px;pointer-events:none;display:none"></div>
<script>
(function() {
  var S = 230;
  var dark = document.body.classList.contains('dark');
  var tip = document.getElementById('tip');
  function draw(id, col, xcol, color, offset) {
    var canvas = document.getElementById(id);
    var ctx = canvas.getContext('2d');
    ctx.fillStyle = dark ? '#1e1e1e' : '#fff';
    ctx.fillRect(0, 0, S + 1, S + 1);
    ctx.fillStyle = dark ? '#555' : '#7f7f7f';
    ctx.fillRect(0, 115, S + 1, 1);
    var rows = document.querySelectorAll('table')[1].querySelectorAll('tr');
    var names = {};
    ctx.fillStyle = color;
    for (var i = 1; i < rows.length; i++) {
      var c = rows[i].querySelectorAll('td');
      if (c.length < 13) continue;
      var v = parseFloat(c[col].textContent);
      var ox = parseFloat(c[xcol].textContent);
      if (isNaN(v) || isNaN(ox)) continue;
      var px = Math.round(ox * 0.01 * S);
      var py = Math.max(0, Math.min(S, S - Math.round((v + offset) * 0.01 * S)));
      ctx.beginPath(); ctx.arc(px, py, 1.5, 0, 6.283); ctx.fill();
      names[py * (S + 1) + px] = c[2].textContent;
    }
    canvas.onmousemove = function(e) {
      var r = canvas.getBoundingClientRect();
      var mx = Math.floor(e.clientX - r.left);
      var my = Math.floor(e.clientY - r.top);
      var best = '', bestD = 26;
      for (var dy = -5; dy <= 5; dy++) for (var dx = -5; dx <= 5; dx++) {
        var d = dx*dx + dy*dy;
        if (d < bestD) { var n = names[(my+dy)*(S+1)+(mx+dx)]; if (n) { best = n; bestD = d; } }
      }
      if (best) { tip.textContent = best; tip.style.left = e.clientX + 12 + 'px'; tip.style.top = e.clientY + 2 + 'px'; tip.style.display = ''; }
      else tip.style.display = 'none';
    };
    canvas.onmouseleave = function() { tip.style.display = 'none'; };
  }
  draw('distAPS', 4, 11, dark ? 'rgba(255,100,100,0.5)' : 'rgba(204,37,41,0.4)', 0);
  draw('distSurvival', 7, 12, dark ? 'rgba(100,220,130,0.5)' : 'rgba(62,150,81,0.4)', 0);
  draw('distKNNPBI', 8, 11, dark ? 'rgba(120,160,255,0.5)' : 'rgba(57,106,177,0.4)', 50);
})();
</script>""")
    out.append("</body></html>")

    return "".join(out)
