"""
/backup and /restore — export/import the entire database as one JSON file.

Covers every table automatically (users, premium, posts, tokens, config,
scheduled posts, auto-publish list, and anything added later), since
Tools.db.export_all()/import_all() walk every registered Table.

Also runs an automatic periodic backup to the Log Channel - see
/settings -> 💾 Auto-Backup for the on/off switch and interval.
"""
import asyncio
import json
import os
import time

from pyrogram import filters

from bot import Bot, Vars, admin_only, logger
from Tools.db import export_all, import_all, get_config, set_config
from .storage import retry_on_flood, igrone_error

BACKUP_DIR = "/tmp"


async def _build_and_send_backup(chat_id, note: str = "") -> bool:
    """Build a fresh backup and send it to `chat_id`. Returns True on success."""
    dump = export_all()
    path = os.path.join(BACKUP_DIR, f"manwa_backup_{int(time.time())}.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(dump, f, ensure_ascii=False)

        size_kb = os.path.getsize(path) / 1024
        counts = ", ".join(f"{k}: {len(v)}" for k, v in dump.items()) or "empty database"
        caption = f"<b>📦 {'Automatic ' if note else ''}Backup</b> ({size_kb:.1f} KB)\n\n<code>{counts}</code>\n\n"
        if note:
            caption += f"<i>{note}</i>\n\n"
        caption += "⚠️ Keep this safe — it contains user data. To restore it, reply to this file with /restore."

        await retry_on_flood(Bot.send_document)(chat_id, path, caption=caption)
        return True
    except Exception as e:
        logger.exception(f"Backup send failed: {e}")
        return False
    finally:
        if os.path.exists(path):
            os.remove(path)


@Bot.on_message(filters.command("backup") & admin_only)
async def backup_cmd(client, message):
    sts = await retry_on_flood(message.reply_text)("<i>📦 Building backup...</i>", quote=True)
    ok = await _build_and_send_backup(message.chat.id)
    if ok:
        await igrone_error(sts.delete)()
    else:
        await igrone_error(sts.edit_text)("❌ Backup failed — check the bot's logs.")


async def auto_backup_loop():
    """Sends a full database backup to the Log Channel automatically, every
    Vars.BACKUP_INTERVAL_DAYS days - see /settings -> 💾 Auto-Backup.
    Checked hourly (cheap - just one config read) so a changed interval or
    a bot restart near the due time doesn't drift far off schedule, without
    needing a separate precise scheduler."""
    while True:
        try:
            if Vars.BACKUP_ENABLED and Vars.LOG_CHANNEL:
                last = get_config("last_auto_backup", 0)
                interval_secs = max(1, int(Vars.BACKUP_INTERVAL_DAYS or 1)) * 86400
                if time.time() - last >= interval_secs:
                    ok = await _build_and_send_backup(
                        Vars.LOG_CHANNEL,
                        note=f"Runs automatically every {Vars.BACKUP_INTERVAL_DAYS} day(s) — /settings to change.",
                    )
                    if ok:
                        set_config("last_auto_backup", int(time.time()))
        except Exception as e:
            logger.exception(f"Auto-backup loop error: {e}")
        finally:
            await asyncio.sleep(3600)


@Bot.on_message(filters.command("restore") & admin_only)
async def restore_cmd(client, message):
    reply = message.reply_to_message
    doc = (reply.document if reply else None) or message.document
    if not doc:
        return await retry_on_flood(message.reply_text)(
            "❌ Reply to a backup <code>.json</code> file with /restore "
            "(or attach the file with /restore as the caption).\n\n"
            "Add <code>overwrite</code> after the command to force-replace "
            "existing entries instead of just filling in missing ones.",
            quote=True,
        )

    overwrite = "overwrite" in (message.text or message.caption or "").lower()
    sts = await retry_on_flood(message.reply_text)("<i>📥 Downloading backup...</i>", quote=True)

    try:
        path = await client.download_media(reply or message)
    except Exception as e:
        logger.exception(e)
        return await igrone_error(sts.edit_text)(f"❌ Couldn't download that file: {e}")

    try:
        with open(path, encoding="utf-8") as f:
            dump = json.load(f)
    except Exception as e:
        return await igrone_error(sts.edit_text)(f"❌ That doesn't look like a valid backup file: {e}")
    finally:
        if os.path.exists(path):
            os.remove(path)

    if not isinstance(dump, dict):
        return await igrone_error(sts.edit_text)("❌ That doesn't look like a valid backup file.")

    await igrone_error(sts.edit_text)(
        "<i>⚠️ Restoring in OVERWRITE mode — existing entries will be replaced...</i>"
        if overwrite else
        "<i>📥 Restoring (merge mode — only filling in missing entries)...</i>"
    )

    try:
        stats = import_all(dump, overwrite=overwrite)
    except Exception as e:
        logger.exception(e)
        return await igrone_error(sts.edit_text)(f"❌ Restore failed: {e}")

    if not stats:
        return await igrone_error(sts.edit_text)(
            "✅ Restore finished — nothing new to write "
            "(use <code>/restore overwrite</code> to force-replace existing entries)."
        )

    lines = "\n".join(f"• {k}: +{v}" for k, v in stats.items())
    await igrone_error(sts.edit_text)(f"<b>✅ Restore complete</b>\n\n{lines}")
