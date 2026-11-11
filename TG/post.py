"""
Post Manager
============
Lets an admin/owner publish a formatted post (poster + title + genre +
description + a "📖 Read Now" button) to a public/private "Post Channel".

Workflow:
  1. Admin sets a Dump Channel and a Post Channel once via /postsettings
     (the bot must be an admin in both).
  2. Admin runs /newpost (or taps "➕ Create New Post"). The bot asks for the
     file(s) first -> each one is copied into the Dump Channel and its
     message id is remembered. Then it asks for poster / title / genre /
     description, shows a preview, and publishes on confirmation.
  3. The published post's "📖 Read Now" button is a deep link
     (https://t.me/<bot>?start=get_<post_id>). Clicking it opens a private
     chat with the bot, which fetches the stored file(s) from the Dump
     Channel and copies them straight to the user.

Multi-channel: a post is published to every channel ticked on its preview
(see Tools/postchannels.py and /channels). Each channel has its own Read Now
label, 💎 Premium link, 💾 Back-up link and File Store Bot, so every channel
gets its own buttons and its own File Store link for the same stored files.
"""

import asyncio
import random
import re
import string
import time
from datetime import datetime

from pyrogram import filters
from pyrogram.types import InlineKeyboardMarkup
from pyrogram.errors import ListenerTimeout as TimeoutError
from pyrogram.errors import FloodWait

from bot import Bot, Vars, admin_only, logger
from Tools.ui import btn, esc, PRIMARY, SUCCESS
from Tools.config import set_value as _set_cfg_value
from Tools.db import (
    get_config, set_config, save_post, get_post,
    delete_post, all_posts, bump_post_reads, uts, get_episode_number,
    save_scheduled, get_scheduled, all_scheduled, delete_scheduled,
    add_autopublish, remove_autopublish, list_autopublish, set_autopublish_progress,
)
from Tools.base import TaskCard, user_priority
from Tools.cworker import send_manga_chapter
from Tools.progress import BatchState
from Tools.templates import render_post, plain_emoji_version, TEMPLATES
from Tools.poster import poster_for_telegram
from Tools import filestore_api
from Tools import postchannels as pc
from Tools import storage_db
from .storage import retry_on_flood, igrone_error, post_targets, get_webs, web_data


def web_data_values():
    return list(web_data.values())



# In-memory wizard drafts, keyed by the admin's user_id, while a post is
# being built and awaiting "✅ Confirm & Publish".
_drafts = {}

# In-memory state for the "search -> 📤 Post to Channel" auto-fetch flow,
# keyed by admin user_id, while chapters/range are being picked.
_auto_state = {}

CANCEL_WORDS = ("/cancel", "cancel")


from Tools.cards import parse_manga_msg   # (moved to Tools/cards.py so the info card can share it)


def _gen_post_id() -> str:
    chars = string.ascii_letters + string.digits
    while True:
        pid = "".join(random.choices(chars, k=8))
        if not get_post(pid):
            return pid


def _norm_channel(value):
    """Channel values may be a numeric id or a @username - keep whichever it is."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def get_post_channel():
    # Canonical key shared with the generic /settings -> Channels panel
    # (Tools/config.py) - both UIs must edit the exact same value or they'll
    # silently overwrite each other.
    return Vars.POST_CHANNEL


def get_dump_channel():
    """Where post files live = the storage channel (Constant Dump, with the
    Dump Channel only as a fallback) - one place, so nothing is stored twice."""
    return storage_db.storage_channel()


def _post_data(title, genres, status="", description="", chapters=None, formats=None, source=None):
    """Shape expected by Tools.templates.render_post. `genres` may be a
    comma-separated string or a list - render_post normalizes it."""
    return {
        "title": title, "genres": genres, "status": status,
        "description": description, "chapters": chapters,
        "formats": formats, "source": source,
    }


def _poster_referer(poster):
    """The Referer a poster's CDN expects: the registered source whose domain the poster lives on, else None
    (Tools.poster then guesses from the host name)."""
    if not isinstance(poster, str) or not poster.startswith("http"):
        return None
    from urllib.parse import urlparse
    host = urlparse(poster).netloc.split(":")[0].lower()
    for web in web_data_values():
        site = urlparse(web.url).netloc.split(":")[0].lower().removeprefix("www.")
        if site and (host == site or host.endswith("." + site)):
            return web.url
    return None


def _build_caption(post_data: dict, template: int = None) -> str:
    return render_post(post_data, template=template)


def _template_row(current: int, prefix: str) -> list:
    """🎨 template-switch buttons for a post preview, wrapped three to a row. -> list of rows"""
    buttons = [
        btn(f"{'✅ ' if n == current else ''}{name}", callback_data=f"{prefix}{n}")
        for n, (name, _fn) in TEMPLATES.items()
    ]
    return [buttons[i:i + 3] for i in range(0, len(buttons), 3)]


async def _publish_photo(send_fn, post_data, template=None):
    """send_fn(caption) -> awaited Message. If the channel/bot rejects the
    template's premium (custom) emoji entities (e.g. no Premium on that
    account), retries once with the plain-emoji version so the post still
    goes out instead of silently failing.
    Returns (sent_message, caption_actually_used)."""
    caption = _build_caption(post_data, template)
    try:
        sent = await send_fn(caption)
    except FloodWait:
        raise   # let retry_on_flood handle throttling - not an emoji problem
    except Exception as e:
        if not Vars.POST_PREMIUM_EMOJI:
            raise
        logger.warning(f"Publish with premium emoji failed, retrying plain: {e}")
        caption = plain_emoji_version(post_data, template)
        sent = await send_fn(caption)
    return sent, caption


def _find_duplicate(title: str):
    """Best-effort 'have we already posted this?' check - same title,
    case/whitespace-insensitive. Returns (post_id, post_data) or None."""
    norm = " ".join(title.split()).lower()
    for pid, data in all_posts().items():
        if " ".join(str(data.get("title", "")).split()).lower() == norm:
            return pid, data
    return None


class PubResult:
    """Outcome of publishing one post to one channel."""
    __slots__ = ("ch", "sent", "error", "via", "link")

    def __init__(self, ch, sent=None, error=None, via="", link=None):
        self.ch, self.sent, self.error, self.via, self.link = ch, sent, error, via, link


def _targets(draft: dict) -> list:
    """The channel profiles a draft goes to: the ones ticked on its preview, or
    every active channel when nothing was chosen (scheduled / auto posts)."""
    pool = {c["id"]: c for c in pc.active_channels()}
    chosen = draft.get("channels")
    if chosen is None:
        return list(pool.values())
    return [pool[i] for i in chosen if i in pool]


def _markup_for(ch: dict, read_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[btn(text, url=url, style=style) for text, url, style in row]
                                 for row in pc.button_specs(ch, read_url)])


async def _read_url_for(ch: dict, draft: dict, cache: dict):
    """-> (url, via-text, link-or-None): where this channel's Read Now button leads.
    A File Store link is generated per channel (its own File Store Bot); channels
    sharing one File Store Bot share one link. Any failure falls back to this
    bot's own delivery, so a File Store hiccup never blocks a post."""
    own = f"https://t.me/{Bot.username}?start=get_{draft['post_id']}"
    fs = pc.fs_for(ch)
    if not (fs and fs.ready):
        return own, "this bot", None

    link = None
    if ch["id"] == pc.MAIN and draft.get("filestore_link"):
        link = draft["filestore_link"]          # a post queued before multi-channel posting existed
    elif fs.cache_key in cache:
        link = cache[fs.cache_key]
    else:
        link = await filestore_api.create_link(_norm_channel(draft["dump_channel"]), draft["file_ids"], fs=fs)
        cache[fs.cache_key] = link

    who = f"File Store @{fs.username}" if fs.username else "File Store Bot"
    if link:
        return link, who, link
    return own, f"this bot ({who} link failed)", None


