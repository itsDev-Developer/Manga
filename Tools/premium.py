"""
Premium plans - the single place that grants, extends, revokes and reports
premium. The /premium panel, the admin commands and the Premium API all call
into this module, so they cannot drift apart.

Data (Tools.db `premium` table, same shape as always so existing users keep working):
    {user_id: {"expiration_timestamp": unix, "Days": last_grant_days,
               "permanent": bool, "since": unix, "source": "...",
               "reminded": expiry_we_warned_about}}

A user is premium while `expiration_timestamp` is in the future. A **permanent**
plan stores a far-future timestamp (PERMANENT_TS) plus `permanent: true`, so every
older check that only compares the timestamp (`is_premium`, the sweep...) keeps
working unchanged - and a permanent plan can never be swept as "expired".
"""
import asyncio
import math
import random
import string
import time

from pyrogram.errors import FloodWait

from bot import Bot, Vars, logger
from Tools import db
from Tools.db import aref, plog, pts, uts
from Tools.ui import fmt_date, fmt_left

MAX_DAYS = 3650
PERMANENT_TS = 4102444800          # 2100-01-01 UTC - the "never" a permanent plan stores
LOG_KEEP_DAYS = 90
REF_KEEP_DAYS = 30
MODES = ("extend", "replace")


class PremiumError(Exception):
  """A request that can't be done - carries a machine-readable code."""

  def __init__(self, code: str, message: str):
    super().__init__(message)
    self.code, self.message = code, message


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #
def _uid(user_id) -> str:
  if isinstance(user_id, bool) or (isinstance(user_id, float) and not user_id.is_integer()):
    raise PremiumError("invalid_user_id", "user_id must be a positive integer.")
  try:
    n = int(user_id)
  except (TypeError, ValueError):
    raise PremiumError("invalid_user_id", "user_id must be a positive integer.")
  if n <= 0:
    raise PremiumError("invalid_user_id", "user_id must be a positive integer.")
  return str(n)


def _row(uid, data, now=None) -> dict:
  """One user's plan. `expires_at` is None for a permanent plan (there's no end)
  and for someone who isn't premium."""
  now = now or time.time()
  data = data or {}
  exp = int(data.get("expiration_timestamp", 0))
  active = exp > now
  permanent = bool(active and data.get("permanent"))
  timed = active and not permanent
  return {
    "user_id": int(uid),
    "premium": active,
    "permanent": permanent,
    "expires_at": exp if timed else None,
    "days_left": round((exp - now) / 86400, 2) if timed else None,
    "since": data.get("since"),
  }


def days_left_whole(row):
  """Whole days remaining, rounded up (a fresh 30-day plan reads 30) - what the API reports."""
  return None if row["days_left"] is None else max(0, math.ceil(row["days_left"]))


def status(user_id) -> dict:
  uid = _uid(user_id)
  return _row(uid, pts.get(uid))


def active_users(expiring_within_days=None, sort="expiry") -> list:
  """Active premium users. sort="expiry": soonest first, permanent last (the
  manager's lists); sort="id": ascending Telegram id (the API's order)."""
  now = time.time()
  rows = []
  for uid, data in list(pts.items()):
    row = _row(uid, data, now)
    if not row["premium"]:
      continue
    if expiring_within_days is not None and (row["permanent"] or row["expires_at"] - now > expiring_within_days * 86400):
      continue
    rows.append(row)
  if sort == "id":
    rows.sort(key=lambda r: r["user_id"])
  else:
    rows.sort(key=lambda r: (r["permanent"], r["expires_at"] or 0))
  return rows


def stats() -> dict:
  return {
    "premium_active": len(active_users()),
    "expiring_7d": len(active_users(expiring_within_days=7)),
    "users_total": len(uts),
  }


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #
def _log(action, uid, source, days=None, expires=None, ref=None):
  key = f"{int(time.time() * 1000):013d}{''.join(random.choices(string.ascii_lowercase, k=3))}"
  plog[key] = {"ts": int(time.time()), "user": int(uid), "action": action, "days": days,
               "expires": expires, "source": source, "ref": ref}
  plog.touch(key)


