#!/usr/bin/env python
from flask import Response, request

import store
import structures
from structures import global_dict, load_blob


def ratings_file():
    parts = request.query_string.decode('utf-8').split("&")
    requests = {}
    if parts[0] != "":
        for pair in parts:
            ab = pair.split('=', 1)
            requests[ab[0]] = ab[1] if len(ab) > 1 else ""

    game = requests.get("game", None)
    if game is None:
        return "NO RUMBLE SPECIFIED IN FORM game=____"

    version = requests.get("version", None)
    if version is None or version != "1":
        return "VERSION NOT SPECIFIED AS version=1"

    rumble = global_dict.get(game, None)
    if rumble is None:
        rumble = structures.Rumble.get_by_key_name(game)
        if rumble is None:
            return "RUMBLE NOT FOUND"
        global_dict[game] = rumble

    botsdict = store.load_scores(rumble.Name)
    r = list(botsdict.values()) if isinstance(botsdict, dict) else []

    out = []
    for bot in r:
        name = bot.Name
        name = name.replace(" ", "_")
        out.append(name)
        out.append("=")
        out.append(str(bot.APS))
        out.append(",")
        out.append(str(bot.Battles))
        out.append(",")
        out.append(bot.LastUpload)
        out.append("\n")

    return Response("".join(out), mimetype="text/plain")
