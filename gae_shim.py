"""Minimal SQLite/in-process replacement for the App Engine bundled services.

Importing this module (before ``main``) registers fake ``google.appengine.*``
modules into ``sys.modules`` so the *unmodified* application code resolves its
``from google.appengine... import ...`` statements against these shims instead
of the real ``appengine-python-standard`` package. On App Engine this module is
never imported, so the production runtime is untouched.

Backends: Datastore -> SQLite (one table per kind, value = pickled property
dict); Memcache -> in-process dict; Task Queue -> a ``queue.Queue`` drained by a
single worker thread (see ``background.py``). Everything assumes a single
process (``gunicorn --workers 1``); concurrency comes from threads.
"""
import collections
import os
import pickle
import queue
import resource
import sqlite3
import threading
import time
import time as _time  # aliased for methods whose param is named ``time`` (GAE API)
import types
import urllib.error
import urllib.request

# Datastore (google.appengine.ext.db)
_DB_PATH = os.environ.get("LITERUMBLE_DB", os.path.join("data", "literumble.db"))


class Blob(bytes):
    """Datastore Blob is just a bytes subclass; pickles/round-trips as bytes."""


class _Property:
    def __init__(self, indexed=True, default=None, **kw):
        self.default = default  # indexed is ignored: nothing is queried by value


class StringProperty(_Property):
    pass


class IntegerProperty(_Property):
    pass


class FloatProperty(_Property):
    pass


class BooleanProperty(_Property):
    pass


class BlobProperty(_Property):
    pass


class DateTimeProperty(_Property):
    pass


class StringListProperty(_Property):
    def __init__(self, indexed=True, default=None, **kw):
        super().__init__(indexed, default if default is not None else [])


def _copy_default(v):
    return list(v) if isinstance(v, list) else v


_kinds = {}  # kind name -> Model subclass, so _get()/delete() can find the class


class _ModelMeta(type):
    def __new__(mcs, name, bases, ns):
        cls = super().__new__(mcs, name, bases, ns)
        props = {}
        for base in bases:
            props.update(getattr(base, "_properties", {}))
        for k, v in ns.items():
            if isinstance(v, _Property):
                props[k] = v.default
        cls._properties = props
        if name != "Model":
            _kinds[name] = cls
        return cls


class _Key:
    def __init__(self, kind, name):
        self.kind = kind
        self._name = name

    def name(self):
        return self._name


class Model(metaclass=_ModelMeta):
    _properties = {}

    def __init__(self, key_name=None, **kwargs):
        self._key_name = key_name
        for pname, default in type(self)._properties.items():
            if pname in kwargs:
                setattr(self, pname, kwargs[pname])
            else:
                setattr(self, pname, _copy_default(default))

    def key(self):
        return _Key(type(self).__name__, self._key_name)

    def put(self):
        put(self)
        return self.key()

    # --- reconstruction from storage (bypasses __init__/defaults) ---
    @classmethod
    def _from_row(cls, key_name, data):
        obj = cls.__new__(cls)
        obj._key_name = key_name
        for pname, default in cls._properties.items():
            setattr(obj, pname, data.get(pname, _copy_default(default)))
        return obj

    @classmethod
    def get_by_key_name(cls, key_name):
        if isinstance(key_name, (list, tuple)):
            return [_get(cls.__name__, n) for n in key_name]
        return _get(cls.__name__, key_name)

    @classmethod
    def all(cls):
        return _Query(cls)


class _Query:
    def __init__(self, cls):
        self.cls = cls

    def run(self, **kw):
        con = _connect()
        _ensure_table(con, self.cls.__name__)
        rows = con.execute(
            "SELECT key_name, data FROM %s" % self.cls.__name__).fetchall()
        for key_name, blob in rows:
            yield self.cls._from_row(key_name, pickle.loads(blob))

    def __iter__(self):
        return self.run()


class _Future:
    def __init__(self, value):
        self._value = value

    def get_result(self):
        return self._value


