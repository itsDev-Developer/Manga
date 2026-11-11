from pyrogram import filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto

from .storage import (
   searchs, pagination, subscribes,
   retry_on_flood, igrone_error, check_fsb, queue,
   split_list, chaptersList, get_episode_number, is_auth_query,
   post_targets, info_markup,
)

from Tools.poster import poster_for_telegram
from bot import Bot, Vars, logger
import html
import random

from Tools.db import ensure_user, get_subs, uts
from Tools.base import Subscribes, TaskCard, user_priority
from Tools.progress import BatchState
from Tools import access
from Tools.access import can_use
from Tools.cards import info_card
from Tools.ui import PRIMARY, SUCCESS, btn
import asyncio



@Bot.on_callback_query(filters.regex("^refresh$"))
async def refresh_handler(_, query):
  if not _.FSB or _.FSB == []:
    await retry_on_flood(query.answer)(
      "✅ Thanks for joining! You can use the bot now.",
      show_alert=True
    )
    return await retry_on_flood(query.message.delete)()

  channel_button, change_data = await check_fsb(_, query)
  if not channel_button:
    await retry_on_flood(query.answer)(
      "✅ Thanks for joining! You can use the bot now.",
      show_alert=True
    )

    return await retry_on_flood(query.message.delete)()

  channel_button = split_list(channel_button)
  channel_button.append([btn("🔄 I've joined — refresh", callback_data="refresh", style=SUCCESS)])

  try:
    await retry_on_flood(query.edit_message_media)(
        media=InputMediaPhoto(random.choice(Vars.PICS),
                              caption=Vars.FORCE_SUB_TEXT),
        reply_markup=InlineKeyboardMarkup(channel_button),
    )
  except:
    await retry_on_flood(query.answer)("You haven't joined every channel yet.")

  if change_data:
    for change_ in change_data:
      _.FSB[change_[0]] = (change_[1], change_[2], change_[3])



@Bot.on_callback_query(filters.regex("^close$"))
async def close_handler(_, query):
  await igrone_error(query.answer)()
  await igrone_error(query.message.reply_to_message.delete)()
  await igrone_error(query.message.delete)()



@Bot.on_callback_query(filters.regex("^kclose$"))
async def kclose_handler(_, query):
  await igrone_error(query.answer)()
  await igrone_error(query.message.reply_to_message.delete)()
  await igrone_error(query.message.delete)()



@Bot.on_callback_query(filters.regex("^chs") & is_auth_query())
async def ch_handler(client, query):
  """This Is Information Handler Of Callback Data"""
  try:
    webs, data = searchs[query.data]
  except:
    return await query.answer(
      "This is an old button, please redo the search",
      show_alert=True
    )
  if not can_use(query.from_user.id, webs.sf):
    return await query.answer(access.denied_alert(webs.sf), show_alert=True)

  try:
    bio_list = await webs.get_chapters(data)
  except:
    return await query.answer("No chapters found", show_alert=True)

  if not bio_list:
    return await query.answer("No chapters found", show_alert=True)

  c = f"pg:{webs.sf}:{hash(bio_list['url'])}:"
  pagination[c] = (webs, bio_list, data)

  subs_bool = get_subs(str(query.from_user.id), bio_list['url'], webs.sf)

  sdata = Subscribes(
    manga_url=bio_list['url'],
    manga_title=bio_list['title'],
    web=webs.sf,
    lastest_chapter=""
  )
  subscribes[f"subs:{hash(bio_list['url'])}"] = (webs, sdata)

  rand_pic = bio_list['poster'] if "poster" in bio_list else random.choice(Vars.PICS)
  caption = info_card(bio_list, type(webs).__name__.replace("Webs", ""))

  is_admin_user = query.from_user.id in Vars.ADMINS
  if is_admin_user:
    post_targets[f"pnew:{webs.sf}:{hash(bio_list['url'])}"] = (webs, bio_list, data)
    post_targets[f"pauto:{webs.sf}:{hash(bio_list['url'])}"] = (webs, bio_list, data)
  markup = info_markup(webs, bio_list['url'], bool(subs_bool), is_admin_user)

  original_pic = rand_pic
  try:
    # a full-resolution local copy instead of the site's thumbnail (never delays the card more than a few seconds)
    if "poster" in bio_list:
      rand_pic = await poster_for_telegram(rand_pic, webs.url, quality=90, timeout=8)
    try:
      await retry_on_flood(query.edit_message_media)(
        InputMediaPhoto(rand_pic, caption=caption),
        reply_markup=markup
      )
    except Exception:
      if rand_pic == original_pic:
        raise
      await retry_on_flood(query.edit_message_media)(        # the local file was refused: try the plain URL
        InputMediaPhoto(original_pic, caption=caption),
        reply_markup=markup
      )
  except Exception:
    await retry_on_flood(query.edit_message_media)(
      InputMediaPhoto(Vars.PICS[-1], caption=caption),
      reply_markup=markup
    )


