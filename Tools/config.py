"""Runtime settings registry.

Each `Item` is one option an admin can change from the bot (/settings).
Values live in the DB (`cfg`), override the environment default and are copied
onto `bot.Vars`, so the rest of the code just reads `Vars.SOMETHING`.
"""
from dataclasses import dataclass, field

from bot import Vars, as_bool, as_channel, as_int
from Tools.db import cfg, cfg_table, get_config, set_config, del_config


@dataclass
class Item:
  key: str            # also the Vars attribute
  label: str
  kind: str           # bool | int | text | channel | choice
  cat: str
  help: str = ""
  lo: int = 0
  hi: int = 0
  choices: tuple = field(default_factory=tuple)   # ((value, label), ...)
  after: str = ""     # hook name run after the value changes
  secret: bool = False  # mask the value in the settings UI (API keys, etc.)


CATEGORIES = {
  "channels": ("📢 Channels", "Where posts, files and logs go. The bot must be admin in each channel."),
  "post": ("🎨 Post Design", "How channel posts look."),
  "filestore": ("🗄 File Store Bot", "The global File Store Bot (used by the main channel unless it has its own). Other channels have theirs under /channels."),
  "access": ("🔐 Access & Security", "Who can use the bot."),
  "premium": ("💎 Premium", "Plans, contact, reminders and the Premium API."),
  "perf": ("⚡ Performance", "Trade speed for CPU / RAM."),
  "progress": ("📊 Progress Bar", "The live download / upload bar."),
  "network": ("🛰 Source Access", "Help for sites that block the bot behind Cloudflare."),
  "webreader": ("🌐 Web Reader", "/read turns a PDF into a link that opens a mobile-friendly reader page served by this bot."),
  "backup": ("💾 Auto-Backup", "Automatic periodic database backups to the Log Channel."),
}

