"""
Unused legacy file — kept only so nothing that references the filename
breaks, but it does NOT run automatically anymore.

This used to start a bare "Hello World" Flask dev server on port 8080 with
no `if __name__ == "__main__"` guard, meaning it ran the instant this file
was imported. That's dangerous: several hosting platforms (Render, Heroku
and similar) auto-detect a top-level `app.py` with a Flask `app` object and
will run *this* instead of `start.sh` - which would mean the Telegram bot
never starts at all, silently.

The bot already has its own tiny built-in health-check server for hosts
like Koyeb/Render (see bot.py, controlled by the WEBS_HOST/PORT env vars),
so this file isn't needed for that either. Left in place only for backward
compatibility; safe to delete.
"""
