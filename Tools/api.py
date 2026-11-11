"""
The bot's HTTP server: the health-check port that hosts like Render/Koyeb need,
plus the REST API other bots use to manage Premium and users.

The endpoints, request bodies, response bodies, scopes and error codes follow
the Link Generation API reference (API_DOCS.md) exactly, so a client written
for that API works here unchanged. This bot implements its Premium, Users,
Stats, Usage and Ping endpoints; it has no link generation, so /links/* is
not offered (a key's `links` scope is accepted for compatibility but unlocks
nothing here). Full reference for this bot: docs/PREMIUM_API.md

Deliberately dependency-free (asyncio streams, no web framework) so it costs
almost no RAM, and strict about what it accepts: bounded header and body sizes,
read timeouts, one request per connection.

Security model
--------------
* Keys are created in the bot (/newapikey or /settings -> 🔑 API Keys), shown
  once, and stored only as a SHA-256 hash.
* Each key carries scopes - `links`, `premium`, `users` - and every endpoint
  checks its scope. /ping and /usage work for any valid key.
* Per-key rate limit (60/min) plus a per-IP throttle on failed authentication.
* The whole API is off until an admin switches it on.
"""
import asyncio
import hashlib
import json
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

from bot import Bot, Vars, is_admin, logger
from Tools import db, premium
from Tools.config import hook
from Tools.db import apk, aref, uts
from Tools.premium import PremiumError

VERSION = "v1"
RATE_LIMIT = 60            # requests per minute, per key
FAIL_LIMIT = 10            # failed authentications per minute, per client IP
MAX_HEADER = 16 * 1024
MAX_BODY = 8 * 1024
READ_TIMEOUT = 10
SCOPES = ("links", "premium", "users")
KEY_PREFIX = "fsk_"
LEGACY_PREFIXES = ("mrx_",)           # keys issued by earlier versions of this bot keep working
READY = False                         # False until the bot has finished starting -> 503 server_starting


def mark_ready():
  global READY
  READY = True


# --------------------------------------------------------------------------- #
# API keys
# --------------------------------------------------------------------------- #
def _hash(key: str) -> str:
  return hashlib.sha256(key.encode()).hexdigest()


def parse_scopes(raw) -> list:
  """'links,premium' | 'all' | ['users'] -> the scopes in canonical order.
  Empty/None -> ['links'] (a new key starts with the least it can have)."""
  if raw is None or (isinstance(raw, str) and not raw.strip()):
    return ["links"]
  items = list(raw) if isinstance(raw, (list, tuple, set)) else re.split(r"[,\s]+", raw.strip().lower())
  items = [i.strip().lower() for i in items if i and i.strip()]
  if "all" in items:
    return list(SCOPES)
  bad = [i for i in items if i not in SCOPES]
  if bad or not items:
    raise ValueError(f"Unknown scope: {', '.join(bad) or '(none)'}. Use links, premium, users, or all.")
  return [s for s in SCOPES if s in items]


def key_scopes(rec: dict) -> list:
  """A key's scopes. Keys from before scopes existed keep working as `links` only
  (the first version of this bot's API used read/write - mapped below)."""
  if rec.get("scopes"):
    return [s for s in SCOPES if s in rec["scopes"]]
  return ["premium"] if rec.get("scope") == "write" else ["links"]


def create_key(label: str, scopes=None, created_by=None):
  """Returns (plain_key, key_id). The plain key is shown once and never stored."""
  scopes = parse_scopes(scopes)
  while True:
    key = KEY_PREFIX + secrets.token_urlsafe(24)
    h = _hash(key)
    if not any(x.startswith(h[:8]) for x in apk):
      break
  apk[h] = {"label": (label or "key").strip()[:40], "scopes": scopes, "prefix": key[:8],
            "created": int(time.time()), "created_by": created_by, "last_used": None, "calls": 0}
  apk.touch(h)
  return key, h[:8]


def list_keys() -> list:
  """[(key_id, record)], oldest first."""
  return sorted(((h[:8], dict(rec)) for h, rec in apk.items()), key=lambda x: x[1].get("created", 0))


def find_key(key_id: str):
  """(hash, record) for a key id (the first 8+ hex chars), or None if unknown/ambiguous."""
  key_id = (key_id or "").strip().lower()
  if len(key_id) < 8 or not re.fullmatch(r"[0-9a-f]+", key_id):
    return None
  hits = [h for h in apk if h.startswith(key_id)]
  return (hits[0], apk[hits[0]]) if len(hits) == 1 else None


