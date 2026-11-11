"""
Admin bot-wide settings panel — /settings.

Every knob in Tools/config.py's registry (channels, post design, access &
security, performance, progress bar) plus the 3 caption templates, premium
emoji slots, extra admins and force-subscribe channels is editable from here.

/settings was already advertised in the bot's command menu but had no
handler at all — this file is that handler. Every change is written to the
DB via Tools.config.set_value()/set_fsb_list()/etc, which also updates
`bot.Vars` immediately, so nothing needs a restart.
"""
import html
import time

from pyrogram import filters
from pyrogram.types import InlineKeyboardMarkup
from pyrogram.errors import ListenerTimeout as TimeoutError

from bot import Bot, Vars, admin_only, logger
from Tools.config import (
    CATEGORIES, ITEMS, BY_KEY, coerce, set_value, reset_value, is_overridden,
    extra_admins, set_extra_admins, fsb_list, set_fsb_list, emoji_map, set_emoji_map,
)
from Tools.templates import SLOTS, SLOT_ORDER, TEMPLATES, SAMPLE, render_post
from Tools import access, api
from Tools.ui import DANGER, PRIMARY, SUCCESS, btn, esc, fmt_left, forwarded_chat_id, forwarded_user_id, kb, nav, plural
from .storage import retry_on_flood, igrone_error

CANCEL_WORDS = ("/cancel", "cancel")


# --------------------------------------------------------------------------- #
# category / item rendering
# --------------------------------------------------------------------------- #
def _fmt_value(item):
  val = getattr(Vars, item.key, None)
  if item.kind == "bool":
    return "✅ On" if val else "❌ Off"
  if item.kind == "choice":
    for v, label in item.choices:
      if v == val:
        return label
    return str(val)
  if val in (None, ""):
    return "Not set"
  if getattr(item, "secret", False):
    text = str(val)
    return f"•••• {text[-4:]}" if len(text) > 4 else "•••• set"
  text = str(val)
  return text if len(text) <= 28 else text[:25] + "…"


def _root_markup():
  cats = [btn(label, callback_data=f"cfgcat:{key}", style=None) for key, (label, _d) in CATEGORIES.items()]
  rows = [cats[i:i + 2] for i in range(0, len(cats), 2)]
  rows.append([btn("🔒 Source Access", callback_data="cfglock", style=PRIMARY), btn("🔑 API Keys", callback_data="cfgapi", style=PRIMARY)])
  rows.append([btn("👮 Extra Admins", callback_data="cfgadmins", style=None), btn("🚧 Force-Sub", callback_data="cfgfsb", style=None)])
  rows.append([btn("📡 Post Channels", callback_data="chm:home", style=PRIMARY)])
  rows.append([btn("🔁 Auto-Publish", callback_data="cfgautopub", style=None), btn("🕒 Scheduled", callback_data="schedlist", style=None)])
  rows.append([btn("✖ Close", callback_data="cfgclose", style=None)])
  return InlineKeyboardMarkup(rows)


def _root_text():
  n_locked = len(access.locked())
  return (
    "<b>🛠 Bot settings</b>\n"
    "<i>Saved instantly — no restart, no env vars.</i>\n\n"
    f"<b>Premium sources</b>  {plural(n_locked, 'source') + ' locked' if n_locked else 'none locked'}\n"
    f"<b>Premium API</b>  {'🟢 on' if Vars.API_ENABLED else '⚪ off'}\n\n"
    "Pick a category:"
  )


def _cat_items(cat_key):
  return [i for i in ITEMS if i.cat == cat_key]


def _cat_markup(cat_key):
  rows = []
  for item in _cat_items(cat_key):
    # Only on/off switches are coloured (green = on); value rows stay plain so a
    # label like "Download threads" can't pick up a colour from its wording.
    style = SUCCESS if item.kind == "bool" and getattr(Vars, item.key, False) else None
    row = [btn(f"{item.label}: {_fmt_value(item)}", callback_data=f"cfgset:{item.key}", style=style)]
    if is_overridden(item.key):
      row.append(btn("↩️", callback_data=f"cfgreset:{item.key}", style=None))
    rows.append(row)
  if cat_key == "post":
    rows.append([btn(f"🎨 Caption template · {TEMPLATES[Vars.POST_TEMPLATE][0]}", callback_data="cfgtmpl", style=PRIMARY)])
    rows.append([btn("🎭 Premium emoji slots", callback_data="cfgemoji", style=None)])
  if cat_key == "premium":
    rows.append([btn("💎 Premium Manager", callback_data="pm:home", style=PRIMARY), btn("🔒 Source Access", callback_data="cfglock", style=None)])
    rows.append([btn("🔑 API Keys", callback_data="cfgapi", style=None)])
  if cat_key == "backup":
    rows.append([btn("📦 Backup now", callback_data="cfgbackupnow")])
    rows.append([btn("ℹ️ How to restore", callback_data="cfgbackup", style=None)])
  if cat_key == "channels":
    rows.append([btn("📡 Post Channels (several, each with its own links)", callback_data="chm:home", style=PRIMARY)])
  if cat_key == "filestore":
    rows.append([btn("📡 Per-channel File Store Bots", callback_data="chm:home", style=None)])
    rows.append([btn("🔌 Test connection", callback_data="cfgfstest", style=PRIMARY)])
    rows.append([btn("📖 Setup guide", callback_data="cfgfsguide", style=None)])
  rows.append(nav(back="cfgroot"))
  return InlineKeyboardMarkup(rows)