@Bot.on_callback_query(filters.regex("^pg") & is_auth_query())
async def pg_handler(client, query):
  """This Is Pagination Handler Of Callback Data"""
  call_data = query.data.split(":")
  page = call_data[-1]

  vols = None
  if call_data[-2] == "v":
    vols = True
    call_data = ":".join(call_data[:-2])
  else:
    call_data = ":".join(call_data[:-1])

  call_data = f"{call_data}:"
  if call_data not in pagination:
    call_data = call_data[:-1]

  if call_data in pagination:
    webs, data, rdata = pagination[call_data]
    sf = webs.sf
    if not can_use(query.from_user.id, sf):
      return await query.answer(access.denied_alert(sf), show_alert=True)
    subs_bool = get_subs(str(query.from_user.id), rdata['url'], sf)
    if sf == "mf" and vols:
      chapters = await webs.get_chapters(rdata, vol=True, page=int(page))
      if not chapters:
        return await query.answer("No chapters found", show_alert=True)
      if "chapters" in chapters and not chapters['chapters']:
        return await query.answer("No chapters found", show_alert=True)

      try:
        chapters = webs.iter_chapters(chapters, vol=True, page=int(page))
      except:
        return await query.answer("No chapters found", show_alert=True)

    else:
      try:
        chapters = await webs.iter_chapters(data, page=int(page))
      except TypeError:
        chapters = webs.iter_chapters(data, page=int(page))

    if not chapters:
      return await query.answer("No chapters found", show_alert=True)

    if chapters == [] or len(chapters) < 1:
      return await query.answer("No chapters found", show_alert=True)

    button = []
    for chapter in chapters:
      c = f"pic|{hash(chapter['url'])}"
      chaptersList[c] = (webs, chapter)
      button.append(btn(chapter['title'], callback_data=c, style=None))

    button = split_list(button[:60])
    c = f"pg:{sf}:{hash(chapters[-1]['url'])}:"
    pagination[c] = (webs, data, rdata)

    if int(page) > 0:
      pre_page_ = []
      
      if int(int(page) - 1) > 0 and webs.iter_chapters(data, page=int(int(page) - 1)):
        pre_page_.append(btn("‹ Prev", callback_data=f"{c}{int(page) - 1}", style=None))
      if int(int(page) - 2) > 0 and webs.iter_chapters(data, page=int(int(page) - 2)):
          pre_page_.append(btn("« −2", callback_data=f"{c}{int(page) - 2}", style=None))
      if int(int(page) - 5) > 0 and webs.iter_chapters(data, page=int(int(page) - 5)):
          pre_page_.append(btn("« −5", callback_data=f"{c}{int(page) - 5}", style=None))
      if pre_page_:
          button.append(pre_page_)

    
    next_page_ = []
    if webs.iter_chapters(data, page=int(int(page) + 1)):
      next_page_.append(btn("Next ›", callback_data=f"{c}{int(page) + 1}", style=None))
    if webs.iter_chapters(data, page=int(int(page) + 2)):
      next_page_.append(btn("+2 »", callback_data=f"{c}{int(page) + 2}", style=None))
    if webs.iter_chapters(data, page=int(int(page) + 5)):
      next_page_.append(btn("+5 »", callback_data=f"{c}{int(page) + 5}", style=None))
    if next_page_:
      button.append(next_page_)

    
    sdata = Subscribes(
      manga_url=rdata['url'],
      manga_title=rdata['title'],
      web=webs.sf,
      lastest_chapter=chapters[0]['title'] if chapters else ""
    )
    c = f"subs:{hash(rdata['url'])}"
    subscribes[c] = (webs, sdata)

    if webs.sf != "dj":
      if subs_bool:
        button.append([btn("🔕 Unsubscribe", callback_data=c)])
      else:
        button.append([btn("🔔 Subscribe", callback_data=c)])

    if sf == "mf":
      callback_data = f"full:{sf}:{hash(chapters[0]['url'])}"
      if int(page) == 1:
        pagination[callback_data] = (chapters[:60], webs)
      else:
        pagination[callback_data] = (chapters, webs)

      button.append(
        [
          btn("📚 Chapters", callback_data=f"{call_data}:{page}", style=PRIMARY) if vols else btn("📦 Volumes", callback_data=f"{call_data}v:{page}", style=None),
          btn("⬇️ Download this page", callback_data=callback_data)
        ]
      )
      button.append([btn("⇦ Back", callback_data=f"plugin_{sf}", style=None), btn("✖ Close", callback_data="kclose", style=None)])

    else:
      callback_data = f"full:{sf}:{hash(chapters[0]['url'])}"
      if int(page) == 1:
        pagination[callback_data] = (chapters[:60], webs)
      else:
        pagination[callback_data] = (chapters, webs)

      button.append([btn("⬇️ Download this page", callback_data=callback_data)])
      button.append([btn("⇦ Back", callback_data=f"plugin_{sf}", style=None), btn("✖ Close", callback_data="kclose", style=None)])

    await retry_on_flood(query.edit_message_reply_markup)(InlineKeyboardMarkup(button))
  else:
    await igrone_error(query.answer)(
      "This is an old button, please redo the search",
      show_alert=True
    )




