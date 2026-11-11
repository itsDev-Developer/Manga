"""
Client for the File Store Bot's Link Generation API (see API_DOCS.md).

Turns Dump Channel message(s) into a monetized delivery link, automatically -
no manual "upload it there yourself and paste the link back" step needed.

How this fits together: this bot's Dump Channel is not a channel the File
Store Bot already stores in, so every request here automatically falls into
"copy" mode (per the docs) - the File Store Bot copies the message(s)
server-side the first time a link is requested. For that to work, **the
File Store Bot must be added as an admin to this bot's Dump Channel** so it
can read what it's copying. See the 🗄 File Store Bot settings screen
(/settings) for the full setup checklist and a "🔌 Test Connection" button
that calls /api/v1/ping.

Every function here fails soft: on any error (not configured, network
error, bad response, rate limited, etc.) it logs a warning and returns
None rather than raising, so a File Store Bot hiccup never blocks a post
from publishing - callers fall back to this bot's own delivery instead.
"""
import asyncio

import requests

from bot import Vars, logger

_TIMEOUT_SINGLE = 30    # seconds - one copy/url/upload
_TIMEOUT_BATCH = 120    # seconds - the docs note batch writes are one at a time
_MAX_BATCH = 100        # the API's own per-request limit


class FS:
    """One File Store Bot connection (API base URL + key).

    Multi-channel posting: every post channel can have its own File Store Bot, so
    each function below takes an optional `fs`. Leave it out and the global
    /settings -> 🗄 File Store Bot values are used, exactly as before."""
    __slots__ = ("url", "key", "enabled", "username", "label")

    def __init__(self, url="", key="", enabled=True, username="", label=""):
        self.url = (url or "").strip()
        self.key = (key or "").strip()
        self.enabled = bool(enabled)
        self.username = (username or "").strip().lstrip("@")
        self.label = label or ""

    @property
    def base(self) -> str:
        return self.url.rstrip("/")

    @property
    def has_credentials(self) -> bool:
        return bool(self.url and self.key)

    @property
    def ready(self) -> bool:
        return bool(self.enabled and self.has_credentials)

    @property
    def cache_key(self) -> tuple:
        """Two channels pointing at the same File Store Bot share one generated link."""
        return (self.base, self.key)

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.key}"}


def global_fs() -> FS:
    """The File Store Bot configured under /settings -> 🗄 File Store Bot."""
    return FS(Vars.FILESTORE_API_URL, Vars.FILESTORE_API_KEY, Vars.FILESTORE_ENABLED,
              Vars.FILESTORE_BOT_USERNAME, "global")


def configured(fs: FS = None) -> bool:
    return (fs or global_fs()).ready


async def _request(fs: FS, method: str, path: str, timeout: int, **kw) -> dict:
    """One HTTP call with a single retry on 429 (capped wait), per the docs'
    'Good habits' section. Always returns a dict: the API's JSON body on
    success (`ok` true), or {"ok": False, "error": code, "message": ...} on
    any failure - API errors keep their real code (e.g. `not_contiguous`) so
    callers can react to them. Never raises."""
    url = f"{fs.base}{path}"
    fn = requests.post if method == "POST" else requests.get

    for attempt in range(2):
        try:
            resp = await asyncio.to_thread(fn, url, headers=fs.headers, timeout=timeout, **kw)
        except requests.RequestException as e:
            logger.warning(f"File Store API request failed ({path}): {e}")
            return {"ok": False, "error": "network", "message": str(e)}

        if resp.status_code == 429 and attempt == 0:
            wait = 5
            try:
                wait = min(float(resp.headers.get("Retry-After", 5)), 10)
            except (TypeError, ValueError):
                pass
            await asyncio.sleep(wait)
            continue

        try:
            data = resp.json()
        except ValueError:
            logger.warning(f"File Store API returned non-JSON ({path}): HTTP {resp.status_code}")
            return {"ok": False, "error": "non_json", "message": f"HTTP {resp.status_code}"}

        if not data.get("ok"):
            return {"ok": False, "error": data.get("error"), "message": data.get("message"), "status": resp.status_code}
        return data

    return {"ok": False, "error": "rate_limited", "message": "still rate limited after one retry"}


