"""
📡 Post Channels manager — /channels

One profile per channel the bot posts to. Each profile has its own
  • 📖 Read Now button text
  • 💎 Premium link   • 💾 Back-up link   (shown as buttons under that channel's posts)
  • 🗄 File Store Bot  (API URL + key - so every channel can use a different File Store Bot)

The data lives in Tools/postchannels.py; publishing is in TG/post.py.
"""
from pyrogram import filters
from pyrogram.errors import ListenerTimeout as TimeoutError

from bot import Bot, Vars, admin_only, logger, as_channel
from Tools import filestore_api
from Tools import postchannels as pc
from Tools import ui as _ui
from Tools.config import set_value
from Tools.ui import PRIMARY, SUCCESS, DANGER, btn, esc, kb, nav, forwarded_chat_id
from .storage import retry_on_flood, igrone_error

CANCEL_WORDS = ("/cancel", "cancel")
KIND = {
  "p": ("💎", "Premium", "premium_url", "premium_label", pc.premium_label),
  "b": ("💾", "Back-up", "backup_url", "backup_label", pc.backup_label),
}


# --------------------------------------------------------------------------- #
# views
# --------------------------------------------------------------------------- #
def _badges(ch) -> str:
  fs = pc.fs_for(ch)
  return ("💎" if ch["premium_url"] else "") + ("💾" if ch["backup_url"] else "") + ("🗄" if (fs and fs.ready) else "")


def _home_view():
  lines = []
  rows = []
  for c in pc.all_channels():
    live = c["enabled"] and c.get("chat") not in (None, "")
    lines.append(f"{'🟢' if live else '⚪'} <b>{esc(c['name'])}</b> — <code>{esc(c.get('chat') or 'not set')}</code> {_badges(c)}")
    rows.append([btn(f"{'🟢' if live else '⚪'} {c['name'][:30]}", callback_data=f"chm:o:{c['id']}", style=None)])
  rows.append([btn("➕ Add channel", callback_data="chm:add", style=SUCCESS)])
  rows.append(nav(back="pmpanel", close=True))
  text = (
    "<b>📡 Post Channels</b>\n"
    "<i>A post can go to several channels at once. Every channel has its own Read Now text, "
    "💎 Premium link, 💾 Back-up link and 🗄 File Store Bot.</i>\n\n"
    + "\n".join(lines)
    + "\n\n<blockquote>🟢 posting · ⚪ off or no channel set · 💎 premium link · 💾 back-up link · 🗄 File Store Bot\n"
      "Tick which channels a post goes to on its preview, just before you publish.</blockquote>"
  )
  return text, kb(*rows)


def _val(url) -> str:
  return f"<code>{esc(url)}</code>" if url else "<i>not set</i>"


def _channel_view(ch):
  live = ch["enabled"] and ch.get("chat") not in (None, "")
  text = (
    f"<b>📡 {esc(ch['name'])}</b>  {'🟢 posting' if live else '⚪ off'}\n"
    f"<b>Channel</b>  <code>{esc(ch.get('chat') or 'not set')}</code>"
    + ("  <i>(the Post Channel from /settings)</i>" if ch["is_main"] else "") + "\n\n"
    f"<b>📖 Read button</b>  <code>{esc(pc.read_label(ch))}</code>\n"
    f"<b>💎 Premium</b>  {_val(ch['premium_url'])}\n"
    f"<b>💾 Back-up</b>  {_val(ch['backup_url'])}\n"
    f"<b>🗄 File Store</b>  {esc(pc.fs_summary(ch))}\n"
    f"<b>🎨 Colours</b>  {' · '.join(pc.STYLE_NAMES[pc.style_value(ch, k)] for k in pc.STYLE_KINDS)}"
  )
  if ch["enabled"] and ch.get("chat") in (None, ""):
    text += "\n\n⚠️ <i>Set the channel below - nothing is posted until then.</i>"
  cid = ch["id"]
  rows = [
    [btn("⚪ Switch off" if ch["enabled"] else "🟢 Switch on", callback_data=f"chm:t:{cid}", style=None),
     btn("✏️ Name", callback_data=f"chm:n:{cid}", style=None),
     btn("📢 Channel", callback_data=f"chm:c:{cid}", style=None)],
    [btn("📖 Read button text", callback_data=f"chm:r:{cid}", style=None)],
    [btn("💎 Premium link", callback_data=f"chm:l:{cid}:p", style=PRIMARY),
     btn("💾 Back-up link", callback_data=f"chm:l:{cid}:b", style=PRIMARY)],
    [btn("🗄 File Store Bot", callback_data=f"chm:f:{cid}", style=PRIMARY)],
    [btn("🎨 Button colours", callback_data=f"chm:s:{cid}", style=PRIMARY)],
  ]
  if not ch["is_main"]:
    rows.append([btn("🗑 Remove channel", callback_data=f"chm:d:{cid}", style=DANGER)])
  rows.append(nav(back="chm:home"))
  return text, kb(*rows)


