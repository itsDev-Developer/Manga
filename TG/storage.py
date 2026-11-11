from Webs import *

web_data = {
    " Manhuaplus ": ManhuaplusWebs(),
    " Mgeko ": MgekoWebs(),
    " Manga18fx ": Manga18fxWebs(),
    " Manhwa18 ": Manhwa18Webs(),
    " HentaiFox ": HentaiFoxWebs(),
    " AllPornComics ": AllPornComicsWebs(),
    " ComicsValley ": ComicsValleyWebs(),
    " NHentai ": NHentaiWebs(),
}

import asyncio
import time

import pyrogram.errors
from pyrogram import filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot import Vars, logger
from Tools.base import LRU, AQueue, igrone_error, queue, retry_on_flood
from Tools.access import can_use, is_locked
from Tools.config import fsb_list
from Tools.ui import PRIMARY, btn
from Tools.db import get_episode_number

# Callback caches. Bounded, because they hold scraped pages (BeautifulSoup trees).
searchs = LRU(300)
backs = LRU(100)
chaptersList = LRU(600)
queueList = LRU(100)
pagination = LRU(300)
subscribes = LRU(300)
post_targets = LRU(100)   # manga info for the admin-only "📤 Post to Channel" button
isearch_cache = LRU(300)  # short id -> original query text, for inline-mode deep links
  # (Telegram's /start deep-link parameter only allows [A-Za-z0-9_-], so a
  #  raw manga title with spaces/punctuation can't go directly in the URL -
  #  see TG/inline.py and TG/cmds.py's isearch_ handling.)

web_data = dict(sorted(web_data.items()))
plugins_name = " ".join(web_data[i].sf for i in web_data)


def split_list(li):
    return [li[x:x + 2] for x in range(0, len(li), 2)]


def check_get_web(url):
    for web in web_data.values():
        if url.startswith(web.url):
            return web


def is_auth_query():
    async def func(flt, _, query):
        reply = query.message.reply_to_message
        if not reply:
            return True

        if not reply.from_user:
            return False

        if reply.from_user.id != query.from_user.id:
            await query.answer("This is not for you", show_alert=True)
            return False
        return True

    return filters.create(func)


def plugins_list(type=None, page=1, user_id=None):
    """The source picker. A 🔒 marks a Premium-only source this user can't use
    (tapping it explains why); 💎 marks one that's Premium-only but open to
    them (Premium members and admins) - see Tools/access.py."""
    prefix = {"updates": "udat_", "gens": "gens_", "subs": "isubs_"}.get(type, "plugin_")
    items = []
    for name, web in web_data.items():
        label = name.strip()
        if is_locked(web.sf):
            label = f"{'💎' if can_use(user_id, web.sf) else '🔒'} {label}"
        items.append(btn(label, callback_data=f"{prefix}{web.sf}", style=None))

    half = len(items) // 2
    items = items[half:] if page != 1 else items[:half]
    rows = split_list(items)
    rows.append([
        btn("Next ›", callback_data="bk.p:2", style=None) if page == 1 else btn("‹ Previous", callback_data="bk.p:1", style=None)
    ])
    rows.append([
        btn("🔎 Search everywhere", callback_data="plugin_all", style=PRIMARY),
        btn("✖ Close", callback_data="kclose", style=None),
    ])
    return InlineKeyboardMarkup(rows)


def info_markup(webs, url, subscribed: bool, admin: bool = False):
    """The keyboard under a manga's info card. Built from scratch every time
    (also by the subscribe toggle) - patching a keyboard the client sent back
    by position is fragile and would drop button colours."""
    c = f"pg:{webs.sf}:{hash(url)}:"
    sc = f"subs:{hash(url)}"
    rows = [[btn("🔕 Unsubscribe", callback_data=sc) if subscribed else btn("🔔 Subscribe", callback_data=sc)]]
    if webs.sf == "mf":
        rows.append([btn("📚 Chapters", callback_data=f"{c}:1", style=PRIMARY),
                     btn("📦 Volumes", callback_data=f"{c}:v:1", style=None)])
    else:
        rows.append([btn("📚 Chapters", callback_data=f"{c}:1", style=PRIMARY)])
    if admin:
        rows.append([btn("📤 Post to Channel", callback_data=f"pnew:{webs.sf}:{hash(url)}", style=None),
                     btn("🔁 Auto-Publish", callback_data=f"pauto:{webs.sf}:{hash(url)}", style=None)])
    rows.append([btn("⇦ Back", callback_data=f"plugin_{webs.sf}", style=None),
                 btn("✖ Close", callback_data="kclose", style=None)])
    return InlineKeyboardMarkup(rows)


def get_webs(sf):
    return next((web for web in web_data.values() if web.sf == sf), None)


async def search_everywhere(query, timeout=6, per_site_timeout=4, for_user=False):
    """Concurrently search every site module, bounded in time so it stays
    fast enough for inline queries / instant deep-link results (unlike
    TG/search.py's search_all(), which is for the interactive search flow
    and can afford to take longer).

    for_user: pass a user id to leave out Premium-only sources that user may
    not use (inline results, deep links). The default searches everything."""
    async def _one(web):
        try:
            return await asyncio.wait_for(web.search(query), timeout=per_site_timeout)
        except Exception:
            return []

    webs = [w for w in web_data.values() if for_user is False or can_use(for_user, w.sf)]
    tasks = [asyncio.create_task(_one(web)) for web in webs]
    if not tasks:
        return []
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    for t in pending:
        t.cancel()
    results = []
    for t in done:
        try:
            r = t.result()
        except Exception:
            r = None
        if r:
            results.extend(r)
    return results


# --------------------------------------------------------------------------- #
# force subscribe
# --------------------------------------------------------------------------- #
_fsb_ok = {}       # user_id -> time of last successful check
_invite_links = {}  # chat -> invite link
FSB_TTL = 300


def fsb_channels():
    return [(c[0], c[1]) for c in fsb_list() if len(c) >= 2]


async def check_fsb(client, user_id):
    """Return the join-buttons of channels the user has NOT joined ([] = all good)."""
    if time.time() - _fsb_ok.get(user_id, 0) < FSB_TTL:
        return []

    buttons = []
    for text, channel in fsb_channels():
        try:
            await client.get_chat_member(channel, user_id)
        except pyrogram.errors.UserNotParticipant:
            link = _invite_links.get(channel)
            if not link:
                try:
                    link = (await client.export_chat_invite_link(channel)) if isinstance(channel, int) \
                        else f"https://t.me/{str(channel).strip()}"
                    _invite_links[channel] = link
                except Exception as e:
                    logger.warning(f"Force-sub: cannot create invite link for {channel}: {e}")
                    continue
            buttons.append(btn(text, url=link, style=PRIMARY))
        except Exception as e:
            # bot not admin / channel wrong: don't lock everybody out, just log it
            logger.warning(f"Force-sub check failed for {channel}: {type(e).__name__}: {e}")

    if not buttons:
        _fsb_ok[user_id] = time.time()
    return buttons


async def safe_photo_reply(message, caption, markup=None, photo=None):
    """reply_photo with a random cover; falls back to plain text if Telegram can't fetch the picture."""
    import random
    try:
        return await retry_on_flood(message.reply_photo)(
            photo or random.choice(Vars.PICS), caption=caption, reply_markup=markup, quote=True)
    except Exception:
        return await retry_on_flood(message.reply_text)(caption, reply_markup=markup, quote=True)