async def _do_publish(client, draft: dict):
    """Publish `draft` to every target channel and save the post record.
    Shared by the manual wizard, the auto-fetch flow, scheduled posts, and
    channel auto-publish, so all four behave identically.

    Returns one PubResult per channel. A channel that fails doesn't stop the
    others; raises only if there's nowhere to post, or every channel failed."""
    targets = _targets(draft)
    if not targets:
        if not pc.active_channels():
            raise RuntimeError("No post channel is set up yet (see /channels).")
        raise RuntimeError("None of the selected channels is switched on and set (see /channels).")

    cache = {}
    results = []
    # one high-quality local copy of the poster, shared by every channel (falls back to the URL itself)
    poster_input = await poster_for_telegram(draft["poster"], _poster_referer(draft["poster"]))
    for ch in targets:
        try:
            read_url, via, link = await _read_url_for(ch, draft, cache)
            markup = _markup_for(ch, read_url)
            chat = _norm_channel(ch["chat"])

            async def _send(caption, _chat=chat, _markup=markup):
                try:
                    return await client.send_photo(
                        _chat, poster_input, caption=caption,
                        reply_markup=_markup, has_spoiler=Vars.POST_SPOILER,
                    )
                except Exception as e:
                    # our high-quality file was refused as a picture (odd size...): send the URL as before
                    if poster_input == draft["poster"] or not any(w in str(e).upper() for w in ("PHOTO", "IMAGE", "MEDIA_")):
                        raise
                    logger.warning(f"poster file refused ({e}); sending the poster URL instead")
                    return await client.send_photo(
                        _chat, draft["poster"], caption=caption,
                        reply_markup=_markup, has_spoiler=Vars.POST_SPOILER,
                    )

            sent, _caption = await retry_on_flood(_publish_photo)(_send, draft["data"], draft["template"])
            results.append(PubResult(ch, sent=sent, via=via, link=link))
        except Exception as e:
            logger.exception(f"Publishing to channel {ch.get('name')} failed: {e}")
            results.append(PubResult(ch, error=str(e) or type(e).__name__))
        if len(targets) > 1:
            await asyncio.sleep(1)     # gentle on Telegram's flood limits when posting to several channels

    done = [r for r in results if r.sent]
    if not done:
        raise RuntimeError("; ".join(f"{r.ch['name']}: {r.error}" for r in results))

    first = done[0]
    save_post(draft["post_id"], {
        "title": draft["data"]["title"],
        "data": draft["data"],
        "template": draft["template"],
        "poster": draft["poster"],
        "dump_channel": draft["dump_channel"],
        "file_ids": draft["file_ids"],
        "filestore_link": first.link,
        # kept for anything that still reads the old single-channel fields
        "post_channel": _norm_channel(first.ch["chat"]),
        "post_msg_id": first.sent.id,
        # one entry per channel the post went to
        "channels": [
            {"id": r.ch["id"], "name": r.ch["name"], "chat": _norm_channel(r.ch["chat"]),
             "msg_id": r.sent.id, "via": r.via, "link": r.link}
            for r in done
        ],
        "created_by": draft.get("created_by"),
        "created_at": int(time.time()),
        "reads": 0,
    })
    return results


def _results_text(results) -> str:
    """Per-channel report shown to the admin after publishing."""
    lines = []
    for r in results:
        name = esc(r.ch["name"])
        if r.sent:
            link = ""
            uname = getattr(r.sent.chat, "username", None)
            if uname:
                link = f" · <a href=\"https://t.me/{uname}/{r.sent.id}\">open</a>"
            lines.append(f"✅ <b>{name}</b> — Read Now via {esc(r.via)}{link}")
        else:
            lines.append(f"❌ <b>{name}</b> — <code>{esc(r.error)}</code>")
    return "\n".join(lines)


def _settings_text():
    storage = get_dump_channel()
    via = "Constant Dump" if Vars.CONSTANT_DUMP_CHANNEL else ("Dump Channel fallback" if storage else "")
    total = len(all_posts())
    chans = pc.all_channels()
    lines = []
    for c in chans:
        mark = "🟢" if (c["enabled"] and c.get("chat")) else "⚪"
        extras = ("💎" if c["premium_url"] else "") + ("💾" if c["backup_url"] else "") + ("🗄" if (pc.fs_for(c) and pc.fs_for(c).ready) else "")
        lines.append(f"{mark} <b>{esc(c['name'])}</b> — <code>{esc(c.get('chat') or 'not set')}</code> {extras}")
    return (
        "<b>📮 Post Manager</b>\n\n"
        "<b>📡 Post channels</b>\n" + "\n".join(lines) + "\n"
        f"<b>💾 Storage:</b> <code>{storage or 'Not Set'}</code>" + (f" <i>({via})</i>" if via else "") + "\n"
        f"<b>📚 Total Posts:</b> <code>{total}</code>\n\n"
        "<blockquote expandable>"
        "• <b>Post channels</b> — every post can go to several channels at once. Each channel has "
        "its own 💎 Premium link, 💾 Back-up link, Read Now text and 🗄 File Store Bot. "
        "Manage them with /channels.\n"
        "• <b>Storage</b> — the single channel every file is stored in, once. It's the "
        "<b>Constant Dump</b> (set in /settings -> 📢 Channels); the Dump Channel below is "
        "only used if no Constant Dump is set. Chapters already stored here are reused "
        "instead of being downloaded again.\n"
        "• The bot must be an <b>admin</b> in every channel it posts to and in the storage channel."
        "</blockquote>"
    )


def _settings_markup():
    return InlineKeyboardMarkup([
        [btn("📡 Post Channels", callback_data="chm:home", style=PRIMARY)],
        [btn("📥 Set Dump Channel (fallback)", callback_data="pmset_dump")],
        [btn("➕ Create New Post", callback_data="pmp_new")],
        [btn("📋 Manage Posts", callback_data="pmp_manage:1")],
        [btn("✖️ Close", callback_data="kclose")],
    ])


# ---------------------------------------------------------------------------
# /postsettings — configure Post Channel & Dump Channel, manage posts
# ---------------------------------------------------------------------------

@Bot.on_message(filters.command(["postsettings", "pm"]) & admin_only)
async def post_settings_cmd(client, message):
    await retry_on_flood(message.reply_text)(
        _settings_text(), quote=True, reply_markup=_settings_markup()
    )


@Bot.on_callback_query(filters.regex("^pmpanel$") & admin_only)
async def post_settings_panel_cb(client, query):
    await igrone_error(query.answer)()
    await retry_on_flood(query.edit_message_text)(
        _settings_text(), reply_markup=_settings_markup()
    )


