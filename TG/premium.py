"""
Premium, as users and admins see it.

Users:   /my_plan, /premium, the 💎 button on the start card, and any
         "Go Premium" button - one page showing their plan (or the offer).
Admins:  /premium opens the Premium Manager - grant / extend, remove, browse
         members, see who is about to expire, look a user up, end expired
         plans. Everything here calls Tools/premium.py, the same code the
         Premium API uses.

Commands kept for muscle memory:
    /add <id> <days> [set]   /del <id>   /del_expired   /premium_users
"""
import re
import time

from pyrogram import filters
from pyrogram.errors import ListenerTimeout as TimeoutError
from pyrogram.errors import MessageNotModified

from bot import Bot, Vars, admin_only, is_admin, logger
from Tools import access
from Tools import premium as P
from Tools.ui import (
  btn, esc, fmt_date, fmt_left, forwarded_user_id, kb, kv, nav, plural, quote, title,
  DANGER, PRIMARY, SUCCESS,
)
from .storage import igrone_error, retry_on_flood

PAGE = 8
DURATIONS = (7, 30, 90, 180, 365)


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
async def _show(query, text, markup=None):
  """Edit the message a button sits on - photo messages (the start card) take a
  caption edit, plain messages a text edit."""
  msg = query.message
  try:
    if getattr(msg, "photo", None) or getattr(msg, "media", None):
      await retry_on_flood(query.edit_message_caption)(text, reply_markup=markup)
    else:
      await retry_on_flood(query.edit_message_text)(text, reply_markup=markup)
  except MessageNotModified:
    pass


def _perks() -> list:
  lines = ["⚡ <b>Priority queue</b> — your downloads are processed first"]
  names = [access.name_of(sf) for sf in sorted(access.locked())]
  if names:
    lines.append(f"🔓 <b>Premium sources</b> — {esc(', '.join(names))}")
  if Vars.SHORTENER:
    lines.append("🔑 <b>No access tokens</b> — skip the verification links")
  return lines


def _plans_block() -> str:
  plans = [esc(l.strip()) for l in (Vars.PREMIUM_PLANS or "").splitlines() if l.strip()]
  return quote("\n".join(plans)) if plans else ""


def _buy_button(label):
  return btn(label, url=P.contact_url(), style=PRIMARY)


# --------------------------------------------------------------------------- #
# the user's page
# --------------------------------------------------------------------------- #
def plan_page(user_id, landing=False):
  """(text, buttons) for this user - their plan if they have one, else the offer.

  landing=True is the page behind https://t.me/<bot>?start=premium: members are
  told Premium is active, everyone else gets "Activate Premium" with the perks
  and a button to do it."""
  st = P.status(user_id)
  rows = []
  if st["premium"]:
    head = (title("✅", "Premium is activated", "You have the full bot — thank you!") if landing
            else title("💎", "Premium", "You're a member — thank you!"))
    if st["permanent"]:
      when = kv("Valid", "Permanent ♾") + "\n"
      left = None
    else:
      left = st["expires_at"] - time.time()
      when = kv("Valid until", fmt_date(st["expires_at"])) + "\n" + kv("Time left", fmt_left(left)) + "\n"
    text = (head + "\n\n" + when
            + (kv("Member since", fmt_date(st["since"])) + "\n" if st.get("since") else "")
            + "\n<b>Your perks</b>\n" + "\n".join(_perks()))
    if left is not None:
      if left < 7 * 86400:
        text += f"\n\n<i>⏳ Ending soon — renew with {esc(P.contact_label())} to keep these.</i>"
        rows.append([_buy_button("💬 Renew")])
      elif Vars.PREMIUM_PLANS:
        rows.append([_buy_button("💬 Extend")])
  else:
    text = (title("💎", "Activate Premium", "Unlock everything the bot can do.") if landing
            else title("💎", "Go Premium", "Unlock everything the bot can do.")) + "\n\n" \
           + "<b>What you get</b>\n" + "\n".join(_perks())
    block = _plans_block()
    if block:
      text += "\n\n<b>Plans</b>\n" + block
    text += f"\n\n<i>Message {esc(P.contact_label())} to activate.</i>"
    rows.append([_buy_button("💎 Activate Premium" if landing else "💬 Buy Premium")])
  if is_admin(user_id):
    text += "\n\n<i>🛠 You're an admin — every source is open to you.</i>"
  return text, rows