def _style_view(ch):
  """🎨 Button colours: tap a button to cycle its colour. The buttons below are drawn in the colour they set."""
  lines = []
  for kind, label in pc.STYLE_KINDS.items():
    own = (ch.get(f"{kind}_style") or "").strip().lower() in pc.STYLE_VALUES
    lines.append(f"{label}  <b>{pc.STYLE_NAMES[pc.style_value(ch, kind)]}</b>  <i>{'this channel' if own else 'global'}</i>")
  note = ""
  if _ui.mode != "native":
    note = ("\n\n⚠️ <i>This bot's Telegram library can't send button colours, so buttons stay plain until it is "
            "updated (<code>pip install -U pyrofork</code>, then restart). Your choices are saved meanwhile.</i>")
  text = (f"<b>🎨 Button colours — {esc(ch['name'])}</b>\n\n" + "\n".join(lines) +
          "\n\n<i>Tap a button to cycle: Global → 🔵 Blue → 🟢 Green → 🔴 Red → ⚪ None → ✨ Auto. "
          "“Global” follows /settings → Post Design. Colours apply to posts published from now on.</i>" + note)
  cid = ch["id"]
  rows = []
  for kind, label in pc.STYLE_KINDS.items():
    own = (ch.get(f"{kind}_style") or "").strip().lower() in pc.STYLE_VALUES
    rows.append([btn(f"{label}: {pc.STYLE_NAMES[pc.style_value(ch, kind)]}{'' if own else ' (global)'}",
                     callback_data=f"chm:sc:{cid}:{kind}", style=pc.style_for(ch, kind))])
  rows.append([btn("↩️ Reset all to global", callback_data=f"chm:sr:{cid}", style=None)])
  rows.append(nav(back=f"chm:o:{cid}"))
  return text, kb(*rows)


def _link_view(ch, k):
  emoji, name, ukey, lkey, labelfn = KIND[k]
  text = (
    f"<b>{emoji} {name} link — {esc(ch['name'])}</b>\n\n"
    f"<b>Link</b>  {_val(ch[ukey])}\n"
    f"<b>Button text</b>  <code>{esc(labelfn(ch))}</code>\n\n"
    f"<i>When set, a {emoji} {name} button appears under every post in <b>this channel</b>. "
    "Leave it empty and the button isn't shown there.</i>"
  )
  cid = ch["id"]
  rows = [[btn("🔗 Set link", callback_data=f"chm:ls:{cid}:{k}", style=SUCCESS),
           btn("🏷 Button text", callback_data=f"chm:ll:{cid}:{k}", style=None)]]
  if k == "p":
    rows.append([btn("🤖 Use this bot's Premium page", callback_data=f"chm:lb:{cid}", style=None)])
  if ch[ukey]:
    rows.append([btn("🗑 Remove link", callback_data=f"chm:lx:{cid}:{k}", style=DANGER)])
  rows.append(nav(back=f"chm:o:{cid}"))
  return text, kb(*rows)


def _mask(key: str) -> str:
  return "<i>not set</i>" if not key else (f"•••• {esc(key[-4:])}" if len(key) > 4 else "•••• set")