@Bot.on_callback_query(filters.regex("^pmset_") & admin_only)
async def post_settings_set_cb(client, query):
    kind = query.data.removeprefix("pmset_")  # "post" or "dump"
    label = "Post Channel" if kind == "post" else "Dump Channel"
    await igrone_error(query.answer)()

    await retry_on_flood(query.edit_message_text)(
        f"<b>📐 Send the {label}</b>\n\n"
        "<blockquote>Forward any message from that channel, or send its "
        "username (without @) or numeric ID. Make sure the bot is an admin "
        f"there.\n\nSend /cancel to abort.</blockquote>"
    )

    try:
        call = await client.listen(
            user_id=query.from_user.id, timeout=120,
            filters=filters.text | filters.forwarded,
        )
    except TimeoutError:
        return await retry_on_flood(query.message.edit_text)(
            "📐 Timed out. Run /postsettings again."
        )

    if call.text and call.text.strip().lower() in CANCEL_WORDS:
        await igrone_error(call.delete)()
        return await retry_on_flood(query.message.edit_text)(
            _settings_text(), reply_markup=_settings_markup()
        )

    value = None
    if call.forward_from_chat:
        value = call.forward_from_chat.id
    elif call.text:
        text = call.text.strip()
        try:
            value = int(text)
        except ValueError:
            value = text

    await igrone_error(call.delete)()

    if value is None:
        return await retry_on_flood(query.message.edit_text)(
            "❌ Couldn't read that as a channel. Run /postsettings again."
        )

    set_config(f"{kind}_channel", value)  # legacy key, kept so any old data still reads back
    _set_cfg_value(f"{kind.upper()}_CHANNEL", value)   # canonical key used everywhere else
    await retry_on_flood(query.message.edit_text)(f"✅ {label} set to <code>{value}</code>.")
    await asyncio.sleep(1.5)
    await retry_on_flood(query.message.edit_text)(
        _settings_text(), reply_markup=_settings_markup()
    )


# ---------------------------------------------------------------------------
# /newpost — step-by-step wizard to build & publish a post
# ---------------------------------------------------------------------------

@Bot.on_callback_query(filters.regex("^pmp_new$") & admin_only)
async def new_post_cb(client, query):
    await igrone_error(query.answer)()
    await create_post_wizard(client, query.message.chat.id, query.from_user.id)


@Bot.on_message(filters.command("newpost") & admin_only)
async def new_post_cmd(client, message):
    await create_post_wizard(client, message.chat.id, message.from_user.id)


async def _ask(client, chat_id, admin_id, text, timeout=180):
    """Send a prompt and wait for the admin's next message."""
    await retry_on_flood(client.send_message)(chat_id, text)
    try:
        return await client.listen(user_id=admin_id, timeout=timeout)
    except TimeoutError:
        await retry_on_flood(client.send_message)(chat_id, "⏰ Timed out. Post creation cancelled.")
        return None


async def _generate_read_link(dump_channel_id, file_ids, fs=None):
    """Automatically turn this post's Dump Channel messages into a File
    Store Bot link via the Link Generation API (Tools/filestore_api.py).
    Returns None (falls back to this bot's own delivery) if the feature is
    off, unconfigured, or the call fails for any reason - a post should
    never be blocked by this."""
    if not filestore_api.configured(fs):
        return None
    link = await filestore_api.create_link(dump_channel_id, file_ids, fs=fs)
    if not link:
        logger.warning("File Store link generation failed - falling back to this bot's own delivery.")
    return link


async def create_post_wizard(client, chat_id, admin_id):
    dump_channel = get_dump_channel()
    if not dump_channel:
        return await retry_on_flood(client.send_message)(
            chat_id,
            "❌ No <b>storage channel</b> is set yet. Set the Constant Dump Channel in /settings -> 📢 Channels first."
        )
    if not pc.active_channels():
        return await retry_on_flood(client.send_message)(
            chat_id, "❌ No <b>post channel</b> is set up yet. Add one with /channels first."
        )

    incoming = []   # the admin's messages; copied into storage together, once /done is sent

    # Step 1 — files, stored in the storage channel
    await retry_on_flood(client.send_message)(
        chat_id,
        "<b>📁 Step 1/5 — Files</b>\n\n"
        "Send the file(s) for this post one at a time (documents, PDFs, "
        "images, or forward them from anywhere). They'll be stored in the "
        "storage channel.\n\nSend <code>/done</code> when finished, or "
        "<code>/cancel</code> to abort."
    )
    while True:
        try:
            call = await client.listen(user_id=admin_id, timeout=300)
        except TimeoutError:
            return await retry_on_flood(client.send_message)(chat_id, "⏰ Timed out. Post creation cancelled.")

        text = (call.text or "").strip().lower()
        if text == "/cancel":
            await igrone_error(call.delete)()
            return await retry_on_flood(client.send_message)(chat_id, "❌ Post creation cancelled.")

        if text == "/done":
            await igrone_error(call.delete)()
            break

        media = call.document or call.video or call.audio or call.photo or call.animation
        if not media:
            await retry_on_flood(client.send_message)(chat_id, "⚠️ Send a file, or /done, or /cancel.")
            continue

        incoming.append(call)
        await retry_on_flood(client.send_message)(
            chat_id, f"✅ Got it ({len(incoming)} file(s) so far). Send another, or /done."
        )

    # Copy everything in one go, holding the exclusive storage write, so the
    # post's files end up as one contiguous run of message ids (required for
    # a File Store batch link) instead of having other uploads land between them.
    file_ids = []
    if incoming:
        if storage_db.busy():
            await retry_on_flood(client.send_message)(
                chat_id, "⏳ Another post is being written to storage - yours is next."
            )
        async with storage_db.exclusive():
            for call in incoming:
                try:
                    copied = await retry_on_flood(call.copy)(_norm_channel(dump_channel))
                    file_ids.append(copied.id)
                except Exception as e:
                    logger.exception(e)
                    await retry_on_flood(client.send_message)(
                        chat_id,
                        f"❌ Couldn't save a file to the storage channel: <code>{e}</code>\n"
                        "Make sure the bot is an admin there."
                    )
        await retry_on_flood(client.send_message)(
            chat_id, f"💾 Stored {len(file_ids)}/{len(incoming)} file(s)."
        )

    if not file_ids:
        return await retry_on_flood(client.send_message)(chat_id, "❌ No files were saved. Post creation cancelled.")

    # (File Store links are generated per channel when the post is published.)

    # Step 2 — poster
    reply = await _ask(
        client, chat_id, admin_id,
        "<b>🖼 Step 2/5 — Poster</b>\n\nSend the poster image (as a photo), or paste an image URL."
    )
    if reply is None:
        return
    if reply.text and reply.text.strip().lower() in CANCEL_WORDS:
        await igrone_error(reply.delete)()
        return await retry_on_flood(client.send_message)(chat_id, "❌ Post creation cancelled.")

    poster = None
    if reply.photo:
        poster = reply.photo.file_id
    elif reply.text and reply.text.strip().startswith("http"):
        poster = reply.text.strip()
    await igrone_error(reply.delete)()

    if not poster:
        return await retry_on_flood(client.send_message)(chat_id, "❌ That's not a valid poster. Post creation cancelled.")

    # Step 3 — title
    reply = await _ask(client, chat_id, admin_id, "<b>📝 Step 3/5 — Title</b>\n\nSend the manga's title.")
    if reply is None:
        return
    title = (reply.text or "").strip()
    await igrone_error(reply.delete)()
    if not title or title.lower() in CANCEL_WORDS:
        return await retry_on_flood(client.send_message)(chat_id, "❌ Post creation cancelled.")

    # Step 4 — genre
    reply = await _ask(
        client, chat_id, admin_id,
        "<b>🏷 Step 4/5 — Genre</b>\n\nSend the genre(s), comma separated.\n"
        "e.g. <code>Action, Fantasy, Drama</code>"
    )
    if reply is None:
        return
    genre_raw = (reply.text or "").strip()
    await igrone_error(reply.delete)()
    if not genre_raw or genre_raw.lower() in CANCEL_WORDS:
        return await retry_on_flood(client.send_message)(chat_id, "❌ Post creation cancelled.")
    genres = [g.strip() for g in genre_raw.split(",") if g.strip()]

    # Step 5 — description
    reply = await _ask(
        client, chat_id, admin_id,
        "<b>🗒 Step 5/5 — Description</b>\n\nSend the synopsis/description."
    )
    if reply is None:
        return
    description = (reply.text or reply.caption or "").strip()
    await igrone_error(reply.delete)()
    if not description or description.lower() in CANCEL_WORDS:
        return await retry_on_flood(client.send_message)(chat_id, "❌ Post creation cancelled.")

    post_id = _gen_post_id()
    post_data = _post_data(title, genres, description=description)
    _drafts[admin_id] = {
        "post_id": post_id,
        "data": post_data,
        "template": Vars.POST_TEMPLATE,
        "poster": poster,
        "dump_channel": _norm_channel(dump_channel),
        "file_ids": file_ids,
        "channels": pc.default_ids(),
    }

    await _show_draft_preview(client, chat_id, admin_id)


