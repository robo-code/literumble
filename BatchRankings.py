#!/usr/bin/env python
import logging
import math
import time
from operator import attrgetter

import numpy
from flask import request
from google.appengine.api import taskqueue
from google.appengine.ext import db

import store
import structures


def queue_batch_rankings():
    taskqueue.add(url='/BatchRankings',
                  payload=request.query_string)
    return "queued"


def queue_hourly_batch_rankings():
    taskqueue.add(url='/BatchRankings', payload="")
    return "queued"


def start_backend():
    return ""


def batch_rankings():
    starttime = time.time()
    try:
        parts = request.get_data().decode('utf-8').split("&")
        requests = {}

        if parts is not None and parts[0] != "":
            for pair in parts:
                ab = pair.split('=', 1)
                requests[ab[0]] = ab[1] if len(ab) > 1 else ""

        force = bool(requests.get("force", False))

        q = structures.Rumble.all()
        rumbles = []
        for r in q.run():
            if r.BatchScoresAccurate and not force:
                continue
            rumbles.append(r)

        for r in rumbles:
            scoresdicts = store.load_scores(r.Name)
            entries = len(scoresdicts)
            r.__dict__["entries"] = entries
        rumbles.sort(key=lambda r: -r.__dict__["entries"])

        for r in rumbles:
            scores = store.load_scores(r.Name)
            if len(scores) == 0:
                continue
            bots = list(scores.values())

            get_key = attrgetter("APS")
            bots.sort(key=lambda b: get_key(b), reverse=True)

            botIndexes = {b.Name: i for i, b in enumerate(bots)}
            for b in bots:
                b.VoteScore = 0.

            botlen = len(bots)
            APSs = numpy.empty([botlen, botlen])
            APSs.fill(numpy.nan)
            # one row per pair; each side is (me, opp); alive = both still in the rumble
            rowid, lo, hi, aps_lo, battles, var_aps, *derived = store.load_pair_table(r.Name, botIndexes)
            alive = (lo >= 0) & (hi >= 0)
            sides = ((lo, hi), (hi, lo))
            APSs[hi[alive], lo[alive]] = aps_lo[alive]
            APSs[lo[alive], hi[alive]] = 100.0 - aps_lo[alive]
            pairCounts = sum(numpy.bincount(me[alive], minlength=botlen) for me, _ in sides)
            for b, c in zip(bots, pairCounts):
                b.Pairings = int(c)
            totalAlivePairs = int(pairCounts.sum())

            numpy.fill_diagonal(APSs, numpy.nan)

            logging.info(str(len(bots)) + " bots loaded, total of " + str(totalAlivePairs) + " alive pairings")

            # Vote
            mins = numpy.nanmax(APSs, 1)
            for i, minimum in enumerate(mins):
                minIndexes = numpy.argwhere(APSs[i, ...] == minimum)
                ties = len(minIndexes)
                if ties > 0:
                    increment = 1. / ties
                    for minIndex in minIndexes:
                        bots[int(minIndex[0])].VoteScore += increment  # [0] unwraps the (1,) row (numpy 1.x GAE and 2.x local)

            for b in bots:
                if b.Pairings > 0:
                    b.VoteScore = 100.0 * b.VoteScore / float(b.Pairings)
                else:
                    b.VoteScore = 0

            # KNN PBI
            half_k = int(math.ceil(math.sqrt(botlen) / 2))
            KNN_PBI = -numpy.ones((botlen, botlen))
            for i in range(len(bots)):
                low_bound = max([0, i - half_k])
                high_bound = min([botlen - 1, i + half_k])
                low_high_bound = min([i + 1, high_bound])
                before = APSs[:, low_bound:i]
                after = APSs[:, low_high_bound:high_bound]
                compare = numpy.hstack((before, after))
                mm = numpy.mean(numpy.ma.masked_array(compare, numpy.isnan(compare)), axis=1)
                KNN_PBI[:, i] = APSs[:, i] - mm.filled(numpy.nan)

            # Avg Normalised Pairing Percentage
            mins = numpy.nanmin(APSs, 1)
            maxs = numpy.nanmax(APSs, 1)
            with numpy.errstate(divide='ignore'):
                # zero range (all-equal APS) -> inf, filtered by the isfinite guard below
                inv_ranges = numpy.float64(1.0) / (maxs - mins)
            NPPs = -numpy.ones((botlen, botlen))
            for i in range(botlen):
                if numpy.isfinite(inv_ranges[i]):
                    NPPs[i, :] = numpy.float64(100) * (APSs[i, :] - mins[i]) * inv_ranges[i]
                else:
                    NPPs[i, :] = numpy.float64(100)

            # structures.pairing_var, vectorised; NaN var_aps (NULL) fails >= 0 -> prior
            pn = numpy.minimum(battles, structures.rolling_battle_cap)
            with numpy.errstate(invalid='ignore', divide='ignore'):
                pvar = numpy.where(var_aps >= 0, structures.shrink_var(var_aps, numpy.maximum(0, pn - 1)),
                                   structures.prior_var) / pn
            hasCI = alive & (pn >= 1)

            def per_bot(masks, vals=(None, None)):  # each bot's sum of vals (or count) over its sides in mask
                return sum(numpy.bincount(me[m], None if v is None else v[m], botlen)
                           for (me, _), v, m in zip(sides, vals, masks))

            ix = [(opp.clip(0), me.clip(0)) for me, opp in sides]  # dead -> 0, masked by alive
            sideNPP = [NPPs[i] for i in ix]
            sideKNN = [KNN_PBI[i] for i in ix]
            apsOK = [alive & ~numpy.isnan(aps_lo) & (lo != hi)] * 2  # self-pair = NaN diagonal
            nppOK = [alive & ~numpy.isnan(v) for v in sideNPP]
            totalAPS, apsCount = per_bot(apsOK, [aps_lo, 100.0 - aps_lo]), per_bot(apsOK)
            totalNPP, nppCount = per_bot(nppOK, sideNPP), per_bot(nppOK)
            ciSum, ciCount = per_bot([hasCI] * 2, [pvar] * 2), per_bot([hasCI] * 2)
            for i, b in enumerate(bots):
                b.ANPP = float(totalNPP[i] / nppCount[i]) if nppCount[i] > 0 else -1.0
                b.APS = float(totalAPS[i] / apsCount[i]) if apsCount[i] > 0 else -1.0
                if ciCount[i] > 0 and pairCounts[i] > 0:
                    b.APS_CI = 1.96 * math.sqrt(ciSum[i] / (pairCounts[i] * ciCount[i]))
                else:
                    b.APS_CI = -1.0

            # current bots' sides get KNNPBI/NPP (store.DEAD_* vs a retired bot); write changed rows only
            new = [c.copy() for c in derived]  # store.DERIVED_COLS order
            newKNN, newNPP = new[:2], new[2:]
            for s, (me, opp) in enumerate(sides):
                mine = me >= 0
                newKNN[s][mine], newNPP[s][mine] = store.DEAD_KNNPBI, store.DEAD_NPP
                newKNN[s][alive] = numpy.nan_to_num(sideKNN[s][alive], nan=store.DEAD_KNNPBI)
                newNPP[s][alive] = numpy.where(nppOK[s][alive], sideNPP[s][alive], store.DEAD_NPP)
            changed = numpy.zeros(len(rowid), bool)
            for old, upd in zip(derived, new):
                changed |= ~numpy.isclose(old, upd, rtol=0, atol=0, equal_nan=True)
            store.update_pair_derived([c[changed] for c in new + [rowid]])

            KNN_PBI = None
            APSs = None
            NPPs = None

            scores = {b.Name: structures.LiteBot(b) for b in bots}
            store.replace_scores(r.Name, scores)

            r.BatchScoresAccurate = True
            db.put([r])

        elapsed = time.time() - starttime
        logging.info("Success in " + str(round(1000 * elapsed) / 1000) + "s")
        return "Success in " + str(round(1000 * elapsed)) + "ms"
    except Exception:
        logging.exception('')
        elapsed = time.time() - starttime
        logging.info("Error in " + str(round(1000 * elapsed) / 1000) + "s")
        return "Error in " + str(round(1000 * elapsed)) + "ms"
