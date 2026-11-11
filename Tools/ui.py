"""
The bot's design system: coloured buttons + the text helpers every screen uses,
so the whole bot reads as one product instead of a pile of screens.

Coloured buttons
----------------
Telegram's Bot API (9.4+) lets an inline button carry a `style`: "primary"
(blue), "success" (green) or "danger" (red). Whether it can actually be sent
depends on the pyrogram build installed, so this module never assumes:

* `probe()` runs once at startup and tries to *serialise* a styled button the
  way the installed library wants it (enum member or plain string). If that
  works, buttons are coloured ("native" mode); if the library predates button
  styles, every button silently stays plain ("plain" mode). Nothing can break
  a screen either way.
* `btn()` is a drop-in for `InlineKeyboardButton(...)`. Given no explicit
  style it picks one from the label (see `infer`) so the colours stay
  consistent everywhere: green = go / confirm, red = destructive, blue =
  the main action on a screen, uncoloured = navigation.
"""
import html
import inspect
import re
import time

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot import logger

PRIMARY, SUCCESS, DANGER = "primary", "success", "danger"

_encode = None      # None -> plain buttons, else fn(style_name) -> what the library expects
mode = "plain"      # "native" | "plain" (what probe() decided)


# --------------------------------------------------------------------------- #
# coloured buttons
# --------------------------------------------------------------------------- #
async def probe(client=None) -> str:
  """Decide once, at startup, whether (and how) this pyrogram build can send
  button colours. Serialises a real styled button to find out - a library that
  accepts the argument but can't encode it would otherwise break every
  keyboard at send time."""
  global _encode, mode
  _encode, mode = None, "plain"
  try:
    if "style" not in inspect.signature(InlineKeyboardButton.__init__).parameters:
      logger.info("Button colours: this pyrogram build has no button styles - using plain buttons")
      return mode
  except (TypeError, ValueError):
    return mode

  candidates = []
  try:
    from pyrogram import enums
    enum = getattr(enums, "ButtonStyle", None)
    if enum is not None:
      candidates.append(lambda name, e=enum: getattr(e, name.upper()))
  except Exception:
    pass
  candidates.append(lambda name: name)             # the Bot API's plain string

  for enc in candidates:
    try:
      markup = InlineKeyboardMarkup([[InlineKeyboardButton("·", callback_data="probe", style=enc(PRIMARY))]])
      res = markup.write(client)
      if inspect.isawaitable(res):
        await res
    except Exception:
      continue
    _encode, mode = enc, "native"
    break
  logger.info(f"Button colours: {mode}")
  return mode


_LEAD_DANGER = ("❌", "🗑", "🚫", "⛔", "🧹")
_LEAD_SUCCESS = ("✅", "🟢", "➕", "📥", "🔔", "💾", "🔓")
_LEAD_PRIMARY = ("📖", "🔎", "🔍", "💎", "📚", "🚀", "⭐")
_W_DANGER = re.compile(r"\b(cancel|delete|remove|revoke|clear|clean|unsubscribe|disable|wipe|reset)\b", re.I)
_W_SUCCESS = re.compile(r"\b(confirm|publish|subscribe|download|save|enable|grant|apply|activate|extend|create|add)\b", re.I)
_W_PRIMARY = re.compile(r"\b(read|search|upgrade|buy)\b|go premium", re.I)


def infer(text: str):
  """The colour a button gets when the caller doesn't say: decided from the
  label's leading emoji or its first words, so it's predictable."""
  t = (text or "").strip()
  if not t:
    return None
  core = re.sub(r"^[^\w]+", "", t)
  head = " ".join(core.split()[:2])
  if t.startswith(_LEAD_DANGER) or _W_DANGER.search(head):
    return DANGER
  if t.startswith(_LEAD_SUCCESS) or _W_SUCCESS.search(head):
    return SUCCESS
  if t.startswith(_LEAD_PRIMARY) or _W_PRIMARY.search(head):
    return PRIMARY
  return None


def btn(text, callback_data=None, url=None, *, style="auto", **kw):
  """Drop-in for InlineKeyboardButton. style: "auto" (infer from the label),
  PRIMARY / SUCCESS / DANGER, or None for a deliberately plain button."""
  if style == "auto":
    style = infer(text)
  if style and _encode is not None:
    kw["style"] = _encode(style)
  if callback_data is not None:
    kw["callback_data"] = callback_data
  if url is not None:
    kw["url"] = url
  return InlineKeyboardButton(text, **kw)