def _check_days(days):
  if isinstance(days, bool) or (isinstance(days, float) and not days.is_integer()):
    raise PremiumError("invalid_days", "days must be a whole number.")
  try:
    days = int(days)
  except (TypeError, ValueError):
    raise PremiumError("invalid_days", "days must be a whole number.")
  if not 1 <= days <= MAX_DAYS:
    raise PremiumError("invalid_days", f"days must be between 1 and {MAX_DAYS}.")
  return days


async def grant(user_id, days=None, mode="extend", source="admin", ref=None, notify=None) -> dict:
  """Give premium.

  days=None  -> a PERMANENT plan.
  mode "extend" (default) adds `days` on top of any time the user still has;
  mode "replace" makes it exactly `days` from now.

  Returns the user's new status plus:
    action               activated | extended | replaced | made_permanent | unchanged
    previous_expires_at  the end of the plan that was running before (unix), else None
    notified             whether the user was messaged
  """
  uid = _uid(user_id)
  if days is not None:
    days = _check_days(days)
  if mode not in MODES:
    raise PremiumError("invalid_mode", 'mode must be "extend" or "replace".')

  now = int(time.time())
  cur = pts.get(uid) or {}
  cur_exp = int(cur.get("expiration_timestamp", 0))
  was_active = cur_exp > now
  was_permanent = was_active and bool(cur.get("permanent"))
  previous = cur_exp if (was_active and not was_permanent) else None

  new_exp, permanent = None, False
  if days is None:                                    # permanent
    if was_permanent:
      action = "unchanged"
    else:
      action = "made_permanent" if was_active else "activated"
      new_exp, permanent = PERMANENT_TS, True
  elif mode == "extend":
    if was_permanent:
      action = "unchanged"                            # nothing can be added to "forever"
    else:
      action = "extended" if was_active else "activated"
      new_exp = (cur_exp if was_active else now) + days * 86400
  else:                                               # replace
    action = "replaced" if was_active else "activated"
    new_exp = now + days * 86400

  if action != "unchanged":
    pts[uid] = {
      "expiration_timestamp": new_exp, "Days": days or 0, "permanent": permanent,
      "since": cur.get("since") if was_active and cur.get("since") else now,
      "source": source,
    }
    pts.touch(uid)
    _log(action, uid, source, days, None if permanent else new_exp, ref)

  result = status(uid)
  result.update(action=action, previous_expires_at=previous, added_days=days,
                created=(action == "activated"), notified=False)
  if action != "unchanged" and (Vars.PREMIUM_NOTIFY if notify is None else notify):
    result["notified"] = await send(uid, granted_text(result))
  return result


async def revoke(user_id, source="admin", notify=None) -> dict:
  """Remove a plan. `removed`: a record existed and was deleted. `was_active`:
  it was a plan that still counted (an expired record not yet swept is removed
  but wasn't active)."""
  uid = _uid(user_id)
  was_active = status(uid)["premium"]
  existed = uid in pts
  if existed:
    pts.drop(uid)
    _log("revoke" if was_active else "clear", uid, source)
  result = {"user_id": int(uid), "removed": existed, "was_active": was_active, "notified": False}
  if was_active and (Vars.PREMIUM_NOTIFY if notify is None else notify):
    result["notified"] = await send(uid, ended_text(revoked=True))
  return result


# --------------------------------------------------------------------------- #
# messages to users
# --------------------------------------------------------------------------- #
_HEADLINES = {
  "activated": "Premium activated",
  "extended": "Premium extended",
  "replaced": "Premium updated",
  "made_permanent": "Premium is now permanent",
}


def granted_text(st: dict) -> str:
  head = _HEADLINES.get(st.get("action"), "Premium activated")
  if st["permanent"]:
    when = "<b>Valid</b>  Permanent ♾"
  else:
    when = (f"<b>Valid until</b>  {fmt_date(st['expires_at'])}\n"
            f"<b>Time left</b>  {fmt_left(st['expires_at'] - time.time())}")
  return (f"<b>💎 {head}</b>\n<i>Thank you for supporting the bot!</i>\n\n{when}\n\n"
          "⚡ Priority queue  ·  🔓 Premium sources\n\n<i>See /my_plan any time.</i>")