async def _show_draft_preview(client, chat_id, admin_id, edit=None):
    draft = _drafts[admin_id]
    active = pc.active_channels()
    # drop channels that were switched off / removed since the draft was made
    draft["channels"] = [i for i in (draft.get("channels") or []) if any(c["id"] == i for c in active)]
    chosen = [c for c in active if c["id"] in draft["channels"]]

    caption = _build_caption(draft["data"], draft["template"])

    rows = []
    if chosen:
        # the buttons exactly as the first selected channel will show them
        for specs in pc.button_specs(chosen[0], "https://t.me"):
            rows.append([btn(text, callback_data="pmp_noop", style=style) for text, _u, style in specs])
    else:
        rows.append([btn("⚠️ No channel selected", callback_data="pmp_noop", style=None)])

    if len(active) > 1:
        toggles = [
            btn(f"{'✅' if c['id'] in draft['channels'] else '☑️'} {c['name'][:22]}",
                callback_data=f"pdchan_{c['id']}", style=None)
            for c in active
        ]
        rows += [toggles[i:i + 2] for i in range(0, len(toggles), 2)]

    rows.extend(_template_row(draft["template"], "pdrafttmpl_"))
    if draft["data"].get("description"):
        hidden = bool(draft["data"].get("hide_description"))
        rows.append([btn("📝 Description: OFF" if hidden else "📝 Description: ON",
                         callback_data="pdraft_desc", style=None if hidden else SUCCESS)])
    rows.append([
        btn("✅ Confirm & Publish", callback_data="pdraft_confirm"),
        btn("🕒 Schedule", callback_data="pdraft_schedule"),
    ])
    rows.append([btn("❌ Cancel", callback_data="pdraft_cancel")])
    preview_markup = InlineKeyboardMarkup(rows)

    text = "<b>🔎 Preview — this is exactly how it will look:</b>\n\n" + caption
    if edit is not None:
        return await retry_on_flood(edit)(caption=text, reply_markup=preview_markup)
    await retry_on_flood(client.send_photo)(
        chat_id, await poster_for_telegram(draft["poster"], _poster_referer(draft["poster"])),
        caption=text, reply_markup=preview_markup,
    )


@Bot.on_callback_query(filters.regex("^pdchan_") & admin_only)
async def draft_channel_cb(client, query):
    admin_id = query.from_user.id
    draft = _drafts.get(admin_id)
    if not draft:
        return await retry_on_flood(query.answer)("⚠️ No pending draft found.", show_alert=True)
    cid = query.data.removeprefix("pdchan_")
    sel = draft.setdefault("channels", [])
    if cid in sel:
        sel.remove(cid)
    elif any(c["id"] == cid for c in pc.active_channels()):
        sel.append(cid)
    ch = pc.get_channel(cid)
    await igrone_error(query.answer)(f"{'✅' if cid in sel else '☑️'} {ch['name'] if ch else cid}")
    await _show_draft_preview(client, None, admin_id, edit=query.edit_message_caption)


@Bot.on_callback_query(filters.regex("^pdrafttmpl_") & admin_only)
async def draft_template_cb(client, query):
    admin_id = query.from_user.id
    draft = _drafts.get(admin_id)
    if not draft:
        return await retry_on_flood(query.answer)("⚠️ No pending draft found.", show_alert=True)
    draft["template"] = int(query.data.removeprefix("pdrafttmpl_"))
    await igrone_error(query.answer)(f"🎨 {TEMPLATES[draft['template']][0]}")
    await _show_draft_preview(client, None, admin_id, edit=query.edit_message_caption)


@Bot.on_callback_query(filters.regex("^pdraft_") & admin_only)
async def draft_decision_cb(client, query):
    admin_id = query.from_user.id
    draft = _drafts.get(admin_id)
    if not draft:
        return await retry_on_flood(query.answer)("⚠️ No pending draft found. Run /newpost again.", show_alert=True)

    action = query.data.removeprefix("pdraft_")
    if action == "cancel":
        del _drafts[admin_id]
        await igrone_error(query.answer)("❌ Cancelled")
        return await igrone_error(query.message.delete)()

    if action == "desc":                    # show / hide the synopsis in this post
        hidden = not draft["data"].get("hide_description")
        draft["data"]["hide_description"] = hidden
        await igrone_error(query.answer)("📝 Description hidden" if hidden else "📝 Description shown")
        return await _show_draft_preview(client, None, admin_id, edit=query.edit_message_caption)

    if action in ("schedule", "confirm", "forcepublish") and not draft.get("channels"):
        return await retry_on_flood(query.answer)("⚠️ Tick at least one channel to post to.", show_alert=True)

    if action == "schedule":
        return await _ask_schedule_time(client, query, admin_id, draft)

    if action == "confirm":
        dup = _find_duplicate(draft["data"]["title"])
        if dup:
            pid, pdata = dup
            await igrone_error(query.answer)()
            warn_markup = InlineKeyboardMarkup([
                [btn("✅ Publish Anyway", callback_data="pdraft_forcepublish")],
                [btn("⇦ Back to preview", callback_data="pdraft_back")],
            ])
            return await retry_on_flood(query.message.edit_caption)(
                f"<b>⚠️ Possible duplicate</b>\n\n"
                f"A post titled <b>{pdata.get('title')}</b> already exists "
                f"(ID <code>{pid}</code>, {pdata.get('reads', 0)} reads).\n\n"
                "Publish this one anyway?",
                reply_markup=warn_markup,
            )
        return await _publish_draft_now(client, query, admin_id, draft)

    if action == "back":
        return await _show_draft_preview(client, None, admin_id, edit=query.edit_message_caption)

    if action == "forcepublish":
        return await _publish_draft_now(client, query, admin_id, draft)