def revoke_key(key_id: str) -> bool:
  found = find_key(key_id)
  if not found:
    return False
  apk.drop(found[0])
  return True


def set_scopes(key_id: str, scopes) -> bool:
  found = find_key(key_id)
  if not found:
    return False
  h, rec = found
  rec["scopes"] = parse_scopes(scopes)
  rec.pop("scope", None)
  apk.touch(h)
  return True


def authenticate(token: str):
  """(key_id, record) for a valid key, else None."""
  if not token or len(token) > 200 or not token.startswith((KEY_PREFIX,) + LEGACY_PREFIXES):
    return None
  h = _hash(token)
  rec = apk.get(h)
  if not rec:
    return None
  now = int(time.time())
  rec["calls"] = rec.get("calls", 0) + 1
  if now - (rec.get("last_used") or 0) > 60:      # don't hit the DB on every request
    rec["last_used"] = now
    apk.touch(h)
  return h[:8], rec


# --------------------------------------------------------------------------- #
# throttling
# --------------------------------------------------------------------------- #
_hits = defaultdict(deque)
_fails = defaultdict(deque)


def _trim(q, now, window=60):
  while q and now - q[0] > window:
    q.popleft()


def _rate(key_id):
  """(allowed, retry_after_seconds)"""
  now = time.monotonic()
  q = _hits[key_id]
  _trim(q, now)
  if len(q) >= RATE_LIMIT:
    return False, int(60 - (now - q[0])) + 1
  q.append(now)
  return True, 0


def _auth_blocked(ip) -> bool:
  now = time.monotonic()
  q = _fails[ip]
  _trim(q, now)
  if not q:
    _fails.pop(ip, None)
  return len(q) >= FAIL_LIMIT


def _auth_failed(ip):
  _fails[ip].append(time.monotonic())
  if len(_fails) > 2000:                              # bound memory under a spray of IPs
    now = time.monotonic()
    for k in [k for k, q in _fails.items() if not q or now - q[-1] > 60]:
      _fails.pop(k, None)


# --------------------------------------------------------------------------- #
# HTTP plumbing
# --------------------------------------------------------------------------- #
class ApiError(Exception):
  def __init__(self, status: int, code: str, message: str, headers: dict = None):
    super().__init__(message)
    self.status, self.code, self.message, self.headers = status, code, message, headers or {}


def _bad(message: str) -> ApiError:
  return ApiError(400, "invalid_request", message)


_REASONS = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
            405: "Method Not Allowed", 408: "Request Timeout", 411: "Length Required",
            413: "Payload Too Large", 429: "Too Many Requests", 431: "Request Header Fields Too Large",
            500: "Internal Server Error", 503: "Service Unavailable"}


async def _readline(reader):
  try:
    return await asyncio.wait_for(reader.readline(), READ_TIMEOUT)
  except ValueError:                       # StreamReader raises this when a line outgrows `limit`
    raise ApiError(431, "headers_too_large", "Request headers are too large.")


async def _read_request(reader):
  """(method, target, headers, body) or None if the client sent nothing."""
  line = await _readline(reader)
  if not line:
    return None
  parts = line.decode("latin-1").strip().split(" ")
  if len(parts) != 3 or not parts[2].startswith("HTTP/"):
    raise ApiError(400, "bad_request", "Malformed request line.")
  method, target = parts[0].upper(), parts[1]
  headers, total = {}, len(line)
  while True:
    h = await _readline(reader)
    total += len(h)
    if total > MAX_HEADER:
      raise ApiError(431, "headers_too_large", "Request headers are too large.")
    if h in (b"\r\n", b"\n", b""):
      break
    name, _, value = h.decode("latin-1").partition(":")
    headers[name.strip().lower()] = value.strip()
  if "chunked" in headers.get("transfer-encoding", "").lower():
    raise ApiError(411, "length_required", "Send a Content-Length instead of chunked encoding.")
  raw_len = headers.get("content-length", "0") or "0"
  if not raw_len.isdigit():
    raise ApiError(400, "bad_request", "Invalid Content-Length.")
  n = int(raw_len)
  if n > MAX_BODY:
    raise ApiError(413, "payload_too_large", f"Body must be at most {MAX_BODY} bytes.")
  body = await asyncio.wait_for(reader.readexactly(n), READ_TIMEOUT) if n else b""
  return method, target, headers, body


