"""
The bot's file storage DB.

One channel - the **Constant Dump Channel** (falling back to the Dump Channel
only if no Constant Dump is set) - holds every file the bot produces, and
each file is stored there **once**:

* An index (Tools.db `filedb`) maps "this chapter, in this exact file
  configuration" -> the message ids holding it. Before downloading anything,
  the worker asks the index; on a hit it server-side copies the stored file
  to the requester (no download, no conversion, no upload), and posts simply
  reference the stored message ids.
* Post flows write a post's files as one **exclusive, contiguous run** of
  message ids. Other writers (archive copies of users' downloads) are queued
  and flushed afterwards, so they can't land in the middle of the run. That
  matters for the File Store Bot: a batch link over a channel it stores in is
  one contiguous id range, and ids with something else in between are
  rejected with `not_contiguous`.
"""
import asyncio
import hashlib
from collections import deque
from contextlib import asynccontextmanager

from bot import Bot, Vars, logger
from Tools.db import filedb_drop, filedb_get, filedb_put


# --------------------------------------------------------------------------- #
# which channel is "the storage"
# --------------------------------------------------------------------------- #
def storage_channel():
  """The single channel files live in. Constant Dump wins; the Dump Channel is
  only the fallback for setups that never configured one."""
  return Vars.CONSTANT_DUMP_CHANNEL or Vars.DUMP_CHANNEL or None


def chat_ref(value):
  """Numeric ids as int, @usernames as-is."""
  try:
    return int(value)
  except (TypeError, ValueError):
    return value


def same_chat(a, b) -> bool:
  if a in (None, "") or b in (None, ""):
    return False
  try:
    return int(a) == int(b)
  except (TypeError, ValueError):
    return str(a).lstrip("@").lower() == str(b).lstrip("@").lower()


# --------------------------------------------------------------------------- #
# the index
# --------------------------------------------------------------------------- #
def make_key(sf, chapter_urls, *parts):
  """Stable id for "these chapters, in this exact output configuration".
  `parts` must include everything that changes the produced file (name, file
  types, quality, password, banners, thumbnail) - two users only share a
  stored file when all of it matches."""
  if not chapter_urls or not all(chapter_urls):
    return None          # can't identify the chapters -> never dedupe
  raw = "\x1f".join([str(sf), "\x1e".join(chapter_urls), *[str(p) for p in parts]])
  return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def remember(key, chat, ids):
  if key and ids:
    filedb_put(key, chat, ids)


async def fetch_stored(key, storage):
  """The stored Messages for `key`, or None. Verifies they still exist - an
  admin may have cleaned the channel - and forgets the entry if they don't."""
  entry = filedb_get(key) if key else None
  if not entry or not same_chat(entry.get("c"), storage):
    return None
  ids = entry.get("i") or []
  if not ids:
    return None
  try:
    msgs = await Bot.get_messages(chat_ref(storage), ids)
  except Exception as e:
    logger.warning(f"storage lookup failed (will process normally): {e}")
    return None          # transient - keep the entry, just don't reuse this time
  if not isinstance(msgs, list):
    msgs = [msgs]
  if len(msgs) != len(ids) or any((not m) or getattr(m, "empty", False) or not m.document for m in msgs):
    filedb_drop(key)
    return None
  return msgs


# --------------------------------------------------------------------------- #
# two workers asking for the same new file at the same moment
# --------------------------------------------------------------------------- #
_inflight = {}


async def wait_inflight(key):
  ev = _inflight.get(key)
  if ev:
    await ev.wait()


def begin(key):
  if not key or key in _inflight:
    return None
  ev = _inflight[key] = asyncio.Event()
  return ev


def end(key, ev):
  if ev is not None:
    ev.set()
    _inflight.pop(key, None)


# --------------------------------------------------------------------------- #
# exclusive writes (contiguous ids) + deferred archive copies
# --------------------------------------------------------------------------- #
_lock = asyncio.Lock()
_pending = deque()
_drainer = None


def busy() -> bool:
  return _lock.locked()


@asynccontextmanager
async def exclusive():
  """Hold this while writing all of one post's files to the storage channel.
  Archive copies submitted meanwhile wait and flush right after."""
  async with _lock:
    yield


def submit(job):
  """Queue an async archive job (a zero-arg coroutine function). It runs as
  soon as no exclusive write is in progress, one job at a time, so each
  job's copies stay adjacent too."""
  global _drainer
  _pending.append(job)
  if _drainer is None or _drainer.done():
    _drainer = asyncio.get_running_loop().create_task(_drain())


async def _drain():
  while _pending:
    async with _lock:
      if not _pending:
        break
      job = _pending.popleft()
      try:
        await job()
      except Exception as e:
        logger.warning(f"archive job failed: {e}")


def contiguous(ids) -> bool:
  ids = sorted(set(ids))
  return bool(ids) and ids[-1] - ids[0] == len(ids) - 1
