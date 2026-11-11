import asyncio
import random
import re
import string
from collections import OrderedDict
from os import path as ospath
from typing import Dict, Optional, Tuple

import pyrogram.errors
from pyrogram.errors import FloodWait

from bot import Bot, Vars, logger
from .db import ensure_user, get_episode_number, is_premium, uts
from .img2pdf import make_thumb, normalize_jpeg, thumbnali_images


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def igrone_error(func, sync=False):
  """Wrap `func` so any exception becomes None (name kept for compatibility)."""
  async def wrapper(*args, **kwargs):
    try:
      if sync:
        return await asyncio.to_thread(func, *args, **kwargs)
      return await func(*args, **kwargs)
    except Exception:
      return None
  return wrapper


_QUIET = (pyrogram.errors.QueryIdInvalid, pyrogram.errors.MessageNotModified)


def retry_on_flood(func, max_wait=120):
  """Await `func`, sleeping through FloodWait. Harmless races (button already answered,
  message not modified) return None; every other error is raised to the caller."""
  async def wrapper(*args, **kwargs):
    for _ in range(5):
      try:
        return await func(*args, **kwargs)
      except FloodWait as e:
        wait = min(e.value + 1, max_wait)
        logger.warning(f"FloodWait: waiting {wait}s")
        await asyncio.sleep(wait)
      except _QUIET:
        return None
    return await func(*args, **kwargs)
  return wrapper


class LRU(OrderedDict):
  """Dict that forgets its oldest entries - keeps callback caches (which hold scraped
  pages) from growing forever."""

  def __init__(self, maxsize=300):
    super().__init__()
    self.maxsize = maxsize

  def __setitem__(self, key, value):
    if key in self:
      self.move_to_end(key)
    super().__setitem__(key, value)
    while len(self) > self.maxsize:
      self.popitem(last=False)


class Subscribes:
  """A class to manage user-specific subscriptions."""
  __slots__ = ("user_id", "manga_url", "web", "lastest_chapter", "manga_title")

  def __init__(self, manga_url: str, web: str, lastest_chapter: str, manga_title: str):
    self.manga_url = manga_url
    self.manga_title = manga_title
    self.web = web
    self.lastest_chapter = lastest_chapter

  def load_to_dict(self):
    return {"url": self.manga_url, "title": self.manga_title, "lastest_chapter": self.lastest_chapter}


_CLEAN_RE = re.compile(r"[_&;:'|*?><`!@#$%^~+=/\\\n]|None|\.jpg")


def clean(txt, length=-1):
  txt = _CLEAN_RE.sub("", str(txt))
  return txt[:length] if length != -1 else txt


def get_file_name(data: list, setting: dict = None):
  setting = setting or {}
  regex = setting.get("regex")
  try:
    flen = int(setting.get("file_name_len") or 30)
  except (TypeError, ValueError):
    flen = 30

  def number(item):
    n = get_episode_number(item.get("title", ""))
    if n is None:
      return clean(item.get("title", "None"))
    return n.zfill(int(regex)) if regex else n

  if len(data) > 1:
    episode_number = f"{number(data[0])} - {number(data[-1])}"
  else:
    episode_number = number(data[0])
  return clean(data[0].get("manga_title", ""), flen), episode_number


async def load_images_(user_id, poster, base_url=None, poster_filename=None):
  """Thumbnail / banners of a user, cached in Process/<user_id> so queued chapters reuse them."""
  setting = {}
  user_setting: dict = ensure_user(user_id)["setting"]
  main_dir = f"Process/{user_id}"

  for key, value in (
    ("banner1_file_path", user_setting.get("banner1")),
    ("banner2_file_path", user_setting.get("banner2")),
    ("thumb_file_name", user_setting.get("thumb")),
  ):
    if not value:
      continue
    target = f"{main_dir}/{key}.jpg"
    is_thumb = key == "thumb_file_name"
    if value == "constant":                       # the manga's own poster; changes per manga
      if not poster:
        continue
      path = await igrone_error(thumbnali_images)(
        image_url=poster, download_dir=main_dir, file_name=f"{key}_c", base_url=base_url)
    elif ospath.exists(target):
      path = target
    elif value.startswith("http"):
      path = await igrone_error(thumbnali_images)(image_url=value, download_dir=main_dir, file_name=key)
    else:
      path = await igrone_error(Bot.download_media)(value, file_name=target)
    if not path:
      continue
    path = await igrone_error(make_thumb if is_thumb else normalize_jpeg, sync=True)(path)
    if path:
      setting[key] = path
  return setting


