"""The queue card: where your downloads are, and what the bot is doing right now."""
import asyncio

from pyrogram import filters
from pyrogram.errors import MessageNotModified

from bot import Bot, Vars, is_admin, logger
from Tools.base import user_priority
from Tools.ui import PRIMARY, btn, esc, kb, nav, plural, quote, title
from TG.storage import igrone_error, queue, retry_on_flood, safe_photo_reply


def get_queue_markup():
  return kb(
    [btn("🔄 Refresh", callback_data="refresh_queue", style=PRIMARY), btn("🧹 Clear my queue", callback_data="clean_queue")],
    [btn("📌 Subscriptions", callback_data="st:subs", style=None), btn("💬 Contact owner", url=f"tg://user?id={Vars.OWNER}", style=None)],
    nav(close=True),
  )


async def get_queue_text(user_id):
  try:
    total, users = queue.qsize(), queue.get_count()
    ongoing = list(queue.ongoing_tasks.values())
    mine = queue.get_count(user_id)

    text = title("📊", "Queue", f"{plural(total, 'chapter')} waiting · {plural(users, 'user')}") + "\n\n"
    text += "<b>Your downloads</b>\n"
    if mine:
      lines = [f"{plural(mine, 'chapter')} in your queue"]
      if user_id in queue.ongoing_tasks:
        t = queue.ongoing_tasks[user_id]
        lines.append(f"▶️ {esc(t.manga_title)} — {esc(t.episode_number)} · <i>processing</i>")
      else:
        waiting = queue.get_available_tasks(user_id)
        if waiting:
          lines.append(f"⏳ {esc(waiting[0].manga_title)} — {esc(waiting[0].episode_number)} · <i>up next</i>")
      text += quote("\n".join(lines)) + "\n"
    else:
      text += "<i>Nothing queued — type a manga name to start.</i>\n"
    text += f"\n<b>Your priority</b>  {'⚡ Priority <i>(Premium)</i>' if user_priority(user_id) == 0 else 'Standard'}\n\n"

    text += "<b>Right now</b>\n"
    if not ongoing:
      text += "<i>The bot is idle.</i>"
    elif is_admin(user_id):
      names = []
      for i, task in enumerate(ongoing, 1):
        try:
          names.append(f"{i}. {(await Bot.get_users(int(task.user_id))).mention()}")
        except Exception:
          names.append(f"{i}. User {task.user_id}")
      text += quote("\n".join(names), expandable=True)
    else:
      text += f"{plural(len(ongoing), 'download')} in progress."      # names are for admins only
    return text
  except Exception as e:
    logger.exception(f"Error generating queue text: {e}")
    return "<b>❌ Couldn't load the queue.</b> Try again in a moment."


async def _edit(query, text, markup):
  """Photo cards take a caption edit, text messages a text edit."""
  msg = query.message
  try:
    if getattr(msg, "photo", None) or getattr(msg, "media", None):
      await retry_on_flood(query.edit_message_caption)(text, reply_markup=markup)
    else:
      await retry_on_flood(query.edit_message_text)(text, reply_markup=markup)
  except MessageNotModified:
    pass


@Bot.on_message(filters.command("queue"))
async def queue_msg_handler(client, message):
  await safe_photo_reply(message, await get_queue_text(message.from_user.id), get_queue_markup())


@Bot.on_callback_query(filters.regex("^st:queue$"))
async def queue_open_cb(client, query):
  """From the start card: open the queue as its own message."""
  await igrone_error(query.answer)()
  await safe_photo_reply(query.message, await get_queue_text(query.from_user.id), get_queue_markup())


@Bot.on_callback_query(filters.regex("^refresh_queue$"))
async def queue_refresh_handler(_, query):
  try:
    await _edit(query, await get_queue_text(query.from_user.id), get_queue_markup())
    await query.answer("🔄 Updated")
  except Exception as e:
    logger.error(f"Queue refresh error: {e}")
    await query.answer("Couldn't refresh — try again.", show_alert=True)


@Bot.on_callback_query(filters.regex("^clean_queue$"))
async def clean_queue_handler(_, query):
  try:
    user_id = query.from_user.id
    if not queue.get_count(user_id):
      return await retry_on_flood(query.answer)("Your queue is already empty.")
    n = await queue.delete_tasks(user_id)
    await retry_on_flood(query.answer)(f"🧹 Removed {plural(n, 'task')}")
    await asyncio.sleep(1)
    await _edit(query, await get_queue_text(user_id), get_queue_markup())
  except Exception as e:
    logger.error(f"Clean queue error: {e}")
    await query.answer("Couldn't clear the queue.", show_alert=True)
