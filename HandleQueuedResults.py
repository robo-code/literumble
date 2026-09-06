#!/usr/bin/env python
import datetime
import json
import logging
import math
import pickle
import random
import time
import zlib
from collections import deque

from flask import request

import store
import structures
from structures import global_dict, load_blob

def handle_queued_results():
    out = []
    results = json.loads(request.get_data(as_text=True))

    rumble = results.get("game", "ERROR")

    bota = results.get("fname")
    botb = results.get("sname")
    logging.debug("Bots : " + bota + " vs. " + botb)

    bd = [[bota, rumble], [botb, rumble]]

    game = structures.Rumble.get_by_key_name(rumble)

    newBot = False

    # bots and pairings both come from the store; the score rows are the
    # single source of truth.
    bots = [None, None]
    pairingsarray = [[], []]
    for i in [0, 1]:
        pairingsarray[i] = store.load_pairings(rumble, bd[i][0])

    if game is None:
        game = structures.Rumble(
            key_name=rumble,
            Name=rumble, Rounds=int(results["rounds"]),
            Field=results["field"], Melee=results["melee"] == "YES",
            Teams=results["teams"] == "YES", TotalUploads=0,
            MeleeSize=10)
        out.append("CREATED NEW GAME TYPE " + rumble + "\n")
        logging.info("Created new game type: " + rumble)
    else:
        field = game.Field == results["field"]
        rounds = (game.Rounds == int(results["rounds"]))
        teams = game.Teams == (results["teams"] == "YES")
        melee = game.Melee == (results["melee"] == "YES")
        allowed = field and rounds and teams and melee
        if not allowed:
            errstr = "OK. ERROR. Incorrect " + rumble + " config: "
            errorReasons = []
            if not field:
                errorReasons.append("field size ")
            if not rounds:
                errorReasons.append("number of rounds ")
            if not teams:
                errorReasons.append("teams ")
            if not melee:
                errorReasons.append("melee ")
            logging.error(errstr + ", ".join(errorReasons) + "  User: " + results["user"])
            return "".join(out)

    scores = store.load_scores(rumble)

    nowStr = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for i in [0, 1]:
        name = bd[i][0]
        bots[i] = scores.get(name, None)
        if bots[i] is None:
            bots[i] = structures.LiteBot(loadDict={
                "Name": name, "Battles": 0, "Pairings": 0, "APS": 0.0,
                "Survival": 0.0, "PL": 0, "VoteScore": 0.0, "Rumble": rumble,
                "LastUpload": nowStr, "Active": False, "ANPP": 0.0,
                "APS_CI": -1.0, "Uploaders": []})
        if not bots[i].Active or name not in scores:
            bots[i].Active = True
            scores[name] = structures.LiteBot(bots[i])
            newBot = True
            out.append("Added " + name + " to " + rumble + "\n")
            logging.info("added new bot!")

    scorea = float(results["fscore"])
    scoreb = float(results["sscore"])

    if scorea + scoreb > 0:
        APSa = 100 * scorea / (scorea + scoreb)
    else:
        APSa = 50

    survivala = float(results["fsurvival"])
    survivalb = float(results["ssurvival"])

    survivala = 100.0 * survivala / game.Rounds
    survivalb = 100.0 * survivalb / game.Rounds

    battleTimeStr = results.get("time", None)
    if battleTimeStr is not None:
        battleTime = datetime.datetime.utcfromtimestamp(int(battleTimeStr) / 1e3)
        lastUploadStr = battleTime.strftime("%Y-%m-%d %H:%M:%S")
    else:
        lastUploadStr = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for b, pairings in zip(bots, pairingsarray):
        if len(pairings) > 0:
            removes = []
            for p in pairings:
                try:
                    p.APS = float(p.APS)
                    p.Survival = float(p.Survival)
                    p.Battles = int(p.Battles)
                except (TypeError, ValueError, AttributeError):
                    removes.append(pairings.index(p))
                    continue

            removes.sort(reverse=True)
            for i in removes:
                pairings.pop(i)

    apair = None
    for p in pairingsarray[0]:
        if p.Name == botb:
            apair = p
    if apair is None:
        apair = structures.ScoreSet(name=botb)
        pairingsarray[0].append(apair)

    bpair = None
    for p in pairingsarray[1]:
        if p.Name == bota:
            bpair = p
    if bpair is None:
        bpair = structures.ScoreSet(name=bota)
        pairingsarray[1].append(bpair)

    if (apair.Battles == 1
            and battleTimeStr is not None
            and apair.APS == APSa
            and apair.Survival == survivala
            and apair.LastUpload == lastUploadStr):
        logging.info("Discarding duplicate battle: " + bota + " vs " + botb)
        return "OK. Duplicate battle discarded."

    for b, pairings in zip(bots, pairingsarray):
        i = 0
        while i < len(pairings):
            if pairings[i].Name == b.Name:
                pairings.pop(i)
                continue

            if pairings[i].Name in scores:
                pairings[i].Alive = True
            else:
                pairings[i].Alive = False

            i += 1

    aBattles = apair.Battles

    maxPerPair = 10000 // len(bots)
    if aBattles > maxPerPair:
        aBattles = maxPerPair

    inv_ab = 1.0 / (aBattles + 1.0)

    apaps_old = apair.APS
    apair.APS *= float(aBattles) * inv_ab
    apair.APS += APSa * inv_ab

    delta_a = APSa - apaps_old
    var_a_old = apair.__dict__.get("Var_APS", -1.0)
    if var_a_old is None or var_a_old < 0:
        var_a_old = 0.0
    apair.Var_APS = (1.0 - inv_ab) * (var_a_old + inv_ab * delta_a * delta_a)
    bpair.Var_APS = apair.Var_APS

    apair.__dict__["Min_APS"] = min(APSa, apair.__dict__.get("Min_APS", 100))
    bpair.APS = 100 - apair.APS
    bpair.__dict__["Min_APS"] = min(100 - APSa, bpair.__dict__.get("Min_APS", 100))

    apair.Survival *= float(aBattles) * inv_ab
    apair.Survival += survivala * inv_ab

    bpair.Survival *= float(aBattles) * inv_ab
    bpair.Survival += survivalb * inv_ab

    apair.Battles += 1
    bpair.Battles = apair.Battles

    apair.LastUpload = lastUploadStr
    bpair.LastUpload = apair.LastUpload

    for b, pairings in zip(bots, pairingsarray):
        aps = 0.0
        survival = 0.0
        pl = 0
        battles = 0
        alivePairings = 0
        ci_var_sum = 0.0
        ci_count = 0
        if len(pairings) > 0:
            for p in pairings:
                if p.Alive:
                    aps += p.APS
                    survival += p.Survival
                    if p.APS > 50:
                        pl += 1
                    else:
                        pl -= 1

                    battles += p.Battles
                    alivePairings += 1

                    pn = min(int(p.Battles), maxPerPair)
                    if pn >= 1:
                        ci_var_sum += structures.pairing_var(p, pn) / pn
                        ci_count += 1

            if alivePairings > 0:
                aps /= alivePairings
                survival /= alivePairings
            b.APS = aps
            b.Survival = survival
            b.PL = pl
            b.Battles = battles
            if ci_count > 0 and alivePairings > 0:
                b.APS_CI = 1.96 * math.sqrt(ci_var_sum / (alivePairings * ci_count))
            else:
                b.APS_CI = -1.0

        b.LastUpload = apair.LastUpload
        b.Pairings = alivePairings

    # only this battle's pair changed; write its single canonical row (both sides)
    store.upsert_pair(rumble, bota, apair, botb, bpair)

    game.TotalUploads += 1

    game.LastUpload = apair.LastUpload
    game.AvgBattles = game.AvgBattles * 0.99 + 0.005 * (bots[0].Battles + bots[1].Battles)
    if game.Uploaders is None:
        uploaders = None
    else:
        uploaders = load_blob(game.Uploaders, {})
    if uploaders is None or not isinstance(uploaders, dict) or len(uploaders) == 0:
        uploaders = {}
    uploaderName = results["user"]

    try:
        uploader = uploaders[uploaderName]
        uploader.latest = apair.LastUpload
        uploader.total += 1
    except KeyError:
        uploader = structures.User(name=uploaderName)
        uploaders[uploaderName] = uploader
    game.Uploaders = zlib.compress(pickle.dumps(uploaders, -1), 1)

    for b in bots:
        try:
            bUploaders = b.Uploaders
            if uploaderName not in bUploaders:
                bUploaders.append(uploaderName)
        except AttributeError:
            b.__dict__["Uploaders"] = [uploaderName]

    for b in bots:
        if b.Name in scores:
            lb = scores[b.Name]
            b.ANPP = lb.ANPP
            b.VoteScore = lb.VoteScore
            scores.pop(b.Name, 1)
        scores[b.Name] = structures.LiteBot(b)

    # only the two battling bots changed; write just their rows
    store.upsert_scores(rumble, [scores[bots[0].Name], scores[bots[1].Name]])
    game.Participants = []

    if game.BatchScoresAccurate:
        game.BatchScoresAccurate = False

    game.put()
    global_dict[rumble] = game

    scoreVals = list(scores.values())
    maxPairs = len(scoreVals) - 1

    if game.PriorityBattles and ((not game.Melee) or
                                 (random.random() < 0.0222
                                  or bots[0].Pairings != maxPairs or bots[1].Pairings != maxPairs
                                  or min([bots[0].Battles, bots[1].Battles]) == min([b.Battles for b in scoreVals])
                                  )):
        priobot = None
        priopairs = None

        missingPairings = [b for b in scoreVals if b.Pairings != maxPairs]

        priobot2 = None

        if len(missingPairings) > 0:
            total = 0
            weighted = [(abs(maxPairs - b.Pairings), b) for b in missingPairings]

            for t in weighted:
                total += t[0]
            running = 0
            point = random.randint(0, max(total - 1, 0))
            for t in weighted:
                running += t[0]
                if running > point:
                    priobot = t[1]
                    break
            if priobot is not None and priobot.Name == bots[0].Name:
                priobot = bots[0]
                priopairs = pairingsarray[0]
            elif priobot is not None and priobot.Name == bots[1].Name:
                priobot = bots[1]
                priopairs = pairingsarray[1]
            elif priobot is not None:
                if priobot.Pairings < maxPairs:
                    priopairs = store.load_pairings(rumble, priobot.Name)
                    logging.info("loaded pairings for local search")
                else:
                    priobot2 = random.choice(scoreVals).Name
                    catch = 10
                    while priobot2 == priobot.Name and catch > 0:
                        priobot2 = random.choice(scoreVals).Name
                        catch -= 1
                    if catch == 0:
                        priobot2 = None
                        logging.info("repeatedly found same bot for prio")
                    else:
                        logging.info("global min search successful for non-paired bot")

            if priobot is not None and priobot2 is None and priopairs is not None:
                pairsdict = {}
                for b in priopairs:
                    pairsdict[b.Name] = b

                possPairs = []
                for p in scores:
                    if p not in pairsdict and p != priobot.Name:
                        possPairs.append(p)

                if len(possPairs) > 0:
                    priobot2 = random.choice(possPairs)
                    logging.info("successful local search for new pair")
                else:
                    logging.info("unsuccessful local search for new pair")
        else:
            active = [b for b in scoreVals if b.Active]
            unmeasured = [b for b in active if getattr(b, "APS_CI", -1.0) < 0]
            if unmeasured:
                priobot = random.choice(unmeasured)
            else:
                point = random.uniform(0, sum(b.APS_CI for b in active))
                running = 0.0
                priobot = active[-1]
                for b in active:
                    running += b.APS_CI
                    if running >= point:
                        priobot = b
                        break

            if priobot.Name == bots[0].Name:
                priopairs = pairingsarray[0]
            elif priobot.Name == bots[1].Name:
                priopairs = pairingsarray[1]
            else:
                priopairs = store.load_pairings(rumble, priobot.Name)
                logging.info("loaded pairings for global search")

        if priobot2 is None and priopairs is not None and priobot is not None:
            alive = [p for p in priopairs
                     if p.Name != priobot.Name and p.Name in scores and scores[p.Name].Active]
            unknown = [p for p in alive if structures.pairing_ci(p) is None]
            if unknown:
                minbat = min(int(p.Battles) for p in unknown)
                poss = [p for p in unknown if int(p.Battles) <= minbat]
                if len(poss) < min(50, 0.5 * len(scores)):
                    poss = [p for p in unknown if int(p.Battles) <= minbat + 1]
                priobot2 = random.choice(poss).Name
                logging.info("successful local search for undefined-CI pair")
            elif alive:
                cap = structures.rolling_battle_cap
                def reduction(p):
                    n = min(int(p.Battles), cap)
                    var = structures.pairing_var(p, n)
                    base = var / (n * (n + 1.0))
                    if abs(p.APS - 50.0) < 1.96 * math.sqrt(var / n):
                        base *= 10.0
                    return base
                scored = [(reduction(p), p) for p in alive]
                best = max(r for r, p in scored)
                band = [p for r, p in scored if r >= 0.5 * best]
                if len(band) < min(50, 0.5 * len(scores)):
                    band = [p for r, p in scored if r >= 0.25 * best]
                priobot2 = random.choice(band).Name
                logging.info("successful local search for high-CI pair")
            else:
                logging.info("unsuccessful local search for CI pair")

        if priobot is not None and priobot2 is not None:
            priobots = [priobot.Name, priobot2]
            priobots = [b.replace(' ', '_') for b in priobots]

            prio_string = "[" + ",".join(priobots) + "]\n"
            logging.info("adding priority battle: " + prio_string + ", " + rumble)

            rq_name = rumble + "|queue"
            try:
                rumble_queue = global_dict[rq_name]
                rumble_queue.append(prio_string)
            except KeyError:
                logging.info("No queue for rumble " + rumble + ", adding one!")
                global_dict[rq_name] = deque()
                rumble_queue = global_dict[rq_name]
                rumble_queue.append(prio_string)
        else:
            logging.info("no suitable priority battle found in " + rumble)
            if priobot is None:
                logging.info("priobot is None")
            else:
                logging.info("priobot2 is None")
    else:
        logging.info("no priority battle attempted for " + rumble)

    out.append("\nOK. " + bota + " vs " + botb + " received")
    return "".join(out)
