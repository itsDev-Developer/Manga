"""Storage layer.

* Every user / premium entry / post / token is its own document (the old code kept
  *everything* in one Mongo document and re-uploaded all of it on every change).
* Reads are served from memory; writes are batched and flushed every few seconds
  from a worker thread, so the event loop never blocks on the database.
* Old single-document databases are migrated automatically on first start.
* With an empty DB_URL a local JSON store (./data/*.json) is used instead of MongoDB.
"""
import asyncio
import copy
import json
import os
import re
import sys
import threading
import time

from loguru import logger

from bot import Vars, remove_site_sf

FLUSH_EVERY = 5  # seconds


# --------------------------------------------------------------------------- #
# backends
# --------------------------------------------------------------------------- #
class _MongoBackend:
  name = "MongoDB"

  def __init__(self, url, dbname):
    from pymongo import MongoClient
    self.client = MongoClient(url, serverSelectionTimeoutMS=8000, maxPoolSize=4)
    self.client.admin.command("ping")
    self.db = self.client[dbname]

  def load(self, table):
    return {str(d.pop("_id")): d for d in self.db[table].find()}

  def save(self, table, upserts: dict, deletes: set):
    from pymongo import DeleteOne, ReplaceOne
    ops = [ReplaceOne({"_id": k}, {**v, "_id": k}, upsert=True) for k, v in upserts.items()]
    ops += [DeleteOne({"_id": k}) for k in deletes]
    if ops:
      self.db[table].bulk_write(ops, ordered=False)


class _JsonBackend:
  name = "local JSON files (./data)"

  def __init__(self, folder="data"):
    self.folder = folder
    os.makedirs(folder, exist_ok=True)
    self._cache = {}

  def _path(self, table):
    return os.path.join(self.folder, f"{table}.json")

  def load(self, table):
    try:
      with open(self._path(table), encoding="utf-8") as f:
        data = json.load(f)
    except FileNotFoundError:
      data = {}
    except Exception as e:
      logger.error(f"Cannot read {self._path(table)}: {e}")
      data = {}
    self._cache[table] = data
    return copy.deepcopy(data)

  def save(self, table, upserts, deletes):
    data = self._cache.setdefault(table, {})
    data.update(upserts)
    for k in deletes:
      data.pop(k, None)
    tmp = self._path(table) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
      json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, self._path(table))


def _make_backend():
  if Vars.DB_URL:
    try:
      backend = _MongoBackend(Vars.DB_URL, Vars.DB_NAME)
      logger.info("Database: MongoDB connected")
      return backend
    except Exception as e:
      logger.error(f"Cannot connect to MongoDB (DB_URL): {e}")
      sys.exit(1)
  logger.warning("DB_URL is empty -> using local JSON storage in ./data (fine for small bots)")
  return _JsonBackend()


backend = _make_backend()


# --------------------------------------------------------------------------- #
# Table: dict mirrored to the DB, one document per key
# --------------------------------------------------------------------------- #
class Table(dict):
  _all = []

  def __init__(self, name):
    super().__init__()
    self.table = name
    self._dirty = set()
    self._deleted = set()
    Table._all.append(self)
    self._load()

  def _load(self):
    raw = backend.load(self.table)
    legacy = raw.get(Vars.DB_NAME)
    self.update({k: v for k, v in raw.items() if k != Vars.DB_NAME})
    if isinstance(legacy, dict) and self.table != "config":
      # old format: ONE document holding every key -> explode into one doc per key
      moved = 0
      for k, v in legacy.items():
        if isinstance(v, dict) and k not in self:
          self[k] = v
          self._dirty.add(k)
          moved += 1
      self._deleted.add(Vars.DB_NAME)
      logger.info(f"Migrated {moved} '{self.table}' entries to the new per-document layout")
    elif isinstance(legacy, dict):
      if "main" not in self:
        self["main"] = legacy
        self._dirty.add("main")
      self._deleted.add(Vars.DB_NAME)
    if self._dirty or self._deleted:
      self.flush()

  def touch(self, key=None):
    """Mark entry (or every entry) as changed."""
    if key is None:
      self._dirty.update(self.keys())
    else:
      self._dirty.add(str(key))

  def drop(self, key):
    key = str(key)
    self.pop(key, None)
    self._dirty.discard(key)
    self._deleted.add(key)

  def snapshot(self):
    """Taken on the event-loop thread, so it is consistent."""
    if not self._dirty and not self._deleted:
      return None
    ups = {k: copy.deepcopy(self[k]) for k in self._dirty if k in self}
    dels = set(self._deleted)
    self._dirty.clear()
    self._deleted.clear()
    return ups, dels

  def restore(self, snap):
    ups, dels = snap
    self._dirty.update(ups)
    self._deleted.update(dels)

  def flush(self):
    snap = self.snapshot()
    if snap:
      try:
        backend.save(self.table, *snap)
      except Exception:
        self.restore(snap)
        raise


