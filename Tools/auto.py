from bot import Bot, Vars, logger
from .db import *
from TG.storage import (
  get_episode_number, 
  get_webs, igrone_error, queue
)
from .cworker import send_manga_chapter, send_error, retry_on_flood
from .base import TaskCard, user_priority
from .access import can_use, name_of
import asyncio


async def _tell_paused_once(user_id, sf):
  """A source was locked after this user subscribed: updates pause, and they're
  told once per source (not once per manga) so it never feels like a silent failure."""
  setting = ensure_user(user_id)["setting"]
  told = setting.setdefault("lock_notified", [])
  if sf in told:
    return
  told.append(sf)
  uts.touch(str(user_id))
  await igrone_error(Bot.send_message)(
    int(user_id),
    f"<b>🔒 {name_of(sf)} is now Premium-only</b>\n\n"
    "<i>Your subscriptions from this source are paused — they resume automatically "
    "if you go Premium. Tap /my_plan to see the plans.</i>")


async def get_updates_manga(num: int = 0):

  async def check_chapters(chapters, lastest_sub_episode):
    if (chapters or len(chapters) != 0) and (lastest_sub_episode != chapters[0]['title']):
      if lastest_sub_episode is None:
        yield chapters[0]
      for chapter in list(reversed(chapters)):
        if lastest_sub_episode := get_episode_number(lastest_sub_episode):
          if (lastest_webs_episode := get_episode_number(chapter['title'])) not in [None, ""]:
            if lastest_sub_episode != lastest_webs_episode:
              if float(lastest_sub_episode) < float(lastest_webs_episode):
                yield chapter

  async for user_id, sf, sdata in get_all_subs():
    webs = get_webs(sf)
    if not webs:
      continue
    if not can_use(int(user_id), sf):          # source is locked and this user isn't premium
      await _tell_paused_once(user_id, sf)
      continue

    url = sdata['url']
    try:
      wdata = {
        "url": url,
        "title": sdata.get("title", None)
      }
      chapters = await webs.get_chapters(wdata, page=1)
      chapters = webs.iter_chapters(chapters, page=1)
      lastest_sub_episode = sdata.get('lastest_chapter', None)

      async for chapter in check_chapters(chapters, lastest_sub_episode):
        pictures = await igrone_error(webs.get_pictures)(chapter['url'], chapter)
        if pictures and len(pictures) != 0:
          user = await Bot.get_users(int(user_id))
          sts = await igrone_error(Bot.send_message)(int(user_id), f"<b><i>Updates: {chapter['manga_title']} - {chapter['title']}\n\nUrl: {chapter['url']}\n\nStatus: <code>Added At Queue</code>\n\nUser:</i></b> <code>{user_id}</code> [{user.mention()}]")
          await asyncio.sleep(3)
          await igrone_error(Bot.send_message)(
            Vars.LOG_CHANNEL, 
            f"<b><i>Updates: {chapter['manga_title']} - {chapter['title']}\n\nUrl: {chapter['url']}\n\nUser:</i></b> <code>{user_id}</code> [{user.mention()}]"
          ) if Vars.LOG_CHANNEL else None
          
          task_card = TaskCard(
            data_list=[chapter.copy()],
            picturesList=pictures,
            webs=webs,
            sts=sts,
            user_id=int(user_id),
            chat_id=int(user_id),
            priority=user_priority(int(user_id)),
            tasks_id=f"Up_{num}",
          )
          
          num += 1
          p_data = {
              'url': url,
              'title': chapter.get("manga_title"),
              'lastest_chapter': get_episode_number(task_card.episode_number)
          }
          await save_lastest_chapter(p_data, str(user_id), webs.sf)
          await queue.put(task_card, True)
          await asyncio.sleep(5)
    except Exception as err:
      logger.exception(err)



async def main_updates():
  while True:
    try:
      if Vars.AUTO_UPDATES:
        await get_updates_manga()
      else:
        logger.info("Auto updates are disabled (Settings -> Performance).")
    except Exception as err:
      logger.exception(f"L - {err}")
    finally:
      await igrone_error(remove_expired_users)()
      minutes = max(1, int(Vars.UPDATE_INTERVAL or 10))
      logger.info(f"Sleeping for {minutes} min...")
      await asyncio.sleep(minutes * 60)
