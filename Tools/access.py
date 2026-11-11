"""
Source access: which manga sources are Premium-only.

An admin locks/unlocks sources in /settings -> 🔒 Source Access. A locked
source can be used by Premium members and admins; everyone else is turned
away with an upgrade prompt. Unlocked sources stay free for everybody, and
with nothing locked (the default) the bot behaves exactly as before.

Every place that lets someone reach a source asks `can_use()` - the single
rule - so the lock can't be bypassed through an old button, an inline result
or a subscription.
"""
from bot import is_admin
from Tools.config import locked_sources, set_locked_sources
from Tools.db import is_premium


def locked() -> set:
  return set(locked_sources())


def is_locked(sf) -> bool:
  return sf in locked()


def can_use(user_id, sf) -> bool:
  """Free sources: everyone. Locked sources: Premium members and admins."""
  if sf not in locked():
    return True
  return bool(user_id) and (is_admin(user_id) or is_premium(user_id))


def sources() -> list:
  """[(sf, display name), ...] for every installed source, A-Z."""
  from TG.storage import web_data           # imported lazily: TG.storage pulls in every scraper
  return [(w.sf, name.strip()) for name, w in web_data.items()]


def name_of(sf) -> str:
  return next((n for s, n in sources() if s == sf), str(sf))


def set_lock(sf, value: bool):
  cur = locked()
  (cur.add if value else cur.discard)(sf)
  set_locked_sources(sorted(cur))


def lock_all():
  set_locked_sources(sorted(s for s, _ in sources()))


def unlock_all():
  set_locked_sources([])


def denied_alert(sf) -> str:
  """Text for a callback alert (Telegram caps these at 200 characters)."""
  return f"🔒 {name_of(sf)} is a Premium source.\nTap 💎 Premium to unlock it."


def denied_text(sf) -> str:
  return (f"<b>🔒 {name_of(sf)} is a Premium source</b>\n\n"
          "<i>Premium members get access to every source, plus priority downloads. "
          "Free sources stay free.</i>")