# SQLite plumbing (single connection, WAL, global write lock)
_con = None
_con_lock = threading.Lock()
_tables = set()


def _connect():
    global _con
    if _con is None:
        d = os.path.dirname(_DB_PATH)
        if d:
            os.makedirs(d, exist_ok=True)
        _con = sqlite3.connect(_DB_PATH, check_same_thread=False)
        _con.execute("PRAGMA journal_mode=WAL")
    return _con


def _ensure_table(con, kind):
    if kind not in _tables:
        con.execute(
            "CREATE TABLE IF NOT EXISTS %s "
            "(key_name TEXT PRIMARY KEY, data BLOB)" % kind)
        con.commit()
        _tables.add(kind)


def _dump(entity):
    return {p: getattr(entity, p, None) for p in type(entity)._properties}


def put(entities):
    if not isinstance(entities, (list, tuple)):
        entities = [entities]
    with _con_lock:
        con = _connect()
        for e in entities:
            _ensure_table(con, type(e).__name__)
            blob = pickle.dumps(_dump(e), pickle.HIGHEST_PROTOCOL)
            con.execute(
                "INSERT OR REPLACE INTO %s (key_name, data) VALUES (?, ?)"
                % type(e).__name__, (e._key_name, sqlite3.Binary(blob)))
        con.commit()
    return [e.key() for e in entities]


def _get(kind, key_name):
    cls = _kinds.get(kind)
    if cls is None:
        return None
    con = _connect()
    _ensure_table(con, kind)
    row = con.execute(
        "SELECT data FROM %s WHERE key_name=?" % kind, (key_name,)).fetchone()
    if row is None:
        return None
    return cls._from_row(key_name, pickle.loads(row[0]))


def get(key):
    return _get(key.kind, key.name())


def get_async(key):
    return _Future(get(key))


def delete(keys):
    if not isinstance(keys, (list, tuple)):
        keys = [keys]
    with _con_lock:
        con = _connect()
        for k in keys:
            key = k.key() if isinstance(k, Model) else k
            _ensure_table(con, key.kind)
            con.execute(
                "DELETE FROM %s WHERE key_name=?" % key.kind, (key.name(),))
        con.commit()


class Key:
    @staticmethod
    def from_path(kind, name):
        return _Key(kind, name)


# Memcache (google.appengine.api.memcache) -- in-process dict + TTL
class _Memcache:
    def __init__(self):
        self._d = {}
        self._lock = threading.Lock()

    def _live(self, key):
        item = self._d.get(key)
        if item is None:
            return None
        value, expiry = item
        if expiry is not None and time.time() > expiry:
            self._d.pop(key, None)
            return None
        return (value,)

    def get(self, key):
        with self._lock:
            hit = self._live(key)
            return hit[0] if hit else None

    def get_multi(self, keys):
        out = {}
        with self._lock:
            for k in keys:
                hit = self._live(k)
                if hit:
                    out[k] = hit[0]
        return out

    def set(self, key, value, time=None):
        expiry = (_time.time() + time) if time else None
        with self._lock:
            self._d[key] = (value, expiry)
        return True

    def set_multi(self, mapping, time=None):
        expiry = (_time.time() + time) if time else None
        with self._lock:
            for k, v in mapping.items():
                self._d[k] = (v, expiry)
        return []

    def delete(self, key):
        with self._lock:
            self._d.pop(key, None)

    def delete_multi(self, keys):
        with self._lock:
            for k in keys:
                self._d.pop(k, None)
        return True


class Client:
    """memcache.Client(): the async writes are just synchronous sets here."""

    def set_multi_async(self, mapping, time=None):
        _memcache.set_multi(mapping, time=time)
        return _Future(None)


_memcache = _Memcache()


# Task queue (google.appengine.api.taskqueue) -- FIFO drained by background.py
class Error(Exception):
    pass


_task_queue = queue.Queue()
_exec_times = collections.deque()  # timestamps of processed tasks (rolling 60s)


