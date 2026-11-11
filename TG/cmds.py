import asyncio
import os
import platform
import shutil
import time
from io import BytesIO
from os import execl
from sys import executable

import psutil
import pyrogram.errors
from pyrogram import filters
from pyrogram.errors import FloodWait
from pyrogram.types import InlineKeyboardMarkup

from bot import Bot, Vars, admin_only, is_admin, logger, owner_only
from Tools import premium
from Tools.db import ensure_user, get_users, uts
from Tools.my_token import verify_token
from Tools.ui import PRIMARY, btn, esc, fmt_left, fmt_uptime, kb, nav, quote, title

from .post import deliver_post
from .premium import premium_landing
from .storage import igrone_error, queue, retry_on_flood, safe_photo_reply

HELP_MSG = (
  title("❓", "How it works", "Search, read and download manga, manhwa & manhua.") + "\n\n"
  "<b>1.</b>  Type a title — e.g. <code>Solo Leveling</code>\n"
  "<b>2.</b>  Pick a source (or search them all)\n"
  "<b>3.</b>  Open a manga → <b>📚 Chapters</b> to download, or <b>🔔 Subscribe</b> to get new chapters automatically\n\n"
  + quote("Chapters arrive as PDF and/or CBZ. Change the format, caption, thumbnail, password and more in ⚙️ Settings.", expandable=True)
  + "\n\n<i>Choose a topic below.</i>"
)

USER_COMMANDS_MSG = (
  title("👤", "Commands") + "\n\n"
  "<b>Everyday</b>\n"
  "<code>&lt;manga name&gt;</code> — search (just type it)\n"
  "/search <code>&lt;name&gt;</code> — the same, usable in groups\n"
  "/subs — your subscriptions\n"
  "/queue — your pending downloads\n"
  "/clean_tasks — cancel them\n\n"
  "<b>Account</b>\n"
  "/read — reply to a PDF to read it online in your browser\n"
  "/my_plan — your Premium plan, or how to get it\n"
  "/us — your settings (format, caption, thumbnail, password, merge size…)\n"
  "/start · /help"
)

ADMIN_COMMANDS_MSG = (
  title("🛠", "Admin commands") + "\n\n"
  "<b>Panels</b>\n"
  "/settings — channels, premium, source locks, API keys, post design, backups…\n"
  "/premium — Premium Manager\n\n"
  "<b>Premium</b>\n"
  "/add <code>&lt;id&gt; &lt;days|permanent&gt; [replace]</code> · /del <code>&lt;id&gt;</code> · /premium_users · /del_expired\n\n"
  "<b>Users</b>\n"
  "/ban · /unban <code>&lt;id&gt;</code> · /info <code>&lt;id&gt;</code>\n"
  "/broadcast · /pbroadcast · /forward · /pforward\n\n"
  "<b>API</b>\n"
  "/newapikey <code>&lt;label&gt; [scopes]</code> · /apikeys · /revokeapikey · /apikeyscopes\n\n"
  "<b>Posting &amp; data</b>\n"
  "/newpost · /channels · /postsettings · /scheduled · /autopublish · /backup · /restore\n\n"
  "<b>System</b>\n"
  "/stats · /clean · /restart · /shell · /export · /import"
)

PREMIUM_HELP_MSG = (
  title("💎", "Premium & sources") + "\n\n"
  "Premium members get <b>priority downloads</b> and access to any <b>🔒 Premium source</b>. "
  "Free sources stay free for everyone.\n\n"
  "In the source picker, 🔒 marks a Premium source you can't use yet; 💎 marks one that's open to you.\n\n"
  "<i>See /my_plan for your plan, or tap below.</i>"
)


def _help_markup(user_id):
  rows = [[btn("👤 Commands", callback_data="helpcmds", style=None), btn("💎 Premium & sources", callback_data="helppm", style=None)]]
  if is_admin(user_id):
    rows.append([btn("🛠 Admin commands", callback_data="helpadmin", style=None)])
  rows.append(nav(back="start_cb"))
  return kb(*rows)


async def _help_page(query, text, markup):
  await igrone_error(query.answer)()
  await retry_on_flood(query.edit_message_caption)(text, reply_markup=markup)


@Bot.on_callback_query(filters.regex("^help_cb$"))
async def help_callback(client, query):
  await _help_page(query, HELP_MSG, _help_markup(query.from_user.id))


@Bot.on_callback_query(filters.regex("^helpcmds$"))
async def help_user_cmds_cb(client, query):
  await _help_page(query, USER_COMMANDS_MSG, kb(nav(back="help_cb")))


