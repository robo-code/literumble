"""SQLite-backed store for rumble scores and pairwise results.

Scores are one row per ``(rumble, name)``; pairings are one row per *unordered*
pair ``(lo < hi)`` so A-vs-B and B-vs-A share a single row. ``aps``/``battles``/
``var_aps``/``lastupload`` are equal on both sides by construction so they are
stored once (``aps`` from the ``lo`` side, ``aps_hi = 100 - aps_lo``); both sides
are written in one transaction so they stay equal. The genuinely per-direction
fields (survival, min_aps, knnpbi, npp) get ``_lo``/``_hi`` columns.

LiteBot/ScoreSet are the in-memory interchange type callers get back.
"""
import json

import gae_shim
import structures

_ready = False


def _con():
    """The shim's shared WAL connection, with our tables ensured once."""
    global _ready
    con = gae_shim._connect()
    if not _ready:
        with gae_shim._con_lock:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS RumbleScore (
                    rumble TEXT, name TEXT,
                    battles INTEGER, pairings INTEGER, aps REAL, survival REAL,
                    pl INTEGER, votescore REAL, lastupload TEXT, active INTEGER,
                    anpp REAL, aps_ci REAL, uploaders TEXT,
                    PRIMARY KEY (rumble, name));
                CREATE TABLE IF NOT EXISTS Pairing (
                    rumble TEXT, lo TEXT, hi TEXT,
                    battles INTEGER, var_aps REAL, lastupload TEXT,
                    aps_lo REAL,   -- aps_hi := 100 - aps_lo (provably exact both writers)
                    surv_lo REAL, surv_hi REAL,
                    min_aps_lo REAL, min_aps_hi REAL,
                    knnpbi_lo REAL, knnpbi_hi REAL,
                    npp_lo REAL, npp_hi REAL,
                    PRIMARY KEY (rumble, lo, hi));
                CREATE INDEX IF NOT EXISTS Pairing_hi ON Pairing (rumble, hi);
                """)
            con.commit()
        _ready = True
    return con


# ---- scores: {name: LiteBot} per rumble -----------------------------------

# column order matches the SELECT/INSERT below; maps 1:1 to LiteBot attrs
_SCORE_COLS = ("name", "battles", "pairings", "aps", "survival", "pl",
               "votescore", "lastupload", "active", "anpp", "aps_ci")


def _row_to_litebot(rumble, row):
    d = {
        "Name": row[0], "Battles": row[1], "Pairings": row[2], "APS": row[3],
        "Survival": row[4], "PL": row[5], "VoteScore": row[6],
        "LastUpload": row[7], "Active": bool(row[8]), "ANPP": row[9],
        "APS_CI": row[10], "Rumble": rumble,
        "Uploaders": json.loads(row[11]) if row[11] else [],
    }
    return structures.LiteBot(loadDict=d)


def load_scores(rumble):
    """``{name: LiteBot}`` for a rumble."""
    con = _con()
    rows = con.execute(
        "SELECT name,battles,pairings,aps,survival,pl,votescore,lastupload,"
        "active,anpp,aps_ci,uploaders FROM RumbleScore WHERE rumble=?",
        (rumble,)).fetchall()
    return {r[0]: _row_to_litebot(rumble, r) for r in rows}


def load_score(rumble, name):
    """One ``LiteBot`` (or None)."""
    con = _con()
    row = con.execute(
        "SELECT name,battles,pairings,aps,survival,pl,votescore,lastupload,"
        "active,anpp,aps_ci,uploaders FROM RumbleScore WHERE rumble=? AND name=?",
        (rumble, name)).fetchone()
    return _row_to_litebot(rumble, row) if row is not None else None


def _score_params(rumble, lb):
    return (rumble, lb.Name, int(lb.Battles), int(lb.Pairings), float(lb.APS),
            float(lb.Survival), int(lb.PL), float(getattr(lb, "VoteScore", 0.0)),
            lb.LastUpload, 1 if lb.Active else 0, float(getattr(lb, "ANPP", 0.0)),
            float(getattr(lb, "APS_CI", -1.0)),
            json.dumps(list(getattr(lb, "Uploaders", []) or [])))


_SCORE_UPSERT = (
    "INSERT OR REPLACE INTO RumbleScore "
    "(rumble,name,battles,pairings,aps,survival,pl,votescore,lastupload,"
    "active,anpp,aps_ci,uploaders) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)")


def upsert_scores(rumble, litebots):
    """Write just the given LiteBots (hot path: the 2 changed bots)."""
    con = _con()
    with gae_shim._con_lock:
        con.executemany(_SCORE_UPSERT,
                        [_score_params(rumble, lb) for lb in litebots])
        con.commit()


def replace_scores(rumble, scores):
    """Replace a rumble's entire score set with ``{name: LiteBot}``."""
    con = _con()
    with gae_shim._con_lock:
        con.execute("DELETE FROM RumbleScore WHERE rumble=?", (rumble,))
        con.executemany(_SCORE_UPSERT,
                        [_score_params(rumble, lb) for lb in scores.values()])
        con.commit()