_lock = threading.Lock()
_last_warn = 0.0


async def flush_all():
  global _last_warn
  for t in Table._all:
    snap = t.snapshot()
    if not snap:
      continue
    try:
      await asyncio.to_thread(_save, t.table, snap)
    except Exception as e:
      t.restore(snap)
      if time.time() - _last_warn > 60:
        _last_warn = time.time()
        logger.warning(f"DB write failed for '{t.table}', will retry: {e}")


def _save(table, snap):
  with _lock:
    backend.save(table, *snap)


def flush_now():
  """Synchronous flush (shutdown)."""
  for t in Table._all:
    try:
      with _lock:
        t.flush()
    except Exception as e:
      logger.error(f"Final flush of '{t.table}' failed: {e}")


async def flusher():
  """Background writer. MUST be running (main.py starts it) - without it, changes stay in
  memory and only reach the database on /restart."""
  while True:
    await asyncio.sleep(FLUSH_EVERY)
    await flush_all()


def storage_report() -> dict:
  """What is stored and where (for the startup log and /dbstatus)."""
  channels = cfg.get("post_channels")
  return {
    "backend": backend.name,
    "persistent": backend.name == "MongoDB",
    "db_name": Vars.DB_NAME,
    "settings_overrides": len(cfg),
    "post_channels": len(channels) if isinstance(channels, list) else 0,
    "api_keys": len(apk),
    "users": len(uts),
    "premium_users": len(pts),
    "posts": len(pks),
    "pending_writes": sum(len(t._dirty) + len(t._deleted) for t in Table._all),
  }


import atexit
atexit.register(flush_now)    # last resort if the process exits without Bot.stop()


# --------------------------------------------------------------------------- #
# tables
# --------------------------------------------------------------------------- #
uts = Table("users")       # {user_id: {"subs": {web: [..]}, "setting": {..}}}
pts = Table("premium")     # {user_id: {"expiration_timestamp": ts}}
pks = Table("posts")       # {post_id: {...}}
tks = Table("tokens")      # {user_id: {...}}
cfg_table = Table("config")  # {"main": {key: value}}
cfg = cfg_table.setdefault("main", {})
sch = Table("scheduled")   # {sched_id: {..draft.., "run_at": unix_ts}}
apb = Table("autopublish")  # {key: {"sf", "url", "title", "last_chapter", "channel", ...}}
bans = Table("banned")      # {user_id: {"ts", "by"}} - kept apart from `users` so a ban can exist before the user ever starts the bot
apk = Table("apikeys")      # {sha256(key): {"label", "scope", "prefix", "created", "created_by", "last_used", "calls"}}
plog = Table("premiumlog")  # audit trail: {ts-rand: {"ts", "user", "action", "days", "expires", "source", "ref"}}
aref = Table("apirefs")     # idempotency: {sha1(key_label|ref): {"ts", "result"}}
fdb = Table("filedb")      # {key: {"c": storage_chat_id, "i": [message ids]}} - the file-storage index


def sync(user_id=None):
  """Persist user data (one user, or everyone when no id is given)."""
  uts.touch(user_id)


def premuim_sync(user_id=None):
  pts.touch(user_id)


def token_sync(user_id=None):
  tks.touch(user_id)


def posts_sync(post_id=None):
  pks.touch(post_id)


# ---- global bot config ------------------------------------------------------
def get_config(key: str, default=None):
  value = cfg.get(key)
  return default if value is None else value


def _flush_config_soon():
  """Settings change rarely and must survive a redeploy, so write them to the database
  right away instead of waiting for the next periodic flush."""
  try:
    asyncio.get_running_loop().create_task(flush_all())
  except RuntimeError:
    pass   # no running loop (startup / tests): the periodic flusher or shutdown flush will do it


def set_config(key: str, value):
  cfg[key] = value
  cfg_table.touch("main")
  _flush_config_soon()


def del_config(key: str):
  if key in cfg:
    del cfg[key]
    cfg_table.touch("main")
    _flush_config_soon()


# ---- posts ------------------------------------------------------------------
def save_post(post_id: str, data: dict):
  pks[post_id] = data
  pks.touch(post_id)


def get_post(post_id: str):
  return pks.get(post_id)


def delete_post(post_id: str) -> bool:
  if post_id in pks:
    pks.drop(post_id)
    return True
  return False


def all_posts() -> dict:
  return dict(pks)