def _cat_text(cat_key):
  label, desc = CATEGORIES[cat_key]
  return f"<b>{label}</b>\n<i>{desc}</i>"


@Bot.on_callback_query(filters.regex("^adm:home$") & admin_only)
async def settings_from_start_menu_cb(client, query):
  # The /start menu's "🛠 Bot Settings (admin)" button pointed at this
  # callback_data with no handler anywhere in the codebase - another dead
  # button. It lives on a photo message, so open the panel as a fresh
  # message rather than trying to edit_message_text a photo's caption.
  await igrone_error(query.answer)()
  await retry_on_flood(client.send_message)(query.message.chat.id, _root_text(), reply_markup=_root_markup())


@Bot.on_callback_query(filters.regex("^cfgbackup$") & admin_only)
async def settings_backup_cb(client, query):
  await igrone_error(query.answer)()
  await retry_on_flood(query.edit_message_text)(
    "<b>💾 Backup & Restore</b>\n\n"
    "• <b>/backup</b> — export everything (users, posts, settings, scheduled "
    "posts, auto-publish list) as one JSON file.\n"
    "• <b>/restore</b> — reply to a backup file with this command to import it "
    "(safe merge by default - existing entries are kept; add "
    "<code>overwrite</code> to force-replace them instead).\n\n"
    "Tap below to create one now.",
    reply_markup=InlineKeyboardMarkup([
      [btn("📦 Backup Now", callback_data="cfgbackupnow")],
      [btn("⇦ Back", callback_data="cfgcat:backup")],
    ]),
  )


@Bot.on_callback_query(filters.regex("^cfgbackupnow$") & admin_only)
async def settings_backup_now_cb(client, query):
  await igrone_error(query.answer)("📦 Building backup...")
  from .backup import backup_cmd
  await backup_cmd(client, query.message)


@Bot.on_callback_query(filters.regex("^cfgfstest$") & admin_only)
async def settings_filestore_test_cb(client, query):
  await igrone_error(query.answer)("🔌 Testing...")
  from Tools import filestore_api

  back = InlineKeyboardMarkup([[btn("⇦ Back", callback_data="cfgcat:filestore")]])

  if not Vars.FILESTORE_API_URL or not Vars.FILESTORE_API_KEY:
    return await retry_on_flood(query.edit_message_text)(
      "❌ <b>Not configured.</b>\n\nSet API Base URL and API Key first "
      "(tap them above), then test again.",
      reply_markup=back,
    )

  data = await filestore_api.ping()
  if not data:
    return await retry_on_flood(query.edit_message_text)(
      "❌ <b>Connection failed.</b>\n\n"
      "Check the API Base URL is correct and reachable, and that the API "
      "Key hasn't been revoked. Check the bot's logs for the exact error.",
      reply_markup=back,
    )

  from .post import get_dump_channel
  storage = get_dump_channel()
  await retry_on_flood(query.edit_message_text)(
    f"✅ <b>Connected!</b>\n\n"
    f"<b>Bot:</b> @{data.get('bot', '?')}\n"
    f"<b>Key:</b> {data.get('key_label', '?')}\n"
    f"<b>API version:</b> {data.get('version', '?')}\n\n"
    f"Links are built from this bot's storage channel"
    + (f" (<code>{storage}</code>)" if storage else " — <b>not set yet</b>, set the Constant Dump")
    + f". Add that channel to @{data.get('bot', 'that bot')} as a DB channel "
    "so links reference the files in place (no copies).",
    reply_markup=back,
  )


@Bot.on_callback_query(filters.regex("^cfgfsguide$") & admin_only)
async def settings_filestore_guide_cb(client, query):
  await igrone_error(query.answer)()
  await retry_on_flood(query.edit_message_text)(
    "<b>📖 File Store Bot setup</b>\n\n"
    "1. In the File Store Bot, run its API-key command (e.g. <code>/newapikey</code>) "
    "to get a bearer token.\n"
    "2. Add <b>API Base URL</b> and <b>API Key</b> above.\n"
    "3. Set this bot's <b>Storage (Constant Dump)</b> channel (Channels) — every file "
    "is stored there once.\n"
    "4. In the File Store Bot, add <b>that same channel</b> as a DB channel "
    "(<code>/adddb</code>). Links then point at the files where they already are — "
    "nothing is copied or stored twice. (If you skip this, the API copies each file "
    "into its own channel instead, which works but duplicates storage; the bot must "
    "then be an admin of the storage channel.)\n"
    "5. Tap <b>🔌 Test Connection</b>, then turn on <b>Use File Store Bot</b>.\n\n"
    "Every new post (manual, Post to Channel, Auto-Publish, scheduled) then gets its "
    "\"Read Now\" link generated automatically. A post's files are written to storage "
    "as one uninterrupted run so a single batch link covers them; if a post's files "
    "ever aren't contiguous (e.g. it reuses chapters stored earlier), the link is built "
    "from an exact copy instead. If a link can't be generated at all, the post still "
    "publishes and just uses this bot's own delivery.",
    reply_markup=InlineKeyboardMarkup([[btn("⇦ Back", callback_data="cfgcat:filestore")]]),
  )