def _fs_view(ch):
  cid = ch["id"]
  own = pc.has_own_fs(ch)
  fs = pc.fs_for(ch)
  if own:
    body = (
      f"<b>API URL</b>  {_val(ch['fs_url'])}\n"
      f"<b>API key</b>  {_mask(ch['fs_key'])}\n"
      f"<b>Bot</b>  {('@' + esc(ch['fs_username'])) if ch['fs_username'] else '<i>filled in by 🔌 Test</i>'}\n"
      f"<b>Status</b>  {'🟢 on' if ch['fs_enabled'] else '⚪ off'}"
    )
  elif ch["is_main"]:
    g = filestore_api.global_fs()
    body = (
      "Using the <b>global</b> File Store Bot from /settings → 🗄 File Store Bot "
      f"({'🟢 on' if g.ready else '⚪ off / not configured'}).\n"
      "Set an API URL and key here to give this channel a File Store Bot of its own."
    )
  else:
    body = "None — <b>this bot</b> delivers the files for this channel's Read Now button.\nAdd an API URL and key to use a File Store Bot instead."
  text = (
    f"<b>🗄 File Store Bot — {esc(ch['name'])}</b>\n\n{body}\n\n"
    "<blockquote expandable>Each channel can have its own File Store Bot. When a post is published, the bot "
    "asks <i>that channel's</i> File Store Bot for a link to the post's files and puts it on the Read Now button. "
    "Channels that share one File Store Bot share one link.\n\n"
    "<b>Setup</b>: in the File Store Bot run <code>/newapikey</code>, paste its URL and key here, then tap 🔌 Test. "
    "Add this bot's storage channel to that File Store Bot as a DB channel (or make it an admin of the storage "
    "channel so it can copy the files). If a link can't be made the post still goes out, delivered by this bot.</blockquote>"
  )
  rows = [[btn("🌐 API URL", callback_data=f"chm:fu:{cid}", style=None),
           btn("🔑 API key", callback_data=f"chm:fk:{cid}", style=None)]]
  if own:
    rows.append([btn("⚪ Switch off" if ch["fs_enabled"] else "🟢 Switch on", callback_data=f"chm:ft:{cid}", style=None),
                 btn("🤖 Bot name", callback_data=f"chm:fn:{cid}", style=None)])
  if own or (fs and fs.has_credentials):
    rows.append([btn("🔌 Test connection", callback_data=f"chm:fp:{cid}", style=PRIMARY)])
  if own:
    rows.append([btn("↩️ Use global settings" if ch["is_main"] else "🗑 Remove File Store Bot",
                     callback_data=f"chm:fx:{cid}", style=DANGER)])
  rows.append(nav(back=f"chm:o:{cid}"))
  return text, kb(*rows)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
async def _show(query, view):
  text, markup = view
  await igrone_error(query.edit_message_text)(text, reply_markup=markup)


async def _ask(client, query, prompt: str, forward: bool = False):
  """Turn the panel into a prompt, wait for the admin's reply.
  -> (text, forwarded_chat_id), or (None, None) on /cancel or timeout."""
  await igrone_error(query.edit_message_text)(prompt + "\n\n<i>Send /cancel to go back.</i>", reply_markup=None)
  try:
    reply = await client.listen(user_id=query.from_user.id, timeout=180)
  except TimeoutError:
    return None, None
  text = (reply.text or "").strip()
  fwd = forwarded_chat_id(reply) if forward else None
  await igrone_error(reply.delete)()          # also keeps API keys out of the chat
  if text.lower() in CANCEL_WORDS:
    return None, None
  return text, fwd


async def _channel_from_reply(text, fwd):
  """A forwarded message, a numeric id or a @username -> the value to store (or None)."""
  if fwd:
    return fwd
  return as_channel(text) if text else None


async def _title_of(client, chat):
  try:
    info = await client.get_chat(chat)
    return (info.title or str(chat)), None
  except Exception as e:
    return None, f"{type(e).__name__}: {e}"


def _alert(query, text):
  return retry_on_flood(query.answer)(text, show_alert=True)


# --------------------------------------------------------------------------- #
# entry points
# --------------------------------------------------------------------------- #
@Bot.on_message(filters.command(["channels", "pchannels"]) & admin_only)
async def channels_cmd(client, message):
  text, markup = _home_view()
  await retry_on_flood(message.reply_text)(text, quote=True, reply_markup=markup)