@Bot.on_callback_query(filters.regex("^full") & is_auth_query())
async def full_handler(client, query):
  """ This Is Full Page Handler Of Callback Data """
  if query.data in pagination:
    chapters, webs = pagination[query.data]
    if not can_use(query.from_user.id, webs.sf):
      return await query.answer(access.denied_alert(webs.sf), show_alert=True)

    merge_size = uts[str(query.from_user.id)].get('setting', {}).get('megre', None)
    priority = user_priority(query.from_user.id)

    try:
      merge_size = int(merge_size) if merge_size else merge_size
    except:
      merge_size = None

    added_item = {}
    chapters = list(reversed(chapters))

    try:
      if merge_size:
        logger.info(merge_size)
        for i in range(0, len(chapters), merge_size):
          data = chapters[i:i + merge_size]
          episode_num = get_episode_number(data[0]['title'])
          if episode_num not in added_item:
            added_item[episode_num] = data  # list of 1+ chapters merged together

      else:
        for data in chapters:
          episode_num = get_episode_number(data['title'])
          if episode_num not in added_item:
            added_item[episode_num] = [data]

      # Fetch pictures per MERGE GROUP (concatenating every chapter in that
      # group), not per individual chapter — a flat fetch-then-zip here used
      # to misalign pictures with groups whenever merge_size > 1, silently
      # giving a merged file the wrong (or missing) pages. Fixed.
      async def _gather_group_pictures(chapter_data):
        pics = []
        for data in chapter_data:
          part = await webs.get_pictures(url=data['url'], data=data)
          if part:
            pics.extend(part)
        return pics

      groups = list(added_item.values())
      picturesList = await asyncio.gather(
        *[_gather_group_pictures(group) for group in groups]
      )

      valid = [(g, p) for g, p in zip(groups, picturesList) if p]
      if not valid:
        await igrone_error(query.answer)("❌ Couldn't fetch pages for any chapter.", show_alert=True)
        return

      # One shared status message + BatchState for the whole batch, so the
      # live progress bar (Tools/progress.py) actually shows while these
      # download - AQueue only ever runs one task per user at a time, so it's
      # safe for every chapter in this batch to update the same message.
      sts = await retry_on_flood(query.message.reply_text)(
        f"<b>📥 Queued {len(valid)} chapter{'s' if len(valid) != 1 else ''}</b>\n<i>Starting the download…</i>"
      )
      batch = BatchState(len(valid))

      tasks = [
          queue.put(
              TaskCard(
                data_list=chapter_data,
                picturesList=pictures,
                webs=webs,
                sts=sts,
                user_id=query.from_user.id,
                chat_id=query.message.chat.id,
                priority=priority,
                batch=batch,
              ),
          ) for chapter_data, pictures in valid
      ]
      
      await asyncio.gather(*tasks)

      await igrone_error(query.answer)(
        f"📥 {len(tasks)} chapter{'s' if len(tasks) != 1 else ''} added to your queue",
        show_alert=True
      )

    except Exception as err:
      logger.exception(err)
      await retry_on_flood(query.message.reply_text)(f"<b>⚠️ Something went wrong</b>\n<code>{err}</code>")
      await igrone_error(query.answer)()
        

  else:
    await retry_on_flood(query.answer)(
      "This is an old button, please redo the search",
      show_alert=True
    )