@Bot.on_callback_query(filters.regex("^cfgautopub$") & admin_only)
async def settings_autopub_cb(client, query):
  await igrone_error(query.answer)()
  from .post import _autopublish_list_view
  text, markup = _autopublish_list_view()
  rows = markup.inline_keyboard if markup else []
  rows = list(rows) + [[btn("⇦ Back", callback_data="cfgroot")]]
  await retry_on_flood(query.edit_message_text)(text, reply_markup=InlineKeyboardMarkup(rows))


# --------------------------------------------------------------------------- #
# 🔒 source access - which sources are Premium-only
# --------------------------------------------------------------------------- #
def _lock_view():
  srcs = access.sources()
  locked = access.locked()
  n = sum(1 for sf, _ in srcs if sf in locked)
  text = ("<b>🔒 Source access</b>\n"
          "<i>Locked sources are for Premium members and admins. Unlocked sources stay free for everyone.</i>\n\n"
          f"<b>Locked</b>  {n} of {len(srcs)}\n"
          "<b>Unlocked</b>  " + str(len(srcs) - n) + "\n\n"
          "Tap a source to switch it.  🔴 Premium  ·  🟢 Free")
  rows = []
  for sf, name in srcs:
    is_locked = sf in locked
    rows.append([btn(f"{'🔒' if is_locked else '🔓'} {name}  ·  {'Premium only' if is_locked else 'Free'}",
                     callback_data=f"cfglk:{sf}", style=DANGER if is_locked else SUCCESS)])
  rows.append([btn("🔒 Lock all", callback_data="cfglkall", style=DANGER), btn("🔓 Unlock all", callback_data="cfgunlkall", style=SUCCESS)])
  rows.append([btn("💎 Premium Manager", callback_data="pm:home", style=None)])
  rows.append(nav(back="cfgroot"))
  return text, InlineKeyboardMarkup(rows)


@Bot.on_callback_query(filters.regex("^cfglock$") & admin_only)
async def settings_lock_cb(client, query):
  await igrone_error(query.answer)()
  text, markup = _lock_view()
  await retry_on_flood(query.edit_message_text)(text, reply_markup=markup)


@Bot.on_callback_query(filters.regex(r"^cfglk:(.+)$") & admin_only)
async def settings_lock_toggle_cb(client, query):
  sf = query.data.split(":", 1)[1]
  names = dict(access.sources())
  if sf not in names:
    return await retry_on_flood(query.answer)("That source no longer exists.", show_alert=True)
  now_locked = sf not in access.locked()
  access.set_lock(sf, now_locked)
  await igrone_error(query.answer)(
    f"🔒 {names[sf]} is now Premium-only" if now_locked else f"🔓 {names[sf]} is now free for everyone")
  text, markup = _lock_view()
  await retry_on_flood(query.edit_message_text)(text, reply_markup=markup)


@Bot.on_callback_query(filters.regex("^(cfglkall|cfgunlkall)$") & admin_only)
async def settings_lock_all_cb(client, query):
  lock = query.data == "cfglkall"
  (access.lock_all if lock else access.unlock_all)()
  await igrone_error(query.answer)("🔒 Every source is now Premium-only" if lock else "🔓 Every source is free again", show_alert=True)
  text, markup = _lock_view()
  await retry_on_flood(query.edit_message_text)(text, reply_markup=markup)


# --------------------------------------------------------------------------- #
# 🔑 API keys - the REST API (docs/PREMIUM_API.md). Scopes: premium, users, links.
# --------------------------------------------------------------------------- #
_pending_key = {}      # admin id -> {"label": str, "scopes": set} while the new-key wizard is open

_SCOPE_INFO = {
  "premium": ("💎 premium", "add / remove / check / list Premium"),
  "users": ("👥 users", "look up, ban and unban users, and read stats"),
  "links": ("🔗 links", "reserved — this bot has no link endpoints"),
}


def _scopes_line(scopes) -> str:
  return ", ".join(scopes) if scopes else "—"


def _api_view():
  keys = api.list_keys()
  now = time.time()
  lines, rows = [], []
  for kid, rec in keys:
    used = f"used {fmt_left(now - rec['last_used'])} ago" if rec.get("last_used") else "never used"
    lines.append(f"• <b>{esc(rec.get('label'))}</b> — {_scopes_line(api.key_scopes(rec))} · {used}")
    rows.append([btn(f"🔧 {rec.get('label')}", callback_data=f"cfgapikey:{kid}", style=None)])
  on = bool(Vars.API_ENABLED)
  text = ("<b>🔑 API keys</b>\n<i>Let another bot or service manage Premium and users over HTTP.</i>\n\n"
          f"<b>API</b>  {'🟢 on' if on else '⚪ off'}\n"
          f"<b>Address</b>  <code>/api/v1/…</code> on this bot's web port (<code>{Vars.PORT}</code>)\n\n"
          + ("<b>Keys</b>\n" + "\n".join(lines) if lines else "<i>No keys yet — create one per bot or service.</i>"))
  if not on:
    text += "\n\n<i>The API answers 503 until you turn it on.</i>"
  head = [[btn("➕ New key", callback_data="cfgapinew"), btn("📖 Guide", callback_data="cfgapidoc", style=None)],
          [btn("🔴 Turn API off" if on else "🟢 Turn API on", callback_data="cfgapitog", style=DANGER if on else SUCCESS)]]
  return text, InlineKeyboardMarkup(head + rows + [nav(back="cfgroot")])