@Bot.on_callback_query(filters.regex("^helppm$"))
async def help_premium_cb(client, query):
  await _help_page(query, PREMIUM_HELP_MSG, kb([btn("💎 My plan", callback_data="pm:me")], nav(back="help_cb")))


@Bot.on_callback_query(filters.regex("^helpadmin$") & admin_only)
async def help_admin_cmds_cb(client, query):
  await _help_page(query, ADMIN_COMMANDS_MSG, kb(nav(back="help_cb")))


@Bot.on_callback_query(filters.regex("^start_cb$"))
async def back_to_start_callback(client, query):
  await igrone_error(query.answer)()
  await retry_on_flood(query.edit_message_caption)(_start_text(query.from_user.id), reply_markup=_start_buttons(query.from_user.id))


@Bot.on_message(filters.command("help"))
async def help_cmd(client, message):
  await safe_photo_reply(message, HELP_MSG, _help_markup(message.from_user.id))


def _plan_line(user_id) -> str:
  st = premium.status(user_id)
  if st["premium"]:
    if st["permanent"]:                      # no end date to count down to
      return "💎 <b>Premium</b> · permanent ♾"
    return f"💎 <b>Premium</b> · {fmt_left(st['expires_at'] - time.time())} left"
  return "🆓 <b>Free plan</b> · /my_plan to upgrade"


def _start_text(user_id=None):
  name = esc(getattr(Bot, "first_name", None) or "Manga Bot")
  text = (title("📚", name, "Search, read & download manga, manhwa and manhua.") + "\n\n"
          + quote("🔎 Just type a title to begin — e.g. <code>One Piece</code>") + "\n\n")
  if user_id:
    text += _plan_line(user_id) + "\n"
  text += f"⚡ <b>Online</b> for {fmt_uptime(time.time() - Vars.PING)}"
  return text


def _start_buttons(user_id):
  rows = [
    [btn("📌 Subscriptions", callback_data="st:subs", style=PRIMARY), btn("📊 Queue", callback_data="st:queue", style=None)],
    [btn("💎 Premium", callback_data="pm:me"), btn("⚙️ Settings", callback_data="mus", style=None)],
    [btn("❓ Help", callback_data="help_cb", style=None)],
  ]
  if is_admin(user_id):
    rows.append([btn("🛠 Bot settings", callback_data="adm:home", style=None), btn("💎 Manage premium", callback_data="pm:home", style=None)])
  rows.append(nav(close=True))
  return kb(*rows)


@Bot.on_message(filters.command("start"))
async def start(client, message):
  ensure_user(message.from_user.id)
  if len(message.command) > 1:
    param = message.command[1]
    if param != "start":
      # Deep link from the "📖 Read Now" button under a channel post: ?start=get_<post_id>
      if param.startswith("get_"):
        return await deliver_post(client, message, param.removeprefix("get_"))
      # Deep link from an inline-mode result: ?start=isearch_<cache_id>
      if param.startswith("isearch_"):
        return await _run_isearch(client, message, param.removeprefix("isearch_"))
      # https://t.me/<bot>?start=premium - "Premium is activated" for members,
      # "Activate Premium" (perks + button) for everyone else.
      if param.lower() == "premium":
        return await premium_landing(message)
      sts = await message.reply("<i>Verifying…</i>")
      return await verify_token(sts, message.from_user.id, param)

  await safe_photo_reply(message, _start_text(message.from_user.id), _start_buttons(message.from_user.id))


async def _run_isearch(client, message, sid):
  """Re-run the search that produced an inline-mode result, once the user
  has opened a private chat with the bot (inline results can't deliver
  files directly - see TG/inline.py). `sid` is a short cache id, not the
  raw query text, since Telegram's deep-link parameter only allows
  [A-Za-z0-9_-] and manga titles/queries can contain anything."""
  import random
  from .storage import search_everywhere, check_get_web, searchs, isearch_cache

  query = isearch_cache.get(sid)
  if not query:
    return await retry_on_flood(message.reply_text)(
      "⏰ That search link expired. Please search again.", quote=True
    )

  sts = await retry_on_flood(message.reply_photo)(
    random.choice(Vars.PICS), caption=f"<i>🔎 Searching: <b>{query}</b> ...</i>", quote=True
  )
  try:
    results = await search_everywhere(query, for_user=message.from_user.id)
  except Exception as e:
    logger.exception(e)
    results = []

  results = [r for r in results if query.lower() in (r.get("title") or "").lower()][:10] or results[:10]
  if not results:
    return await igrone_error(sts.edit_caption)(f"❌ No results for <b>{query}</b>.")

  rows = []
  for r in results:
    webs_ = check_get_web(r.get("url", ""))
    if not webs_:
      continue
    c = f"chs|{webs_.sf}{hash(r['url'])}"
    searchs[c] = (webs_, r)
    site_name = type(webs_).__name__.replace("Webs", "")
    rows.append([btn(f"{r['title']} [{site_name}]", callback_data=c)])

  if not rows:
    return await igrone_error(sts.edit_caption)(f"❌ No results for <b>{query}</b>.")

  await igrone_error(sts.edit_caption)(
    f"<i>Results for <b>{query}</b>:</i>", reply_markup=InlineKeyboardMarkup(rows)
  )


