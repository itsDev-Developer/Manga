"""Post channels - one profile per channel the bot publishes to.

Every profile has its OWN
  * 💎 Premium button link   (+ label)
  * 💾 Back-up button link   (+ label)
  * 📖 Read Now button label
  * 🗄 File Store Bot API    (URL + key, so each channel can use a different File Store Bot)

A post goes to whichever channels are ticked on its preview; each channel gets
its own buttons and its own File Store link.

Storage: one list in the config table under "post_channels" (so /backup and
/restore carry it automatically).

The "main" channel is the pre-existing single Post Channel (/settings -> Channels
-> Post Channel, `Vars.POST_CHANNEL`). It always exists, so nothing configured
before this feature stops working: it keeps using the global File Store settings
until it is given File Store settings of its own.
"""
import re
import secrets

from bot import Vars
from Tools.db import cfg, set_config
from Tools.filestore_api import FS, global_fs

KEY = "post_channels"
MAIN = "main"

DEFAULT_PREMIUM_LABEL = "💎 Premium"
DEFAULT_BACKUP_LABEL = "💾 Back-up"
LABEL_MAX = 32

_BLANK = {
  "name": "",
  "enabled": True,
  "read_label": "",          # empty -> the global "Button text" (/settings -> Post Design)
  "premium_url": "", "premium_label": "",
  "backup_url": "", "backup_label": "",
  "read_style": "", "premium_style": "", "backup_style": "",   # "" -> the global colour (/settings -> Post Design)
  "fs_enabled": False, "fs_url": "", "fs_key": "", "fs_username": "",
}
EDITABLE = tuple(_BLANK)


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #
def _records() -> list:
  raw = cfg.get(KEY)
  return [dict(r) for r in raw if isinstance(r, dict) and r.get("id")] if isinstance(raw, list) else []


def _save(records: list):
  set_config(KEY, records)


def _resolve(rec: dict) -> dict:
  ch = {**_BLANK, **rec}
  if ch["id"] == MAIN:
    ch["chat"] = Vars.POST_CHANNEL
    ch["name"] = rec.get("name") or "Main channel"
  else:
    ch["name"] = rec.get("name") or str(rec.get("chat") or "Channel")
  ch["is_main"] = ch["id"] == MAIN
  return ch


def all_channels() -> list:
  """Every profile, main first. Always contains the main channel."""
  recs = _records()
  main = next((r for r in recs if r["id"] == MAIN), {"id": MAIN})
  return [_resolve(main)] + [_resolve(r) for r in recs if r["id"] != MAIN]


def active_channels() -> list:
  """Profiles that can be posted to right now: switched on AND a channel is set."""
  return [c for c in all_channels() if c["enabled"] and c.get("chat") not in (None, "", 0)]


def default_ids() -> list:
  return [c["id"] for c in active_channels()]


def get_channel(cid: str):
  return next((c for c in all_channels() if c["id"] == cid), None)


def chats_of_post(post: dict) -> list:
  """Every (chat, message_id) a stored post was published to - new multi-channel
  records and old single-channel ones alike."""
  out = []
  for item in post.get("channels") or []:
    if item.get("chat") not in (None, "") and item.get("msg_id"):
      out.append((_chat_value(item["chat"]), int(item["msg_id"])))
  if not out and post.get("post_channel") and post.get("post_msg_id"):
    out.append((_chat_value(post["post_channel"]), int(post["post_msg_id"])))
  return out


def _chat_value(value):
  try:
    return int(value)
  except (TypeError, ValueError):
    return value


def add_channel(chat, name: str = ""):
  """-> (channel, error). Refuses a channel that's already in the list."""
  chat = _chat_value(chat)
  for c in all_channels():
    if c.get("chat") not in (None, "") and str(c["chat"]).lower() == str(chat).lower():
      return None, f"That channel is already in the list as “{c['name']}”."
  recs = _records()
  ids = {r["id"] for r in recs}
  cid = secrets.token_hex(3)
  while cid in ids or cid == MAIN:
    cid = secrets.token_hex(3)
  rec = {"id": cid, "chat": chat, "name": (name or str(chat))[:40]}
  recs.append(rec)
  _save(recs)
  return get_channel(cid), None


def update_channel(cid: str, **fields):
  """Change profile fields. Unknown field names are ignored. The channel itself
  (`chat`) of a non-main profile can be changed too."""
  recs = _records()
  rec = next((r for r in recs if r["id"] == cid), None)
  if rec is None:
    if cid != MAIN:
      return None
    rec = {"id": MAIN}
    recs.append(rec)
  for k, v in fields.items():
    if k in EDITABLE or (k == "chat" and cid != MAIN):
      rec[k] = v
  _save(recs)
  return get_channel(cid)