@Bot.on_callback_query(filters.regex("^cfgapi$") & admin_only)
async def settings_api_cb(client, query):
  await igrone_error(query.answer)()
  text, markup = _api_view()
  await retry_on_flood(query.edit_message_text)(text, reply_markup=markup)


@Bot.on_callback_query(filters.regex("^cfgapitog$") & admin_only)
async def settings_api_toggle_cb(client, query):
  set_value("API_ENABLED", not Vars.API_ENABLED)       # its hook opens the web port if it wasn't open
  await igrone_error(query.answer)("🟢 API is on" if Vars.API_ENABLED else "⚪ API is off")
  text, markup = _api_view()
  await retry_on_flood(query.edit_message_text)(text, reply_markup=markup)


# ---- new key: label -> pick scopes -> create ---------------------------------------------------
def _scope_picker(label, chosen):
  rows = [[btn(f"{'✅' if sc in chosen else '⬜'} {name}  —  {desc}", callback_data=f"cfgapisc:{sc}", style=SUCCESS if sc in chosen else None)]
          for sc, (name, desc) in _SCOPE_INFO.items()]
  rows.append([btn("🔑 Create key", callback_data="cfgapimk", style=PRIMARY)] if chosen else [])
  rows.append(nav(back="cfgapi"))
  text = (f"<b>🔑 What can “{esc(label)}” do?</b>\n\n"
          "Tick the scopes it needs — <i>give each service the least it needs.</i>\n"
          "<i>Scopes can be changed later without making a new key.</i>")
  return text, InlineKeyboardMarkup([r for r in rows if r])


@Bot.on_callback_query(filters.regex("^cfgapinew$") & admin_only)
async def settings_api_new_cb(client, query):
  await igrone_error(query.answer)()
  await retry_on_flood(query.edit_message_text)(
    "<b>➕ New API key</b>\n\nSend a short name for it — who or what will use it "
    "(e.g. <code>payments-bot</code>).\n\n<i>/cancel to go back.</i>",
    reply_markup=kb(nav(back="cfgapi")))
  try:
    reply = await client.listen(user_id=query.from_user.id, timeout=120)
  except TimeoutError:
    text, markup = _api_view()
    return await igrone_error(query.message.edit_text)(text, reply_markup=markup)
  label = (reply.text or "").strip()
  await igrone_error(reply.delete)()
  if not label or label.lower() in CANCEL_WORDS:
    text, markup = _api_view()
    return await retry_on_flood(query.message.edit_text)(text, reply_markup=markup)
  _pending_key[query.from_user.id] = {"label": label[:40], "scopes": {"premium"}}
  text, markup = _scope_picker(label[:40], {"premium"})
  await retry_on_flood(query.message.edit_text)(text, reply_markup=markup)


@Bot.on_callback_query(filters.regex("^cfgapisc:(links|premium|users)$") & admin_only)
async def settings_api_scope_cb(client, query):
  pending = _pending_key.get(query.from_user.id)
  if not pending:
    return await retry_on_flood(query.answer)("That took too long — start again.", show_alert=True)
  sc = query.data.split(":")[1]
  pending["scopes"] ^= {sc}
  await igrone_error(query.answer)()
  text, markup = _scope_picker(pending["label"], pending["scopes"])
  await retry_on_flood(query.edit_message_text)(text, reply_markup=markup)


@Bot.on_callback_query(filters.regex("^cfgapimk$") & admin_only)
async def settings_api_create_cb(client, query):
  pending = _pending_key.pop(query.from_user.id, None)
  if not pending or not pending["scopes"]:
    return await retry_on_flood(query.answer)("That took too long — start again.", show_alert=True)
  key, kid = api.create_key(pending["label"], pending["scopes"], query.from_user.id)
  await igrone_error(query.answer)("🔑 Key created")
  await retry_on_flood(query.edit_message_text)(
    f"<b>🔑 Key created — {esc(pending['label'])}</b>\n\n"
    f"<code>{key}</code>\n\n"
    f"<b>Key ID</b>  <code>{kid}</code>\n<b>Scopes</b>  {_scopes_line(api.parse_scopes(pending['scopes']))}\n\n"
    "⚠️ <b>This is the only time it's shown.</b> Copy it now and keep it somewhere safe "
    "(an environment variable or secrets manager) — never in code or a chat. "
    "If it ever leaks, revoke it here.\n\n"
    "<b>Try it</b>\n"
    f"<code>curl -H \"Authorization: Bearer {key[:8]}…\" https://YOUR-DOMAIN/api/v1/ping</code>\n\n"
    + ("" if Vars.API_ENABLED else "<i>The API is currently off — turn it on from the keys screen.</i>"),
    reply_markup=kb([btn("✅ I've saved it — hide this", callback_data="cfgapi")]))


# ---- one key: scopes + revoke --------------------------------------------------------------------
def _key_view(kid):
  found = api.find_key(kid)
  if not found:
    return None
  _h, rec = found
  scopes = api.key_scopes(rec)
  used = f"{fmt_left(time.time() - rec['last_used'])} ago" if rec.get("last_used") else "never"
  text = (f"<b>🔧 {esc(rec.get('label'))}</b>\n\n"
          f"<b>Key ID</b>  <code>{kid}</code>\n<b>Scopes</b>  {_scopes_line(scopes)}\n"
          f"<b>Last used</b>  {used}\n<b>Requests</b>  {rec.get('calls', 0)}\n\n"
          "<i>Tap a scope to switch it — takes effect immediately.</i>")
  rows = [[btn(f"{'✅' if sc in scopes else '⬜'} {name}", callback_data=f"cfgapits:{kid}:{sc}", style=SUCCESS if sc in scopes else None)]
          for sc, (name, _d) in _SCOPE_INFO.items()]
  rows.append([btn("🗑 Revoke this key", callback_data=f"cfgapidel:{kid}")])
  rows.append(nav(back="cfgapi"))
  return text, InlineKeyboardMarkup(rows)