@Bot.on_callback_query(filters.regex(r"^chm:") & admin_only)
async def channels_cb(client, query):
  parts = query.data.split(":")
  act = parts[1]
  cid = parts[2] if len(parts) > 2 else None
  k = parts[3] if len(parts) > 3 else None

  if act == "home":
    await igrone_error(query.answer)()
    return await _show(query, _home_view())

  if act == "add":
    await igrone_error(query.answer)()
    text, fwd = await _ask(
      client, query,
      "<b>➕ Add a post channel</b>\n\nForward any message from the channel, or send its numeric id "
      "(<code>-100…</code>) or @username.\n<i>The bot must be an admin there.</i>", forward=True)
    chat = await _channel_from_reply(text, fwd)
    if not chat:
      return await _show(query, _home_view())
    title, err = await _title_of(client, chat)
    ch, error = pc.add_channel(chat, title or str(chat))
    if error:
      await igrone_error(client.send_message)(query.from_user.id, f"❌ {error}")
      return await _show(query, _home_view())
    if err:
      await igrone_error(client.send_message)(
        query.from_user.id,
        f"⚠️ Added, but I couldn't open that channel yet (<code>{esc(err)}</code>). "
        "Make sure the bot is an <b>admin</b> there before posting.")
    return await _show(query, _channel_view(ch))

  ch = pc.get_channel(cid) if cid else None
  if not ch:
    await _alert(query, "That channel no longer exists.")
    return await _show(query, _home_view())

  await igrone_error(query.answer)()

  # ---- the channel itself ----------------------------------------------------
  if act == "o":
    return await _show(query, _channel_view(ch))

  if act == "t":
    pc.update_channel(cid, enabled=not ch["enabled"])
    return await _show(query, _channel_view(pc.get_channel(cid)))

  if act == "n":
    text, _ = await _ask(client, query, f"<b>✏️ Name</b>\n\nCurrent: <b>{esc(ch['name'])}</b>\nSend a new name (max 40 characters).")
    if text:
      pc.update_channel(cid, name=text[:40])
    return await _show(query, _channel_view(pc.get_channel(cid)))

  if act == "c":
    text, fwd = await _ask(
      client, query,
      f"<b>📢 Channel for “{esc(ch['name'])}”</b>\n\nForward any message from it, or send its numeric id / @username.\n"
      "<i>The bot must be an admin there.</i>", forward=True)
    chat = await _channel_from_reply(text, fwd)
    if chat:
      if ch["is_main"]:
        set_value("POST_CHANNEL", chat)         # the same value /settings -> Channels edits
      else:
        pc.update_channel(cid, chat=chat)
      _, err = await _title_of(client, chat)
      if err:
        await igrone_error(client.send_message)(
          query.from_user.id,
          f"⚠️ Saved, but I couldn't open that channel yet (<code>{esc(err)}</code>). Make the bot an <b>admin</b> there.")
    return await _show(query, _channel_view(pc.get_channel(cid)))

  if act == "r":
    text, _ = await _ask(
      client, query,
      f"<b>📖 Read button text</b>\n\nCurrent: <code>{esc(pc.read_label(ch))}</code>\n"
      "Send the text for this channel's Read Now button (max 32 characters), or <code>-</code> to use the default "
      f"(<code>{esc(Vars.POST_BUTTON)}</code>).")
    if text:
      pc.update_channel(cid, read_label="" if text == "-" else text[:pc.LABEL_MAX])
    return await _show(query, _channel_view(pc.get_channel(cid)))

  if act == "d":
    if ch["is_main"]:
      return await _alert(query, "The main channel can't be removed - switch it off instead.")
    return await _show(query, (
      f"<b>🗑 Remove “{esc(ch['name'])}”?</b>\n\nIts settings (links, File Store Bot) are deleted. "
      "Posts already published there stay where they are.",
      kb([btn("🗑 Yes, remove", callback_data=f"chm:dy:{cid}", style=DANGER)],
         [btn("⇦ Keep it", callback_data=f"chm:o:{cid}", style=None)])))

  if act == "dy":
    pc.delete_channel(cid)
    return await _show(query, _home_view())

  # ---- 🎨 button colours ---------------------------------------------------------
  if act == "s":
    return await _show(query, _style_view(ch))

  if act == "sc" and k in pc.STYLE_KINDS:
    pc.update_channel(cid, **{f"{k}_style": pc.next_style(ch, k)})
    return await _show(query, _style_view(pc.get_channel(cid)))

  if act == "sr":
    pc.update_channel(cid, read_style="", premium_style="", backup_style="")
    return await _show(query, _style_view(pc.get_channel(cid)))

  # ---- 💎 / 💾 links ------------------------------------------------------------
  if act == "l" and k in KIND:
    return await _show(query, _link_view(ch, k))

  if act == "ls" and k in KIND:
    emoji, name, ukey, _lkey, _fn = KIND[k]
    text, _ = await _ask(
      client, query,
      f"<b>{emoji} {name} link — {esc(ch['name'])}</b>\n\nSend the link for the button: "
      "<code>https://…</code>, <code>t.me/…</code> or <code>@username</code>.")
    if text:
      url = pc.normalize_url(text)
      if url:
        pc.update_channel(cid, **{ukey: url})
      else:
        await igrone_error(client.send_message)(
          query.from_user.id, "❌ That isn't a usable link. Send something like <code>https://t.me/yourchannel</code>.")
    return await _show(query, _link_view(pc.get_channel(cid), k))

  if act == "ll" and k in KIND:
    emoji, name, _ukey, lkey, fn = KIND[k]
    default = pc.DEFAULT_PREMIUM_LABEL if k == "p" else pc.DEFAULT_BACKUP_LABEL
    text, _ = await _ask(
      client, query,
      f"<b>🏷 {name} button text</b>\n\nCurrent: <code>{esc(fn(ch))}</code>\n"
      f"Send the new text (max 32 characters), or <code>-</code> for the default (<code>{default}</code>).")
    if text:
      pc.update_channel(cid, **{lkey: "" if text == "-" else text[:pc.LABEL_MAX]})
    return await _show(query, _link_view(pc.get_channel(cid), k))

  if act == "lx" and k in KIND:
    pc.update_channel(cid, **{KIND[k][2]: ""})
    return await _show(query, _link_view(pc.get_channel(cid), k))

  if act == "lb":
    if not Bot.username:
      return await _alert(query, "The bot's username isn't known yet - try again in a moment.")
    pc.update_channel(cid, premium_url=f"https://t.me/{Bot.username}?start=premium")
    return await _show(query, _link_view(pc.get_channel(cid), "p"))

  # ---- 🗄 File Store ------------------------------------------------------------
  if act == "f":
    return await _show(query, _fs_view(ch))

  if act in ("fu", "fk"):
    is_url = act == "fu"
    text, _ = await _ask(
      client, query,
      f"<b>{'🌐 API URL' if is_url else '🔑 API key'} — {esc(ch['name'])}</b>\n\n"
      + ("The File Store Bot's public API address, e.g. <code>https://your-app.example.com</code>."
         if is_url else "The bearer token from that File Store Bot's <code>/newapikey</code>. (Your message is deleted right away.)"))
    if text:
      value = text.strip()
      if is_url and not value.lower().startswith(("http://", "https://")):
        await igrone_error(client.send_message)(query.from_user.id, "❌ The URL must start with https:// (or http://).")
      else:
        had = bool(ch["fs_url"] and ch["fs_key"])
        pc.update_channel(cid, **({"fs_url": value.rstrip("/")} if is_url else {"fs_key": value}))
        now = pc.get_channel(cid)
        if not had and now["fs_url"] and now["fs_key"]:
          pc.update_channel(cid, fs_enabled=True)       # both parts are in for the first time -> switch it on
    return await _show(query, _fs_view(pc.get_channel(cid)))

  if act == "fn":
    text, _ = await _ask(client, query, "<b>🤖 File Store Bot username</b>\n\nOptional, no @ - only shown to you. 🔌 Test fills it in for you.")
    if text:
      pc.update_channel(cid, fs_username=text.lstrip("@")[:64])
    return await _show(query, _fs_view(pc.get_channel(cid)))

  if act == "ft":
    pc.update_channel(cid, fs_enabled=not ch["fs_enabled"])
    return await _show(query, _fs_view(pc.get_channel(cid)))

  if act == "fx":
    pc.update_channel(cid, fs_url="", fs_key="", fs_username="", fs_enabled=False)
    return await _show(query, _fs_view(pc.get_channel(cid)))

  if act == "fp":
    fs = pc.fs_for(ch)
    back = kb(nav(back=f"chm:f:{cid}"))
    if not fs or not fs.has_credentials:
      return await _show(query, ("❌ <b>Not configured.</b>\n\nSet the API URL and API key first.", back))
    await igrone_error(query.edit_message_text)("<i>🔌 Testing…</i>")
    data = await filestore_api.ping(fs)
    if not data:
      return await _show(query, (
        "❌ <b>Connection failed.</b>\n\nCheck the URL is reachable and the key hasn't been revoked. "
        "The bot's logs have the exact error.", back))
    if pc.has_own_fs(ch) and not ch["fs_username"] and data.get("bot"):
      pc.update_channel(cid, fs_username=str(data["bot"]).lstrip("@"))
    from .post import get_dump_channel
    storage = get_dump_channel()
    return await _show(query, (
      f"✅ <b>Connected!</b>\n\n<b>Bot</b>  @{esc(data.get('bot', '?'))}\n<b>Key</b>  {esc(data.get('key_label', '?'))}\n"
      f"<b>API version</b>  {esc(data.get('version', '?'))}\n\n"
      "Links are built from this bot's storage channel"
      + (f" (<code>{esc(storage)}</code>)" if storage else " — <b>not set yet</b>")
      + f". Add it to @{esc(data.get('bot', 'that bot'))} as a DB channel, or make that bot an admin there.", back))

  logger.warning(f"channels: unhandled callback {query.data}")