# --------------------------------------------------------------------------- #
# task card + queue
# --------------------------------------------------------------------------- #
class TaskCard:
  """One chapter (or one merged group of chapters) to download and send."""
  __slots__ = (
    "episode_number", "manga_title", "poster", "webs", "picturesList", "sts", "url", "data",
    "tasks_id", "user_id", "chat_id", "priority", "setting",
    "pictures_fn", "batch", "future", "skip_dump", "title", "chapter_urls",
  )

  def __init__(self, webs, sts, picturesList, user_id, chat_id, priority,
               tasks_id=None, data_list: list = None, pictures_fn=None,
               batch=None, future=None, skip_dump=False):
    data_list = data_list or [{}]
    ensure_user(user_id)
    self.picturesList = picturesList or []
    self.pictures_fn = pictures_fn      # async callable -> list of urls, resolved lazily by the worker
    self.poster = data_list[0].get("poster", "") or ""
    self.webs = webs
    self.sts = sts
    self.batch = batch                  # progress.BatchState shared by a whole job
    self.future = future                # resolved with the sent messages when finished
    self.skip_dump = skip_dump
    self.chapter_urls = [d.get("url", "") for d in data_list]   # identifies this output in the storage index
    self.setting = uts[str(user_id)]["setting"]
    self.manga_title, self.episode_number = get_file_name(data_list, self.setting)
    self.title = f"{self.manga_title} - {self.episode_number}"

    if len(data_list) == 1:
      self.url = data_list[0].get("url", "")
    else:
      self.url = f"{data_list[0].get('url', '')} - {data_list[-1].get('url', '')}"

    self.tasks_id = tasks_id
    self.user_id = user_id
    self.chat_id = chat_id
    self.priority = priority

  async def get_banner(self) -> dict:
    return await load_images_(self.user_id, self.poster, self.webs.url, self.manga_title)

  def check_queue(self):
    """True when this user has nothing else waiting."""
    return queue.get_count(int(self.user_id)) == 0


def user_priority(user_id) -> int:
  return 0 if is_premium(user_id) else 1


class AQueue:
  """FIFO queue with one running task per user and premium-first ordering.
  Workers sleep on an Event instead of polling."""
  __slots__ = ("storage_data", "ongoing_tasks", "maxsize", "_event")

  def __init__(self, maxsize: Optional[int] = None):
    self.storage_data: Dict[str, Tuple[TaskCard, bool]] = {}   # {task_id: (TaskCard, is_auto_update)}
    self.ongoing_tasks: Dict[int, TaskCard] = {}                # {user_id: TaskCard}
    self.maxsize = maxsize
    self._event = asyncio.Event()

  def _new_id(self) -> str:
    chars = string.ascii_letters + string.digits
    while True:
      task_id = "".join(random.choices(chars, k=7))
      if task_id not in self.storage_data:
        return task_id

  async def put(self, tasks: TaskCard, updates: bool = False) -> str:
    if self.maxsize and len(self.storage_data) >= self.maxsize:
      raise asyncio.QueueFull("Queue full")
    tasks.tasks_id = self._new_id()
    self.storage_data[tasks.tasks_id] = (tasks, updates)
    self._event.set()
    return tasks.tasks_id

  def get_available_tasks(self, user_id=None):
    """Best task whose owner has nothing running (lowest priority value first, then oldest)."""
    busy = {int(u) for u in self.ongoing_tasks}
    best = None
    for item in self.storage_data.values():
      card = item[0]
      if int(card.user_id) in busy:
        continue
      if user_id is not None and int(card.user_id) != int(user_id):
        continue
      if best is None or card.priority < best[0].priority:
        best = item
    return best

  async def get(self, worker_id: int) -> Tuple[TaskCard, bool]:
    while True:
      item = self.get_available_tasks()
      if item:
        card = item[0]
        self.ongoing_tasks[int(card.user_id)] = card
        del self.storage_data[card.tasks_id]
        return item
      self._event.clear()
      await self._event.wait()

  async def delete_task(self, task_id: str) -> bool:
    item = self.storage_data.get(task_id)
    if item and item[1] is not True:
      del self.storage_data[task_id]
      return True
    return False

  async def delete_tasks(self, user_id: int) -> int:
    """Remove every waiting (not running, not auto-update) task of a user."""
    deleted = 0
    for task_id, (card, updates) in list(self.storage_data.items()):
      if updates or int(card.user_id) != int(user_id):
        continue
      del self.storage_data[task_id]
      if card.future and not card.future.done():
        card.future.set_result(None)
      if card.sts and not card.batch:
        await igrone_error(card.sts.delete)()
      deleted += 1
    return deleted

  def get_count(self, user_id: int = 0) -> int:
    """Waiting tasks of a user - or, with 0, how many users have waiting tasks."""
    if user_id == 0:
      return len({int(c.user_id) for c, upd in self.storage_data.values() if not upd})
    return sum(1 for c, _ in self.storage_data.values() if int(c.user_id) == int(user_id))

  def task_exists(self, task_id: str) -> bool:
    return task_id in self.storage_data or any(c.tasks_id == task_id for c in self.ongoing_tasks.values())

  def qsize(self) -> int:
    return len(self.storage_data)

  def empty(self) -> bool:
    return not self.storage_data

  async def task_done(self, tasks_card: TaskCard) -> bool:
    removed = self.ongoing_tasks.pop(int(tasks_card.user_id), None) is not None
    self._event.set()      # the user's next task may start now
    return removed

  def get_ongoing_count(self, user_id: int) -> int:
    return sum(1 for t in self.ongoing_tasks.values() if int(t.user_id) == int(user_id))


queue = AQueue()