@Bot.on_callback_query(filters.regex(r"^cfgapikey:([0-9a-f]{8,})$") & admin_only)
async def settings_api_key_cb(client, query):
  view = _key_view(query.data.split(":")[1])
  if not view:
    return await retry_on_flood(query.answer)("That key is already gone.", show_alert=True)
  await igrone_error(query.answer)()
  await retry_on_flood(query.edit_message_text)(view[0], reply_markup=view[1])


@Bot.on_callback_query(filters.regex(r"^cfgapits:([0-9a-f]{8,}):(links|premium|users)$") & admin_only)
async def settings_api_key_scope_cb(client, query):
  _, kid, sc = query.data.split(":")
  found = api.find_key(kid)
  if not found:
    return await retry_on_flood(query.answer)("That key is already gone.", show_alert=True)
  scopes = set(api.key_scopes(found[1])) ^ {sc}
  if not scopes:
    return await retry_on_flood(query.answer)("A key needs at least one scope — revoke it instead.", show_alert=True)
  api.set_scopes(kid, scopes)
  await igrone_error(query.answer)()
  view = _key_view(kid)
  await retry_on_flood(query.edit_message_text)(view[0], reply_markup=view[1])


@Bot.on_callback_query(filters.regex(r"^cfgapidel:([0-9a-f]{8,})$") & admin_only)
async def settings_api_del_cb(client, query):
  kid = query.data.split(":")[1]
  found = api.find_key(kid)
  if not found:
    return await retry_on_flood(query.answer)("That key is already gone.", show_alert=True)
  await igrone_error(query.answer)()
  await retry_on_flood(query.edit_message_text)(
    f"<b>🗑 Revoke “{esc(found[1].get('label'))}”?</b>\n\nAnything using this key stops working immediately.",
    reply_markup=kb([btn("🗑 Yes, revoke", callback_data=f"cfgapidely:{kid}", style=DANGER)], [btn("⇦ Keep it", callback_data=f"cfgapikey:{kid}", style=None)]))


@Bot.on_callback_query(filters.regex(r"^cfgapidely:([0-9a-f]{8,})$") & admin_only)
async def settings_api_del_yes_cb(client, query):
  ok = api.revoke_key(query.data.split(":")[1])
  await igrone_error(query.answer)("🗑 Key revoked" if ok else "Already revoked")
  text, markup = _api_view()
  await retry_on_flood(query.edit_message_text)(text, reply_markup=markup)


@Bot.on_callback_query(filters.regex("^cfgapidoc$") & admin_only)
async def settings_api_doc_cb(client, query):
  await igrone_error(query.answer)()
  await retry_on_flood(query.edit_message_text)(
    "<b>📖 API — quick guide</b>\n\n"
    "Send <code>Authorization: Bearer &lt;key&gt;</code> (or <code>X-API-Key: &lt;key&gt;</code>) to your bot's public URL:\n\n"
    "<b>premium</b> scope\n"
    "<code>POST /api/v1/premium/add</code>  <i>{\"user_id\":123,\"days\":30}</i>\n"
    "<code>POST /api/v1/premium/remove</code>\n"
    "<code>GET  /api/v1/premium/{user_id}</code>\n"
    "<code>GET  /api/v1/premium?limit=50&amp;offset=0</code>\n\n"
    "<b>users</b> scope\n"
    "<code>GET  /api/v1/users/{user_id}</code>\n"
    "<code>POST /api/v1/users/ban</code> · <code>/unban</code>\n"
    "<code>GET  /api/v1/stats</code>\n\n"
    "<b>any key</b>: <code>GET /api/v1/ping</code> · <code>GET /api/v1/usage</code>\n\n"
    "Leave out <code>days</code> for a <b>permanent</b> plan. Add <code>request_id</code> and a retried request can never apply twice.\n\n"
    "<i>Full reference: <b>docs/PREMIUM_API.md</b> in the bot's files.</i>",
    reply_markup=kb(nav(back="cfgapi")))


@Bot.on_message(filters.command("settings") & admin_only)
async def settings_cmd(client, message):
  await retry_on_flood(message.reply_text)(_root_text(), quote=True, reply_markup=_root_markup())


@Bot.on_callback_query(filters.regex("^cfgroot$") & admin_only)
async def settings_root_cb(client, query):
  await igrone_error(query.answer)()
  await retry_on_flood(query.edit_message_text)(_root_text(), reply_markup=_root_markup())


@Bot.on_callback_query(filters.regex("^cfgclose$") & admin_only)
async def settings_close_cb(client, query):
  await igrone_error(query.answer)()
  await igrone_error(query.message.delete)()


@Bot.on_callback_query(filters.regex("^cfgcat:") & admin_only)
async def settings_cat_cb(client, query):
  cat_key = query.data.split(":", 1)[1]
  if cat_key not in CATEGORIES:
    return await retry_on_flood(query.answer)("Unknown category.", show_alert=True)
  await igrone_error(query.answer)()
  await retry_on_flood(query.edit_message_text)(_cat_text(cat_key), reply_markup=_cat_markup(cat_key))