async def premium_landing(message):
  """https://t.me/<bot>?start=premium - see TG/cmds.py's /start handler."""
  from .storage import safe_photo_reply
  text, rows = plan_page(message.from_user.id, landing=True)
  await safe_photo_reply(message, text, kb(*rows, [btn("🏠 Menu", callback_data="start_cb", style=None), btn("✖ Close", callback_data="kclose", style=None)]))


@Bot.on_message(filters.command("my_plan"))
async def my_plan_cmd(client, message):
  text, rows = plan_page(message.from_user.id)
  await retry_on_flood(message.reply_text)(text, quote=True, reply_markup=kb(*rows, nav(close=True)))


@Bot.on_callback_query(filters.regex("^pm:me$"))
async def plan_from_menu_cb(client, query):
  """From the start card: has a way back to it."""
  await igrone_error(query.answer)()
  text, rows = plan_page(query.from_user.id)
  await _show(query, text, kb(*rows, nav(back="start_cb")))


@Bot.on_callback_query(filters.regex("^premuim$"))
async def plan_legacy_cb(client, query):
  """The old 'Premium' button (token gate, locked-source prompts...) - kept so
  messages already sent still work. It can't know what screen it sits on, so
  it only offers Close."""
  await igrone_error(query.answer)()
  text, rows = plan_page(query.from_user.id)
  await _show(query, text, kb(*rows, nav(close=True)))


# --------------------------------------------------------------------------- #
# admin: the Premium Manager
# --------------------------------------------------------------------------- #
def _panel():
  st = P.stats()
  total = st["users_total"]
  pct = f" · {st['premium_active'] / total * 100:.1f}% of users" if total else ""
  text = (title("💎", "Premium Manager", "Grant, extend, remove and review plans.") + "\n\n"
          + kv("Members", f"{st['premium_active']} active") + "\n"
          + kv("Ending soon", f"{st['expiring_7d']} within 7 days") + "\n"
          + kv("Users", f"{total}{pct}")
          + f"\n\n<b>Premium link</b>  <code>https://t.me/{getattr(Bot, 'username', None) or 'YourBot'}?start=premium</code>"
          + f"\n<i>API: {'on' if Vars.API_ENABLED else 'off'} · reminders: "
            f"{'on' if Vars.PREMIUM_NOTIFY and Vars.PREMIUM_REMIND_DAYS else 'off'}</i>")
  return text, kb(
    [btn("➕ Grant / extend", callback_data="pm:add"), btn("➖ Remove", callback_data="pm:rm")],
    [btn("📋 Members", callback_data="pm:ls:a:1", style=PRIMARY), btn("⏳ Ending soon", callback_data="pm:ls:e:1", style=None)],
    [btn("🔍 Look up user", callback_data="pm:find", style=None), btn("🧹 End expired", callback_data="pm:sw", style=None)],
    [btn("⚙️ Plans & settings", callback_data="cfgcat:premium", style=None), btn("🔒 Source access", callback_data="cfglock", style=None)],
    nav(close=True),
  )


@Bot.on_message(filters.command("premium"))
async def premium_cmd(client, message):
  if is_admin(message.from_user.id):
    text, markup = _panel()
    return await retry_on_flood(message.reply_text)(text, quote=True, reply_markup=markup)
  text, rows = plan_page(message.from_user.id)
  await retry_on_flood(message.reply_text)(text, quote=True, reply_markup=kb(*rows, nav(close=True)))


@Bot.on_callback_query(filters.regex("^pm:home$") & admin_only)
async def panel_cb(client, query):
  await igrone_error(query.answer)()
  await _show(query, *_panel())


@Bot.on_callback_query(filters.regex("^pm:sw$") & admin_only)
async def sweep_cb(client, query):
  n = await P.sweep()
  await igrone_error(query.answer)(f"🧹 {plural(n, 'expired plan')} ended." if n else "Nothing has expired.", show_alert=True)
  await _show(query, *_panel())


