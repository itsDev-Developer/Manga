from bot import  Bot, Vars, logger, is_admin
from pyrogram import filters
from pyrogram.types import InlineKeyboardMarkup, InputMediaPhoto
import random
import time

from Tools import access
from Tools.access import can_use
from Tools.ui import PRIMARY, btn, esc, mini_bar, title

from Tools.db import ensure_user, premium_user

from .storage import (
    get_webs, igrone_error, plugins_list, 
    retry_on_flood, searchs, is_auth_query, 
    web_data, check_get_web
)

from Tools.my_token import verify_token, get_token, check_token_
import asyncio



def _picker_text(query: str) -> str:
  return (title("🔎", "Where should I look?", "Pick a source — or search all of them.")
          + f"\n\n<blockquote>{esc(query[:120])}</blockquote>")


@Bot.on_message(filters.command("search"))
@check_token_
async def search_group(client, message):
  if Vars.IS_PRIVATE:
    if not is_admin(message.chat.id):
      return await message.reply(Vars.PRIVATE_TXT)

  if Vars.SHORTENER:
    if not await premium_user(message.from_user.id):
      if not verify_token(message.from_user.id):
        if not is_admin(message.from_user.id):
          return await get_token(message, message.from_user.id)

  ensure_user(message.from_user.id)

  try:
    txt = message.text.split(" ")[1]
  except:
    return await message.reply("<b>Usage</b>\n<code>/search &lt;manga name&gt;</code>")
  
  photo = random.choice(Vars.PICS)
  await retry_on_flood(message.reply_photo)(
    photo, caption=_picker_text(message.text.split(" ", 1)[1]),
    reply_markup=plugins_list(user_id=message.from_user.id), quote=True
  )



@Bot.on_message(filters.text & filters.private & ~filters.regex(r"/"))
@check_token_
async def search(client, message):
  if Vars.IS_PRIVATE:
    if not is_admin(message.chat.id):
      return await message.reply(Vars.PRIVATE_TXT)
  
  ensure_user(message.from_user.id)
  photo = random.choice(Vars.PICS)
  await retry_on_flood(message.reply_photo)(
    photo, caption=_picker_text(message.text),
    reply_markup=plugins_list(user_id=message.from_user.id), quote=True
  )


@Bot.on_callback_query(filters.regex("^bk") & is_auth_query())
async def bk_handler(client, query):
  """This Is Back Handler Of Callback Data"""
  photo = random.choice(Vars.PICS)
  try: page = int(query.data.split(":")[-1])
  except:  page = 1

  ensure_user(query.from_user.id)
  reply = query.message.reply_to_message
  await retry_on_flood(query.message.edit_media)(
      media=InputMediaPhoto(photo, caption=_picker_text(reply.text if reply and reply.text else "")),
      reply_markup=plugins_list(page=page, user_id=query.from_user.id),
  )
  await igrone_error(query.answer)()


async def search_all(search, sts, user_id=None, max_concurrent=5):
  """Search every source this user may use, concurrently (the old version awaited
  them one after another, so ten slow sites took ten times as long). Returns
  (results, found, no_found, locked_skipped)."""
  webs = [(name.strip(), web) for name, web in web_data.items() if can_use(user_id, web.sf)]
  skipped = len(web_data) - len(webs)
  semaphore = asyncio.Semaphore(max_concurrent)
  by_name, found, no_found = {}, [], []
  state = {"done": 0, "last": 0.0}

  async def one(name, web):
    async with semaphore:
      try:
        result = await asyncio.wait_for(web.search(search), 25)
      except Exception:
        result = []
    by_name[name] = result or []
    (found if result else no_found).append(name)
    state["done"] += 1
    now = time.monotonic()
    if now - state["last"] > 1.2 or state["done"] == len(webs):      # don't hammer Telegram with edits
      state["last"] = now
      await igrone_error(sts.edit_message_caption)(
        f"<b>🔎 Searching…</b>  <i>{esc(search[:60])}</i>\n\n"
        f"{mini_bar(state['done'], len(webs))}  <b>{state['done']}/{len(webs)}</b>  ·  <i>{esc(name)}</i>")

  await asyncio.gather(*(one(n, w) for n, w in webs))

  results = [r for name, _ in webs for r in by_name.get(name, [])]      # stable, site-by-site order
  return results, (", ".join(found) or None), (", ".join(no_found) or None), skipped