@Bot.on_callback_query(filters.regex("^cfgreset:") & admin_only)
async def settings_reset_cb(client, query):
  key = query.data.split(":", 1)[1]
  item = BY_KEY.get(key)
  if not item:
    return await retry_on_flood(query.answer)("Unknown setting.", show_alert=True)
  reset_value(key)
  await igrone_error(query.answer)(f"↩️ {item.label} reset to default.")
  await retry_on_flood(query.edit_message_text)(_cat_text(item.cat), reply_markup=_cat_markup(item.cat))


@Bot.on_callback_query(filters.regex("^cfgset:") & admin_only)
async def settings_item_cb(client, query):
  key = query.data.split(":", 1)[1]
  item = BY_KEY.get(key)
  if not item:
    return await retry_on_flood(query.answer)("Unknown setting.", show_alert=True)

  if item.kind == "bool":
    set_value(key, not bool(getattr(Vars, key)))
    await igrone_error(query.answer)(f"{item.label}: {_fmt_value(item)}")
    return await retry_on_flood(query.edit_message_text)(_cat_text(item.cat), reply_markup=_cat_markup(item.cat))

  if item.kind == "choice":
    rows = [[btn(f"{'✅ ' if v == getattr(Vars, key) else ''}{label}", callback_data=f"cfgchoice:{key}:{v}",
                 style=SUCCESS if v == getattr(Vars, key) else None)]
            for v, label in item.choices]
    rows.append([btn("⇦ Back", callback_data=f"cfgcat:{item.cat}")])
    await igrone_error(query.answer)()
    return await retry_on_flood(query.edit_message_text)(
      f"<b>{item.label}</b>\n<i>{item.help}</i>", reply_markup=InlineKeyboardMarkup(rows)
    )

  hint = ""
  if item.kind == "int":
    hint = f" — a whole number, {item.lo}–{item.hi}"
  elif item.kind == "channel":
    hint = " — forward a message from the channel, or send its @username / numeric id"
  elif item.hi:
    hint = f" — max {item.hi} characters, send <code>-</code> to clear"

  await igrone_error(query.answer)()
  await retry_on_flood(query.edit_message_text)(
    f"<b>📐 {item.label}</b>{hint}\n\n<i>{item.help}</i>\n\n"
    f"Current: <code>{html.escape(_fmt_value(item))}</code>\n\n"
    "Send the new value, or /cancel."
  )
  try:
    reply = await client.listen(user_id=query.from_user.id, timeout=120)
  except TimeoutError:
    return await igrone_error(query.message.edit_text)(_cat_text(item.cat), reply_markup=_cat_markup(item.cat))

  raw = reply.text.strip() if reply.text else None
  if item.kind == "channel" and forwarded_chat_id(reply):
    raw = forwarded_chat_id(reply)
  await igrone_error(reply.delete)()

  if raw is None or (isinstance(raw, str) and raw.lower() in CANCEL_WORDS):
    return await retry_on_flood(query.message.edit_text)(_cat_text(item.cat), reply_markup=_cat_markup(item.cat))

  value, error = coerce(item, raw)
  if error:
    await igrone_error(client.send_message)(query.from_user.id, f"❌ {error}")
    return await retry_on_flood(query.message.edit_text)(_cat_text(item.cat), reply_markup=_cat_markup(item.cat))

  set_value(key, value)
  await retry_on_flood(query.message.edit_text)(_cat_text(item.cat), reply_markup=_cat_markup(item.cat))


@Bot.on_callback_query(filters.regex("^cfgchoice:") & admin_only)
async def settings_choice_cb(client, query):
  _, key, value = query.data.split(":", 2)
  item = BY_KEY.get(key)
  if not item:
    return await retry_on_flood(query.answer)("Unknown setting.", show_alert=True)
  set_value(key, value)
  await igrone_error(query.answer)(f"✅ {item.label} set.")
  await retry_on_flood(query.edit_message_text)(_cat_text(item.cat), reply_markup=_cat_markup(item.cat))


# --------------------------------------------------------------------------- #
# 🎨 caption templates — live preview against a sample manga, tap to apply
# --------------------------------------------------------------------------- #
def _tmpl_markup():
  rows = [
    [btn(f"{'✅ ' if n == Vars.POST_TEMPLATE else ''}{n}. {name}", callback_data=f"cfgtmplset:{n}")]
    for n, (name, _fn) in TEMPLATES.items()
  ]
  rows.append([btn("🎭 Premium Emoji Slots", callback_data="cfgemoji")])
  rows.append([btn("⇦ Back", callback_data="cfgcat:post")])
  return InlineKeyboardMarkup(rows)


@Bot.on_callback_query(filters.regex("^cfgtmpl$") & admin_only)
async def settings_tmpl_cb(client, query):
  await igrone_error(query.answer)()
  preview = render_post(SAMPLE, template=Vars.POST_TEMPLATE)
  text = f"<b>🎨 Caption Templates</b>  (currently #{Vars.POST_TEMPLATE})\n\n{preview}"
  await retry_on_flood(query.edit_message_text)(text, reply_markup=_tmpl_markup())