async def _user_name(client, uid) -> str:
  try:
    u = await client.get_users(uid)
    name = " ".join(x for x in (u.first_name, u.last_name) if x) or "User"
    return esc(name) + (f" (@{esc(u.username)})" if u.username else "")
  except Exception:
    return "<i>unknown — hasn't started this bot</i>"


async def _ask_user(client, query, prompt):
  """Ask for a user (ID or a forwarded message). Returns the id or None - and
  puts the panel back if the admin gave up."""
  await _show(query, f"{prompt}\n\n<i>Send their numeric ID, or forward any message from them.\n/cancel to go back.</i>",
              kb(nav(back="pm:home")))
  try:
    reply = await client.listen(user_id=query.from_user.id, timeout=120)
  except TimeoutError:
    await _show(query, *_panel())
    return None
  uid = forwarded_user_id(reply)
  text = (reply.text or "").strip()
  await igrone_error(reply.delete)()
  if uid is None and text.isdigit():
    uid = int(text)
  if not uid or uid <= 0:
    if text.lower() not in ("/cancel", "cancel"):
      await igrone_error(client.send_message)(query.from_user.id, "❌ That wasn't a user ID. Send digits, or forward a message from them.")
    await _show(query, *_panel())
    return None
  return uid


async def _user_card(client, uid, notice="", back="pm:home"):
  st = P.status(uid)
  name = await _user_name(client, uid)
  if st["permanent"]:
    plan = "🟢 Premium · ♾ permanent"
  elif st["premium"]:
    plan = f"🟢 Premium · until {fmt_date(st['expires_at'])} ({fmt_left(st['expires_at'] - time.time())})"
  else:
    plan = "⚪ Free"
  text = ((notice + "\n\n") if notice else "") + (
    title("👤", f"User {uid}") + "\n\n"
    + kv("Name", name) + "\n" + kv("Plan", plan)
    + (f"\n{kv('Since', fmt_date(st['since']))}" if st.get("since") else ""))
  rows = [[btn(f"➕ {d} days", callback_data=f"pm:g:{uid}:{d}") for d in (7, 30, 90)],
          [btn("✏️ Custom days", callback_data=f"pm:gc:{uid}", style=None)]]
  if not st["permanent"]:
    rows[1].append(btn("♾ Permanent", callback_data=f"pm:gp:{uid}", style=None))
  if st["premium"]:
    rows[1].append(btn("🗑 Remove", callback_data=f"pm:rm:{uid}"))
  return text, kb(*rows, nav(back=back))


# ---- grant ---------------------------------------------------------------------
@Bot.on_callback_query(filters.regex("^pm:(add|find)$") & admin_only)
async def add_cb(client, query):
  await igrone_error(query.answer)()
  uid = await _ask_user(client, query, title("➕", "Grant premium") if query.data == "pm:add" else title("🔍", "Look up a user"))
  if uid:
    await _show(query, *await _user_card(client, uid))


def _grant_notice(res):
  dm = "" if res.get("notified") else "\n<i>They weren't notified (notifications are off, or they haven't started the bot).</i>"
  if res["action"] == "unchanged":
    return "ℹ️ <b>Nothing changed</b> — they already have a permanent plan."
  if res["permanent"]:
    return f"✅ <b>Premium is now permanent</b> ♾.{dm}"
  verb = {"activated": "activated", "extended": "extended", "replaced": "set"}.get(res["action"], "updated")
  added = f"+{plural(res['added_days'], 'day')}, " if res["action"] != "replaced" else ""
  return f"✅ <b>Premium {verb}</b> — {added}valid until {fmt_date(res['expires_at'])}.{dm}"


@Bot.on_callback_query(filters.regex(r"^pm:g:(\d+):(\d+)$") & admin_only)
async def grant_cb(client, query):
  uid, days = (int(x) for x in query.data.split(":")[2:4])
  try:
    res = await P.grant(uid, days, source=f"admin:{query.from_user.id}")
  except P.PremiumError as e:
    return await retry_on_flood(query.answer)(f"❌ {e.message}", show_alert=True)
  await igrone_error(query.answer)(f"✅ +{days} days")
  await _show(query, *await _user_card(client, uid, _grant_notice(res)))


