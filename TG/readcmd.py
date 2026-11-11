"""/read - turn a PDF into an online reading link (see Tools/webreader.py).

    reply to a PDF with /read        or        send a PDF with /read as its caption
"""
import os

from pyrogram import filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot import Bot, Vars, is_admin, logger
from Tools import webreader
from Tools.ui import esc
from .storage import igrone_error, retry_on_flood

USAGE = ("<b>📖 Read a PDF online</b>\n"
         "Reply to a PDF with <code>/read</code> (or send a PDF with <code>/read</code> as its caption) "
         "and I'll give you a link that opens it in a reader page.")


def _is_pdf(doc) -> bool:
  name = (getattr(doc, "file_name", "") or "").lower()
  return (getattr(doc, "mime_type", "") or "").lower() == "application/pdf" or name.endswith(".pdf")


@Bot.on_message(filters.command("read") & filters.private)
async def read_cmd(client, message):
  user = message.from_user
  if not Vars.WEB_READER_ENABLED:
    return await message.reply_text("📖 <b>The web reader is switched off.</b>", quote=True)
  if not webreader.public_base():
    hint = ("\nSet it in /settings → 🌐 Web Reader → Public URL." if is_admin(user.id) else "")
    return await message.reply_text("📖 <b>The web reader isn't set up yet</b> (no public URL)." + hint, quote=True)

  doc = message.document or (message.reply_to_message.document if message.reply_to_message else None)
  if not doc:
    return await message.reply_text(USAGE, quote=True)
  if not _is_pdf(doc):
    return await message.reply_text("❌ That isn't a PDF file.", quote=True)

  limit = max(1, Vars.WEB_READER_MAX_MB) * 1024 * 1024
  if (doc.file_size or 0) > limit:
    return await message.reply_text(f"❌ That PDF is too big. The limit is <b>{Vars.WEB_READER_MAX_MB} MB</b>.", quote=True)
  if not is_admin(user.id) and webreader.active_for(user.id) >= max(1, Vars.WEB_READER_PER_USER):
    return await message.reply_text(
      f"❌ You already have {Vars.WEB_READER_PER_USER} active reading links. They expire after "
      f"{Vars.WEB_READER_HOURS} h - try again later.", quote=True)

  status = await message.reply_text("⏳ <i>Preparing your reader…</i>", quote=True)
  token = webreader.new_token()
  path = webreader.tmp_path(token)
  try:
    await client.download_media(doc, file_name=path)
    with open(path, "rb") as f:
      if f.read(1024).find(b"%PDF") < 0:
        raise ValueError("not a real PDF")
    name = getattr(doc, "file_name", None) or "document.pdf"
    webreader.register(token, name, user.id)
  except Exception as e:
    logger.warning(f"/read failed for {user.id}: {type(e).__name__}: {e}")
    webreader.drop(token)
    return await igrone_error(status.edit_text)("❌ I couldn't read that file. Is it a valid PDF?")

  url = webreader.link(token)
  text = (f"<b>📖 Ready to read</b>\n<i>{esc(name)}</i>\n\n"
          f"<code>{esc(url)}</code>\n\n"
          f"<i>The link works for {Vars.WEB_READER_HOURS} h, then it and the stored copy are deleted. "
          f"Anyone with the link can open it, so share carefully.</i>")
  markup = InlineKeyboardMarkup([[InlineKeyboardButton("📖 Open reader", url=url)]]) if url.startswith("https://") else None
  await retry_on_flood(status.edit_text)(text, reply_markup=markup, disable_web_page_preview=True)
