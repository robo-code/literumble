#!/usr/bin/env python
from flask import request

import store


def nth_repl(s, sub, repl, nth):
    find = s.find(sub)
    i = find != -1
    while find != -1 and i != nth:
        find = s.find(sub, find + 1)
        i += 1
    if i == nth:
        return s[:find] + repl + s[find + len(sub):]
    return s


def rreplace(s, old, new, occurrence):
    li = s.rsplit(old, occurrence)
    return new.join(li)


def remove_old_participant():
    if request.method == "POST":
        raw = request.get_data(as_text=True)
    else:
        raw = request.query_string.decode('utf-8')

    parts = raw.split("&")
    requests = {}
    if parts[0] != "":
        for pair in parts:
            ab = pair.split('=')
            if len(ab) == 2:
                requests[ab[0]] = ab[1]
    return _remove_from_rumble(requests)


def _remove_from_rumble(requests):
    if "version" not in requests or requests["version"] != "1":
        return "ERROR. bad/no version"

    game = requests.get("game", None)
    if game is None:
        return "ERROR. no game specified"

    name = requests.get("name", None)
    if name is None:
        return "ERROR. no name specified"
    while name.count("%20") > 0:
        name = rreplace(name, "%20", " ", 1)

    if name.count(" ") != 0:
        return "ERROR. Bot name missing version."

    entry = None
    error_messages = []
    for n in range(name.count("_")):
        check_name = nth_repl(name, "_", " ", n + 1)
        entry = store.load_score(game, check_name)
        if entry is None:
            error_messages.append("ERROR. name/game does not exist: " + check_name + "/" + game)
        else:
            name = check_name
            break
    if entry is None:
        return "\n".join(error_messages)

    # drop the bot from the rumble's scores; its Pairing rows are kept for history
    # (they'll just read as not-alive since it's no longer a participant)
    store.delete_scores(game, [name])

    return "OK. " + name + " retired from " + game
