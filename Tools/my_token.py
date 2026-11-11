"""Free-user verification through a URL shortener (enable it from /settings)."""
import asyncio
import functools
import random
import string
import time

import requests
from pyrogram.errors import FloodWait
from pyrogram.types import InlineKeyboardMarkup

from bot import Bot, Vars, is_admin, logger
from Tools.db import is_premium, tks, token_sync
from Tools.ui import PRIMARY, btn

MIN_SECONDS = 40     # how long the shortener page must take (bypass detection)


def generate_token():
  return "".join(random.choices(string.ascii_letters + string.digits, k=8))


def _short_sync(url):
  api = Vars.SHORTENER_API
  if not api or "{}" not in api:
    return url
  try:
    data = requests.get(api.replace("{}", url), timeout=15).json()
    return data.get("shortenedUrl") or data.get("shortened_url") or data.get("short_url") or url
  except Exception as e:
    logger.warning(f"Shortener request failed: {e}")
    return url


async def get_short(url):
  return await asyncio.to_thread(_short_sync, url)


def save_token(user_id: str, token: str, _id: int, _cid: int, short_token_link: str):
  tks[str(user_id)] = {
    "token": token,
    "expires_at": time.time() + Vars.DURATION * 3600,
    "duration": time.time() + MIN_SECONDS,
    "msg_id": _id,
    "chat_id": _cid,
    "s_link": short_token_link,
    "verify": None,
  }
  token_sync(user_id)


def expired_token_():
  now = time.time()
  for user_id in [u for u, d in tks.items() if d.get("expires_at", 0) < now]:
    tks.drop(user_id)


def shortener_enabled() -> bool:
  return bool(Vars.SHORTENER and Vars.SHORTENER_API)


def check_token_(func):
  @functools.wraps(func)
  async def wrapper(client, message, *args, **kwargs):
    if not shortener_enabled() or not message.from_user:
      return await func(client, message, *args, **kwargs)
    uid = message.from_user.id
    if is_admin(uid) or is_premium(uid):
      return await func(client, message, *args, **kwargs)
    data = tks.get(str(uid))
    if data:
      if data.get("verify") == "True":
        if data["expires_at"] > time.time():
          return await func(client, message, *args, **kwargs)
        return await get_token(message, uid)
      return await message.reply(
        "<b>🔑 Verify your access first</b>\n<i>Open the link below, finish the short step, then come back.</i>",
        reply_markup=_keyboard(data['s_link']))
    sts = await message.reply("<i>Checking your access…</i>")
    return await get_token(sts, uid)
  return wrapper


def _keyboard(link):
  return InlineKeyboardMarkup([
    [btn("🔑 Get access token", url=link, style=PRIMARY)],
    [btn("💎 Go Premium", callback_data="premuim"),
     btn("✖ Close", callback_data="close", style=None)],
  ])


async def verify_token(message, user_id, token):
  user_id = str(user_id)
  data = tks.get(user_id)
  if not data or data["expires_at"] <= time.time():
    return await get_token(message, user_id)
  if data["verify"] == "True":
    return await message.edit("<b>✅ Already verified</b>\n<i>You're all set — just type a manga name.</i>")
  if data["token"] != token:
    return await get_token(message, user_id)
  if data["duration"] < time.time():
    data["verify"] = "True"
    token_sync(user_id)
    return await message.edit(f"<b>✅ Access granted</b>\n<i>Valid for {Vars.DURATION} hour(s). Type a manga name to start.</i>")
  return await message.edit(Vars.BYPASS_TXT, reply_markup=_keyboard(data["s_link"]))


async def get_token(message, user_id):
  user_id = str(user_id)
  old = tks.get(user_id)
  if old and old.get("msg_id") and old.get("chat_id"):
    try:
      await Bot.delete_messages(int(old["chat_id"]), int(old["msg_id"]))
    except FloodWait as e:
      await asyncio.sleep(e.value + 2)
    except Exception:
      pass

  new_token = generate_token()
  short_link = await get_short(f"https://telegram.me/{Bot.username}?start={new_token}")
  save_token(user_id, new_token, message.id, message.chat.id, short_link)
  text = ("<b>🔑 Access token needed</b>\n"
          f"<i>Open the link, complete the short verification, and you're in for {Vars.DURATION} hour(s).</i>\n\n"
          "<blockquote>💎 Premium members skip this step.</blockquote>")
  try:
    await message.edit_text(text, reply_markup=_keyboard(short_link))
  except FloodWait as e:
    await asyncio.sleep(e.value + 2)
    await message.edit_text(text, reply_markup=_keyboard(short_link))