ITEMS = [
  Item("POST_CHANNEL", "Post Channel (main)", "channel", "channels", "The main channel for public posts (poster + info + Read Now button). Add more channels - each with its own Premium / Back-up links and File Store Bot - with /channels."),
  Item("DUMP_CHANNEL", "Dump Channel (fallback)", "channel", "channels", "Only used as the file storage when no Constant Dump is set. Must be a numeric id (forward a message from it)."),
  Item("LOG_CHANNEL", "Log Channel", "channel", "channels", "Bot logs / delivered downloads. Leave unset to disable."),
  Item("CONSTANT_DUMP_CHANNEL", "Storage (Constant Dump)", "channel", "channels", "The file-storage DB. Every file is stored here once and reused instead of being downloaded again; posts and File Store links point at it. Make the bot an admin here (and add this same channel to the File Store Bot as a DB channel). Overrides users' own dump channels."),

  Item("POST_BUTTON", "Button text", "text", "post", "Text of the button under each post.", hi=32),
  Item("POST_FOOTER", "Footer line", "text", "post", "Extra line at the bottom of every post (e.g. your channel @name). Send - to clear.", hi=120),
  Item("POST_READ_STYLE", "📖 Read button colour", "choice", "post", "Colour of the Read Now button under channel posts. Each channel can override it in /channels → 🎨 Button colours.",
       choices=(("auto", "✨ Auto"), ("primary", "🔵 Blue"), ("success", "🟢 Green"), ("danger", "🔴 Red"), ("none", "⚪ None"))),
  Item("POST_PREMIUM_STYLE", "💎 Premium button colour", "choice", "post", "Colour of the Premium button under channel posts (channels with a Premium link).",
       choices=(("auto", "✨ Auto"), ("primary", "🔵 Blue"), ("success", "🟢 Green"), ("danger", "🔴 Red"), ("none", "⚪ None"))),
  Item("POST_BACKUP_STYLE", "💾 Back-up button colour", "choice", "post", "Colour of the Back-up button under channel posts (channels with a Back-up link).",
       choices=(("auto", "✨ Auto"), ("primary", "🔵 Blue"), ("success", "🟢 Green"), ("danger", "🔴 Red"), ("none", "⚪ None"))),
  Item("POST_DESC_LEN", "Synopsis length", "int", "post", "Max characters of synopsis in the post.", lo=80, hi=700),
  Item("POST_STYLISH", "Stylish fonts", "bool", "post", "Small-caps labels (ꜱᴛᴀᴛᴜꜱ) and refined accents. Off = plain labels."),
  Item("POST_PREMIUM_EMOJI", "Premium emoji", "bool", "post", "Use your custom (premium) emoji where set. Falls back to normal emoji automatically."),
  Item("POST_SPOILER", "Blur poster", "bool", "post", "Hide the poster behind a spoiler blur."),

  Item("FILESTORE_ENABLED", "Use File Store Bot", "bool", "filestore",
       "When on, every new post's \"Read Now\" button is generated through that bot's Link Generation API instead of this bot's own delivery. Falls back to this bot's own delivery automatically if the API call ever fails."),
  Item("FILESTORE_API_URL", "API Base URL", "text", "filestore",
       "The File Store Bot's public API address, e.g. https://your-app.example.com (no trailing slash needed).", hi=200),
  Item("FILESTORE_API_KEY", "API Key", "text", "filestore",
       "Bearer token from that bot's /newapikey command. Shown masked once set.", hi=200, secret=True),
  Item("FILESTORE_BOT_USERNAME", "File Store Bot username", "text", "filestore",
       "No @. Optional - just for your own reference; the /ping test shows this automatically.", hi=64),

  Item("IS_PRIVATE", "Private mode", "bool", "access", "Only admins can use the bot."),
  Item("PRIVATE_TXT", "Private message", "text", "access", "Shown to everyone else in private mode. HTML allowed.", hi=500),
  Item("FORCE_SUB_TEXT", "Force-sub message", "text", "access", "Shown above the join buttons. HTML allowed.", hi=500),
  Item("SHORTENER", "Token / shortener", "bool", "access", "Require free users to verify a shortened link."),
  Item("SHORTENER_API", "Shortener API", "text", "access", "URL with {} where the link goes, e.g. https://site.com/api?key=K&url={}", hi=400),
  Item("DURATION", "Token hours", "int", "access", "How long a verified token stays valid.", lo=1, hi=720),

  Item("WORKERS", "Parallel chapters", "int", "perf", "Chapters processed at once. Lower = less RAM/CPU.", lo=1, hi=10, after="workers"),
  Item("DL_THREADS", "Download threads", "int", "perf", "Simultaneous image downloads/conversions.", lo=1, hi=16),
  Item("IMG_QUALITY", "Default quality", "int", "perf", "JPEG quality when the user has not chosen one (lower = smaller files).", lo=10, hi=100),
  Item("PREMIUM_CONTACT", "Contact to buy", "text", "premium", "Who users message to buy, e.g. @YourName. Empty = the owner's profile.", hi=64),
  Item("PREMIUM_PLANS", "Plans & prices", "text", "premium", "Shown on the Premium page - one plan per line, e.g. 30 days — $1.05.", hi=700),
  Item("PREMIUM_NOTIFY", "Notify users", "bool", "premium", "DM users when premium starts, is about to end, and ends."),
  Item("PREMIUM_REMIND_DAYS", "Remind before expiry (days)", "int", "premium", "How many days before expiry to remind a user. 0 = never.", lo=0, hi=30),
  Item("API_ENABLED", "Premium API", "bool", "premium", "Let other bots add/remove premium over HTTP with an API key (see the API Keys screen).", after="api"),
  Item("AUTO_UPDATES", "Auto updates", "bool", "perf", "Check subscriptions for new chapters."),
  Item("STORAGE_REUSE", "Reuse stored files", "bool", "perf", "Serve a chapter that's already in the storage channel with a server-side copy instead of downloading, converting and uploading it again."),
  Item("UPDATE_INTERVAL", "Update interval (min)", "int", "perf", "How often subscriptions are checked.", lo=5, hi=1440),

  Item("PROGRESS", "Progress bar", "bool", "progress", "Show live progress while downloading / uploading."),
  Item("PROGRESS_STYLE", "Bar style", "choice", "progress", "Look of the bar.",
       choices=(("smooth", "▰▰▰▱▱"), ("blocks", "███░░"), ("dots", "●●●○○"), ("squares", "■■■□□"))),
  Item("PROGRESS_INTERVAL", "Refresh seconds", "int", "progress", "Seconds between edits (higher = fewer Telegram API calls).", lo=2, hi=20),

  Item("FLARESOLVERR_URL", "FlareSolverr URL", "text", "network", "Optional. Address of a FlareSolverr server (e.g. http://flaresolverr:8191) - a real browser the bot hands a page to when a site's bot protection blocks every other method. Send - to clear.", hi=200),
  Item("WEB_READER_ENABLED", "Web reader", "bool", "webreader", "Let users run /read on a PDF to get an online reading link.", after="webreader"),
  Item("PUBLIC_URL", "Public URL", "text", "webreader", "This bot's public web address, e.g. https://your-app.koyeb.app - used to build the links. Render/Koyeb are detected automatically.", hi=200),
  Item("WEB_READER_HOURS", "Link lifetime (hours)", "int", "webreader", "After this the link and the stored PDF are deleted.", lo=1, hi=720),
  Item("WEB_READER_MAX_MB", "Max PDF size (MB)", "int", "webreader", "Bigger files are refused (they use disk and bandwidth).", lo=1, hi=500),
  Item("WEB_READER_PER_USER", "Links per user", "int", "webreader", "Active links one non-admin user may hold at once.", lo=1, hi=50),

  Item("BACKUP_ENABLED", "Auto-backup", "bool", "backup", "Send a full database backup to the Log Channel automatically."),
  Item("BACKUP_INTERVAL_DAYS", "Every (days)", "int", "backup", "How often to send the automatic backup.", lo=1, hi=30),
]
BY_KEY = {i.key: i for i in ITEMS}
DEFAULTS = {i.key: getattr(Vars, i.key) for i in ITEMS}
DEFAULTS["POST_TEMPLATE"] = Vars.POST_TEMPLATE