@Bot.on_callback_query(filters.regex("^cfgtmplset:") & admin_only)
async def settings_tmpl_set_cb(client, query):
  n = int(query.data.split(":", 1)[1])
  if n not in TEMPLATES:
    return await retry_on_flood(query.answer)("Unknown template.", show_alert=True)
  set_value("POST_TEMPLATE", n)
  await igrone_error(query.answer)(f"🎨 {TEMPLATES[n][0]} set as the default template.")
  preview = render_post(SAMPLE, template=n)
  text = f"<b>🎨 Caption Templates</b>  (currently #{n})\n\n{preview}"
  await retry_on_flood(query.edit_message_text)(text, reply_markup=_tmpl_markup())


# --------------------------------------------------------------------------- #
# 🎭 premium emoji slots — map each slot to a custom-emoji id
# --------------------------------------------------------------------------- #
def _emoji_markup():
  m = emoji_map()
  rows = []
  for slot in SLOT_ORDER:
    label, fallback = SLOTS[slot]
    mark = "🎭" if slot in m else fallback
    rows.append([btn(f"{mark} {label}", callback_data=f"cfgemojiset:{slot}", style=None)])
    if slot in m:
      rows[-1].append(btn("↩️", callback_data=f"cfgemojidel:{slot}"))
  rows.append([btn("⇦ Back", callback_data="cfgtmpl")])
  return InlineKeyboardMarkup(rows)


@Bot.on_callback_query(filters.regex("^cfgemoji$") & admin_only)
async def settings_emoji_cb(client, query):
  await igrone_error(query.answer)()
  text = (
    "<b>🎭 Premium Emoji Slots</b>\n\n"
    "Tap a slot, then send a message containing the custom (Telegram Premium) "
    "emoji you want for it. Unmapped slots just use the plain emoji shown — "
    "posts always work even without Premium.\n\n"
    f"Premium emoji are currently <b>{'ON' if Vars.POST_PREMIUM_EMOJI else 'OFF'}</b> "
    "(toggle in 🎨 Post Design)."
  )
  await retry_on_flood(query.edit_message_text)(text, reply_markup=_emoji_markup())


@Bot.on_callback_query(filters.regex("^cfgemojidel:") & admin_only)
async def settings_emoji_del_cb(client, query):
  slot = query.data.split(":", 1)[1]
  m = emoji_map()
  m.pop(slot, None)
  set_emoji_map(m)
  await igrone_error(query.answer)(f"↩️ {SLOTS[slot][0]} reset to the plain emoji.")
  await retry_on_flood(query.edit_message_text)(query.message.text, reply_markup=_emoji_markup())


@Bot.on_callback_query(filters.regex("^cfgemojiset:") & admin_only)
async def settings_emoji_set_cb(client, query):
  slot = query.data.split(":", 1)[1]
  if slot not in SLOTS:
    return await retry_on_flood(query.answer)("Unknown slot.", show_alert=True)

  await igrone_error(query.answer)()
  await retry_on_flood(query.edit_message_text)(
    f"<b>🎭 {SLOTS[slot][0]} emoji</b>\n\n"
    "Send a message containing the custom (premium) emoji for this slot "
    "(just tap it from your emoji panel), or /cancel."
  )
  try:
    reply = await client.listen(user_id=query.from_user.id, timeout=120)
  except TimeoutError:
    return await igrone_error(query.message.edit_text)(
      f"🎭 {SLOTS[slot][0]} — timed out.", reply_markup=_emoji_markup()
    )

  text = reply.text or reply.caption
  if text and text.strip().lower() in CANCEL_WORDS:
    await igrone_error(reply.delete)()
    return await retry_on_flood(query.message.edit_text)(
      f"🎭 {SLOTS[slot][0]} — cancelled.", reply_markup=_emoji_markup()
    )

  emoji_id = None
  for entity in (reply.entities or reply.caption_entities or []):
    if "CUSTOM_EMOJI" in str(entity.type) and getattr(entity, "custom_emoji_id", None):
      emoji_id = entity.custom_emoji_id
      break
  await igrone_error(reply.delete)()

  if not emoji_id:
    await igrone_error(client.send_message)(
      query.from_user.id,
      "❌ That message didn't contain a custom (premium) emoji. "
      "Regular unicode emoji can't be mapped — try again from /settings."
    )
    return await retry_on_flood(query.message.edit_text)(
      f"🎭 {SLOTS[slot][0]} — no custom emoji found.", reply_markup=_emoji_markup()
    )

  m = emoji_map()
  m[slot] = str(emoji_id)
  set_emoji_map(m)
  await retry_on_flood(query.message.edit_text)(
    f"✅ {SLOTS[slot][0]} emoji saved.", reply_markup=_emoji_markup()
  )


# --------------------------------------------------------------------------- #
# 👮 extra admins
# --------------------------------------------------------------------------- #
def _admins_markup():
  rows = [[btn(f"❌ {uid}", callback_data=f"cfgadmindel:{uid}")] for uid in extra_admins()]
  rows.append([btn("➕ Add admin", callback_data="cfgadminadd")])
  rows.append([btn("⇦ Back", callback_data="cfgroot")])
  return InlineKeyboardMarkup(rows)


def _admins_text():
  ids = extra_admins()
  body = "\n".join(f"• <code>{i}</code>" for i in ids) if ids else "<i>None added from the bot yet.</i>"
  return (
    "<b>👮 Extra Admins</b>\n\n"
    "In addition to the <code>ADMINS</code> env var and the owner. "
    f"Tap ❌ to remove one.\n\n{body}"
  )


