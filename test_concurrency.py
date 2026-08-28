#!/usr/bin/env python
"""Hammers the shim from many threads: no reader may see half a write.
Run: ``python test_concurrency.py`` (exit != 0 on failure).
"""
import os
import tempfile
import threading

# BEFORE importing the shim: it reads this at import.
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["LITERUMBLE_DB"] = _tmp.name

import gae_shim    # noqa: E402
import store       # noqa: E402
import structures  # noqa: E402

THREADS = 8
ROUNDS = 60
RUMBLE = "conc"


def run(*targets):
    """Run each fn on THREADS threads, failing on anything a thread raised."""
    errors = []

    def wrapper(fn, i):
        try:
            fn(i)
        except Exception as e:  # noqa: BLE001
            errors.append("%s(%d): %r" % (fn.__name__, i, e))

    ts = [threading.Thread(target=wrapper, args=(fn, i))
          for fn in targets for i in range(THREADS)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors, errors


def test_pair_writes_are_never_torn():
    """The writer stamps its round into both sides' Survival, so a reader
    seeing surv_lo != surv_hi has caught half an upsert_pair commit."""
    def writer(i):
        a, b = "botA%d" % i, "botB%d" % i
        for r in range(ROUNDS):
            v = float(r % 90) + 5.0
            ss_a = structures.ScoreSet(b, v, survival=float(r), battles=r + 1)
            ss_b = structures.ScoreSet(a, 100.0 - v, survival=float(r), battles=r + 1)
            store.upsert_pair(RUMBLE, a, ss_a, b, ss_b)

    def reader(i):
        con = store._con()
        for _ in range(ROUNDS):
            for lo, hi, surv_lo, surv_hi in con.execute(
                    "SELECT lo,hi,surv_lo,surv_hi FROM Pairing WHERE rumble=?",
                    (RUMBLE,)):
                assert surv_lo == surv_hi, (lo, hi, surv_lo, surv_hi)

    run(writer, reader)
    for i in range(THREADS):
        a, b = "botA%d" % i, "botB%d" % i
        fwd = {s.Name: s for s in store.load_pairings(RUMBLE, a)}[b]
        rev = {s.Name: s for s in store.load_pairings(RUMBLE, b)}[a]
        assert abs(fwd.APS + rev.APS - 100.0) < 1e-9, (fwd.APS, rev.APS)
        assert fwd.Battles == rev.Battles == ROUNDS


def test_datastore_put_get_and_query_race():
    """A torn datastore write surfaces as an unpicklable blob."""
    class Thing(gae_shim.Model):
        value = gae_shim.StringProperty()

    def churn(i):
        for r in range(ROUNDS):
            t = Thing(key_name="k%d" % i)
            t.value = "v%d-%d" % (i, r)
            t.put()
            got = Thing.get_by_key_name("k%d" % i)
            assert got is not None and got.value.startswith("v%d-" % i), got
            list(Thing.all().run())

    run(churn)
    assert len({t._key_name for t in Thing.all().run()}) == THREADS


def test_failed_write_leaves_no_open_transaction():
    """An open transaction pins the thread's read snapshot: every page it serves
    afterwards is stale."""
    con = store._con()

    class Boom(Exception):
        pass

    try:
        with gae_shim.write(con):
            con.execute("DELETE FROM RumbleScore WHERE rumble=?", ("leak",))
            raise Boom
    except Boom:
        pass

    assert not con.in_transaction, "failed write left a transaction open"

    # a rollback that left the snapshot pinned would still pass the check above
    def other():
        lb = structures.LiteBot(loadDict={"Name": "after", "Rumble": "leak",
                                          "Battles": 1, "Pairings": 1, "APS": 1.0,
                                          "Survival": 1.0, "PL": 0, "VoteScore": 0.0,
                                          "LastUpload": "2020-01-01 00:00:00",
                                          "Active": True, "ANPP": 0.0, "APS_CI": -1.0,
                                          "Uploaders": []})
        store.upsert_scores("leak", [lb])

    t = threading.Thread(target=other)
    t.start()
    t.join()
    assert store.load_score("leak", "after") is not None, "reader stuck on a stale snapshot"


def test_half_written_pairing_reads_back_with_defaults():
    """A None Survival reaches float() in _put_side and kills the whole
    BatchRankings pass."""
    ss = structures.ScoreSet(name="Zeta", aps=60.0, survival=55.0, battles=3)
    store.upsert_pairing("half", "Alpha", ss)   # only Alpha's side exists

    back = store.load_pairings("half", "Zeta")
    assert len(back) == 1, back
    assert back[0].Survival is not None, "NULL column read back as None"

    store.replace_pairings("half", "Zeta", back)


def main():
    test_pair_writes_are_never_torn()
    test_datastore_put_get_and_query_race()
    test_failed_write_leaves_no_open_transaction()
    test_half_written_pairing_reads_back_with_defaults()
    print("test_concurrency OK")


if __name__ == "__main__":
    try:
        main()
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(_tmp.name + suffix)
            except OSError:
                pass