_hooks = {}


def hook(name):
  def deco(fn):
    _hooks[name] = fn
    return fn
  return deco


def coerce(item: Item, raw):
  """Validate/convert user input. Returns (value, error)."""
  if item.kind == "bool":
    return as_bool(raw), None
  if item.kind == "int":
    try:
      n = int(str(raw).strip())
    except ValueError:
      return None, "Send a whole number."
    if not item.lo <= n <= item.hi:
      return None, f"Must be between {item.lo} and {item.hi}."
    return n, None
  if item.kind == "channel":
    v = as_channel(raw)
    return v, None if v else "Send a channel id, @username or forward a message from it."
  if item.kind == "choice":
    ok = {c[0] for c in item.choices}
    return (raw, None) if raw in ok else (None, "Invalid choice.")
  raw = str(raw).strip()
  if raw == "-":
    return "", None
  if item.hi and len(raw) > item.hi:
    return None, f"Too long (max {item.hi} characters)."
  return raw, None


def apply_all():
  """Copy DB overrides onto Vars (call at startup)."""
  for key in list(DEFAULTS) + ["POST_TEMPLATE"]:
    if key in cfg and cfg[key] is not None:
      setattr(Vars, key, cfg[key])
  rebuild_admins()


def set_value(key, value):
  set_config(key, value)
  setattr(Vars, key, value)
  item = BY_KEY.get(key)
  if item and item.after and item.after in _hooks:
    _hooks[item.after]()


def reset_value(key):
  del_config(key)
  setattr(Vars, key, DEFAULTS.get(key))
  item = BY_KEY.get(key)
  if item and item.after and item.after in _hooks:
    _hooks[item.after]()


def is_overridden(key) -> bool:
  return cfg.get(key) is not None


# ---- lists stored in config ------------------------------------------------
def extra_admins() -> list:
  return list(cfg.get("admins") or [])


def _env_admins():
  import os
  from bot import as_int_list
  return as_int_list(os.environ.get("ADMINS"))


def rebuild_admins():
  """ADMINS = env admins + owner + admins added from the bot."""
  ids = _env_admins() + ([Vars.OWNER] if Vars.OWNER else []) + list(cfg.get("admins") or [])
  Vars.ADMINS[:] = list(dict.fromkeys(ids))


def set_extra_admins(ids):
  set_config("admins", list(dict.fromkeys(ids)))
  rebuild_admins()


def locked_sources() -> list:
  """Source ids (Web.sf) that are Premium-only. Empty = everything is free."""
  return list(cfg.get("locked_sources") or [])


def set_locked_sources(sfs):
  set_config("locked_sources", list(dict.fromkeys(sfs)))


def fsb_list() -> list:
  from bot import parse_fsb
  val = cfg.get("fsb")
  return val if isinstance(val, list) else parse_fsb(Vars.FORCE_SUB_CHANNEL)


def set_fsb_list(items):
  set_config("fsb", items)


def emoji_map() -> dict:
  return dict(cfg.get("emoji") or {})


def set_emoji_map(m):
  set_config("emoji", m)