async def _write(writer, status, body: bytes, ctype="application/json; charset=utf-8", extra=None, head=False):
  lines = [f"HTTP/1.1 {status} {_REASONS.get(status, 'OK')}", f"Content-Type: {ctype}",
           f"Content-Length: {len(body)}", "Connection: close", "Cache-Control: no-store",
           "X-Content-Type-Options: nosniff"]
  lines += [f"{k}: {v}" for k, v in (extra or {}).items()]
  writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + (b"" if head else body))
  await writer.drain()


def _json(obj) -> bytes:
  return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _error_body(code, message) -> bytes:
  return _json({"ok": False, "error": code, "message": message})


def _client_ip(headers, writer) -> str:
  fwd = headers.get("x-forwarded-for", "")
  if fwd:
    return fwd.split(",")[0].strip()[:64]
  peer = writer.get_extra_info("peername")
  return str(peer[0]) if peer else "?"


async def _handle(reader, writer):
  try:
    try:
      req = await _read_request(reader)
      if req is None:
        return
      method, target, headers, body = req
      path = urlsplit(target).path.rstrip("/") or "/"
      if path.startswith("/read/"):
        from Tools import webreader      # the online PDF reader (/read command)
        return await webreader.handle(writer, method, path, headers, _client_ip(headers, writer))
      if not (path == "/api" or path.startswith("/api/")):
        # everything else is the platform's health check
        return await _write(writer, 200, b"OK", "text/plain", head=(method == "HEAD"))
      status, obj, extra = await _dispatch(method, urlsplit(target), headers, body, _client_ip(headers, writer))
      await _write(writer, status, _json(obj), extra=extra)
    except ApiError as e:
      await _write(writer, e.status, _error_body(e.code, e.message), extra=e.headers)
    except asyncio.TimeoutError:
      await _write(writer, 408, _error_body("timeout", "The request took too long to arrive."))
    except (asyncio.IncompleteReadError, ConnectionError):
      return
    except Exception as e:
      logger.exception(f"api: unhandled error: {e}")
      await _write(writer, 500, _error_body("internal_error", "Something went wrong on our side."))
  except Exception:
    pass
  finally:
    try:
      writer.close()
    except Exception:
      pass


# --------------------------------------------------------------------------- #
# response building - the shapes are the ones in API_DOCS.md
# --------------------------------------------------------------------------- #
def _local(ts):
  """Dates are the bot server's local time, as 2026-10-30T12:00:00 (no zone)."""
  return datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%S") if ts else None


def _plan(row: dict) -> dict:
  return {
    "premium": row["premium"],
    "permanent": row["permanent"],
    "expires_at": _local(row["expires_at"]),
    "expires_at_unix": row["expires_at"],
    "days_left": premium.days_left_whole(row),
  }


def _int_param(query, name, default, lo, hi):
  if name not in query:
    return default
  raw = query[name]
  if not re.fullmatch(r"\d{1,9}", raw or "") or not lo <= int(raw) <= hi:
    raise _bad(f"'{name}' must be an integer between {lo} and {hi}.")
  return int(raw)


def _user_id(body: dict) -> int:
  if "user_id" not in body or body["user_id"] is None:
    raise _bad("user_id is required.")
  try:
    return int(premium._uid(body["user_id"]))
  except PremiumError as e:
    raise _bad(e.message)


def _flag(body: dict, name: str) -> bool:
  value = body.get(name, False)
  if value is None:
    return False
  if not isinstance(value, bool):
    raise _bad(f"'{name}' must be true or false.")
  return value


class Ctx:
  def __init__(self, key_id, rec, query, body, match):
    self.key_id, self.rec, self.query, self.body, self.match = key_id, rec, query, body, match

  @property
  def source(self):
    return f"api:{self.rec.get('label', self.key_id)}"

  def object_body(self) -> dict:
    if not isinstance(self.body, dict):
      raise _bad("The body must be a JSON object.")
    return self.body


# --------------------------------------------------------------------------- #
# endpoints
# --------------------------------------------------------------------------- #
async def h_ping(c: Ctx):
  return {"ok": True, "bot": getattr(Bot, "username", None), "key_label": c.rec.get("label"),
          "scopes": key_scopes(c.rec), "version": VERSION}


