import asyncio
import html
import os
import re
import shutil
from functools import partial
from time import time

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup  # noqa: F401

from bot import Bot, Vars, logger
from Tools.base import TaskCard, igrone_error, queue, retry_on_flood
from Tools.config import hook
from Tools.img2cbz import images_to_cbz
from Tools.img2pdf import convert_images_to_pdf, download_and_convert_images
from Tools.progress import Reporter
from Tools.storage_db import (
  begin, chat_ref, end, fetch_stored, make_key, remember, same_chat, storage_channel, submit, wait_inflight,
)

LOG_CAPTION = """{caption}

{url}

<code>Downloaded By</code>: <code>{user_id}</code>
<code>PDF Password</code>: <code>{password}</code>
<code>Time Taken</code>: <code>{time_taken}</code>"""

_BAD_FILENAME = re.compile(r'[\\/:*?"<>|\n\r]')


class NormalError(Exception):
  """Error already reported to the user."""


async def send_error(task_card: TaskCard, error_text):
  text = ("<b>⚠️ Couldn't finish this chapter</b>\n"
          f"<blockquote>{html.escape(str(task_card.title or task_card.url))}</blockquote>\n"
          f"<code>{html.escape(str(error_text))}</code>\n"
          "<i>Try again in a moment — if it keeps failing, the source may be down.</i>")[:4000]
  try:
    if task_card.sts:
      msg = await retry_on_flood(task_card.sts.edit)(text)
    else:
      msg = await retry_on_flood(Bot.send_message)(int(task_card.user_id), text)
    if Vars.LOG_CHANNEL and msg:
      await igrone_error(msg.copy)(Vars.LOG_CHANNEL)
  except Exception as e:
    logger.warning(f"send_error failed: {e}")


def _names(card: TaskCard):
  """(file_name, caption) built from the user's templates."""
  setting = card.setting
  file_name = setting.get("file_name") or "Chapter {episode_number} {manga_title}"
  if (card.webs.sf == "mf" and "Vol" in card.manga_title) or "Volume" in card.manga_title:
    file_name = file_name.replace("Chapter", "Vol")

  def fill(text):
    return (text.replace("{episode_number}", card.episode_number)
                .replace("{chapter_num}", card.episode_number)
                .replace("{manga_title}", card.manga_title))

  file_name = _BAD_FILENAME.sub("", fill(file_name)).strip()[:120] or "chapter"
  caption = setting.get("caption") or "<blockquote>{file_name}</blockquote>"
  return file_name, fill(caption).replace("{file_name}", file_name)


def _file_types(setting):
  types = [t for t in (setting.get("type") or []) if t in ("PDF", "CBZ")]
  return types or ["PDF", "CBZ"]


def _quality(setting):
  try:
    q = int(setting.get("compress") or Vars.IMG_QUALITY)
  except (TypeError, ValueError):
    q = Vars.IMG_QUALITY
  return max(10, min(100, q))


async def _copy_out(msgs, chat, caption=None):
  for m in msgs:
    try:
      await retry_on_flood(Bot.copy_message)(int(chat), m.chat.id, m.id, **({"caption": caption} if caption else {}))
    except Exception as e:
      logger.warning(f"copy to {chat} failed: {e}")
      return False
  return True


async def _archive(sent, chat, key):
  """Storage-DB archive job (runs via storage_db.submit, never in the middle of
  a post's contiguous write): server-side copy into the storage channel, then
  record where it went so the next request for the same file can reuse it."""
  ids = []
  for m in sent:
    try:
      copy = await retry_on_flood(Bot.copy_message)(chat_ref(chat), m.chat.id, m.id)
    except Exception as e:
      logger.warning(f"archive copy to {chat} failed: {e}")
      return
    ids.append(copy.id)
  remember(key, chat, ids)


async def _deliver_stored(stored, card: TaskCard, storage, caption):
  """Hand over an output that's already in the storage channel - nothing is
  downloaded, converted or uploaded. Returns the sent Messages, or None."""
  if same_chat(card.chat_id, storage):
    return stored            # post flows: the files already live where the post needs them
  out = []
  try:
    for m in stored:
      out.append(await retry_on_flood(Bot.copy_message)(
        int(card.chat_id), chat_ref(storage), m.id, caption=caption))
  except Exception as e:
    logger.warning(f"copy from storage failed, processing normally instead: {e}")
    return None
  return out


async def _log_delivery(sent, card: TaskCard, caption, password, start_time):
  if not Vars.LOG_CHANNEL:
    return
  taken = divmod(int(time() - start_time), 60)
  log_caption = LOG_CAPTION.format(
    caption=caption, url=card.url, user_id=card.user_id,
    password=password, time_taken=f"{taken[0]}m, {taken[1]}s")[:1024]
  await _copy_out(sent[:1], Vars.LOG_CHANNEL, log_caption)