@Bot.on_message(filters.command(["clean_tasks", "clean_queue"]))
async def deltask(client, message):
  if queue.get_count(message.from_user.id):
    numb = await queue.delete_tasks(message.from_user.id)
    await message.reply(f"<i>All Your Tasks Deleted:- {numb} </i>")
  else:
    await message.reply("<i>There is no any your pending tasks.... </i>")


# --------------------------------------------------------------------------- #
# admin: users / premium
# --------------------------------------------------------------------------- #
@Bot.on_message(filters.command("info") & admin_only)
async def get_info_(client, message):
  try:
    user_id = str(message.command[1])
  except IndexError:
    return await message.reply("<code>/info user_id</code>")
  user = uts.get(user_id)
  if not user:
    return await message.reply("<code>User Not Found</code>")
  setting = user.get("setting", {})
  keys = ("file_name", "caption", "thumb", "banner1", "banner2", "dump", "type", "megre",
          "regex", "file_name_len", "password", "compress")
  txt = f"<b>User ID: {user_id}</b>\n" + "\n".join(f"<b>{k}: {setting.get(k, 'None')}</b>" for k in keys)
  await message.reply(txt[:4000])


@Bot.on_message(filters.command(["broadcast", "b"]) & admin_only)
async def b_handler(_, msg):
  return await broadcast_(_, msg)


@Bot.on_message(filters.command(["pbroadcast", "pb"]) & admin_only)
async def pb_handler(_, msg):
  return await broadcast_(_, msg, pin=True)


@Bot.on_message(filters.command(["forward", "fd"]) & admin_only)
async def fb_handler(_, msg):
  return await broadcast_(_, msg, forward=True)


@Bot.on_message(filters.command(["pforward", "pfd"]) & admin_only)
async def pfb_handler(_, msg):
  return await broadcast_(_, msg, pin=True, forward=True)


async def broadcast_(_, message, pin=None, forward=None):
  if not message.reply_to_message:
    return await message.reply_text("<code>Reply to a message to broadcast it.</code>")
  sts = await message.reply_text("<code>Broadcasting...</code>")
  source = message.reply_to_message
  users = get_users()
  stats = dict(ok=0, blocked=0, deleted=0, failed=0)

  async def send(user_id):
    return await (source.forward(user_id) if forward else source.copy(user_id))

  for user_id in users:
    try:
      try:
        sent = await send(user_id)
      except FloodWait as e:
        await asyncio.sleep(e.value + 1)
        sent = await send(user_id)
      if pin:
        await igrone_error(sent.pin)(both_sides=True)
      stats["ok"] += 1
    except (pyrogram.errors.UserIsBlocked, pyrogram.errors.UserNotParticipant):
      stats["blocked"] += 1
    except pyrogram.errors.InputUserDeactivated:
      stats["deleted"] += 1
    except Exception:
      stats["failed"] += 1
    await asyncio.sleep(0.05)           # stay far below Telegram's 30 msg/s limit

  await retry_on_flood(sts.edit)(
    f"<b><u>Broadcast Completed</u>\n\nTotal Users: <code>{len(users)}</code>\n"
    f"Successful: <code>{stats['ok']}</code>\nBlocked: <code>{stats['blocked']}</code>\n"
    f"Deleted Accounts: <code>{stats['deleted']}</code>\nFailed: <code>{stats['failed']}</code></b>")


# --------------------------------------------------------------------------- #
# admin: system
# --------------------------------------------------------------------------- #
@Bot.on_message(filters.command("restart") & admin_only)
async def restart_(client, message):
  from Tools.db import flush_now
  msg = await message.reply_text("<code>Restarting.....</code>", quote=True)
  with open("restart_msg.txt", "w") as file:
    file.write(f"{msg.chat.id}:{msg.id}")
  flush_now()
  execl(executable, executable, "-B", "main.py")


def humanbytes(size):
  if not size:
    return "0 B"
  size = float(size)
  for unit in ("B", "KB", "MB", "GB", "TB"):
    if size < 1024 or unit == "TB":
      return f"{size:.2f} {unit}"
    size /= 1024


