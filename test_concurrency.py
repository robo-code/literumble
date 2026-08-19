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


def main():
    test_pair_writes_are_never_torn()
    test_datastore_put_get_and_query_race()
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