def delete_scores(rumble, names):
    con = _con()
    with gae_shim._con_lock:
        con.executemany("DELETE FROM RumbleScore WHERE rumble=? AND name=?",
                        [(rumble, n) for n in names])
        con.commit()


# ---- pairings: one canonical row per unordered pair -----------------------

# SELECT column order for load_pairings / _row_to_scoreset
#  0 lo 1 hi 2 battles 3 var_aps 4 lastupload 5 aps_lo 6 surv_lo 7 surv_hi
#  8 min_aps_lo 9 min_aps_hi 10 knnpbi_lo 11 knnpbi_hi 12 npp_lo 13 npp_hi
_PAIR_COLS = ("lo,hi,battles,var_aps,lastupload,aps_lo,surv_lo,surv_hi,"
              "min_aps_lo,min_aps_hi,knnpbi_lo,knnpbi_hi,npp_lo,npp_hi")
_PAIR_SELECT = "SELECT " + _PAIR_COLS + " FROM Pairing"


def _row_to_scoreset(botname, row):
    """Build the ScoreSet for ``botname`` (opponent = the other side)."""
    lo, hi, aps_lo = row[0], row[1], row[5]
    ss = structures.ScoreSet()
    ss.Battles, ss.Var_APS, ss.LastUpload = row[2], row[3], row[4]
    if botname == lo:
        ss.Name = hi
        ss.APS = aps_lo
        ss.Survival, ss.Min_APS = row[6], row[8]
        ss.KNNPBI, ss.NPP = row[10], row[12]
    else:
        ss.Name = lo
        ss.APS = 100.0 - aps_lo
        ss.Survival, ss.Min_APS = row[7], row[9]
        ss.KNNPBI, ss.NPP = row[11], row[13]
    return ss


def load_pairings(rumble, botname):
    """``[ScoreSet]`` from ``botname``'s perspective. ``Alive`` is left default;
    callers recompute it from score membership."""
    con = _con()
    # UNION ALL of two point lookups, NOT "lo=? OR hi=?": the OR defeats both
    # indexes and full-scans the rumble's pairings (~200ms on roborumble). Each
    # arm hits an index -- lo via the PK, hi via Pairing_hi -- so it's ~1ms.
    rows = con.execute(
        _PAIR_SELECT + " WHERE rumble=? AND lo=? UNION ALL " +
        _PAIR_SELECT + " WHERE rumble=? AND hi=?",
        (rumble, botname, rumble, botname)).fetchall()
    return [_row_to_scoreset(botname, r) for r in rows]


# join Pairing to the opponent's RumbleScore: same p-side lookups as load_pairings,
# INNER JOIN so only opponents still in the rumble come back (the "alive" filter is
# free) and each row carries the opponent's aps/survival (cols 14/15). For the
# display path (BotDetails/BotCompare) -- avoids loading the whole scores dict.
_pj = ",".join("p." + c for c in _PAIR_COLS.split(","))
_ALIVE_SELECT = (
    "SELECT " + _pj + ",s.aps,s.survival FROM Pairing p "
    "JOIN RumbleScore s ON s.rumble=p.rumble AND s.name=p.hi "
    "WHERE p.rumble=? AND p.lo=? "
    "UNION ALL "
    "SELECT " + _pj + ",s.aps,s.survival FROM Pairing p "
    "JOIN RumbleScore s ON s.rumble=p.rumble AND s.name=p.lo "
    "WHERE p.rumble=? AND p.hi=?")