def _record_execution():
    now = time.time()
    _exec_times.append(now)
    while _exec_times and _exec_times[0] < now - 60:
        _exec_times.popleft()


class _Stats:
    def __init__(self):
        self.tasks = _task_queue.qsize()
        self.executed_last_minute = len(_exec_times)  # _record_execution prunes to 60s


class TaskQueue:
    def fetch_statistics_async(self):
        return _Future(_Stats())


def add(url, payload=None, headers=None, **kw):
    _task_queue.put((url, payload, dict(headers or {})))


# Users, urlfetch, runtime, mail
def is_current_user_admin():
    try:
        from flask import request
        return request.remote_addr in ("127.0.0.1", "::1")
    except Exception:
        return False


GET = "GET"
POST = "POST"


class _FetchResult:
    def __init__(self, status_code, content):
        self.status_code = status_code
        self.content = content


def fetch(url, method=GET, payload=None, headers=None, deadline=None, **kw):
    req = urllib.request.Request(url, data=payload, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=deadline or 30) as r:
            return _FetchResult(r.getcode(), r.read())
    except urllib.error.HTTPError as e:
        return _FetchResult(e.code, e.read())
    except (urllib.error.URLError, OSError):
        return _FetchResult(0, b"")  # no network: caller treats non-200 as failure


class _Mem:
    @property
    def current(self):
        # ru_maxrss is KB on Linux
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def memory_usage():
    return _Mem()


def wrap_wsgi_app(app):
    return app


class OverQuotaError(Exception):
    pass


# Build the fake module tree and register it in sys.modules
def _install():
    import sys

    def mod(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m
        return m

    db_mod = mod(
        "google.appengine.ext.db",
        Model=Model, Blob=Blob, Key=Key,
        StringProperty=StringProperty, IntegerProperty=IntegerProperty,
        FloatProperty=FloatProperty, BooleanProperty=BooleanProperty,
        BlobProperty=BlobProperty, StringListProperty=StringListProperty,
        DateTimeProperty=DateTimeProperty,
        put=put, get=get, get_async=get_async, delete=delete)

    memcache_mod = mod(
        "google.appengine.api.memcache",
        get=_memcache.get, get_multi=_memcache.get_multi,
        set=_memcache.set, set_multi=_memcache.set_multi,
        delete=_memcache.delete, delete_multi=_memcache.delete_multi,
        Client=Client)

    taskqueue_mod = mod(
        "google.appengine.api.taskqueue",
        add=add, Queue=TaskQueue, Error=Error,
        _task_queue=_task_queue, _record_execution=_record_execution)

    users_mod = mod("google.appengine.api.users",
                    is_current_user_admin=is_current_user_admin)

    urlfetch_mod = mod("google.appengine.api.urlfetch",
                       fetch=fetch, GET=GET, POST=POST)

    mail_mod = mod("google.appengine.api.mail",
                   send_mail=lambda *a, **k: None)

    apiproxy_errors_mod = mod("google.appengine.runtime.apiproxy_errors",
                              OverQuotaError=OverQuotaError, Error=Error)

    runtime_mod = mod("google.appengine.runtime",
                      memory_usage=memory_usage,
                      apiproxy_errors=apiproxy_errors_mod)

    api_mod = mod(
        "google.appengine.api",
        memcache=memcache_mod, taskqueue=taskqueue_mod, users=users_mod,
        urlfetch=urlfetch_mod, mail=mail_mod, runtime=runtime_mod,
        wrap_wsgi_app=wrap_wsgi_app)

    ext_mod = mod("google.appengine.ext", db=db_mod)

    appengine_mod = mod("google.appengine",
                        api=api_mod, ext=ext_mod, runtime=runtime_mod)

    # google may already exist (namespace package); attach appengine to it.
    google_mod = sys.modules.get("google") or mod("google")
    google_mod.appengine = appengine_mod
    sys.modules["google"] = google_mod


_install()
