"""
Inline mode — @YourBot <query> from any chat, not just a DM with the bot.

Requires inline mode to be turned on for this bot via @BotFather -> Bot
Settings -> Inline Mode (this can't be enabled from inside the bot's own
code - it's a BotFather-side setting).

Each result is a shareable card (title + site) with a deep-link button back
into the bot, since actually reading/downloading a chapter needs a private
chat with the bot (file delivery, settings, etc.) - inline results can't do
that directly.

Note: results deliberately don't set a thumbnail. Telegram/pyrogram renamed
that parameter (thumb_url -> thumbnail_url) at different times across
library versions, and guessing wrong would crash every inline query outright
- title + site name is enough to pick the right result either way.
"""
import random
import string

from pyrogram import filters
from pyrogram.types import InlineQueryResultArticle, InputTextMessageContent, InlineKeyboardMarkup

from bot import Bot, Vars, logger
from Tools.ui import btn
from .storage import search_everywhere, check_get_web, isearch_cache

CACHE_TIME = 120


def _short_id() -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=8))


async def _answer(inline_query, results, **kw):
    """inline_query.answer() raises if Telegram already expired the query id
    (client was slow) - not worth crashing the handler over."""
    try:
        await inline_query.answer(results, **kw)
    except Exception as e:
        logger.warning(f"inline answer failed (likely just expired): {e}")


@Bot.on_inline_query()
async def inline_search(client, inline_query):
    query = (inline_query.query or "").strip()

    if len(query) < 2:
        return await _answer(
            inline_query,
            [InlineQueryResultArticle(
                title="Type at least 2 characters…",
                description="e.g. One Piece, Solo Leveling",
                input_message_content=InputTextMessageContent(
                    f"🔎 Search manga with @{Bot.username} <query>"
                ),
            )],
            cache_time=1,
        )

    try:
        results = await search_everywhere(query, for_user=inline_query.from_user.id)   # Premium-only sources stay out of free users' results
    except Exception as e:
        logger.exception(e)
        results = []

    if not results:
        return await _answer(
            inline_query, [],
            cache_time=30,
            switch_pm_text=f'No results for "{query}" — tap to search inside the bot',
            switch_pm_parameter="start",
        )

    articles = []
    seen_titles = set()
    sid = _short_id()
    isearch_cache[sid] = query
    deep_link = f"https://t.me/{Bot.username}?start=isearch_{sid}"

    for r in results:
        title = r.get("title") or "Unknown"
        if title in seen_titles:
            continue
        seen_titles.add(title)

        webs_ = check_get_web(r.get("url", ""))
        site_name = type(webs_).__name__.replace("Webs", "") if webs_ else ""

        # Opening the bot with this payload re-runs the same search there,
        # so the user can subscribe/download - see TG/cmds.py's /start handler.
        # (Every result shares one deep link since re-searching the original
        # query text the user typed will surface the same matches again.)
        markup = InlineKeyboardMarkup([[btn("📖 Open in bot", url=deep_link)]])
        caption = f"<b>{title}</b>" + (f"\n<i>{site_name}</i>" if site_name else "")

        articles.append(InlineQueryResultArticle(
            title=title, description=site_name,
            input_message_content=InputTextMessageContent(caption),
            reply_markup=markup,
        ))
        if len(articles) >= 30:
            break

    await _answer(inline_query, articles, cache_time=CACHE_TIME)