@Bot.on_callback_query(filters.regex("^cfgadmins$") & admin_only)
async def settings_admins_cb(client, query):
  await igrone_error(query.answer)()
  await retry_on_flood(query.edit_message_text)(_admins_text(), reply_markup=_admins_markup())


@Bot.on_callback_query(filters.regex("^cfgadmindel:") & admin_only)
async def settings_admin_del_cb(client, query):
  uid = int(query.data.split(":", 1)[1])
  set_extra_admins([i for i in extra_admins() if i != uid])
  await igrone_error(query.answer)(f"❌ Removed {uid}.")
  await retry_on_flood(query.edit_message_text)(_admins_text(), reply_markup=_admins_markup())


@Bot.on_callback_query(filters.regex("^cfgadminadd$") & admin_only)
async def settings_admin_add_cb(client, query):
  await igrone_error(query.answer)()
  await retry_on_flood(query.edit_message_text)(
    "<b>➕ Add admin</b>\n\nSend the user's numeric Telegram ID, or forward a message from them, or /cancel."
  )
  try:
    reply = await client.listen(user_id=query.from_user.id, timeout=120)
  except TimeoutError:
    return await igrone_error(query.message.edit_text)(_admins_text(), reply_markup=_admins_markup())

  uid = None
  if forwarded_user_id(reply):
    uid = forwarded_user_id(reply)
  elif reply.text and reply.text.strip().lstrip("-").isdigit():
    uid = int(reply.text.strip())
  await igrone_error(reply.delete)()

  if not uid:
    await igrone_error(client.send_message)(query.from_user.id, "❌ Send a numeric user id, or forward a message from them.")
    return await retry_on_flood(query.message.edit_text)(_admins_text(), reply_markup=_admins_markup())

  ids = extra_admins()
  if uid not in ids:
    ids.append(uid)
    set_extra_admins(ids)
    await client.set_admin_menu(uid)
  await retry_on_flood(query.message.edit_text)(_admins_text(), reply_markup=_admins_markup())


# --------------------------------------------------------------------------- #
# 🚧 force-subscribe channels
# --------------------------------------------------------------------------- #
def _fsb_markup():
  rows = [[btn(f"❌ {text} → {chat}", callback_data=f"cfgfsbdel:{idx}")]
          for idx, (text, chat) in enumerate(fsb_list())]
  rows.append([btn("➕ Add channel", callback_data="cfgfsbadd")])
  rows.append([btn("⇦ Back", callback_data="cfgroot")])
  return InlineKeyboardMarkup(rows)


def _fsb_text():
  items = fsb_list()
  body = "\n".join(f"• <b>{html.escape(t)}</b> → <code>{c}</code>" for t, c in items) if items else "<i>None set — force-sub is off.</i>"
  return "<b>🚧 Force-Subscribe Channels</b>\n\nUsers must join all of these before using the bot.\n\n" + body


@Bot.on_callback_query(filters.regex("^cfgfsb$") & admin_only)
async def settings_fsb_cb(client, query):
  await igrone_error(query.answer)()
  await retry_on_flood(query.edit_message_text)(_fsb_text(), reply_markup=_fsb_markup())


@Bot.on_callback_query(filters.regex("^cfgfsbdel:") & admin_only)
async def settings_fsb_del_cb(client, query):
  idx = int(query.data.split(":", 1)[1])
  items = fsb_list()
  if 0 <= idx < len(items):
    items.pop(idx)
    set_fsb_list(items)
  await igrone_error(query.answer)("❌ Removed.")
  await retry_on_flood(query.edit_message_text)(_fsb_text(), reply_markup=_fsb_markup())


@Bot.on_callback_query(filters.regex("^cfgfsbadd$") & admin_only)
async def settings_fsb_add_cb(client, query):
  await igrone_error(query.answer)()
  await retry_on_flood(query.edit_message_text)(
    "<b>➕ Add force-sub channel</b>\n\n"
    "Send the button text and the channel, separated by a colon, e.g.\n"
    "<code>Join Updates: @mychannel</code>\nor forward a message from the channel, or /cancel."
  )
  try:
    reply = await client.listen(user_id=query.from_user.id, timeout=120)
  except TimeoutError:
    return await igrone_error(query.message.edit_text)(_fsb_text(), reply_markup=_fsb_markup())

  text_val, chat_val = None, None
  if forwarded_chat_id(reply):
    chat_val = forwarded_chat_id(reply)
    text_val = getattr(getattr(reply, "forward_origin", None), "chat", None) and reply.forward_origin.chat.title or "Join Channel"
  elif reply.text and ":" in reply.text:
    left, right = reply.text.rsplit(":", 1)
    text_val, chat_val = left.strip(), right.strip().lstrip("@")
    if chat_val.lstrip("-").isdigit():
      chat_val = int(chat_val)
  await igrone_error(reply.delete)()

  if not text_val or not chat_val or (reply.text and reply.text.strip().lower() in CANCEL_WORDS):
    if not (reply.text and reply.text.strip().lower() in CANCEL_WORDS):
      await igrone_error(client.send_message)(
        query.from_user.id, "❌ Send it as <code>Text: @channel</code>, or forward a message from the channel."
      )
    return await retry_on_flood(query.message.edit_text)(_fsb_text(), reply_markup=_fsb_markup())

  items = fsb_list()
  items.append([text_val, chat_val])
  set_fsb_list(items)
  await retry_on_flood(query.message.edit_text)(_fsb_text(), reply_markup=_fsb_markup())