def bump_post_reads(post_id: str):
  if post_id in pks:
    pks[post_id]["reads"] = pks[post_id].get("reads", 0) + 1
    pks.touch(post_id)


# ---- helpers ----------------------------------------------------------------
_EP_PATTERNS = (
  r"Chapter\s+(\d+(?:\.\d+)?)",
  r"Volume\s+(\d+) Chapter\s+(\d+(?:\.\d+)?)",
  r"Chapter\s+(\d+)\s+-\s+(\d+(?:\.\d+)?)",
  r"(\d+(?:\.\d+)?)",
)
_EP_RE = [re.compile(p) for p in _EP_PATTERNS]


def get_episode_number(text):
  """Extract episode/chapter number from text"""
  text = str(text)
  for pattern in _EP_RE:
    match = pattern.search(text)
    if match:
      return match.group(1) if match.lastindex == 1 else match.group(2)
  return None


def ensure_user(user_id):
  user_id = str(user_id)
  user = uts.get(user_id)
  changed = False
  if user is None:
    user = uts[user_id] = {"subs": {}, "setting": {}}
    changed = True
  # Older schema versions stored "subs" as a flat list; `"subs" not in user`
  # alone wouldn't catch that, since the key *is* present - just the wrong
  # type - which then crashed every `subs.get(...)` call downstream.
  if not isinstance(user.get("subs"), dict):
    user["subs"] = {}
    changed = True
  if not isinstance(user.get("setting"), dict):
    user["setting"] = {}
    changed = True
  if changed:
    uts.touch(user_id)
  return user


def user_setting(user_id) -> dict:
  return ensure_user(user_id)["setting"]


def save_setting(user_id, key, value):
  ensure_user(user_id)["setting"][key] = value
  uts.touch(str(user_id))


# ---- premium ----------------------------------------------------------------
async def add_premium(user_id, time_limit_days):
  user_id = str(user_id)
  pts[user_id] = {"expiration_timestamp": int(time.time()) + int(time_limit_days) * 86400,
                  "Days": int(time_limit_days)}
  pts.touch(user_id)


async def remove_premium(user_id):
  pts.drop(str(user_id))


expired_pending = []   # (user_id, data) dropped below - Tools.premium tells those users their plan ended


async def remove_expired_users():
  now = int(time.time())
  for user, data in [(u, d) for u, d in pts.items() if d.get("expiration_timestamp", 0) < now]:
    pts.drop(user)
    expired_pending.append((user, data))


async def get_all_premuim():
  for user_id, data in list(pts.items()):
    yield user_id, data


async def premium_user(user_id):
  return pts.get(str(user_id))


def is_premium(user_id) -> bool:
  data = pts.get(str(user_id))
  return bool(data and data.get("expiration_timestamp", 0) > time.time())


def get_users(user_id=None):
  """No argument -> list of every known user id. With an id -> that user's data."""
  if user_id is not None:
    return uts.get(str(user_id))
  return [int(u) for u in uts if str(u).lstrip("-").isdigit()]


# ---- subscriptions ----------------------------------------------------------
def _clean_subs(items):
  """Legacy data occasionally has a non-dict entry (e.g. a bare url string
  from an older storage format) mixed into a site's subscription list.
  Skip those instead of crashing on `.get()` - see the get_subs() crash
  history for why this matters."""
  return [s for s in (items or []) if isinstance(s, dict)]


async def add_sub(user_id, rdata, web: str, chapter=None):
  user = ensure_user(user_id)
  data = rdata.load_to_dict()
  subs = user["subs"].setdefault(web, [])
  if not any(s.get("url") == data["url"] for s in _clean_subs(subs)):
    subs.append(data)
  uts.touch(str(user_id))


def get_subs(user_id, manga_url=None, web=None):
  """manga_url given -> True/None (is it subscribed). Otherwise the list of subscriptions."""
  subs = ensure_user(user_id)["subs"]
  webs = [web] if web else list(subs)
  if manga_url:
    return True if any(
      s.get("url") == manga_url for w in webs for s in _clean_subs(subs.get(w))
    ) else None
  return [s for w in webs for s in _clean_subs(subs.get(w))]


async def delete_sub(user_id, manga_url=None, web=None):
  """
  manga_url + web -> remove that manga     manga_url only -> remove it from every site
  web only        -> remove the whole site  nothing        -> remove every subscription
  """
  user = ensure_user(user_id)
  subs = user["subs"]
  if not manga_url:
    if web:
      subs.pop(web, None)
    else:
      subs.clear()
  else:
    for site in ([web] if web else list(subs)):
      if site in subs:
        subs[site] = [s for s in _clean_subs(subs[site]) if s.get("url") != manga_url]
        if not subs[site]:
          del subs[site]
  uts.touch(str(user_id))