def kb(*rows):
  """kb([btn, btn], [btn]) -> InlineKeyboardMarkup. Empty rows are dropped."""
  return InlineKeyboardMarkup([list(r) for r in rows if r])


def nav(back=None, close=False):
  """The standard bottom row: ⇦ Back and/or ✖ Close."""
  row = []
  if back:
    row.append(btn("⇦ Back", callback_data=back, style=None))
  if close:
    row.append(btn("✖ Close", callback_data="kclose", style=None))
  return row


# --------------------------------------------------------------------------- #
# text helpers - one voice for every screen
# --------------------------------------------------------------------------- #
RULE = "┈┈┈┈┈┈┈┈┈┈┈┈"


def esc(value) -> str:
  return html.escape(str(value))


def title(emoji: str, text: str, sub: str = "") -> str:
  """Screen header: bold title, optional muted subtitle."""
  out = f"<b>{emoji} {esc(text)}</b>"
  return out + (f"\n<i>{sub}</i>" if sub else "")


def kv(label: str, value, emoji: str = "") -> str:
  """'Label  value' row. `value` must already be escaped/formatted."""
  return f"{emoji + ' ' if emoji else ''}<b>{esc(label)}</b>  {value}"


def quote(text: str, expandable: bool = False) -> str:
  return f"<blockquote{' expandable' if expandable else ''}>{text}</blockquote>"


def plural(n, word: str, many: str = None) -> str:
  return f"{n} {word if n == 1 else (many or word + 's')}"


def fmt_left(seconds) -> str:
  """'23 days', '1 day 5 h', '5 h 10 min', '12 min' - never a bare number.
  Whole days round UP, like any subscription countdown: a 30-day plan granted a
  moment ago reads "30 days", not "29 days"."""
  s = max(0, int(seconds))
  if s >= 2 * 86400:
    return plural(-(-s // 86400), "day")
  d, rem = divmod(s, 86400)
  h, rem = divmod(rem, 3600)
  m = rem // 60
  if d == 1:
    return f"1 day {h} h" if h else "1 day"
  if h:
    return f"{h} h {m} min" if m else f"{h} h"
  return f"{max(m, 1)} min"


def fmt_date(ts) -> str:
  return time.strftime("%d %b %Y", time.gmtime(ts))


def fmt_datetime(ts) -> str:
  return time.strftime("%d %b %Y, %H:%M UTC", time.gmtime(ts))


def fmt_uptime(seconds) -> str:
  s = max(0, int(seconds))
  d, rem = divmod(s, 86400)
  h, rem = divmod(rem, 3600)
  m = rem // 60
  return f"{d}d {h}h {m}m" if d else (f"{h}h {m}m" if h else f"{m}m")


# --------------------------------------------------------------------------- #
# forwarded-message helpers (forward_from / forward_from_chat are deprecated -
# newer pyrogram builds expose `forward_origin`; fall back for older ones)
# --------------------------------------------------------------------------- #
def forwarded_user_id(msg):
  origin = getattr(msg, "forward_origin", None)
  if origin is not None:
    user = getattr(origin, "sender_user", None)
    if user is not None and getattr(user, "id", None):
      return user.id
    return None
  user = getattr(msg, "forward_from", None)
  return getattr(user, "id", None) if user else None


def forwarded_chat_id(msg):
  origin = getattr(msg, "forward_origin", None)
  if origin is not None:
    for attr in ("chat", "sender_chat"):
      chat = getattr(origin, attr, None)
      if chat is None:
        continue
      cid = getattr(chat, "id", None) or getattr(getattr(chat, "sender_chat", None), "id", None)
      if cid:
        return cid
    return None
  chat = getattr(msg, "forward_from_chat", None)
  return getattr(chat, "id", None) if chat else None


def mini_bar(done: int, total: int, width: int = 10) -> str:
  """▰▰▰▱▱▱▱▱▱▱ - a compact progress strip."""
  total = max(total, 1)
  filled = min(width, int(width * done / total))
  return "▰" * filled + "▱" * (width - filled)