def ended_text(revoked: bool = False) -> str:
  return ("<b>💎 Your Premium has ended</b>\n\n"
          + ("<i>Your plan was removed by the owner.</i>" if revoked else "<i>Thanks for being a member.</i>")
          + "\n\nTap /my_plan to see the plans and renew.")


def reminder_text(left_seconds: float, expires_at: int) -> str:
  return (f"<b>⏳ Your Premium ends in {fmt_left(left_seconds)}</b>\n\n"
          f"<b>Valid until</b>  {fmt_date(expires_at)}\n\n"
          "<i>Renew from /my_plan to keep priority downloads and premium sources.</i>")


async def send(user_id, text) -> bool:
  """Best-effort DM - False if the user never started the bot / blocked it."""
  for attempt in (1, 2):
    try:
      await Bot.send_message(int(user_id), text)
      return True
    except FloodWait as e:
      if attempt == 2:
        return False
      await asyncio.sleep(min(e.value, 30))
    except Exception:
      return False
  return False


# --------------------------------------------------------------------------- #
# housekeeping
# --------------------------------------------------------------------------- #
async def sweep() -> int:
  """Drop expired plans and tell each user. Returns how many ended."""
  await db.remove_expired_users()
  ended = list(db.expired_pending)
  db.expired_pending.clear()
  for uid, data in ended:
    _log("expire", uid, "system", expires=data.get("expiration_timestamp"))
    if Vars.PREMIUM_NOTIFY:
      await send(uid, ended_text())
  return len(ended)


async def remind() -> int:
  """One heads-up per plan, N days before it ends (Settings -> Premium).
  Permanent plans never end, so they're never reminded."""
  days = int(Vars.PREMIUM_REMIND_DAYS or 0)
  if not days or not Vars.PREMIUM_NOTIFY:
    return 0
  now, sent = time.time(), 0
  for uid, data in list(pts.items()):
    if data.get("permanent"):
      continue
    exp = int(data.get("expiration_timestamp", 0))
    left = exp - now
    if 0 < left <= days * 86400 and data.get("reminded") != exp:
      data["reminded"] = exp           # mark first: never double-send, even if the DM fails
      pts.touch(uid)
      if await send(uid, reminder_text(left, exp)):
        sent += 1
  return sent


def prune():
  """Keep the audit trail and idempotency records from growing forever."""
  cutoff = time.time() - LOG_KEEP_DAYS * 86400
  for key in [k for k, v in plog.items() if v.get("ts", 0) < cutoff]:
    plog.drop(key)
  cutoff = time.time() - REF_KEEP_DAYS * 86400
  for key in [k for k, v in aref.items() if v.get("ts", 0) < cutoff]:
    aref.drop(key)


async def premium_loop():
  """Every 15 minutes: end expired plans, send reminders; prune old records hourly."""
  tick = 0
  while True:
    try:
      await sweep()
      await remind()
      tick += 1
      if tick % 4 == 1:
        prune()
    except Exception as e:
      logger.exception(f"premium loop: {e}")
    finally:
      await asyncio.sleep(900)


# --------------------------------------------------------------------------- #
# contact (the "Activate Premium" button)
# --------------------------------------------------------------------------- #
def contact_url() -> str:
  c = (Vars.PREMIUM_CONTACT or "").strip()
  if c.startswith("http") or c.startswith("tg://"):
    return c
  if c.startswith("@") and len(c) > 1:
    return f"https://t.me/{c[1:]}"
  if c.isdigit():
    return f"tg://user?id={c}"
  if c:
    return f"https://t.me/{c.lstrip('@')}"
  return f"tg://user?id={Vars.OWNER}"


def contact_label() -> str:
  c = (Vars.PREMIUM_CONTACT or "").strip()
  return c if c.startswith("@") else ("the owner" if not c else c)
