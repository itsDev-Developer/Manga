from bot import Bot, Vars, logger
from Tools.db import get_subs, add_sub, delete_sub, ensure_user
from TG.storage import igrone_error, subscribes, filters
import random
from TG.storage import retry_on_flood, check_get_web, is_auth_query, searchs, info_markup
from pyrogram.types import InlineKeyboardMarkup, InputMediaPhoto
from Tools import access
from Tools.access import can_use
from Tools.ui import btn, esc, plural, title


@Bot.on_callback_query(filters.regex("^subs") & is_auth_query())
async def subs_handler(client, query):
  """Subscribe / unsubscribe from the button under a manga."""
  if query.data not in subscribes:
    return await retry_on_flood(query.answer)("This is an old button, please redo the search")

  webs, data = subscribes[query.data]
  if not can_use(query.from_user.id, webs.sf):
    return await query.answer(access.denied_alert(webs.sf), show_alert=True)

  uid = str(query.from_user.id)
  if get_subs(uid, data.manga_url, webs.sf):
    await delete_sub(uid, data.manga_url, webs.sf)
    subscribed = False
    await igrone_error(query.answer)("🔕 Unsubscribed")
  else:
    await add_sub(uid, data, webs.sf)
    subscribed = True
    await igrone_error(query.answer)("🔔 Subscribed — new chapters will be sent to you")

  rows = [list(r) for r in (query.message.reply_markup.inline_keyboard if query.message.reply_markup else [])]
  on_chapter_list = any((b.callback_data or "").startswith("full:") for r in rows for b in r)
  if on_chapter_list:
    # The chapter list's keyboard is large and page-specific: swap just the one button.
    fresh = btn("🔕 Unsubscribe", callback_data=query.data) if subscribed else btn("🔔 Subscribe", callback_data=query.data)
    rows = [[fresh if b.callback_data == query.data else b for b in r] for r in rows]
    markup = InlineKeyboardMarkup(rows)
  else:
    # The info card: rebuild it whole (keeps every button's colour).
    markup = info_markup(webs, data.manga_url, subscribed, query.from_user.id in Vars.ADMINS)

  try:
    await retry_on_flood(query.edit_message_reply_markup)(markup)
  except Exception:
    await retry_on_flood(client.edit_message_reply_markup)(
      query.message.chat.id, query.message.id, markup)



def _subs_caption(user_id):
  n = len(get_subs(str(user_id)) or [])
  return title("📌", "Your subscriptions", f"{plural(n, 'manga')} — new chapters are sent to you automatically.")


async def isubs_handle(process, page, user_id):
  """This Is Subscribe Handler Of Callback Data"""
  def iterate_subs(subs, page=1):
    try:
      return subs[(page - 1) * 10:page * 10] if page != 1 else subs[:10]
    except:
      return None

  ensure_user(user_id)

  all_manga_subs = get_subs(user_id)
  if not all_manga_subs or all_manga_subs is True:
    # Show the empty state and return None - returning the edit call's result
    # (a truthy Message) made callers try to build a keyboard from it.
    await retry_on_flood(process)("<b>📌 No subscriptions yet</b>\n\n<i>Open any manga and tap 🔔 Subscribe — new chapters are sent to you automatically.</i>" if process.__name__ != "answer" else "📌 No subscriptions yet — open a manga and tap 🔔 Subscribe.")
    return None

  button = []
  manga_subs = iterate_subs(all_manga_subs, page=int(page))
  if not manga_subs:
    return button

  for data in manga_subs:
    web = check_get_web(data['url'])
    if not web: continue
    
    webs_name = type(web).__name__
    webs_name = webs_name.replace("Webs", "")

    c = f"chs|{web.sf}{hash(data['url'])}"
    searchs[c] = (web, data)
    lock = "🔒 " if not can_use(int(user_id), web.sf) else ""
    button.append([
        btn(f"{lock}{data['title']} · {webs_name}", callback_data=c, style=None)
    ])

  page = int(page)
  pages = max(1, -(-len(all_manga_subs) // 10))
  arrow = []
  if iterate_subs(all_manga_subs, page=page - 1):
    arrow.append(btn("‹ Prev", callback_data=f"isubs:{page-1}", style=None))
  if pages > 1:
    arrow.append(btn(f"{page}/{pages}", callback_data="noop", style=None))
  if iterate_subs(all_manga_subs, page=page + 1):
    arrow.append(btn("Next ›", callback_data=f"isubs:{page+1}", style=None))

  button.append(arrow)
  button.append([
    btn("🗑 Remove all subscriptions", callback_data="pqi_clean_all_subs")
  ])

  button.append([
      btn("📊 Queue", callback_data="st:queue", style=None),
      btn("✖ Close", callback_data="kclose", style=None)
  ])

  return button


async def _send_subs(message, user_id):
  """Open the subscription list as a new message under `message`."""
  sts = await retry_on_flood(message.reply_text)("<i>Loading your subscriptions…</i>", quote=True)
  user_id = str(user_id)
  ensure_user(user_id)
  button = await isubs_handle(process=sts.edit_text, page=1, user_id=user_id)
  if not button:
    return
  try:
    await retry_on_flood(sts.edit_media)(
      InputMediaPhoto(random.choice(Vars.PICS), caption=_subs_caption(user_id)),
      reply_markup=InlineKeyboardMarkup(button)
    )
  except Exception:
    await retry_on_flood(message.reply_photo)(
      photo=random.choice(Vars.PICS),
      caption=_subs_caption(user_id),
      reply_markup=InlineKeyboardMarkup(button),
      quote=True,
    )


@Bot.on_message(filters.command(["subs", "subscribes"]))
async def isubs_cmds(_, message):
  await _send_subs(message, message.from_user.id)


@Bot.on_callback_query(filters.regex("^st:subs$"))
async def subs_open_cb(_, query):
  """From the start card / queue card: open the list as its own message."""
  await igrone_error(query.answer)()
  await _send_subs(query.message, query.from_user.id)


@Bot.on_callback_query(filters.regex("^isubs") & is_auth_query())
async def isubs_callback(client, query):
  """This Is Subscribe Handler Of Callback Data"""
  try:
    page = query.data.split(":")[1]
  except:
    page = 1
  
  user_id = str(query.from_user.id)
  ensure_user(user_id)
  button =  await isubs_handle(
    process=query.answer,
    page=page,
    user_id=user_id
  )
  if not button:
    return
  
  await igrone_error(query.edit_message_reply_markup)(InlineKeyboardMarkup(button))


@Bot.on_callback_query(filters.regex("^pqi_clean_all_subs$") & is_auth_query())
async def _clean_all_subs_handler(client, query):
  """This Is Clean All Subs Handler Of Callback Data"""
  user_id = str(query.from_user.id)
  if not get_subs(user_id):
    return await query.answer("📌 No subscriptions yet — open a manga and tap 🔔 Subscribe.", show_alert=True)

  await delete_sub(user_id)
  await query.answer("🗑 All subscriptions removed", show_alert=True)
  try: 
    await retry_on_flood(query.message.delete)()
  except: 
    pass
