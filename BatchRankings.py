#!/usr/bin/env python
import datetime
import gc
import logging
import math
import pickle
import sys
import time
import zlib
from operator import attrgetter

import numpy
from flask import request
from google.appengine.api import memcache, runtime, taskqueue
from google.appengine.ext import db

import structures
from structures import load_blob


def list_split(alist, split_size):
    return [alist[i:i + split_size] for i in range(0, len(alist), split_size)]


def dict_split(d, chunk_size=1):
    items = list(d.items())
    return [
        dict(items[i:i + chunk_size])
        for i in range(0, len(items), chunk_size)
    ]


def queue_batch_rankings():
    taskqueue.add(url='/BatchRankings',
                  payload=request.query_string)
    return "queued"


def queue_hourly_batch_rankings():
    payload = "write=true" if datetime.datetime.now(datetime.timezone.utc).hour % 3 == 0 else ""
    taskqueue.add(url='/BatchRankings', payload=payload)
    return "queued"


def start_backend():
    return ""


def batch_rankings():
    starttime = time.time()
    try:
        cutoff_date = datetime.datetime.now() + datetime.timedelta(-365)
        cutoff_date_string = cutoff_date.strftime("%Y-%m-%d %H:%M:%S")

        parts = request.get_data().decode('utf-8').split("&")
        requests = {}

        if parts is not None and parts[0] != "":
            for pair in parts:
                ab = pair.split('=', 1)
                requests[ab[0]] = ab[1] if len(ab) > 1 else ""

        force = bool(requests.get("force", False))
        write = bool(requests.get("write", False))
        minwrite = bool(requests.get("minwrite", False))

        rpcList = []
        client = memcache.Client()

        q = structures.Rumble.all()
        rumbles = []
        for r in q.run():
            memr = memcache.get(r.Name)
            if memr is not None:
                r = memr
            if r.BatchScoresAccurate and not force:
                continue
            rumbles.append(r)

        for r in rumbles:
            scoresdicts = load_blob(r.ParticipantsScores, {})
            entries = len(scoresdicts) if hasattr(scoresdicts, "__len__") else 0
            r.__dict__["entries"] = entries
        rumbles.sort(key=lambda r: -r.__dict__["entries"])

        first = True
        for r in rumbles:
            if not first:
                time.sleep(5)
                gc.collect()
                gc.collect(2)
            first = False

            logging.info("mem usage at start of " + r.Name + ": " + str(runtime.memory_usage().current) + "MB")
            scores = load_blob(r.ParticipantsScores, {})
            if not isinstance(scores, dict):
                scores = {}

            if len(scores) == 0:
                continue

            r.ParticipantsScores = None

            particHash = [p + "|" + r.Name for p in scores]

            particSplit = list_split(particHash, 32)
            ppDict = {}
            for l in particSplit:
                ppDict.update(memcache.get_multi(l))
                time.sleep(0.1)

            particSplit = None

            bots = [ppDict.get(h, None) for h in particHash]

            botsdict = {}

            missingHashes = []
            missingIndexes = []
            for i in range(len(bots)):
                if bots[i] is None:
                    missingHashes.append(particHash[i])
                    missingIndexes.append(i)

                elif isinstance(bots[i], structures.BotEntry):
                    bots[i] = structures.CachedBotEntry(bots[i])

            if len(missingHashes) > 0:
                bmis = structures.BotEntry.get_by_key_name(missingHashes)

                lostList = []

                for i in range(len(missingHashes)):
                    if bmis[i] is not None:
                        cb = structures.CachedBotEntry(bmis[i])
                        bots[missingIndexes[i]] = cb
                        botsdict[missingHashes[i]] = cb

                    else:
                        bots[missingIndexes[i]] = None
                        lostList.append(missingHashes[i])

            while len(particHash) > 0:
                particHash.pop()
            particHash = None

            while len(missingHashes) > 0:
                missingHashes.pop()
            missingHashes = None

            while len(missingIndexes) > 0:
                missingIndexes.pop()
            missingIndexes = None

            logging.info("mem usage after loading bots: " + str(runtime.memory_usage().current) + "MB")

            bots = [b for b in bots if b is not None]

            get_key = attrgetter("APS")
            bots.sort(key=lambda b: get_key(b), reverse=True)

            gc.collect()

            botIndexes = {}
            for i, b in enumerate(bots):
                sys.intern(b.Name)
                botIndexes[b.Name] = i
                b.VoteScore = 0.

            botlen = len(bots)
            APSs = numpy.empty([botlen, botlen])
            APSs.fill(numpy.nan)
            totalAlivePairs = 0
            for i, b in enumerate(bots):
                pairings = load_blob(b.PairingsList, [])
                if not isinstance(pairings, list):
                    pairings = []
                removes = []
                alivePairings = 0
                for q_idx, p in enumerate(pairings):
                    j = botIndexes.get(p.Name, -1)
                    if j != -1:
                        APSs[j, i] = numpy.float64(p.APS)
                        p.Alive = True
                        alivePairings += 1
                    else:
                        removes.append(q_idx)
                b.Pairings = alivePairings
                totalAlivePairs += alivePairings
                removes.reverse()
                removed = False
                for q_idx in removes:
                    p = pairings[q_idx]
                    if p.LastUpload < cutoff_date_string:
                        removed = True
                        pairings.pop(q_idx)
                    else:
                        if p.Alive:
                            removed = True
                        p.Alive = False
                if removed:
                    b.PairingsList = zlib.compress(pickle.dumps(pairings, -1), 1)

            gc.collect()

            APSs += numpy.float64(100) - APSs.transpose()
            APSs *= numpy.float64(0.5)

            numpy.fill_diagonal(APSs, numpy.nan)

            gc.collect()
            logging.info(str(len(bots)) + " bots loaded, total of " + str(totalAlivePairs) + " alive pairings")
            logging.info("mem usage after unzipping pairings: " + str(runtime.memory_usage().current) + "MB")

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
            inv_ranges = numpy.float64(1.0) / (maxs - mins)
            NPPs = -numpy.ones((botlen, botlen))
            for i in range(botlen):
                if numpy.isfinite(inv_ranges[i]):
                    NPPs[i, :] = numpy.float64(100) * (APSs[i, :] - mins[i]) * inv_ranges[i]
                else:
                    NPPs[i, :] = numpy.float64(100)

            changedBots = []

            botsdict = {}

            maxPerPair = structures.rolling_battle_cap
            for i, b in enumerate(bots):
                pairings = load_blob(b.PairingsList, [])
                if not isinstance(pairings, list):
                    pairings = []
                nppCount = 0
                totalNPP = 0.0

                apsCount = 0
                totalAPS = 0.0

                aliveCount = 0

                ci_var_sum = 0.0
                ci_count = 0

                changed = False
                for p in pairings:
                    j = botIndexes.get(p.Name, -1)
                    if j != -1:
                        p.Alive = True
                        changePotential = (p.KNNPBI == 0.0 and p.NPP == -1)

                        aliveCount += 1

                        pvar = p.__dict__.get("Var_APS", -1.0)
                        if pvar is not None and pvar >= 0 and int(p.Battles) >= 2:
                            ci_var_sum += max(0.0, pvar) / (min(int(p.Battles), maxPerPair) - 1)
                            ci_count += 1
                        p.KNNPBI = float(KNN_PBI[j, i])
                        p.NPP = float(NPPs[j, i])

                        if not numpy.isnan(APSs[j, i]):
                            p.APS = float(APSs[j, i])
                            totalAPS += p.APS
                            apsCount += 1

                        if numpy.isnan(p.KNNPBI):
                            p.KNNPBI = 0

                        if numpy.isnan(p.NPP):
                            p.NPP = -1
                        else:
                            totalNPP += p.NPP
                            nppCount += 1

                        if changePotential and p.KNNPBI != 0.0 and p.NPP != -1:
                            changed = True
                    else:
                        p.Alive = False
                        p.KNNPBI = 0
                        p.NPP = -1

                if nppCount > 0:
                    b.ANPP = float(totalNPP / nppCount)
                else:
                    b.ANPP = -1.0
                if apsCount > 0:
                    b.APS = float(totalAPS / apsCount)
                else:
                    b.APS = -1.0

                if ci_count > 0 and aliveCount > 0:
                    b.APS_CI = 1.96 * math.sqrt(ci_var_sum / (aliveCount * ci_count))
                else:
                    b.APS_CI = -1.0

                b.PairingsList = zlib.compress(pickle.dumps(pairings, -1), 1)
                b.Pairings = aliveCount
                if b.Pairings > 0:
                    botsdict[b.key_name] = b
                if changed:
                    changedBots.append(b)

            KNN_PBI = None
            APSs = None
            NPPs = None
            logging.info("mem usage after zipping: " + str(runtime.memory_usage().current) + "MB")

            gc.collect()
            if len(botsdict) > 0:
                splitlist = dict_split(botsdict, 20)
                logging.info("split bots into " + str(len(splitlist)) + " sections")

                for d in splitlist:
                    rpcList.append(client.set_multi_async(d))
                    time.sleep(.5)

                logging.info("wrote " + str(len(botsdict)) + " bots to memcache")

            botsdict.clear()
            botsdict = None

            scores = {b.Name: structures.LiteBot(b) for b in bots}

            r.ParticipantsScores = None
            gc.collect()

            r.ParticipantsScores = db.Blob(zlib.compress(pickle.dumps(scores, pickle.HIGHEST_PROTOCOL), 3))
            logging.info("mem usage after participants zipping: " + str(runtime.memory_usage().current) + "MB")
            scores = None

            if write:
                writebots = [None] * len(bots)
                for i, b in enumerate(bots):
                    putb = structures.BotEntry(key_name=b.key_name)
                    putb.init_from_cache(b)
                    writebots[i] = putb
                write_lists = list_split(writebots, 50)
                for subset in write_lists:
                    db.put(subset)
                    time.sleep(0.1)
                logging.info("wrote " + str(len(writebots)) + " bots to database")

            while len(bots) > 0:
                bots.pop()
            bots = None

            if minwrite:
                writebots = [None] * len(changedBots)
                for i, b in enumerate(changedBots):
                    putb = structures.BotEntry(key_name=b.key_name)
                    putb.init_from_cache(b)
                    writebots[i] = putb
                write_lists = list_split(writebots, 50)
                for subset in write_lists:
                    db.put(subset)
                    time.sleep(0.1)
                logging.info("wrote " + str(len(writebots)) + " changed bots to database")

            while len(changedBots) > 0:
                changedBots.pop()
            changedBots = None
            gc.collect()

            if write or minwrite:
                r.BatchScoresAccurate = True

            rpcList.append(client.set_multi_async({r.Name: r}))

            db.put([r])
            r = None
            logging.info("mem usage after write: " + str(runtime.memory_usage().current) + "MB")

        for rpc in rpcList:
            rpc.get_result()

        elapsed = time.time() - starttime
        logging.info("Success in " + str(round(1000 * elapsed) / 1000) + "s")
        return "Success in " + str(round(1000 * elapsed)) + "ms"
    except Exception:
        logging.exception('')
        elapsed = time.time() - starttime
        logging.info("Error in " + str(round(1000 * elapsed) / 1000) + "s")
        return "Error in " + str(round(1000 * elapsed)) + "ms"