@Bot.on_message(filters.command("stats"))
async def show_stats(client, message):
  t0 = time.time()
  status_msg = await message.reply("📊 <b>Accessing System Details...</b>")
  ping_ms = int((time.time() - t0) * 1000)

  proc = psutil.Process(os.getpid())
  proc.cpu_percent(None)
  psutil.cpu_percent(None)
  net_start = psutil.net_io_counters()
  await asyncio.sleep(1.5)                       # never block the event loop
  net_end = psutil.net_io_counters()
  span = 1.5

  total, used, free = shutil.disk_usage(".")
  ram = psutil.virtual_memory()
  uptime = time.strftime("%Hh %Mm %Ss", time.gmtime(time.time() - Vars.PING))
  mem = proc.memory_info()

  text = f"""🖥️ <b>System Statistics</b>

💾 <b>Disk</b>  <code>{humanbytes(used)} / {humanbytes(total)}</code> (free {humanbytes(free)})
🧠 <b>RAM</b>  <code>{humanbytes(ram.used)} / {humanbytes(ram.total)}</code> ({ram.percent}%)
⚡ <b>CPU</b>  <code>{psutil.cpu_percent(None)}%</code> on {os.cpu_count()} cores

🔌 <b>Bot process</b>
├ CPU: <code>{proc.cpu_percent(None)}%</code>
└ RAM: <code>{humanbytes(mem.rss)}</code>

🌐 <b>Network</b>
├ Up: <code>{humanbytes((net_end.bytes_sent - net_start.bytes_sent) / span)}/s</code>
└ Down: <code>{humanbytes((net_end.bytes_recv - net_start.bytes_recv) / span)}/s</code>

📟 {platform.system()} {platform.release()} · Python {platform.python_version()}
⏱ Uptime: <code>{uptime}</code> · Ping: <code>{ping_ms} ms</code>
📦 Queue: <code>{queue.qsize()}</code> waiting · <code>{len(queue.ongoing_tasks)}</code> running"""
  await retry_on_flood(status_msg.edit)(text)


@Bot.on_message(filters.command("shell") & owner_only)
async def shell(_, message):
  cmd = message.text.split(maxsplit=1)
  if len(cmd) == 1:
    return await message.reply("<code>No command to execute was given.</code>")
  proc = await asyncio.create_subprocess_shell(
    cmd[1], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
  stdout, stderr = await proc.communicate()
  stdout, stderr = stdout.decode(errors="replace").strip(), stderr.decode(errors="replace").strip()
  reply = ""
  if stdout:
    reply += f"<b>Stdout</b>\n<pre>{stdout}</pre>\n"
  if stderr:
    reply += f"<b>Stderr</b>\n<pre>{stderr}</pre>"
  if len(reply) > 3000:
    bio = BytesIO(reply.encode())
    await message.reply_document(bio, file_name="shell_output.txt")
  else:
    await message.reply(reply or "No Reply")


@Bot.on_message(filters.command("export") & admin_only)
async def export_(_, message):
  cmd = message.text.split(maxsplit=1)
  if len(cmd) == 1:
    return await message.reply("<code>File Name Not given.</code>")
  name = cmd[1]
  if os.path.isfile(name):
    await message.reply_document(name)
  else:
    await message.reply("<code>File Not Found</code>")


@Bot.on_message(filters.command("import") & admin_only)
async def import_(_, message):
  cmd = message.text.split(maxsplit=1)
  if len(cmd) == 1 or not message.reply_to_message or not message.reply_to_message.document:
    return await message.reply("<code>Reply to a file with /import file_name</code>")
  if os.path.exists(cmd[1]):
    return await message.reply("<code>File already exists</code>")
  await message.reply_to_message.download(file_name=cmd[1])
  await message.reply("<code>Imported.</code>")


@Bot.on_message(filters.command(["clean", "c"]) & admin_only)
async def clean_files(_, message):
  """Delete leftover work files."""
  sts = await message.reply_text("🔍 Cleaning files...")
  shutil.rmtree("Process", ignore_errors=True)
  removed = 0
  for root, dirs, files in os.walk(".", topdown=True):
    dirs[:] = [d for d in dirs if d not in (".git", "venv", "env", "__pycache__", "data")]
    for file in files:
      if file.lower().endswith((".mkv", ".mp4", ".zip", ".pdf", ".cbz", ".temp")):
        try:
          os.remove(os.path.join(root, file))
          removed += 1
        except OSError:
          pass
  await sts.edit(f"🧹 Removed <b>{removed}</b> leftover file(s) and the Process folder.")