@Bot.on_callback_query(filters.regex(r"^pm:gp:(\d+)$") & admin_only)
async def grant_permanent_cb(client, query):
  uid = int(query.data.split(":")[2])
  res = await P.grant(uid, None, source=f"admin:{query.from_user.id}")
  await igrone_error(query.answer)("♾ Permanent")
  await _show(query, *await _user_card(client, uid, _grant_notice(res)))


@Bot.on_callback_query(filters.regex(r"^pm:gc:(\d+)$") & admin_only)
async def grant_custom_cb(client, query):
  await igrone_error(query.answer)()
  uid = int(query.data.split(":")[2])
  await _show(query, f"{title('✏️', 'Custom duration')}\n\nHow many days should user <code>{uid}</code> get?\n\n"
                     f"<i>Add <code>replace</code> to replace their time instead of extending it (e.g. <code>45 replace</code>).\n/cancel to go back.</i>",
              kb(nav(back=f"pm:u:{uid}:h")))
  try:
    reply = await client.listen(user_id=query.from_user.id, timeout=120)
  except TimeoutError:
    return await _show(query, *await _user_card(client, uid))
  parts = (reply.text or "").split()
  await igrone_error(reply.delete)()
  if not parts or not parts[0].isdigit():
    return await _show(query, *await _user_card(client, uid, "❌ <b>Send a whole number of days.</b>"))
  mode = "replace" if len(parts) > 1 and parts[1].lower() in ("replace", "set") else "extend"
  try:
    res = await P.grant(uid, int(parts[0]), mode=mode, source=f"admin:{query.from_user.id}")
  except P.PremiumError as e:
    return await _show(query, *await _user_card(client, uid, f"❌ <b>{esc(e.message)}</b>"))
  await _show(query, *await _user_card(client, uid, _grant_notice(res)))


# ---- remove -----------------------------------------------------------------------
@Bot.on_callback_query(filters.regex(r"^pm:rm(?::(\d+))?$") & admin_only)
async def remove_cb(client, query):
  await igrone_error(query.answer)()
  parts = query.data.split(":")
  uid = int(parts[2]) if len(parts) > 2 else await _ask_user(client, query, title("➖", "Remove premium"))
  if not uid:
    return
  st = P.status(uid)
  if not st["premium"]:
    return await _show(query, *await _user_card(client, uid, "ℹ️ <b>That user isn't premium.</b>"))
  await _show(
    query,
    f"{title('🗑', 'Remove premium?')}\n\nUser <code>{uid}</code> loses their plan "
    f"({'permanent' if st['permanent'] else fmt_left(st['expires_at'] - time.time()) + ' left'}) and is told so.",
    kb([btn("🗑 Yes, remove", callback_data=f"pm:rmy:{uid}", style=DANGER)], [btn("⇦ Keep", callback_data=f"pm:u:{uid}:h", style=None)]))


@Bot.on_callback_query(filters.regex(r"^pm:rmy:(\d+)$") & admin_only)
async def remove_yes_cb(client, query):
  uid = int(query.data.split(":")[2])
  res = await P.revoke(uid, source=f"admin:{query.from_user.id}")
  await igrone_error(query.answer)("🗑 Removed" if res["removed"] else "They weren't premium")
  dm = "" if res.get("notified") else " They weren't notified."
  await _show(query, *await _user_card(client, uid, f"🗑 <b>Premium removed.</b>{dm}"))