async def get_all_subs():
  """Yield (user_id, website_sf, sub) - premium users first."""
  order = [u for u in pts if u in uts] + [u for u in uts if u not in pts]
  for user_id in order:
    subs = (uts.get(user_id) or {}).get("subs")
    if not isinstance(subs, dict):
      continue
    for website_sf, items in list(subs.items()):
      if website_sf in remove_site_sf:
        await delete_sub(user_id, web=website_sf)
        continue
      for sub in _clean_subs(items):
        yield user_id, website_sf, sub


async def save_lastest_chapter(data: dict, user_id: str, web_sf: str):
  """Update the latest chapter for a subscribed manga"""
  try:
    data = {k: data[k] for k in ("title", "url", "lastest_chapter") if k in data}
    subs = (uts.get(str(user_id)) or {}).get("subs", {}).get(web_sf, [])
    for i, sub in enumerate(subs):
      if data.get("url") == sub.get("url") or data.get("title") == sub.get("title"):
        subs[i] = data
        uts.touch(str(user_id))
        break
  except Exception as err:
    logger.exception(f"Error at Save Lastest Chapter: {err}")


# ---- scheduled posts ---------------------------------------------------------
def save_scheduled(sched_id: str, data: dict):
  sch[sched_id] = data
  sch.touch(sched_id)


def get_scheduled(sched_id: str):
  return sch.get(sched_id)


def all_scheduled() -> dict:
  return dict(sch)


def delete_scheduled(sched_id: str) -> bool:
  if sched_id in sch:
    sch.drop(sched_id)
    return True
  return False


# ---- channel auto-publish -----------------------------------------------------
def _autopub_key(sf: str, url: str) -> str:
  import hashlib
  return hashlib.sha1(f"{sf}:{url}".encode()).hexdigest()[:20]


def add_autopublish(sf: str, url: str, title: str, added_by=None) -> str:
  key = _autopub_key(sf, url)
  apb[key] = {
    "sf": sf, "url": url, "title": title, "last_chapter": None,
    "added_by": added_by, "added_at": int(time.time()),
  }
  apb.touch(key)
  return key


def remove_autopublish(key: str) -> bool:
  if key in apb:
    apb.drop(key)
    return True
  return False


def list_autopublish() -> dict:
  return dict(apb)


def set_autopublish_progress(key: str, last_chapter):
  if key in apb:
    apb[key]["last_chapter"] = last_chapter
    apb.touch(key)


# ---- bans ---------------------------------------------------------------------
def is_banned(user_id) -> bool:
  return str(user_id) in bans


def ban_user(user_id, by=None) -> bool:
  """True if this changed anything (False: already banned)."""
  uid = str(int(user_id))
  if uid in bans:
    return False
  bans[uid] = {"ts": int(time.time()), "by": by}
  bans.touch(uid)
  return True


def unban_user(user_id) -> bool:
  uid = str(int(user_id))
  if uid not in bans:
    return False
  bans.drop(uid)
  return True


# ---- file-storage index -------------------------------------------------------
# One entry per stored output (a chapter or merged group, in one specific
# file configuration) -> the message ids holding it in the storage channel.
# See Tools/storage_db.py for how it's used.
def filedb_get(key: str):
  return fdb.get(key)


def filedb_put(key: str, chat, ids: list):
  fdb[key] = {"c": chat, "i": list(ids)}
  fdb.touch(key)


def filedb_drop(key: str):
  if key in fdb:
    fdb.drop(key)


# ---- backup / restore ---------------------------------------------------------
def export_all() -> dict:
  """Full DB dump - every Table that has been created (users, premium, posts,
  tokens, config, scheduled, autopublish, and any added later) is included
  automatically, since they all register themselves in Table._all."""
  return {t.table: dict(t) for t in Table._all}


def import_all(dump: dict, overwrite: bool = False) -> dict:
  """Restore a previous export_all() dump.
  overwrite=False (default): only fills in keys that don't already exist -
    safe to run against a live bot without clobbering newer data.
  overwrite=True: replaces existing keys with the backup's version too.
  Returns {table_name: count_of_keys_written}."""
  tables_by_name = {t.table: t for t in Table._all}
  stats = {}
  for name, data in (dump or {}).items():
    table = tables_by_name.get(name)
    if table is None or not isinstance(data, dict):
      continue
    written = 0
    for k, v in data.items():
      if overwrite or k not in table:
        table[k] = v
        table.touch(k)
        written += 1
    if written:
      stats[name] = written
  return stats