@Bot.on_callback_query(filters.regex("^pic") & is_auth_query())
async def pic_handler(client, query):
  """This Is Pictures Handler Of Callback Data"""
  if query.data in chaptersList:
    webs, data = chaptersList[query.data]
    user_id = query.from_user.id
    if not can_use(user_id, webs.sf):
      return await query.answer(access.denied_alert(webs.sf), show_alert=True)
    try:
      pictures = await webs.get_pictures(url=data['url'], data=data)
    except:
      return await query.answer("No pictures found", show_alert=True)

    if not pictures:
      return await query.answer("No pictures found", show_alert=True)

    ensure_user(user_id)
    sts = await retry_on_flood(query.message.reply_text)("<b>📥 Adding to your queue…</b>")
    try:
      txt = (f"<b>📥 Added to your queue</b>\n\n<b>{html.escape(str(data['manga_title']))}</b>\n"
             f"<i>{html.escape(str(data['title']))}</i>")
      priority = user_priority(query.from_user.id)
      
      task_id = await queue.put(
        TaskCard(
          data_list=[data.copy()],
          picturesList=pictures,
          webs=webs,
          sts=sts,
          user_id=query.from_user.id,
          chat_id=query.message.chat.id,
          priority=priority
        ),
      )
      
      button = [[btn("❌ Cancel", callback_data=f"cql:{task_id}")]]
      await retry_on_flood(sts.edit)(txt, reply_markup=InlineKeyboardMarkup(button))
      
      await query.answer("📥 Added to your queue")
    except Exception as err:
      logger.exception(err)
      await retry_on_flood(sts.edit)(f"<b>⚠️ Something went wrong</b>\n<code>{html.escape(str(err))}</code>")
      await igrone_error(query.answer)()
  else:
    await igrone_error(query.answer)("This is an old button, please redo the search", show_alert=True)


@Bot.on_callback_query(filters.regex("^cql"))
async def cl_handler(client, query):
  """This Is Cancel Handler Of Callback Data"""
  task_id = query.data.split(":")[-1]

  if await queue.delete_task(task_id):
    await retry_on_flood(query.message.edit_text)("<b>❌ Task cancelled</b>")
  else:
    await retry_on_flood(query.answer)("That task has already finished or was cancelled.", show_alert=True)
    await retry_on_flood(query.message.delete)()


@Bot.on_callback_query(filters.regex("^noop$"))
async def noop_handler(_, query):
  """Buttons that are only labels (e.g. the "2/5" page counter)."""
  await igrone_error(query.answer)()