async def _publish_draft_now(client, query, admin_id, draft):
    try:
        results = await _do_publish(client, {**draft, "created_by": admin_id})
    except Exception as e:
        logger.exception(e)
        return await retry_on_flood(query.answer)(f"❌ Failed to publish: {e}", show_alert=True)

    del _drafts[admin_id]
    ok = sum(1 for r in results if r.sent)
    await igrone_error(query.answer)(f"✅ Published to {ok}/{len(results)} channel(s)")
    head = "✅ <b>Post published!</b>" if ok == len(results) else f"⚠️ <b>Published to {ok} of {len(results)} channels</b>"
    txt = f"{head}\n\n<b>ID:</b> <code>{draft['post_id']}</code>\n\n{_results_text(results)}"
    await igrone_error(query.message.edit_caption)(txt)


# ---------------------------------------------------------------------------
# 🕒 Scheduled posting
# ---------------------------------------------------------------------------

_REL_RE = re.compile(r"^\s*(?:in\s*)?(\d+)\s*([mhd])\w*\s*$", re.I)
_ABS_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M")


def _parse_when(text: str):
    """'in 2h' / 'in 30m' / 'in 1d' -> relative. 'YYYY-MM-DD HH:MM' -> absolute
    (server time - see the prompt). Returns a unix timestamp, or None."""
    text = text.strip()
    m = _REL_RE.match(text)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        secs = {"m": 60, "h": 3600, "d": 86400}[unit]
        return int(time.time()) + n * secs
    for fmt in _ABS_FORMATS:
        try:
            return int(datetime.strptime(text, fmt).timestamp())
        except ValueError:
            continue
    return None


def _gen_sched_id() -> str:
    chars = string.ascii_letters + string.digits
    while True:
        sid = "".join(random.choices(chars, k=8))
        if not get_scheduled(sid):
            return sid


async def _ask_schedule_time(client, query, admin_id, draft):
    await igrone_error(query.answer)()
    await retry_on_flood(query.message.edit_caption)(
        "<b>🕒 Schedule this post</b>\n\n"
        "Send when to publish it:\n"
        "• <code>in 2h</code>, <code>in 30m</code>, <code>in 1d</code> - relative to now\n"
        "• <code>2026-10-01 14:00</code> - an exact date/time (server clock, usually UTC)\n\n"
        "Send /cancel to go back."
    )
    try:
        reply = await client.listen(user_id=admin_id, timeout=120)
    except TimeoutError:
        return await igrone_error(query.message.edit_caption)(
            "⏰ Timed out.", reply_markup=None
        )

    text = (reply.text or "").strip()
    await igrone_error(reply.delete)()

    if text.lower() in CANCEL_WORDS:
        return await _show_draft_preview(client, None, admin_id, edit=query.message.edit_caption)

    run_at = _parse_when(text)
    if not run_at or run_at <= time.time():
        await igrone_error(client.send_message)(
            admin_id, "❌ Couldn't understand that time (or it's in the past). Try again."
        )
        return await _show_draft_preview(client, None, admin_id, edit=query.message.edit_caption)

    sched_id = _gen_sched_id()
    save_scheduled(sched_id, {**draft, "created_by": admin_id, "chat_id": query.message.chat.id, "run_at": run_at})
    del _drafts[admin_id]

    when_txt = datetime.fromtimestamp(run_at).strftime("%Y-%m-%d %H:%M")
    await igrone_error(query.message.edit_caption)(
        f"<b>🕒 Scheduled!</b>\n\nWill publish at <code>{when_txt}</code> (server time).\n"
        f"Manage pending posts with /scheduled."
    )


@Bot.on_message(filters.command("scheduled") & admin_only)
async def scheduled_list_cmd(client, message):
    text, markup = _scheduled_list_view()
    await retry_on_flood(message.reply_text)(text, quote=True, reply_markup=markup)


@Bot.on_callback_query(filters.regex("^schedlist$") & admin_only)
async def scheduled_list_cb(client, query):
    await igrone_error(query.answer)()
    text, markup = _scheduled_list_view()
    await retry_on_flood(query.edit_message_text)(text, reply_markup=markup)


