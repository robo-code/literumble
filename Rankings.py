#!/usr/bin/env python
import time
from operator import attrgetter

from flask import Response, request

import store
import structures
from structures import load_blob


def rankings():
    starttime = time.time()
    query = request.query_string.decode('utf-8').replace("%20", " ")
    parts = query.split("&")
    requests = {}
    if parts[0] != "":
        for pair in parts:
            ab = pair.split('=', 1)
            requests[ab[0]] = ab[1] if len(ab) > 1 else ""

    game = requests.get("game", "meleerumble")
    lim = int(requests.get("limit", "10000000"))
    order = requests.get("order", "APS")
    timing = bool(requests.get("timing", False))
    api = bool(requests.get("api", False))
    dark = requests.get("theme", "") == "dark"

    extraArgs = ""
    if timing:
        extraArgs += "&amp;timing=1"
    if dark:
        extraArgs += "&amp;theme=dark"
    if lim < 100000:
        extraArgs += "&amp;limit=" + str(lim)

    reverseSort = True
    if len(order) == 0:
        order = "APS"
    if order[0] == "-":
        order = order[1:]
        reverseSort = False
    if order == "Latest Battle":
        order = "LastUpload"
    elif order == "Competitor":
        order = "Name"
    elif order == "Vote":
        order = "VoteScore"
    elif order == "APS CI":
        order = "APS_CI"

    parsing = time.time() - starttime

    rumble = structures.Rumble.get_by_key_name(game)
    if rumble is None:
        return "RUMBLE NOT FOUND"

    flagmap = None
    flagmapholder = structures.FlagMap.get_by_key_name(structures.default_flag_map)
    if flagmapholder is not None:
        flagmap = flagmapholder.InternalMap

    flagmap = load_blob(flagmap, {})
    if not isinstance(flagmap, dict):
        flagmap = {}

    botsdict = store.load_scores(rumble.Name)
    if isinstance(botsdict, dict):
        bots = list(botsdict.values())
    elif isinstance(botsdict, list):
        bots = botsdict
    else:
        bots = []

    retrievetime = time.time() - starttime - parsing
    for b in bots:
        b.PWIN = 50.0 * float(b.PL) / b.Pairings + 50.0
        if b.VoteScore is None:
            b.VoteScore = 0
        if b.ANPP is None:
            b.ANPP = 0
        package = b.Name.split(".")[0]
        if package in flagmap:
            b.Flag = flagmap[package]
        else:
            b.Flag = "NONE"

    get_key = attrgetter(order)
    bots.sort(key=lambda b: get_key(b), reverse=reverseSort)

    if api:
        headings = ["\"name\"",
                    "\"flag\"",
                    "\"rank\"",
                    "\"APS\"",
                    "\"APS_CI\"",
                    "\"PWIN\"",
                    "\"ANPP\"",
                    "\"vote\"",
                    "\"survival\"",
                    "\"pairings\"",
                    "\"battles\"",
                    "\"latest\""]
        escapes = ["\"", "\"", "", "", "", "", "", "", "", "", "", "\""]
        outs = ["[\n"]
        count = 0
        for bot in bots:
            count += 1
            if count > lim:
                break
            cells = [
                bot.Name, bot.Flag, count,
                bot.APS,
                getattr(bot, "APS_CI", -1.0),
                bot.PWIN,
                bot.ANPP,
                bot.VoteScore,
                bot.Survival,
                bot.Pairings, bot.Battles, bot.LastUpload]

            outs.append("{")
            for i in range(len(cells)):
                outs.append(headings[i])
                outs.append(":")
                outs.append(escapes[i])
                outs.append(structures.fmt(cells[i]))
                outs.append(escapes[i])
                outs.append(",")
            outs[-1] = "},\n"
        outs[-1] = ("}\n]")
        return Response("".join(outs), mimetype="application/json")

    sorttime = time.time() - parsing - retrievetime - starttime

    if order == "LastUpload":
        order = "Latest Battle"
    elif order == "Name":
        order = "Competitor"
    elif order == "VoteScore":
        order = "Vote"
    elif order == "APS_CI":
        order = "APS CI"
    out = []

    gameTitle = structures.home_link("LITERUMBLE", dark) + " RANKINGS - " + game.upper() + " WITH " + str(len(bots)) + " BOTS"
    out.append(structures.header(game, gameTitle, dark))

    pairVals = [b.Pairings for b in bots]
    if pairVals and max(pairVals) == min(pairVals) == (len(bots) - 1):
        out.append("<big>Rankings Stable</big>")
    else:
        out.append("<big>Rankings Not Stable</big>")
    out.append("\n<table>\n<tr>")

    headings = ["", "Flag", "Competitor", "APS", "APS CI", "PWIN", "ANPP", "Vote", "Survival", "Pairings", "Battles", "Latest Battle"]
    for heading in headings:
        sortedBy = order == heading
        if order == heading and reverseSort:
            heading = "-" + heading
        orderl = []
        orderl.append("<a href=\"Rankings?game=")
        orderl.append(game)
        orderl.append("&amp;order=")
        orderl.append(heading.replace(" ", "%20"))
        orderl.append(extraArgs)
        orderl.append("\">")
        orderl.append(heading)
        orderl.append("</a>")
        orderHref = "".join(orderl)
        if sortedBy:
            out.append("\n<th class=\"sortedby\">" + orderHref + "</th>")
        else:
            out.append("\n<th>" + orderHref + "</th>")
    out.append("\n</tr>")
    rank = 1
    for bot in bots:
        if rank > lim:
            break

        botName = bot.Name
        bnh = []
        bnh.append("<a href=\"BotDetails?game=")
        bnh.append(game)
        bnh.append("&amp;name=")
        bnh.append(botName.replace(" ", "%20"))
        bnh.append(extraArgs)
        bnh.append("\" >")
        bnh.append(botName)
        bnh.append("</a>")
        botNameHref = "".join(bnh)

        ft = []
        ft.append("<img id='flag' src=\"/flags/")
        ft.append(bot.Flag)
        ft.append(".gif\">")
        flagtag = "".join(ft)

        botCI = getattr(bot, "APS_CI", -1.0)
        ciStr = "n/a" if (botCI is None or botCI < 0) else "&plusmn;" + structures.fmt(botCI)

        cells = [rank, flagtag, botNameHref,
                 bot.APS,
                 ciStr,
                 bot.PWIN,
                 bot.ANPP,
                 bot.VoteScore,
                 bot.Survival,
                 bot.Pairings, bot.Battles, bot.LastUpload]

        out.append("\n<tr>")
        for cell in cells:
            out.append("\n<td>")
            out.append(structures.fmt(cell))
            out.append("</td>")
        out.append("\n</tr>")
        del bot.PWIN
        rank += 1

    out.append("</table>")
    htmltime = time.time() - parsing - retrievetime - sorttime - starttime

    elapsed = time.time() - starttime
    if timing:
        out.append("\n<br> Page served in " + str(int(round(elapsed * 1000))) + "ms. ")
        out.append("\n<br> parsing: " + str(int(round(parsing * 1000))))
        out.append("\n<br> retrieve: " + str(int(round(retrievetime * 1000))))
        out.append("\n<br> sort: " + str(int(round(sorttime * 1000))))
        out.append("\n<br> html generation: " + str(int(round(htmltime * 1000))))
    out.append("</body></html>")

    return "".join(out)
