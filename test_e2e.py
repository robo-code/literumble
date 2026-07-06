#!/usr/bin/env python
"""End-to-end test of the SQLite/shim deploy: spin up a fresh instance, upload a
couple of battles, drain the queue, tick the crons, and assert the pages show
the right data via the ``api=1`` JSON. Run: ``python test_e2e.py`` (exit != 0 on
failure). In-process via Flask's test client -- no sockets, no sleeping.
"""
import json
import os
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
import structures   # noqa: E402
from google.appengine.api import taskqueue  # noqa: E402  (the shim)

app = wsgi_local.app
client = app.test_client()

GAME = "TestRumble"


def make_battle(fname, sname, fscore, sscore):
    """A minimal valid /UploadedResults body (a 1v1 battle, A-vs-B)."""
    fields = {
        "client": "1.10.3", "version": "1", "game": GAME,
        "rounds": "35", "field": "1000x1000", "melee": "NO", "teams": "NO",
        "fname": fname, "sname": sname,
        "fscore": str(fscore), "sscore": str(sscore),
        "fsurvival": "35" if fscore >= sscore else "0",
        "ssurvival": "0" if fscore >= sscore else "35",
        "user": "tester", "time": str(int(time.time() * 1000)),
    }
    return "&".join("%s=%s" % kv for kv in fields.items())


def upload(fname, sname, fscore, sscore):
    r = client.post("/UploadedResults", data=make_battle(fname, sname, fscore, sscore))
    body = r.get_data(as_text=True)
    assert r.status_code == 200 and "OK" in body, (r.status_code, body)
    assert "added to queue" in body, "upload not accepted: " + body
    return body


def get_json(path):
    r = client.get(path)
    assert r.status_code == 200, (path, r.status_code)
    return json.loads(r.get_data(as_text=True))


def main():
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
    # WHEN Alpha and Bravo are compared
    cmp_html = client.get(
        "/BotCompare?game=%s&bota=Alpha&botb=Bravo" % GAME).get_data(as_text=True)
    # THEN all three bots show up in the comparison
    for n in ("Alpha", "Bravo", "Charlie"):
        assert n in cmp_html, "BotCompare missing %s" % n

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
