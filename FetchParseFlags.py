#!/usr/bin/env python
import zlib
import pickle

from google.appengine.ext import db
from google.appengine.api import urlfetch

import structures
from structures import global_dict


def fetch_parse_flags():
    url = "http://robowiki.net/wiki/RoboRumble/Country_Flags?action=raw"

    result = urlfetch.fetch(url, method=urlfetch.GET)
    if result.status_code == 200:
        content = result.content
        if isinstance(content, bytes):
            content = content.decode('utf-8', errors='replace')
        tag = "<pre>"
        endtag = "</pre>"
        startIndex = content.find(tag) + len(tag)
        endIndex = content.find(endtag, startIndex)
        assoc = content[startIndex:endIndex]
        lines = assoc.split("\n")
        flag_map = {}
        for line in lines:
            if len(line) > 0:
                parts = line.split(",")
                if len(parts) == 2 and parts[1] in structures.allowed_flags:
                    flag_map[parts[0]] = parts[1]

        db_map = structures.FlagMap(key_name=structures.default_flag_map)
        db_map.InternalMap = db.Blob(zlib.compress(pickle.dumps(flag_map, pickle.HIGHEST_PROTOCOL)))
        db.put(db_map)
        global_dict[structures.default_flag_map] = db_map.InternalMap

        return "\nSuccess!"
    return "\nfetch failed: status " + str(result.status_code)