def load_pairings_alive(rumble, botname):
    """Like ``load_pairings`` but only opponents still participating, each ScoreSet
    carrying ``OppAPS``/``OppSurvival`` (the opponent's own rumble score). One
    indexed join instead of a Python membership+lookup against a full scores dict."""
    con = _con()
    out = []
    for r in con.execute(_ALIVE_SELECT, (rumble, botname, rumble, botname)):
        ss = _row_to_scoreset(botname, r)
        ss.Alive = True
        ss.OppAPS, ss.OppSurvival = r[14], r[15]
        out.append(ss)
    return out


def _put_side(con, rumble, botname, ss):
    """Upsert ``botname``'s side of the ``botname`` vs ``ss.Name`` pair. Writes only
    that side's directional columns; the shared columns (equal on both sides) are
    (re)written too."""
    opp = ss.Name
    if botname <= opp:
        lo, hi, side = botname, opp, "lo"
        aps_lo = float(ss.APS)
    else:
        lo, hi, side = opp, botname, "hi"
        aps_lo = 100.0 - float(ss.APS)
    shared = (int(ss.Battles), float(getattr(ss, "Var_APS", -1.0)),
              ss.LastUpload, aps_lo)
    dirvals = (float(getattr(ss, "Survival", 0.0)),
               float(getattr(ss, "Min_APS", 100.0)),
               float(getattr(ss, "KNNPBI", 0.0)),
               float(getattr(ss, "NPP", -1.0)))
    con.execute(
        "INSERT INTO Pairing (rumble,lo,hi,battles,var_aps,lastupload,aps_lo,"
        "surv_{s},min_aps_{s},knnpbi_{s},npp_{s}) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(rumble,lo,hi) DO UPDATE SET "
        "battles=excluded.battles,var_aps=excluded.var_aps,"
        "lastupload=excluded.lastupload,aps_lo=excluded.aps_lo,"
        "surv_{s}=excluded.surv_{s},min_aps_{s}=excluded.min_aps_{s},"
        "knnpbi_{s}=excluded.knnpbi_{s},npp_{s}=excluded.npp_{s}".format(s=side),
        (rumble, lo, hi) + shared + dirvals)


def upsert_pairing(rumble, botname, scoreset):
    """Write the single ``botname`` vs ``scoreset.Name`` pair (hot path)."""
    con = _con()
    with gae_shim._con_lock:
        _put_side(con, rumble, botname, scoreset)
        con.commit()


def upsert_pair(rumble, name_a, ss_a, name_b, ss_b):
    """Write both sides of the ``a`` vs ``b`` pair in one commit (hot path).
    ``ss_a`` is a-vs-b from a's perspective, ``ss_b`` the mirror."""
    con = _con()
    with gae_shim._con_lock:
        _put_side(con, rumble, name_a, ss_a)
        _put_side(con, rumble, name_b, ss_b)
        con.commit()


def replace_pairings(rumble, botname, scoresets):
    """Rewrite all of ``botname``'s pairing sides (BatchRankings). Rows for
    opponents dropped from ``scoresets`` are left untouched (pairings are never
    deleted -- kept for historical lookup)."""
    con = _con()
    with gae_shim._con_lock:
        for ss in scoresets:
            _put_side(con, rumble, botname, ss)
        con.commit()


def has_history(rumble, name):
    """Retired bots keep their Pairing rows (see RemoveOldParticipant) after their
    RumbleScore row goes, so this is how you tell "retired" from "typo"."""
    con = _con()
    return con.execute(
        "SELECT 1 FROM Pairing WHERE rumble=? AND lo=? "
        "UNION ALL SELECT 1 FROM Pairing WHERE rumble=? AND hi=? LIMIT 1",
        (rumble, name, rumble, name)).fetchone() is not None