async def send_manga_chapter(card: TaskCard):
  """Download -> convert -> upload one task. Returns the list of sent Messages (or None).

  The storage channel (Constant Dump) is consulted first: a chapter that's
  already stored in this exact file configuration is copied out server-side
  instead of being downloaded, converted and uploaded again - and a freshly
  produced one is stored there exactly once (see Tools/storage_db.py)."""
  start_time = time()
  main_dir = f"Process/{card.tasks_id}"
  download_dir, out_files = f"{main_dir}/pictures", []
  ok = False
  password = None
  rep = Reporter(card.sts, title=card.title, batch=card.batch)
  storage = storage_channel()
  key, producing = None, None

  try:
    file_name, caption = _names(card)
    setting = card.setting
    types = _file_types(setting)
    password = setting.get("password") or None

    # ---- already stored? reuse it -----------------------------------------
    if storage and Vars.STORAGE_REUSE:
      key = make_key(
        card.webs.sf, card.chapter_urls, file_name, ",".join(sorted(types)), _quality(setting),
        password or "", setting.get("banner1") or "", setting.get("banner2") or "",
        setting.get("thumb") or "")
      await wait_inflight(key)           # someone may be producing this very file right now
      stored = await fetch_stored(key, storage)
      if stored:
        await rep.update("♻️ Already stored", force=True, note="sending a copy…")
        sent = await _deliver_stored(stored, card, storage, caption)
        if sent:
          await _log_delivery(sent, card, caption, password, start_time)
          ok = True
          return sent
      producing = begin(key)

    if not card.picturesList and card.pictures_fn:
      await rep.update("🔎 Fetching pages", force=True)
      try:
        card.picturesList = await card.pictures_fn() or []
      except Exception as e:
        logger.warning(f"pictures_fn failed for {card.url}: {e}")
    if not card.picturesList:
      await igrone_error(send_error)(card, "Error at Getting Picture")
      raise NormalError()

    banners = await card.get_banner()
    thumb = banners.get("thumb_file_name")

    try:
      cs = card.webs.cs
      cs = cs if cs in ("chrome", "plain") else (cs is True)
    except Exception:
      cs = False

    async def on_page(done, total, nbytes):
      await rep.update("📥 Downloading", done, total, nbytes=nbytes)

    await rep.update("📥 Downloading", 0, len(card.picturesList), force=True)
    files = await download_and_convert_images(
      card.picturesList, download_dir, card.webs.url,
      quality=_quality(setting), cs=cs, progress=on_page)
    if banners.get("banner1_file_path"):
      files.insert(0, banners["banner1_file_path"])
    if banners.get("banner2_file_path"):
      files.append(banners["banner2_file_path"])

    await rep.update("🗜 Building files", len(files), len(files), force=True, note="almost there…")
    if "PDF" in types:
      pdf = f"{main_dir}/{file_name}.pdf"
      err = await asyncio.to_thread(convert_images_to_pdf, files, pdf, password)
      if err:
        await igrone_error(send_error)(card, err)
        raise NormalError()
      out_files.append(pdf)
    if "CBZ" in types:
      cbz = f"{main_dir}/{file_name}.cbz"
      err = await asyncio.to_thread(images_to_cbz, files, cbz)
      if err:
        await igrone_error(send_error)(card, err)
        raise NormalError()
      out_files.append(cbz)

    # ---- upload (with live progress) --------------------------------------
    sent = []
    for i, path in enumerate(out_files, 1):
      label = f"📤 Uploading {i}/{len(out_files)}" if len(out_files) > 1 else "📤 Uploading"

      async def on_upload(current, total, _label=label):
        await rep.update(_label, nbytes=current, byte_total=total)

      await rep.update(label, nbytes=0, byte_total=os.path.getsize(path) or 1, force=True)
      msg = await retry_on_flood(Bot.send_document)(
        int(card.chat_id), path, caption=caption, thumb=thumb,
        file_name=os.path.basename(path), progress=on_upload)
      sent.append(msg)

    # ---- keep it in the storage DB - exactly once -------------------------
    if same_chat(card.chat_id, storage):
      remember(key, storage, [m.id for m in sent])       # it was uploaded straight into storage
    if not card.skip_dump:
      const = Vars.CONSTANT_DUMP_CHANNEL
      if const:
        if not same_chat(card.chat_id, const):             # never copy a file into the channel it's already in
          submit(partial(_archive, sent, const, key))      # server-side copy, after any contiguous post write
      else:
        personal = setting.get("dump")
        if personal and not same_chat(card.chat_id, personal) and not await _copy_out(sent, personal):
          await igrone_error(send_error)(card, "Add the bot to the Dump Channel (or provide a valid one)")
          await asyncio.sleep(5)

    await _log_delivery(sent, card, caption, password, start_time)

    ok = True
    return sent

  except NormalError:
    return None
  except asyncio.CancelledError:
    raise
  except Exception as e:
    await igrone_error(send_error)(card, str(e))
    logger.exception(f"Error processing task: {e}")
    return None
  finally:
    end(key, producing)
    if card.batch:
      card.batch.done += 1
      if not ok:
        card.batch.failed += 1
    shutil.rmtree(main_dir, ignore_errors=True)
    if card.check_queue():
      shutil.rmtree(f"Process/{card.user_id}", ignore_errors=True)   # cached banners/thumb
    if ok and card.sts and not card.batch:
      await igrone_error(card.sts.delete)()


# --------------------------------------------------------------------------- #
# workers
# --------------------------------------------------------------------------- #
_workers = {}   # worker_id -> Task


async def worker(worker_id: int):
  while worker_id < Vars.WORKERS:          # lower the setting and extra workers retire after their job
    card, _ = await queue.get(worker_id)
    logger.info(f"Worker {worker_id} processing task {card.tasks_id}")
    result = None
    try:
      result = await send_manga_chapter(card)
    except asyncio.CancelledError:
      raise
    except Exception as err:
      logger.exception(f"Worker {worker_id} error: {err}")
      await igrone_error(send_error)(card, err)
    finally:
      if card.future and not card.future.done():
        card.future.set_result(result)
      await queue.task_done(card)
  _workers.pop(worker_id, None)


def ensure_workers():
  """Start missing workers (call at startup and after the WORKERS setting changes)."""
  for i in range(int(Vars.WORKERS)):
    task = _workers.get(i)
    if task is None or task.done():
      _workers[i] = asyncio.get_running_loop().create_task(worker(i))


@hook("workers")
def _workers_changed():
  try:
    ensure_workers()
    queue._event.set()      # wake idle workers so surplus ones can retire
  except RuntimeError:
    pass