async def h_usage(c: Ctx):
  return {"ok": True, "key_id": c.key_id, "label": c.rec.get("label"),
          "links_created": 0, "ads_completed": 0,                 # this bot has no link generation / ads
          "scopes": key_scopes(c.rec), "created_at": _local(c.rec.get("created")),
          "rate_limit_per_min": RATE_LIMIT}


_ref_lock = asyncio.Lock()


async def h_premium_add(c: Ctx):
  b = c.object_body()
  uid = _user_id(b)
  days = b.get("days")                       # omitted / null -> a permanent plan
  mode = b.get("mode") or "extend"
  if mode not in premium.MODES:
    raise _bad("mode must be 'extend' or 'replace'.")
  notify = _flag(b, "notify")
  request_id = b.get("request_id")
  if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 128):
    raise _bad("request_id must be a string of 1-128 characters.")

  async def do():
    try:
      res = await premium.grant(uid, days, mode=mode, source=c.source, ref=request_id, notify=notify)
    except PremiumError as e:
      raise _bad(e.message)
    return {"ok": True, "duplicate": False, "action": res["action"], "user_id": uid, **_plan(res),
            "previous_expires_at": _local(res["previous_expires_at"])}

  if not request_id:
    return await do()
  # `request_id` makes a retry safe: a repeat from the same key changes nothing and
  # says so with "duplicate": true. The lock closes the race between two simultaneous
  # deliveries of the same request.
  rk = hashlib.sha1(f"{c.key_id}|{request_id}".encode()).hexdigest()
  async with _ref_lock:
    prev = aref.get(rk)
    if prev:
      return {**prev["result"], "duplicate": True}
    out = await do()
    aref[rk] = {"ts": int(time.time()), "result": out}
    aref.touch(rk)
    return out


async def h_premium_remove(c: Ctx):
  b = c.object_body()
  uid = _user_id(b)
  try:
    res = await premium.revoke(uid, source=c.source, notify=_flag(b, "notify"))
  except PremiumError as e:
    raise _bad(e.message)
  return {"ok": True, "removed": res["removed"], "was_active": res["was_active"], "user_id": uid,
          "premium": False, "permanent": False, "expires_at": None, "expires_at_unix": None, "days_left": None}


async def h_premium_get(c: Ctx):
  uid = int(c.match.group(1))
  try:
    row = premium.status(uid)
  except PremiumError as e:
    raise _bad(e.message)
  return {"ok": True, "user_id": uid, **_plan(row)}


async def h_premium_list(c: Ctx):
  limit = _int_param(c.query, "limit", 50, 1, 200)
  offset = _int_param(c.query, "offset", 0, 0, 10_000_000)
  rows = premium.active_users(sort="id")           # oldest id first
  return {"ok": True, "total": len(rows), "limit": limit, "offset": offset,
          "users": [{"user_id": r["user_id"], **_plan(r)} for r in rows[offset:offset + limit]]}


async def h_user_get(c: Ctx):
  uid = int(c.match.group(1))
  try:
    row = premium.status(uid)
  except PremiumError as e:
    raise _bad(e.message)
  return {"ok": True, "user_id": uid, "started_bot": str(uid) in uts, "banned": db.is_banned(uid), "premium": _plan(row)}


async def h_ban(c: Ctx):
  uid = _user_id(c.object_body())
  if is_admin(uid) or uid == Vars.OWNER:
    raise ApiError(400, "protected_user", "Admins and the owner can't be banned.")
  return {"ok": True, "user_id": uid, "banned": True, "changed": db.ban_user(uid, by=c.source)}


async def h_unban(c: Ctx):
  uid = _user_id(c.object_body())
  return {"ok": True, "user_id": uid, "banned": False, "changed": db.unban_user(uid)}


async def h_stats(c: Ctx):
  return {"ok": True, "users": len(uts), "banned": len(db.bans), "premium_active": len(premium.active_users())}


