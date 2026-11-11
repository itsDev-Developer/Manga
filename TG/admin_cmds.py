"""
Admin commands for bans and API keys.

    /ban <id>  /unban <id>
    /newapikey <label> [scopes]   /apikeys   /revokeapikey <id>   /apikeyscopes <id> <scopes>

Scopes: links, premium, users - or `all`. A key made without scopes gets `links`
only (the API reference's "least privilege by default"); this bot has no link
endpoints, so for the premium API a key needs `premium` and/or `users`.
"""
import time

from pyrogram import filters

from bot import Bot, Vars, admin_only, is_admin
from Tools import api, db
from Tools.ui import btn, esc, fmt_left, kb, nav
from .storage import igrone_error, retry_on_flood


def _scopes_text(scopes):
  return ", ".join(scopes) if scopes else "—"


# --------------------------------------------------------------------------- #
# bans
# --------------------------------------------------------------------------- #
def _target(message):
  args = message.command[1:]
  return int(args[0]) if args and args[0].lstrip("-").isdigit() else None


@Bot.on_message(filters.command("ban") & admin_only)
async def ban_cmd(client, message):
  uid = _target(message)
  if not uid or uid <= 0:
    return await message.reply_text("<b>Usage</b>\n<code>/ban &lt;user_id&gt;</code>", quote=True)
  if is_admin(uid) or uid == Vars.OWNER:
    return await message.reply_text("⚠️ <b>Admins and the owner can't be banned.</b>", quote=True)
  changed = db.ban_user(uid, by=f"admin:{message.from_user.id}")
  await message.reply_text(f"🚫 <b>User <code>{uid}</code> is banned.</b>" if changed else f"ℹ️ <b><code>{uid}</code> was already banned.</b>", quote=True)


@Bot.on_message(filters.command("unban") & admin_only)
async def unban_cmd(client, message):
  uid = _target(message)
  if not uid or uid <= 0:
    return await message.reply_text("<b>Usage</b>\n<code>/unban &lt;user_id&gt;</code>", quote=True)
  changed = db.unban_user(uid)
  await message.reply_text(f"✅ <b>User <code>{uid}</code> can use the bot again.</b>" if changed else f"ℹ️ <b><code>{uid}</code> wasn't banned.</b>", quote=True)


# --------------------------------------------------------------------------- #
# API keys
# --------------------------------------------------------------------------- #
@Bot.on_message(filters.command("newapikey") & admin_only)
async def newapikey_cmd(client, message):
  if message.chat.type.name != "PRIVATE":
    return await message.reply_text("🔒 <b>Create API keys in a private chat with me</b> — the key is shown once.", quote=True)
  args = message.command[1:]
  if not args:
    return await message.reply_text(
      "<b>Usage</b>\n<code>/newapikey &lt;label&gt; [scopes]</code>\n\n"
      "Scopes: <code>premium</code>, <code>users</code>, <code>links</code> — comma separated, or <code>all</code>. "
      "Without scopes the key gets <code>links</code> only.\n\n"
      "<i>e.g.</i> <code>/newapikey shopbot premium,users</code>", quote=True)
  try:
    scopes = api.parse_scopes(args[1] if len(args) > 1 else None)
  except ValueError as e:
    return await message.reply_text(f"❌ {esc(e)}", quote=True)
  key, kid = api.create_key(args[0], scopes, message.from_user.id)
  await message.reply_text(
    f"<b>🔑 Key created — {esc(args[0][:40])}</b>\n\n<code>{key}</code>\n\n"
    f"<b>Key ID</b>  <code>{kid}</code>\n<b>Scopes</b>  {_scopes_text(scopes)}\n\n"
    "⚠️ <b>This is the only time it's shown.</b> Copy it now, store it safely, then delete this message."
    + ("" if Vars.API_ENABLED else "\n\n<i>The API is off — turn it on in /settings → 🔑 API Keys.</i>"), quote=True)


@Bot.on_message(filters.command("apikeys") & admin_only)
async def apikeys_cmd(client, message):
  keys = api.list_keys()
  if not keys:
    return await message.reply_text("<b>🔑 No API keys yet</b>\n<i>Create one with /newapikey.</i>", quote=True)
  now, lines, rows = time.time(), [], []
  for kid, rec in keys:
    used = f"used {fmt_left(now - rec['last_used'])} ago" if rec.get("last_used") else "never used"
    lines.append(f"• <b>{esc(rec.get('label'))}</b> — <code>{kid}</code>\n   {_scopes_text(api.key_scopes(rec))} · {used}")
    rows.append([btn(f"🔧 {rec.get('label')}", callback_data=f"cfgapikey:{kid}", style=None)])
  await message.reply_text("<b>🔑 API keys</b>\n\n" + "\n".join(lines) + f"\n\n<i>Premium API: {'on' if Vars.API_ENABLED else 'off'}</i>",
                           quote=True, reply_markup=kb(*rows, nav(close=True)))


@Bot.on_message(filters.command("revokeapikey") & admin_only)
async def revokeapikey_cmd(client, message):
  args = message.command[1:]
  if not args:
    return await message.reply_text("<b>Usage</b>\n<code>/revokeapikey &lt;key_id&gt;</code> — see /apikeys", quote=True)
  ok = api.revoke_key(args[0])
  await message.reply_text("🗑 <b>Key revoked.</b> It stops working immediately." if ok else "❌ <b>No key with that ID.</b> See /apikeys.", quote=True)


@Bot.on_message(filters.command("apikeyscopes") & admin_only)
async def apikeyscopes_cmd(client, message):
  args = message.command[1:]
  if len(args) < 2:
    return await message.reply_text("<b>Usage</b>\n<code>/apikeyscopes &lt;key_id&gt; &lt;scopes&gt;</code>\n"
                                    "<i>e.g.</i> <code>/apikeyscopes a1b2c3d4 premium,users</code>", quote=True)
  try:
    scopes = api.parse_scopes(args[1])
  except ValueError as e:
    return await message.reply_text(f"❌ {esc(e)}", quote=True)
  if not api.set_scopes(args[0], scopes):
    return await message.reply_text("❌ <b>No key with that ID.</b> See /apikeys.", quote=True)
  await message.reply_text(f"✅ <b>Scopes updated</b>  →  {_scopes_text(scopes)}\n<i>Takes effect immediately.</i>", quote=True)


# --------------------------------------------------------------------------- #
# /dbstatus - where is everything stored?
# --------------------------------------------------------------------------- #
@Bot.on_message(filters.command("dbstatus") & filters.private & admin_only)
async def dbstatus_cmd(_, message):
  r = db.storage_report()
  head = ("✅ <b>MongoDB connected</b> - settings survive redeploys."
          if r["persistent"] else
          "⚠️ <b>Local files only</b> - everything is lost on redeploy. Set <code>DB_URL</code>.")
  await message.reply_text(
    f"{head}\n\n"
    f"<b>Backend:</b> <code>{esc(r['backend'])}</code>\n"
    f"<b>Database:</b> <code>{esc(r['db_name'])}</code>\n"
    f"<b>Saved settings:</b> {r['settings_overrides']}\n"
    f"<b>Post channels:</b> {r['post_channels']}\n"
    f"<b>API keys:</b> {r['api_keys']}\n"
    f"<b>Users / premium:</b> {r['users']} / {r['premium_users']}\n"
    f"<b>Unsaved writes:</b> {r['pending_writes']}")
