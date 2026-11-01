#!/usr/bin/env python
"""End-to-end test of the SQLite/shim deploy: spin up a fresh instance, upload a
couple of battles, drain the queue, tick the crons, and assert the pages show
the right data via the ``api=1`` JSON. Run: ``python test_e2e.py`` (exit != 0 on
failure). In-process via Flask's test client -- no sockets, no sleeping.
"""
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time

# Fresh throwaway DB BEFORE importing the app (gae_shim reads this at import).
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["LITERUMBLE_DB"] = _tmp.name

import wsgi_local  # noqa: E402  (imports gae_shim first, starts background threads)
import background   # noqa: E402
import store        # noqa: E402
import structures   # noqa: E402
from google.appengine.api import taskqueue  # noqa: E402  (the shim)

app = wsgi_local.app
client = app.test_client()

GAME = "TestRumble"


def make_battle(fname, sname, fscore, sscore, game=GAME, settings=""):
    """A minimal valid /UploadedResults body (a 1v1 battle, A-vs-B)."""
    fields = {
        "client": "1.12.0", "version": "1", "game": game,
        "rounds": "35", "field": "1000x1000", "melee": "NO", "teams": "NO",
        "fname": fname, "sname": sname,
        "fscore": str(fscore), "sscore": str(sscore),
        "fbulletd": str(fscore // 2), "sbulletd": str(sscore // 2),
        "fsurvival": "35" if fscore >= sscore else "0",
        "ssurvival": "0" if fscore >= sscore else "35",
        "user": "tester", "time": str(int(time.time() * 1000)),
    }
    return "&".join("%s=%s" % kv for kv in fields.items()) + settings


def upload(fname, sname, fscore, sscore, game=GAME, settings=""):
    r = client.post("/UploadedResults", data=make_battle(fname, sname, fscore, sscore, game, settings))
    body = r.get_data(as_text=True)
    assert r.status_code == 200 and "OK" in body, (r.status_code, body)
    assert "added to queue" in body, "upload not accepted: " + body
    return body


def battles(game):
    """The battles of the Alpha-Bravo pairing, which unlike a bot's total never drops when an opponent retires."""
    for pairing in store.load_pairings(game, "Alpha"):
        if pairing.Name == "Bravo":
            return int(pairing.Battles)
    return 0


def get_json(path):
    r = client.get(path)
    assert r.status_code == 200, (path, r.status_code)
    return json.loads(r.get_data(as_text=True))


def main():
    structures._selfcheck()

    # This is one cumulative flow: each scenario's GIVEN is the state the ones
    # above it left behind (a fresh empty DB seeded battle by battle).

    # === Scenario: uploaded battles produce a ranking ===
    # GIVEN a fresh empty rumble
    # WHEN Alpha beats Bravo twice and the queue + crons run
    upload("Alpha", "Bravo", 60, 40)
    upload("Alpha", "Bravo", 65, 35)
    background.drain()
    for path in ("/RumbleStats?regen=1", "/RumbleStats?regen=1&theme=dark",
                 "/", "/?theme=dark"):
        background.tick(path)
    ranks = get_json("/Rankings?game=%s&api=1" % GAME)
    by_name = {b["name"]: b for b in ranks}
    # THEN both bots appear, winner ranked first with the higher APS
    assert set(by_name) == {"Alpha", "Bravo"}, by_name
    assert by_name["Alpha"]["APS"] > by_name["Bravo"]["APS"], ranks
    assert by_name["Alpha"]["APS"] > 50 and by_name["Bravo"]["APS"] < 50, ranks
    assert int(by_name["Alpha"]["battles"]) == 2, by_name["Alpha"]
    assert int(by_name["Alpha"]["pairings"]) == 1, by_name["Alpha"]
    assert ranks[0]["name"] == "Alpha", "winner should rank first: %s" % ranks

    # === Scenario: bot details expose the pairing ===
    # GIVEN Alpha and Bravo have fought
    # WHEN Alpha's details are requested
    detail = get_json("/BotDetails?game=%s&name=Alpha&api=1" % GAME)
    opps = {p["name"]: p for p in detail["pairingsList"]}
    # THEN the pairing vs Bravo is present with the correct sign
    assert detail["name"] == "Alpha" and detail["APS"] > 50, detail
    assert "Bravo" in opps, detail
    assert opps["Bravo"]["APS"] > 50, opps["Bravo"]

    # === Scenario: batch ranking flips the accuracy flag ===
    # GIVEN a rumble whose batch scores are not yet accurate
    # WHEN the hourly-queue cron and a write=true batch run
    accurate_before = structures.Rumble.get_by_key_name(GAME).BatchScoresAccurate
    background.tick("/QueueHourlyBatchRankings")
    taskqueue.add(url="/BatchRankings", payload="write=true")
    background.drain()
    accurate_after = structures.Rumble.get_by_key_name(GAME).BatchScoresAccurate
    ranks = get_json("/Rankings?game=%s&api=1" % GAME)
    # THEN the flag flips and the ranking order is preserved
    assert accurate_before is False, accurate_before
    assert accurate_after is True, accurate_after
    assert ranks[0]["name"] == "Alpha", "batch must not break ranking: %s" % ranks

    # === Scenario: comparing two bots against a shared opponent ===
    # GIVEN Alpha and Bravo each also fought Charlie
    upload("Alpha", "Charlie", 55, 45)
    upload("Bravo", "Charlie", 52, 48)
    background.drain()
    # WHEN the comparison page loads both bots' details
    page = client.get("/BotCompare?game=%s&bota=Alpha&botb=Bravo" % GAME).get_data(as_text=True)
    assert "BotDetails?api=1" in page, page[:200]
    a, b = (json.loads(client.get("/BotDetails?api=1&game=%s&name=%s" % (GAME, n)).get_data(as_text=True))
            for n in ("Alpha", "Bravo"))
    # THEN Charlie is a common opponent, carrying its own rumble score
    common = {p["name"] for p in a["pairingsList"]} & {p["name"] for p in b["pairingsList"]}
    assert "Charlie" in common, common
    charlie = [p for p in a["pairingsList"] if p["name"] == "Charlie"][0]
    assert 0 <= charlie["oppAPS"] <= 100 and 0 <= charlie["oppSurvival"] <= 100, charlie

    # === Scenario: the server recommends a priority battle ===
    # GIVEN all three bots are paired and one pairing has zero sample variance
    upload("Alpha", "Bravo", 60, 40)
    upload("Alpha", "Bravo", 60, 40)
    background.drain()
    # WHEN a further battle is uploaded, picking up the queued recommendation
    body = upload("Alpha", "Charlie", 55, 45)
    background.drain()
    # THEN the response leads with a [BotA,BotB] pairing of two distinct bots
    assert body.startswith("["), "no priority battle returned: %r" % body
    pair = body[1:body.index("]")].split(",")
    known = {"Alpha", "Bravo", "Charlie"}
    assert len(pair) == 2 and pair[0] != pair[1], body
    assert set(p.replace("_", " ") for p in pair) <= known, body

    # === Scenario: data survives across processes ===
    # GIVEN battles written to the SQLite file
    # WHEN a separate process opens its own connection and reads them back
    code = ("import gae_shim, store;"
            "a=store.load_score('%s', 'Alpha');"
            "b=store.load_score('%s', 'Bravo');"
            "print(a.APS, b.APS)" % (GAME, GAME))
    out = subprocess.check_output(
        [sys.executable, "-c", code],
        env={**os.environ, "LITERUMBLE_DB": _tmp.name}, text=True).split()
    aps_a, aps_b = float(out[0]), float(out[1])
    # THEN it sees the same scores
    assert aps_a > aps_b and aps_a > 50, out

    # === Scenario: retiring an old participant ===
    # GIVEN Delta 1.0 has entered the rumble (request names underscore spaces)
    upload("Delta 1.0", "Alpha", 30, 70)
    upload("Delta 1.0", "Charlie", 40, 60)
    background.drain()
    present_before = any(b["name"] == "Delta 1.0"
                         for b in get_json("/Rankings?game=%s&api=1" % GAME))

    def alpha_opps():
        d = get_json("/BotDetails?game=%s&name=Alpha&api=1" % GAME)
        return {p["name"] for p in d["pairingsList"]}
    paired_before = "Delta 1.0" in alpha_opps()
    # WHEN it is removed
    removal = client.get(
        "/RemoveOldParticipant?version=1&game=%s&name=Delta_1.0" % GAME
    ).get_data(as_text=True)
    present_after = any(b["name"] == "Delta 1.0"
                        for b in get_json("/Rankings?game=%s&api=1" % GAME))
    # THEN it was present, is acknowledged retired, and is gone from the ranking
    assert present_before, "Delta should have entered the rumble"
    assert "retired" in removal, removal
    assert not present_after, "Delta should be gone after retirement"

    # === Scenario: a retired opponent drops out of pairing views ===
    # GIVEN Delta was Alpha's opponent and is now retired
    # WHEN Alpha's pairings are read
    # THEN Delta was listed before removal and is gone after
    assert paired_before, "Delta should have been one of Alpha's pairings"
    assert "Delta 1.0" not in alpha_opps(), "retired Delta must not show in Alpha's pairings"

    # === Scenario: a retired bot can still be compared by typing its name ===
    # GIVEN Delta is retired but both it and Alpha fought Charlie
    # WHEN Delta's name is entered on the comparison page
    cmp_retired = client.get(
        "/BotDetails?api=1&game=%s&name=Delta%%201.0" % GAME).get_data(as_text=True)
    # THEN its details load with their shared opponent
    assert "ERROR" not in cmp_retired, cmp_retired[:200]
    assert "Charlie" in [p["name"] for p in json.loads(cmp_retired)["pairingsList"]], cmp_retired[:400]
    # AND a name that never fought here is still rejected
    cmp_bogus = client.get(
        "/BotDetails?api=1&game=%s&name=Nope%%209.9" % GAME).get_data(as_text=True)
    assert "ERROR" in cmp_bogus, cmp_bogus[:200]

    # === Scenario: a retired bot keeps its details page ===
    # GIVEN Delta is retired but its pairings against Charlie survived
    # WHEN Delta's details page is opened
    det_retired = client.get(
        "/BotDetails?game=%s&name=Delta 1.0" % GAME).get_data(as_text=True)
    det_json = get_json("/BotDetails?game=%s&name=Delta 1.0&api=1" % GAME)
    # THEN it renders, listing the opponents it still has history against, with
    # its own stats rebuilt from those pairings
    assert "ERROR" not in det_retired, det_retired[:200]
    assert "Charlie" in det_retired, det_retired[:400]
    assert det_json["pairings"] > 0 and det_json["battles"] > 0, det_json
    assert 0.0 <= float(det_json["APS"]) <= 100.0, det_json
    # AND a name that never fought here is still rejected
    det_bogus = client.get(
        "/BotDetails?game=%s&name=Nope 9.9" % GAME).get_data(as_text=True)
    assert "ERROR" in det_bogus, det_bogus[:200]

    # === Scenario: HTML pages render (light and dark theme) ===
    # GIVEN bots exist (the api=1 JSON branches are asserted above); the
    # landing/stats pages show the rumble name, the ranking pages show the bot
    pages = {
        "/": GAME, "/RumbleStats": GAME,
        "/Rankings?game=%s" % GAME: "Alpha",
        "/BotDetails?game=%s&name=Alpha" % GAME: "Alpha",
    }
    for path, needle in pages.items():
        for p in (path, path + ("&" if "?" in path else "?") + "theme=dark"):
            # WHEN the page is fetched in this theme
            html = client.get(p).get_data(as_text=True)
            # THEN its expected content is present
            assert needle in html, "%s missing %s:\n%s" % (p, needle, html[:200])

    # === Scenario: sorting by a column ===
    # GIVEN the ranking and details pages, each with a sort link per column
    def sort_links(path):
        html = client.get(path).get_data(as_text=True)
        return set(re.findall(r'href="((?:Rankings|BotDetails)\?[^"]*order=[^"]*)"', html.replace("&amp;", "&")))
    rank_links = sort_links("/Rankings?game=%s" % GAME)
    detail_links = sort_links("/BotDetails?game=%s&name=Alpha" % GAME)
    # WHEN every sort link is followed
    # THEN each sorts (incl. Latest Battle and the Opponent columns via their attrs)
    assert len(rank_links) >= 10 and len(detail_links) >= 10, (rank_links, detail_links)
    for link in rank_links | detail_links | sort_links("/BotDetails?game=%s&name=Alpha&order=Name" % GAME):
        assert client.get("/" + link).status_code == 200, link
    # AND a leading '-' flips the order
    def first_bot(path):
        return re.search(r'BotDetails\?game=%s&amp;name=(\w+)' % GAME, client.get(path).get_data(as_text=True)).group(1)
    assert first_bot("/Rankings?game=%s&order=APS" % GAME) == "Alpha"
    assert first_bot("/Rankings?game=%s&order=-APS" % GAME) != "Alpha"
    # AND an unknown column is a bad request, while the APIs ignore order
    assert client.get("/Rankings?game=%s&order=Latest+Battle" % GAME).status_code == 400
    assert client.get("/BotDetails?game=%s&name=Alpha&order=bogus" % GAME).status_code == 400
    assert get_json("/Rankings?game=%s&api=1&order=bogus" % GAME) == get_json("/Rankings?game=%s&api=1" % GAME)
    assert get_json("/BotDetails?game=%s&name=Alpha&api=1&order=-bogus" % GAME) == get_json(
        "/BotDetails?game=%s&name=Alpha&api=1" % GAME)

    # === Scenario: RatingsFile feed for roborumble (RATINGS.URL) ===
    # GIVEN bots exist and Delta has been retired
    # WHEN the plain-text ratings feed is fetched (`name=APS,battles,latest`),
    # and once more without the required version
    txt = client.get("/RatingsFile?game=%s&version=1" % GAME).get_data(as_text=True)
    ratings = {}
    for line in txt.splitlines():
        if not line:
            continue
        name, rest = line.split("=", 1)
        ratings[name] = float(rest.split(",")[0])
    no_version = client.get("/RatingsFile?game=%s" % GAME).get_data(as_text=True)
    # THEN active bots are listed, the retired one absent, and version is required
    assert "Alpha" in ratings and ratings["Alpha"] > 50, txt
    assert "Delta_1.0" not in ratings, "retired bot should be gone:\n" + txt
    assert "VERSION" in no_version, no_version

    # === Scenario: cron endpoints are loopback/header protected ===
    # GIVEN a cron-only endpoint
    # WHEN it is hit without, then with, the App Engine cron header
    without_header = client.get("/FetchParseFlags").status_code
    with_header = client.get(
        "/FetchParseFlags", headers={"X-Appengine-Cron": "true"}).status_code
    # THEN it is rejected, then allowed
    assert without_header == 403, without_header
    assert with_header == 200, with_header

    # === Scenario: a rumble stored before these settings existed has them off ===
    # GIVEN a stored rumble without the settings
    # WHEN it is read
    old = structures.Rumble._from_row("OldRumble", {"Name": "OldRumble"})
    # THEN both settings are off
    assert old.HideNames is False and old.ClearData is False, old

    # === Scenario: an upload without the settings works as before ===
    # GIVEN a rumble with the settings off
    before = battles(GAME)
    # WHEN an upload without the settings arrives
    upload("Alpha", "Bravo", 60, 40)
    background.drain()
    # THEN it counts
    assert battles(GAME) == before + 1, battles(GAME)

    # The rejected uploads below log an expected config-mismatch error each.
    logging.disable(logging.ERROR)
    # === Scenario: a rumble with the settings off rejects results with them on ===
    # GIVEN the same rumble
    before = battles(GAME)
    # WHEN uploads with either setting on arrive, the first as the client sends it
    upload("Alpha", "Bravo", 60, 40, settings="&hidenames=YES\n")
    upload("Alpha", "Bravo", 60, 40, settings="&cleardata=YES")
    background.drain()
    # THEN neither counts
    assert battles(GAME) == before, battles(GAME)

    # === Scenario: a new rumble takes the settings from its first upload ===
    # GIVEN no rumble called HiddenRumble
    # WHEN the first upload has both settings on, as the client sends it
    both = "&cleardata=YES&hidenames=YES\r\n"
    upload("Alpha", "Bravo", 60, 40, game="HiddenRumble", settings=both)
    background.drain()
    # THEN the rumble has them on, and the battle counts
    hidden = structures.Rumble.get_by_key_name("HiddenRumble")
    assert hidden.HideNames is True and hidden.ClearData is True, hidden
    assert battles("HiddenRumble") == 1, battles("HiddenRumble")

    # === Scenario: a rumble with the settings on only takes results with them on ===
    # GIVEN that rumble
    # WHEN uploads with no settings, or only one of them, arrive
    upload("Alpha", "Bravo", 60, 40, game="HiddenRumble")
    upload("Alpha", "Bravo", 60, 40, game="HiddenRumble", settings="&hidenames=YES\n")
    upload("Alpha", "Bravo", 60, 40, game="HiddenRumble", settings="&cleardata=YES")
    background.drain()
    # THEN they do not count, but one with both on does
    assert battles("HiddenRumble") == 1, battles("HiddenRumble")
    upload("Alpha", "Bravo", 61, 39, game="HiddenRumble", settings=both)
    background.drain()
    assert battles("HiddenRumble") == 2, battles("HiddenRumble")
    logging.disable(logging.NOTSET)

    print("test_e2e OK")


if __name__ == "__main__":
    try:
        main()
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(_tmp.name + suffix)
            except OSError:
                pass
