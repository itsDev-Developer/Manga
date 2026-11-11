"""Tiny "wait for the user's next message" helper.

Replaces `client.listen`. A handler in an early group (-20) hands the next private
message of a waiting user straight to the waiting coroutine and stops the update, so
generic handlers (e.g. the "any text = search" handler) can never steal it.
"""
import asyncio

from pyrogram import filters

from bot import Bot

_pending = {}   # user_id -> (future, allowed_commands)


async def _is_waiting(_, __, message):
  user = message.from_user
  return bool(user and user.id in _pending)


def command_of(message):
  text = (message.text or message.caption or "").strip()
  if text.startswith("/"):
    return text.split()[0].split("@")[0].lower()
  return None


@Bot.on_message(filters.private & filters.incoming & filters.create(_is_waiting), group=-20)
async def _resolve(client, message):
  fut, allowed = _pending.get(message.from_user.id, (None, ()))
  if not fut or fut.done():
    return
  cmd = command_of(message)
  if cmd and cmd not in ("/cancel", *allowed):
    # any other command aborts the question and runs normally
    fut.set_result(None)
    message.continue_propagation()
  fut.set_result(message)
  message.stop_propagation()


async def ask(client, chat_id, user_id, prompt=None, timeout=120, allowed=(), **kw):
  """Optionally send `prompt`, then wait for the user's next message.

  Returns the Message, or None on timeout / when the user ran another command.
  A "/cancel" message is returned as-is (use `is_cancel`).
  """
  old = _pending.pop(user_id, None)
  if old and not old[0].done():
    old[0].set_result(None)
  fut = asyncio.get_running_loop().create_future()
  _pending[user_id] = (fut, tuple(allowed))
  try:
    if prompt:
      await client.send_message(chat_id, prompt, **kw)
    return await asyncio.wait_for(fut, timeout)
  except asyncio.TimeoutError:
    return None
  finally:
    if _pending.get(user_id, (None,))[0] is fut:
      _pending.pop(user_id, None)


def is_cancel(message) -> bool:
  return message is None or command_of(message) == "/cancel" or \
      (message.text or "").strip().lower() == "cancel"


def forwarded_chat(message):
  chat = getattr(message, "forward_from_chat", None)
  if chat is None:
    origin = getattr(message, "forward_origin", None)
    chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
  return chat