# (method, path, handler, required scope - None = any valid key)
ROUTES = [
  ("GET",  re.compile(r"^/api/v1/ping$"),                  h_ping,           None),
  ("GET",  re.compile(r"^/api/v1/usage$"),                 h_usage,          None),
  ("POST", re.compile(r"^/api/v1/premium/add$"),           h_premium_add,    "premium"),
  ("POST", re.compile(r"^/api/v1/premium/remove$"),        h_premium_remove, "premium"),
  ("GET",  re.compile(r"^/api/v1/premium/(\d{1,20})$"),    h_premium_get,    "premium"),
  ("GET",  re.compile(r"^/api/v1/premium$"),               h_premium_list,   "premium"),
  ("GET",  re.compile(r"^/api/v1/users/(\d{1,20})$"),      h_user_get,       "users"),
  ("POST", re.compile(r"^/api/v1/users/ban$"),             h_ban,            "users"),
  ("POST", re.compile(r"^/api/v1/users/unban$"),           h_unban,          "users"),
  ("GET",  re.compile(r"^/api/v1/stats$"),                 h_stats,          "users"),
]


async def _dispatch(method, parts, headers, body, ip):
  if not Vars.API_ENABLED:
    raise ApiError(503, "api_disabled", "The API is switched off. Enable it in /settings -> Premium.")
  if not READY:
    raise ApiError(503, "server_starting", "The bot is still starting up - retry in a few seconds.", {"Retry-After": "5"})
  path = parts.path.rstrip("/") or "/"

  matched, allowed = None, []
  for m, rx, fn, scope in ROUTES:
    mm = rx.match(path)
    if mm:
      allowed.append(m)
      if m == method:
        matched = (fn, scope, mm)
  if not allowed:
    if path.startswith("/api/v1/links"):
      raise ApiError(404, "not_found", "Link generation isn't part of this bot - it offers the premium, users, stats, usage and ping endpoints.")
    raise ApiError(404, "not_found", f"No such endpoint: {path}")
  if matched is None:
    raise ApiError(405, "method_not_allowed", f"Use {', '.join(allowed)} for this endpoint.", {"Allow": ", ".join(allowed)})
  fn, scope, mm = matched

  # ---- authentication: Authorization: Bearer <key>  or  X-API-Key: <key> ------------------
  if _auth_blocked(ip):
    raise ApiError(429, "rate_limited", "Too many failed attempts - wait a minute.", {"Retry-After": "60"})
  auth_header, x_key = headers.get("authorization", ""), headers.get("x-api-key", "").strip()
  if not auth_header and not x_key:
    raise ApiError(401, "missing_api_key", "Send your key as 'Authorization: Bearer <key>' or 'X-API-Key: <key>'.",
                   {"WWW-Authenticate": "Bearer"})
  token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else x_key
  auth = authenticate(token)
  if not auth:
    _auth_failed(ip)
    raise ApiError(401, "invalid_api_key", "That API key is wrong or has been revoked.", {"WWW-Authenticate": "Bearer"})
  key_id, rec = auth

  allowed_now, retry = _rate(key_id)
  if not allowed_now:
    raise ApiError(429, "rate_limited", f"Limit is {RATE_LIMIT} requests per minute per key.", {"Retry-After": str(retry)})
  if scope and scope not in key_scopes(rec):
    raise ApiError(403, "insufficient_scope", f"This key doesn't have the '{scope}' scope.")

  # ---- payload ------------------------------------------------------------------------------------
  query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
  data = None
  if method == "POST":
    try:
      data = json.loads(body.decode("utf-8")) if body else None
    except (ValueError, UnicodeDecodeError):
      data = None
    if data is None:
      raise ApiError(400, "invalid_json", "The body is missing or isn't valid JSON.")

  return 200, await fn(Ctx(key_id, rec, query, data, mm)), {}


# --------------------------------------------------------------------------- #
# server lifecycle
# --------------------------------------------------------------------------- #
_server = None


async def ensure_server():
  """Start the HTTP server once (health check always; /api/* only when enabled)."""
  global _server
  if _server is not None:
    return _server
  try:
    _server = await asyncio.start_server(_handle, "0.0.0.0", int(Vars.PORT), limit=MAX_HEADER)
    logger.info(f"HTTP server listening on :{Vars.PORT} (API {'on' if Vars.API_ENABLED else 'off'})")
  except Exception as e:
    logger.warning(f"HTTP server failed to start: {e}")
  return _server


@hook("api")
def _api_toggled():
  """Turning the API on from /settings must work even if the web port wasn't open at boot."""
  try:
    asyncio.get_running_loop().create_task(ensure_server())
  except RuntimeError:
    pass