async def ping(fs: FS = None) -> dict:
    """GET /api/v1/ping - used by the Test Connection buttons. Deliberately only
    requires the URL+key to be set, not the "enabled" toggle - so an admin can
    verify the connection works before switching it on for real posts. Returns
    the raw response dict (bot, key_label, version) on success, or None."""
    fs = fs or global_fs()
    if not fs.has_credentials:
        return None
    data = await _request(fs, "GET", "/api/v1/ping", 15)
    if not data.get("ok"):
        logger.warning(f"File Store API ping failed ({fs.label or fs.base}): {data.get('error')} - {data.get('message')}")
        return None
    return data


def _is_contiguous(ids: list) -> bool:
    ids = sorted(ids)
    return ids[-1] - ids[0] == len(ids) - 1


async def create_link(dump_channel_id: int, message_ids: list, fs: FS = None) -> str:
    """Turn one or more storage-channel messages into a File Store Bot link.
    Returns the link string, or None if not configured / over the batch
    limit / the call failed for any reason - callers should fall back to
    this bot's own delivery in that case.

    Batches, in order of preference:
      1. `message_ids` - when the ids are one contiguous run. If the File
         Store Bot stores in this channel this is "reference" mode: nothing
         is copied, the link just encodes the range. (Post flows write a
         post's files as an exclusive contiguous run precisely so this works.)
      2. `items` - anything else, or if (1) is rejected as `not_contiguous`.
         The API copies exactly those messages into a clean range of its own.
         Always works, but costs a second copy - the fallback, not the plan.
    """
    fs = fs or global_fs()
    ids = list(dict.fromkeys(message_ids or []))     # drop duplicates, keep order
    if not fs.ready or not dump_channel_id or not ids:
        return None
    if len(ids) > _MAX_BATCH:
        logger.warning(
            f"File Store API: {len(ids)} files is over the {_MAX_BATCH}-per-link "
            "limit - falling back to this bot's own delivery for this post."
        )
        return None

    if len(ids) == 1:
        data = await _request(
            fs, "POST", "/api/v1/links/single", _TIMEOUT_SINGLE,
            json={"source_chat_id": dump_channel_id, "message_id": ids[0]},
        )
    else:
        data = None
        if _is_contiguous(ids):
            data = await _request(
                fs, "POST", "/api/v1/links/batch", _TIMEOUT_BATCH,
                json={"source_chat_id": dump_channel_id, "message_ids": sorted(ids)},
            )
            if not data.get("ok") and data.get("error") != "not_contiguous":
                logger.warning(f"File Store API error (batch): {data.get('error')} - {data.get('message')}")
                return None
        else:
            logger.info(
                f"File Store API: {len(ids)} ids aren't one contiguous run - using 'items' (copy) for this link."
            )

        if data is None or not data.get("ok"):
            body = {"items": [{"source_chat_id": dump_channel_id, "message_id": i} for i in ids]}
            data = await _request(fs, "POST", "/api/v1/links/batch", _TIMEOUT_BATCH, json=body)
            if not data.get("ok") and data.get("status") == 409:
                # "storage channel was busy while copying a batch - just retry"
                await asyncio.sleep(2)
                data = await _request(fs, "POST", "/api/v1/links/batch", _TIMEOUT_BATCH, json=body)

        if data.get("ok") and data.get("skipped"):
            logger.warning(f"File Store API skipped {len(data['skipped'])} item(s): {data['skipped']}")

    if not data.get("ok"):
        logger.warning(f"File Store API error: {data.get('error')} - {data.get('message')}")
        return None
    return data.get("link")