# ---- browse ------------------------------------------------------------------------------
def _list_view(kind, page):
  rows_all = P.active_users(expiring_within_days=7 if kind == "e" else None)
  pages = max(1, (len(rows_all) + PAGE - 1) // PAGE)
  page = min(max(1, page), pages)
  chunk = rows_all[(page - 1) * PAGE: page * PAGE]
  head = "⏳ Ending soon" if kind == "e" else "📋 Premium members"
  if not rows_all:
    text = f"{title(head.split()[0], head.split(' ', 1)[1])}\n\n<i>{'Nobody is ending within 7 days.' if kind == 'e' else 'No active premium members yet.'}</i>"
    return text, kb(nav(back="pm:home"))
  text = (title(head.split()[0], head.split(" ", 1)[1], f"{plural(len(rows_all), 'member')} · page {page}/{pages}")
          + "\n\n<i>Tap a member to manage them.</i>")
  btns = []
  for r in chunk:
    if r["permanent"]:
      label = f"♾ {r['user_id']} · permanent"
    else:
      left = r["expires_at"] - time.time()
      label = f"{'🟡' if left < 7 * 86400 else '🟢'} {r['user_id']} · {fmt_left(left)}"
    btns.append([btn(label, callback_data=f"pm:u:{r['user_id']}:{kind}{page}", style=None)])
  pager = []
  if page > 1:
    pager.append(btn("‹ Prev", callback_data=f"pm:ls:{kind}:{page - 1}", style=None))
  if page < pages:
    pager.append(btn("Next ›", callback_data=f"pm:ls:{kind}:{page + 1}", style=None))
  return text, kb(*btns, pager, nav(back="pm:home"))


@Bot.on_callback_query(filters.regex(r"^pm:ls:([ae]):(\d+)$") & admin_only)
async def list_cb(client, query):
  await igrone_error(query.answer)()
  _, _, kind, page = query.data.split(":")
  await _show(query, *_list_view(kind, int(page)))


@Bot.on_callback_query(filters.regex(r"^pm:u:(\d+):([hae])(\d*)$") & admin_only)
async def user_cb(client, query):
  await igrone_error(query.answer)()
  uid, origin, page = re.fullmatch(r"pm:u:(\d+):([hae])(\d*)", query.data).groups()
  back = "pm:home" if origin == "h" else f"pm:ls:{origin}:{page or 1}"
  await _show(query, *await _user_card(client, int(uid), back=back))


# --------------------------------------------------------------------------- #
# classic commands (same engine, quick to type)
# --------------------------------------------------------------------------- #
def _usage(text):
  return f"<b>Usage</b>\n<code>{text}</code>"


@Bot.on_message(filters.command(["add", "add_premium"]) & admin_only)
async def add_cmd(client, message):
  args = message.command[1:]
  forever = len(args) > 1 and args[1].lower() in ("perm", "permanent", "forever")
  if len(args) < 2 or not args[0].isdigit() or not (forever or args[1].isdigit()):
    return await message.reply_text(_usage("/add <user_id> <days|permanent> [replace]") +
                                    "\n\n<i>Extends by default; add <code>replace</code> to set their remaining time to exactly that.\n"
                                    "<code>permanent</code> gives a plan that never ends.</i>", quote=True)
  mode = "replace" if len(args) > 2 and args[2].lower() in ("replace", "set") else "extend"
  try:
    res = await P.grant(int(args[0]), None if forever else int(args[1]), mode=mode, source=f"admin:{message.from_user.id}")
  except P.PremiumError as e:
    return await message.reply_text(f"❌ {esc(e.message)}", quote=True)
  await message.reply_text(_grant_notice(res) + f"\n<b>User</b>  <code>{args[0]}</code>", quote=True)


@Bot.on_message(filters.command(["del", "del_premium"]) & admin_only)
async def del_cmd(client, message):
  args = message.command[1:]
  if not args or not args[0].isdigit():
    return await message.reply_text(_usage("/del <user_id>"), quote=True)
  res = await P.revoke(int(args[0]), source=f"admin:{message.from_user.id}")
  await message.reply_text(
    ("🗑 <b>Premium removed</b>" + ("" if res["notified"] else " — they weren't notified.")) if res["removed"]
    else "ℹ️ <b>That user isn't premium.</b>", quote=True)


@Bot.on_message(filters.command(["del_expired", "del_expired_premium"]) & admin_only)
async def del_expired_cmd(client, message):
  n = await P.sweep()
  await message.reply_text(f"🧹 <b>{plural(n, 'expired plan')} ended.</b>" if n else "✅ <b>Nothing has expired.</b>", quote=True)


@Bot.on_message(filters.command("premium_users") & admin_only)
async def premium_users_cmd(client, message):
  text, markup = _list_view("a", 1)
  await message.reply_text(text, quote=True, reply_markup=markup)