@Bot.on_callback_query(filters.regex("^plugin_") & is_auth_query())
async def cb_handler(client, query):
  """ This Is Search  Handler Of Callback Data """

  def iterate_(results, page=1):
    try:
      return results[(page - 1) * 8:page * 8] if page != 1 else results[:8]
    except:
      return None

  data = query.data.split("_")[-1]
  photo = random.choice(Vars.PICS)

  try:
    page = int(query.data.split("_")[-2])
  except:
    page = 1

  reply = query.message.reply_to_message
  if not reply:
    return await query.answer("This is an old button, please redo the search",
                              show_alert=True)

  reply = reply.text
  if reply.startswith("/search"):
    search = reply.split(" ", 1)[-1]
  else:
    search = reply

  
  reply_markup = query.message.reply_markup

  results = None
  results_ = None
  found = None
  no_found = None
  skipped = 0
  webs = get_webs(data)
  if webs and not can_use(query.from_user.id, webs.sf):
    # A locked source tapped in the picker (or from an old message).
    return await query.answer(access.denied_alert(webs.sf), show_alert=True)
  if not webs:
    webs = "all"
    await igrone_error(query.edit_message_caption)(
      f"<b>🔎 Searching…</b>  <i>{esc(search[:60])}</i>")

  if webs == "all":
    results_, found, no_found, skipped = await search_all(search, query, user_id=query.from_user.id)
  else:
    results_ = await igrone_error(webs.search)(search)

  results = iterate_(results_, page=page)
  if not results or not results_:
    note = f"\n\n<i>🔒 {skipped} Premium source(s) weren't searched — see /my_plan.</i>" if skipped else ""
    return await retry_on_flood(query.edit_message_media)(
        media=InputMediaPhoto(
            photo, caption=(f"<b>😕 No results</b>\n<i>Nothing found for “{esc(search[:60])}”. "
                            f"Try another spelling, or a different source.</i>{note}")[:1024]),
        reply_markup=reply_markup,
    )

  button = []
  for result in results:
    c = f"chs|{data}{result['id']}" if "id" in result else f"chs|{data}{hash(result['url'])}"
    if webs == "all":
      webs_ = check_get_web(result['url'])
      if not webs_:
        continue

      searchs[c] = (webs_, result)
      web_name = type(webs_).__name__
      web_name = web_name.replace("Webs", "")
      button.append([btn(f"{result['title']} · {web_name}", callback_data=c, style=None)])
    else:
      searchs[c] = (webs, result)
      button.append([btn(result['title'], callback_data=c, style=None)])

  await igrone_error(query.answer)()

  total_pages = max(1, -(-len(results_) // 8))
  arrow = []
  if iterate_(results_, page=int(page - 1)):
    arrow.append(btn("‹ Prev", callback_data=f"plugin_{page-1}_{data}", style=None))
  arrow.append(btn(f"{page}/{total_pages}", callback_data="noop", style=None))
  if iterate_(results_, page=int(page + 1)):
    arrow.append(btn("Next ›", callback_data=f"plugin_{page+1}_{data}", style=None))
  if total_pages > 1:
    button.append(arrow)

  button.append([
      btn("⇦ Back", callback_data="bk.p", style=None),
      btn("✖ Close", callback_data="kclose", style=None),
  ])

  if webs != "all":
    web_name = type(webs).__name__.replace("Webs", "")
    where = f"from <b>{esc(web_name)}</b>"
    extra = ""
  else:
    where = "from <b>all sources</b>"
    extra = (f"\n\n<blockquote expandable><b>Found on</b>  <i>{esc(found)}</i></blockquote>" if found else "")
    extra += (f"\n<blockquote expandable><b>Nothing on</b>  <i>{esc(no_found)}</i></blockquote>" if no_found else "")
    if skipped:
      extra += f"\n<i>🔒 {skipped} Premium source(s) weren't searched — see /my_plan.</i>"
  caption_text = (f"<b>🔎 {esc(search[:60])}</b>\n"
                  f"<i>{len(results_)} result{'s' if len(results_) != 1 else ''} {where}</i>{extra}")

  await retry_on_flood(query.edit_message_media)(
    InputMediaPhoto(photo, caption=caption_text[:1024]),
      reply_markup=InlineKeyboardMarkup(button)
  )