def delete_channel(cid: str) -> bool:
  if cid == MAIN:
    return False
  recs = _records()
  kept = [r for r in recs if r["id"] != cid]
  if len(kept) == len(recs):
    return False
  _save(kept)
  return True


# --------------------------------------------------------------------------- #
# File Store (one per channel)
# --------------------------------------------------------------------------- #
def has_own_fs(ch: dict) -> bool:
  return bool(ch.get("fs_url") or ch.get("fs_key"))


def fs_for(ch: dict):
  """The File Store Bot this channel uses -> an FS, or None (= this bot delivers).

  * a channel with its own URL/key uses them (switch: its own "File Store" toggle);
  * the main channel with none of its own falls back to the global
    /settings -> 🗄 File Store Bot values - the behaviour from before channels existed;
  * any other channel with none of its own is delivered by this bot."""
  if has_own_fs(ch):
    return FS(ch["fs_url"], ch["fs_key"], ch["fs_enabled"], ch["fs_username"], ch["name"])
  if ch["id"] == MAIN:
    g = global_fs()
    g.label = ch["name"]
    return g
  return None


def fs_summary(ch: dict) -> str:
  """One human line: where does this channel's Read Now button lead?"""
  fs = fs_for(ch)
  if fs is None:
    return "this bot delivers the files"
  who = f"@{fs.username}" if fs.username else (fs.base or "File Store Bot")
  src = "own" if has_own_fs(ch) else "global settings"
  if not fs.has_credentials:
    return f"not configured ({src}) → this bot delivers"
  if not fs.enabled:
    return f"{who} · switched off ({src}) → this bot delivers"
  return f"{who} · {src}"


# --------------------------------------------------------------------------- #
# buttons
# --------------------------------------------------------------------------- #
_URL_OK = re.compile(r"^(?:https?|tg)://\S+$", re.I)


def normalize_url(text):
  """A usable button URL from what an admin typed, or None.
  Accepts https://…, tg://…, t.me/…, and @username."""
  t = (text or "").strip()
  if not t:
    return None
  if re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{3,31}", t):
    return "https://t.me/" + t[1:]
  if re.match(r"^(?:t|telegram)\.me/", t, re.I):
    t = "https://" + t
  return t if _URL_OK.match(t) and len(t) <= 512 else None


def read_label(ch: dict) -> str:
  return (ch.get("read_label") or Vars.POST_BUTTON or "📖 Read Now")[:LABEL_MAX]


def premium_label(ch: dict) -> str:
  return (ch.get("premium_label") or DEFAULT_PREMIUM_LABEL)[:LABEL_MAX]


def backup_label(ch: dict) -> str:
  return (ch.get("backup_label") or DEFAULT_BACKUP_LABEL)[:LABEL_MAX]


# Button colours --------------------------------------------------------------
STYLE_VALUES = ("primary", "success", "danger", "none", "auto")
STYLE_NAMES = {"primary": "🔵 Blue", "success": "🟢 Green", "danger": "🔴 Red", "none": "⚪ None", "auto": "✨ Auto"}
STYLE_KINDS = {"read": "📖 Read", "premium": "💎 Premium", "backup": "💾 Back-up"}


def style_value(ch: dict, kind: str) -> str:
  """The colour setting in force for one button: this channel's own, else the global one."""
  own = (ch.get(f"{kind}_style") or "").strip().lower()
  if own in STYLE_VALUES:
    return own
  glob = str(getattr(Vars, f"POST_{kind.upper()}_STYLE", "auto") or "auto").strip().lower()
  return glob if glob in STYLE_VALUES else "auto"


def style_for(ch: dict, kind: str):
  """What Tools.ui.btn(style=...) wants: "auto", "primary"/"success"/"danger", or None (plain)."""
  v = style_value(ch, kind)
  return None if v == "none" else v


def next_style(ch: dict, kind: str) -> str:
  """Tap-to-cycle order for the channel colour editor: global -> blue -> green -> red -> none -> auto -> global."""
  cur = (ch.get(f"{kind}_style") or "").strip().lower()
  order = ["", "primary", "success", "danger", "none", "auto"]
  return order[(order.index(cur) + 1) % len(order)] if cur in order else ""


def button_specs(ch: dict, read_url: str) -> list:
  """[[(text, url, style), ...], ...] - Read Now on top, Premium + Back-up (when this
  channel has them) side by side underneath. `style` is ready for Tools.ui.btn."""
  rows = [[(read_label(ch), read_url, style_for(ch, "read"))]]
  extra = []
  if ch.get("premium_url"):
    extra.append((premium_label(ch), ch["premium_url"], style_for(ch, "premium")))
  if ch.get("backup_url"):
    extra.append((backup_label(ch), ch["backup_url"], style_for(ch, "backup")))
  if extra:
    rows.append(extra)
  return rows