def _scheduled_list_view():
    items = sorted(all_scheduled().items(), key=lambda kv: kv[1].get("run_at", 0))
    if not items:
        return "📭 No scheduled posts.", None
    lines = ["<b>🕒 Scheduled Posts</b>\n"]
    rows = []
    for sid, data in items:
        when_txt = datetime.fromtimestamp(data.get("run_at", 0)).strftime("%Y-%m-%d %H:%M")
        title = data.get("data", {}).get("title", sid)
        lines.append(f"• <b>{title}</b> — <code>{when_txt}</code>")
        rows.append([btn(f"🗑 Cancel: {title[:24]}", callback_data=f"schedcancel_{sid}")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


@Bot.on_callback_query(filters.regex("^schedcancel_") & admin_only)
async def scheduled_cancel_cb(client, query):
    sid = query.data.removeprefix("schedcancel_")
    delete_scheduled(sid)
    await igrone_error(query.answer)("🗑 Cancelled.")
    text, markup = _scheduled_list_view()
    await retry_on_flood(query.edit_message_text)(text, reply_markup=markup)


async def scheduled_posts_loop():
    """Fires any scheduled posts whose time has come. Persisted to the DB
    (not just in-memory), so a restart before the scheduled time doesn't
    lose it - see Tools/db.py's `sch` table."""
    while True:
        try:
            now = int(time.time())
            for sid, draft in list(all_scheduled().items()):
                if draft.get("run_at", 0) > now:
                    continue
                delete_scheduled(sid)
                try:
                    results = await _do_publish(Bot, draft)
                    logger.info(f"Scheduled post {sid} ({draft.get('data', {}).get('title')}) published.")
                    await igrone_error(Bot.send_message)(
                        draft.get("chat_id") or draft.get("created_by"),
                        f"✅ <b>Scheduled post published!</b>\n\n<b>ID:</b> <code>{draft['post_id']}</code>\n\n"
                        f"{_results_text(results)}",
                    )
                except Exception as e:
                    logger.exception(f"Scheduled post {sid} failed: {e}")
                    await igrone_error(Bot.send_message)(
                        draft.get("chat_id") or draft.get("created_by"),
                        f"❌ Scheduled post for <b>{draft.get('data', {}).get('title')}</b> failed: <code>{e}</code>",
                    )
        except Exception as e:
            logger.exception(e)
        finally:
            await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# 🔁 Auto-publish — watches specific manga and auto-posts new chapters
# ---------------------------------------------------------------------------

@Bot.on_callback_query(filters.regex("^pauto:") & admin_only)
async def auto_publish_track_cb(client, query):
    if query.data not in post_targets:
        return await retry_on_flood(query.answer)(
            "This is an old button, please redo the search", show_alert=True
        )
    webs, bio_list, data = post_targets[query.data]
    title = bio_list.get("title", "Unknown")
    add_autopublish(webs.sf, bio_list["url"], title, query.from_user.id)
    await retry_on_flood(query.answer)(f"🔁 Now watching \"{title}\" for new chapters.", show_alert=True)

    post_channel = bool(pc.active_channels())
    dup = _find_duplicate(title)
    already_posted = dup is not None

    if already_posted and post_channel:
        # It's already on the channel(s) - let readers there know new
        # chapters will keep coming automatically from now on.
        for chat, _msg in pc.chats_of_post(dup[1]):
            try:
                await retry_on_flood(client.send_message)(
                    _norm_channel(chat),
                    f"🔁 <b>Auto-Publish activated</b> for <b>{title}</b> — new chapters "
                    "will be posted here automatically as they release.",
                )
            except Exception as e:
                logger.exception(e)
    else:
        # Not posted yet - give the admins a heads-up so they can post the
        # series properly (with poster/genre/synopsis) before or alongside
        # whatever auto-publish ends up posting on its own.
        reason = "no post channel is set up yet" if not post_channel else "it hasn't been posted there yet"
        for admin_id in Vars.ADMINS:
            await igrone_error(client.send_message)(
                admin_id,
                f"⚠️ <b>{title}</b> was just added to Auto-Publish, but {reason}. "
                "New chapters will still be downloaded and posted automatically "
                "as their own post when they release - consider using /newpost "
                "or \"📤 Post to Channel\" first so readers have a proper intro "
                "post (poster, genre, synopsis) to find the series from.",
            )


@Bot.on_message(filters.command("autopublish") & admin_only)
async def autopublish_list_cmd(client, message):
    text, markup = _autopublish_list_view()
    await retry_on_flood(message.reply_text)(text, quote=True, reply_markup=markup)


def _autopublish_list_view():
    items = list_autopublish()
    if not items:
        return (
            "📭 Nothing is tracked for auto-publish yet.\n\n"
            "Search a manga, open it, and tap <b>🔁 Auto-Publish</b> on its info card.",
            None,
        )
    lines = ["<b>🔁 Auto-Publish List</b>\n<i>New chapters get downloaded and posted automatically.</i>\n"]
    rows = []
    for key, entry in items.items():
        lines.append(f"• <b>{entry.get('title')}</b> ({entry.get('sf')})")
        rows.append([btn(f"🗑 Remove: {entry.get('title', '')[:24]}", callback_data=f"autopubdel_{key}")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


@Bot.on_callback_query(filters.regex("^autopubdel_") & admin_only)
async def autopublish_del_cb(client, query):
    key = query.data.removeprefix("autopubdel_")
    remove_autopublish(key)
    await igrone_error(query.answer)("🗑 Removed.")
    text, markup = _autopublish_list_view()
    await retry_on_flood(query.edit_message_text)(text, reply_markup=markup)


async def _auto_publish_now(webs, bio_list, new_chapters):
    """Download `new_chapters` (oldest -> newest) and publish them as one
    post, using the exact same pipeline as every other publish path."""
    dump_channel_id = get_dump_channel()
    try:
        dump_channel_id = int(dump_channel_id)
    except (TypeError, ValueError):
        logger.warning("Auto-publish skipped: the storage channel (Constant Dump) isn't set to a numeric id.")
        return

    file_ids = []
    async with storage_db.exclusive():
        for chapter in new_chapters:
            try:
                pics = await webs.get_pictures(url=chapter["url"], data=chapter)
            except Exception as e:
                logger.exception(e)
                pics = None
            if not pics:
                continue
            try:
                tasks_card = TaskCard(
                    data_list=[chapter], picturesList=pics, webs=webs, sts=None,
                    user_id=Vars.OWNER, chat_id=dump_channel_id, priority=1,
                    tasks_id=f"autopub{webs.sf}{int(time.time() * 1000)}",
                )
                doc = await send_manga_chapter(tasks_card)
            except Exception as e:
                logger.exception(e)
                doc = None
            if doc:
                file_ids.extend(m.id for m in doc)

    if not file_ids:
        logger.warning(f"Auto-publish: couldn't download any of {len(new_chapters)} new chapter(s) for {bio_list.get('title')}.")
        return

    title, genre, status, description = parse_manga_msg(bio_list)
    if not description:
        description = "No description available."
    poster = bio_list.get("poster") or random.choice(Vars.PICS)
    source = type(webs).__name__.replace("Webs", "")

    post_data = _post_data(
        title, genre, status=status, description=description,
        chapters=len(new_chapters), formats="PDF · CBZ", source=source,
    )
    draft = {
        "post_id": _gen_post_id(), "data": post_data, "template": Vars.POST_TEMPLATE,
        "poster": poster, "dump_channel": dump_channel_id, "file_ids": file_ids,
        "channels": pc.default_ids(), "created_by": Vars.OWNER,
    }
    results = await _do_publish(Bot, draft)
    ok = sum(1 for r in results if r.sent)
    logger.info(f"Auto-published {title} — {len(new_chapters)} new chapter(s) to {ok}/{len(results)} channel(s).")


async def _check_autopublish():
    for key, entry in list(list_autopublish().items()):
        webs = get_webs(entry["sf"])
        if not webs:
            continue
        try:
            bio = await webs.get_chapters({"url": entry["url"], "title": entry.get("title")}, page=1)
            chapters = webs.iter_chapters(bio, page=1)
        except Exception as e:
            logger.warning(f"Auto-publish check failed for {entry.get('title')}: {e}")
            continue
        if not chapters:
            continue

        latest_url = chapters[0].get("url")
        last_seen = entry.get("last_chapter")
        if last_seen == latest_url:
            continue  # nothing new since last check

        new_chapters = []
        for ch in chapters:  # newest-first
            if ch.get("url") == last_seen:
                break
            new_chapters.append(ch)
        if last_seen is None:
            # first time tracking this manga - just baseline on the newest
            # chapter rather than posting its entire back-catalogue at once.
            new_chapters = chapters[:1]
        new_chapters.reverse()  # oldest -> newest, matches every other flow

        set_autopublish_progress(key, latest_url)
        try:
            await _auto_publish_now(webs, bio, new_chapters)
        except Exception as e:
            logger.exception(f"Auto-publish failed for {entry.get('title')}: {e}")
        await asyncio.sleep(5)


async def autopublish_loop():
    while True:
        try:
            if Vars.AUTO_UPDATES:
                await _check_autopublish()
        except Exception as e:
            logger.exception(e)
        finally:
            minutes = max(5, int(Vars.UPDATE_INTERVAL or 10))
            await asyncio.sleep(minutes * 60)


@Bot.on_callback_query(filters.regex("^pmp_noop$"))
async def noop_cb(client, query):
    await igrone_error(query.answer)("This is just a preview of the button 🙂")


# ---------------------------------------------------------------------------
# Manage existing posts (list / delete)
# ---------------------------------------------------------------------------

@Bot.on_callback_query(filters.regex("^pmp_manage") & admin_only)
async def manage_posts_cb(client, query):
    await igrone_error(query.answer)()
    try:
        page = int(query.data.split(":")[-1])
    except Exception:
        page = 1

    posts = all_posts()
    items = sorted(posts.items(), key=lambda kv: kv[1].get("created_at", 0), reverse=True)

    if not items:
        button = InlineKeyboardMarkup([[btn("⇦ Back", callback_data="pmpanel")]])
        return await retry_on_flood(query.edit_message_text)("📭 No posts yet.", reply_markup=button)

    per_page = 8
    total_pages = max(1, (len(items) + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    page_items = items[(page - 1) * per_page: page * per_page]

    button = []
    for post_id, data in page_items:
        title = data.get("title", post_id)[:26]
        reads = data.get("reads", 0)
        button.append([
            btn(f"📖 {title} ({reads})", url=f"https://t.me/{Bot.username}?start=get_{post_id}", style=None),
            btn("🗑", callback_data=f"pmp_del_{post_id}"),
        ])

    nav = []
    if page > 1:
        nav.append(btn("⬅️", callback_data=f"pmp_manage:{page - 1}"))
    nav.append(btn(f"{page}/{total_pages}", callback_data="pmp_noop"))
    if page < total_pages:
        nav.append(btn("➡️", callback_data=f"pmp_manage:{page + 1}"))
    button.append(nav)
    button.append([btn("⇦ Back", callback_data="pmpanel")])

    await retry_on_flood(query.edit_message_text)(
        f"<b>📋 Manage Posts</b> ({len(items)} total)\n"
        "<i>Number in brackets = how many times it's been delivered.</i>",
        reply_markup=InlineKeyboardMarkup(button),
    )


@Bot.on_callback_query(filters.regex("^pmp_del_") & admin_only)
async def delete_post_cb(client, query):
    post_id = query.data.removeprefix("pmp_del_")
    post = get_post(post_id)
    if not post:
        return await retry_on_flood(query.answer)("⚠️ Already deleted.", show_alert=True)

    for chat, msg_id in pc.chats_of_post(post):      # every channel the post was published to
        await igrone_error(client.delete_messages)(_norm_channel(chat), msg_id)

    delete_post(post_id)
    await retry_on_flood(query.answer)("🗑 Post deleted.", show_alert=True)

    query.data = "pmp_manage:1"
    await manage_posts_cb(client, query)


# ---------------------------------------------------------------------------
# "📤 Post to Channel" — triggered from a manga's info card in search results.
# Auto-fetches details, lets the admin pick which chapters, downloads them
# straight into the Dump Channel, then hands off to the same preview/confirm
# step used by the manual /newpost wizard.
# ---------------------------------------------------------------------------

async def _gather_all_chapters(webs, data, max_pages=50):
    """Walk every page of the chapter list and return it newest-first
    (matching each site module's native order), capped at max_pages as a
    safety net against runaway pagination.

    Every site module works the same way as the manual pagination handler
    (TG/callback.py::pg_handler): `get_chapters()` must be awaited first to
    (re)fetch/populate the page's chapter data, and only then does the sync
    `iter_chapters()` extract that page's list from it. The previous version
    skipped `get_chapters()` entirely and fed raw search/bio data straight
    into `iter_chapters()`, which expects the fetched payload - so it always
    returned nothing (or, for sites whose first page happens to include the
    whole list, silently missed every chapter beyond it). That's why
    "📤 Post to Channel" reported "No chapters found" / posted incomplete
    results.
    """
    all_chapters = []
    seen = set()
    for page in range(1, max_pages + 1):
        try:
            data = await webs.get_chapters(data, page=page)
        except Exception as e:
            logger.exception(e)
            break

        if not data:
            break

        try:
            chapters = webs.iter_chapters(data, page=page)
        except Exception as e:
            logger.exception(e)
            break

        if not chapters:
            break

        new = [c for c in chapters if c.get("url") not in seen]
        if not new:
            break
        seen.update(c["url"] for c in new)
        all_chapters.extend(new)

        if len(chapters) < 60:  # short page = last page
            break

    return all_chapters


def _parse_range(chapters, text):
    """'5' -> chapter 5 only. '1-20' -> chapters 1..20. '-5' -> last 5."""
    text = text.strip()
    try:
        if "-" in text:
            a, b = text.split("-", 1)
            a, b = a.strip(), b.strip()
            if a == "":
                n = int(b)
                return chapters[-n:] if n > 0 else None
            start, end = int(a) - 1, int(b)
            if start < 0 or end <= start:
                return None
            return chapters[start:end]
        n = int(text)
        if n < 1 or n > len(chapters):
            return None
        return [chapters[n - 1]]
    except (ValueError, IndexError):
        return None


@Bot.on_callback_query(filters.regex("^pnew:") & admin_only)
async def post_new_from_search_cb(client, query):
    if query.data not in post_targets:
        return await retry_on_flood(query.answer)(
            "This is an old button, please redo the search", show_alert=True
        )

    if not get_dump_channel() or not pc.active_channels():
        return await retry_on_flood(query.answer)(
            "❌ Set a storage channel (Constant Dump) and add a post channel first - see /channels.",
            show_alert=True,
        )

    webs, bio_list, data = post_targets[query.data]
    admin_id = query.from_user.id

    await igrone_error(query.answer)("🔎 Fetching chapter list...")
    await retry_on_flood(query.edit_message_caption)(
        f"<b>🔎 Fetching chapters for {bio_list.get('title', 'this manga')}...</b>"
    )

    chapters = await _gather_all_chapters(webs, bio_list)
    if not chapters:
        return await retry_on_flood(query.edit_message_caption)("❌ No chapters found.")

    chapters = list(reversed(chapters))  # oldest -> newest, so "1" = chapter 1

    _auto_state[admin_id] = {
        "webs": webs,
        "bio_list": bio_list,
        "chapters": chapters,
        "chat_id": query.message.chat.id,
    }

    total = len(chapters)
    button = InlineKeyboardMarkup([
        [btn(f"📥 All ({total})", callback_data="prange_all")],
        [btn("🆕 Latest chapter only", callback_data="prange_latest")],
        [btn("🔢 Custom range", callback_data="prange_custom")],
        [btn("❌ Cancel", callback_data="prange_cancel")],
    ])
    await retry_on_flood(query.edit_message_caption)(
        f"<b>{bio_list.get('title', 'Manga')}</b>\n\n"
        f"Found <b>{total}</b> chapter(s). What should this post include?",
        reply_markup=button,
    )


@Bot.on_callback_query(filters.regex("^prange_") & admin_only)
async def post_range_cb(client, query):
    admin_id = query.from_user.id
    state = _auto_state.get(admin_id)
    if not state:
        return await retry_on_flood(query.answer)(
            "⚠️ This expired — open the manga from search again.", show_alert=True
        )

    choice = query.data.removeprefix("prange_")
    await igrone_error(query.answer)()
    chapters = state["chapters"]

    if choice == "cancel":
        del _auto_state[admin_id]
        return await igrone_error(query.message.delete)()

    elif choice == "all":
        selected = chapters

    elif choice == "latest":
        selected = chapters[-1:]

    elif choice == "custom":
        await retry_on_flood(query.edit_message_caption)(
            "<b>🔢 Send the chapter range</b>\n\n"
            "e.g. <code>1-20</code> for chapters 1 through 20, <code>-5</code> for "
            "the last 5, or a single number for one chapter.\n\nSend /cancel to abort."
        )
        try:
            reply = await client.listen(user_id=admin_id, timeout=120)
        except TimeoutError:
            del _auto_state[admin_id]
            return await retry_on_flood(query.message.edit_caption)("⏰ Timed out.")

        text = (reply.text or "").strip()
        await igrone_error(reply.delete)()

        if text.lower() in CANCEL_WORDS:
            del _auto_state[admin_id]
            return await retry_on_flood(query.message.edit_caption)("❌ Cancelled.")

        selected = _parse_range(chapters, text)
        if not selected:
            del _auto_state[admin_id]
            return await retry_on_flood(query.message.edit_caption)(
                "❌ Couldn't understand that range. Open the manga from search again to retry."
            )
    else:
        return

    del _auto_state[admin_id]
    await _run_auto_post(client, state["chat_id"], admin_id, state["webs"], state["bio_list"], selected)


async def _run_auto_post(client, chat_id, admin_id, webs, bio_list, chapters):
    dump_channel = get_dump_channel()
    try:
        dump_channel_id = int(dump_channel)
    except (TypeError, ValueError):
        return await retry_on_flood(client.send_message)(
            chat_id,
            "❌ Auto-download needs the storage channel (Constant Dump) set as a numeric ID.\n"
            "Reconfigure it via /postsettings by <b>forwarding</b> a message from "
            "that channel (rather than typing its @username)."
        )

    sts = await retry_on_flood(client.send_message)(
        chat_id, f"<b>📥 Preparing to download {len(chapters)} chapter(s)...</b>"
    )

    merge_size = uts.get(str(admin_id), {}).get("setting", {}).get("megre", None)
    try:
        merge_size = int(merge_size) if merge_size else None
    except (TypeError, ValueError):
        merge_size = None
    priority = user_priority(admin_id)

    groups = []
    if merge_size and merge_size > 1:
        for i in range(0, len(chapters), merge_size):
            groups.append(chapters[i:i + merge_size])
    else:
        groups = [[c] for c in chapters]

    file_ids = []
    done = 0
    failed = 0
    total_groups = len(groups)
    # Shared across every chapter of this job so the live progress bar (below)
    # can show "📦 Chapter i/N" - previously `sts=None` was passed per chapter,
    # so the admin saw nothing but a static line until the whole job finished.
    batch = BatchState(total_groups)

    if storage_db.busy():
        await igrone_error(sts.edit)("<b>⏳ Another post is being written to storage - yours starts right after it...</b>")
    # One exclusive, uninterrupted write for the whole post: its files must be
    # a contiguous run of message ids for a File Store batch link to cover them.
    async with storage_db.exclusive():
        for idx, group in enumerate(groups, start=1):
            label = group[0]["title"] if len(group) == 1 else f"{group[0]['title']} … {group[-1]['title']}"
            await igrone_error(sts.edit)(
                f"<b>🔎 Fetching pages {idx}/{total_groups}</b>\n<i>{label}</i>\n\n"
                f"✅ {done} done · ❌ {failed} failed"
            )

            # Fetch every chapter's pages within this merge-group and concatenate
            # them in order, so a merged file actually contains all of them
            # (rather than only the first chapter's pages).
            group_pictures = []
            ok = True
            for chapter in group:
                try:
                    pics = await webs.get_pictures(url=chapter["url"], data=chapter)
                except Exception as e:
                    logger.exception(e)
                    pics = None
                if not pics:
                    ok = False
                    break
                group_pictures.extend(pics)

            if not ok or not group_pictures:
                failed += 1
                batch.done += 1
                batch.failed += 1
                continue

            try:
                tasks_card = TaskCard(
                    data_list=group,
                    picturesList=group_pictures,
                    webs=webs,
                    sts=sts,
                    user_id=admin_id,
                    chat_id=dump_channel_id,
                    priority=priority,
                    tasks_id=f"apost{admin_id}{idx}",
                    batch=batch,
                )
                doc = await send_manga_chapter(tasks_card)
            except Exception as e:
                logger.exception(e)
                doc = None

            if doc:
                file_ids.extend(m.id for m in doc)
                done += 1
            else:
                failed += 1

    if not file_ids:
        return await retry_on_flood(sts.edit)(
            "❌ Couldn't download any chapters. Check the bot's admin permissions "
            "in the storage channel, or try again."
        )

    await igrone_error(sts.edit)(f"<b>✅ Downloaded {done}/{total_groups} — building preview...</b>")

    title, genre, status, description = parse_manga_msg(bio_list)
    if not description:
        description = "No description available."

    poster = bio_list.get("poster") or random.choice(Vars.PICS)
    file_types = [t for t in (uts.get(str(admin_id), {}).get("setting", {}).get("type") or [])
                  if t in ("PDF", "CBZ")] or ["PDF", "CBZ"]
    source = type(webs).__name__.replace("Webs", "")

    post_id = _gen_post_id()
    post_data = _post_data(
        title, genre, status=status, description=description,
        chapters=len(chapters), formats=" · ".join(file_types), source=source,
    )
    await igrone_error(sts.delete)()

    _drafts[admin_id] = {
        "post_id": post_id,
        "data": post_data,
        "template": Vars.POST_TEMPLATE,
        "poster": poster,
        "dump_channel": dump_channel_id,
        "file_ids": file_ids,
        "channels": pc.default_ids(),
    }

    await _show_draft_preview(client, chat_id, admin_id)
    if failed:
        await retry_on_flood(client.send_message)(
            chat_id, f"<i>⚠️ {failed} chapter group(s) failed to download and were skipped.</i>"
        )


# ---------------------------------------------------------------------------
# Delivery — called from /start when the deep link is ?start=get_<post_id>
# ---------------------------------------------------------------------------

async def deliver_post(client, message, post_id):
    post = get_post(post_id)
    if not post:
        return await retry_on_flood(message.reply_text)(
            "❌ This link is invalid or has expired.", quote=True
        )

    sts = await retry_on_flood(message.reply_text)("<code>📦 Fetching your file(s)...</code>", quote=True)
    dump_channel = post.get("dump_channel")
    file_ids = post.get("file_ids", [])
    if not dump_channel or not file_ids:
        return await retry_on_flood(sts.edit_text)("❌ No files are linked to this post. Please contact the admin.")

    sent = 0
    for msg_id in file_ids:
        try:
            await retry_on_flood(client.copy_message)(
                message.chat.id, _norm_channel(dump_channel), int(msg_id)
            )
            sent += 1
            await asyncio.sleep(1)
        except Exception as e:
            logger.exception(f"Failed delivering post {post_id} file {msg_id}: {e}")

    if not sent:
        return await retry_on_flood(sts.edit_text)("❌ Couldn't fetch the file(s). Please contact the admin.")

    bump_post_reads(post_id)
    await retry_on_flood(sts.edit_text)(
        f"✅ Sent {sent} file(s) for <b>{post.get('title', 'this manga')}</b>. Enjoy reading! 📖"
    )

    if Vars.LOG_CHANNEL:
        await igrone_error(client.send_message)(
            Vars.LOG_CHANNEL,
            f"📖 Post <code>{post_id}</code> (<b>{post.get('title')}</b>) delivered to "
            f"<code>{message.from_user.id}</code> [{message.from_user.mention()}]"
        )
