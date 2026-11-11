# Premium & Users API

Manage **Premium** and **users** on this bot from another bot or service — add, extend, replace or
remove plans, look people up, ban them, read stats.

This API follows the same contract as the Link Generation API reference (`API_DOCS.md`): same
endpoints, same request and response bodies, same scopes, same error codes. A client written
for that API works against this bot unchanged.

> **What this bot implements:** `ping`, `usage`, `premium/*`, `users/*`, `stats`.
> This bot has no link generation, so the `links/*` endpoints don't exist here (a key's `links`
> scope is accepted for compatibility but unlocks nothing on this bot).

- **Base path:** `/api/v1`
- **Format:** JSON in, JSON out
- **Auth:** `Authorization: Bearer <key>` — or `X-API-Key: <key>`
- **Dates:** the bot server's local time, `2026-10-30T12:00:00` (no zone), always next to a `…_unix` value

---

## 1. Quick start

1. **Turn the API on** — `/settings` → 🔑 **API Keys** → **Turn API on** (or *Settings → Premium →
   Premium API*). Until then every `/api/…` call answers `503 api_disabled`.
2. **Create a key** — in a private chat with the bot:

   ```
   /newapikey shopbot premium,users
   ```

   or `/settings` → 🔑 API Keys → ➕ New key (tick the scopes). The key (`fsk_…`) is shown **once** —
   the bot keeps only a hash, so a lost key can't be recovered; revoke it and make another.
3. **Find your URL** — the API is served on the bot's web port (`PORT`; Render/Koyeb set it for you),
   e.g. `https://my-bot.onrender.com`. On a VPS, put it behind a reverse proxy that terminates HTTPS.
   **Never send a key over plain `http://` across the internet.**
4. **Try it:**

```bash
curl https://YOUR-DOMAIN/api/v1/ping -H "Authorization: Bearer fsk_xxxxxxxxxxxxxxxxxxxxxxxx"
```

```json
{ "ok": true, "bot": "MangaRex_Bot", "key_label": "shopbot", "scopes": ["premium", "users"], "version": "v1" }
```

---

## 2. Authentication, scopes, limits

Send the key on every request, either way:

```
Authorization: Bearer fsk_xxxxxxxxxxxxxxxxxxxxxxxx
X-API-Key: fsk_xxxxxxxxxxxxxxxxxxxxxxxx
```

### Scopes

A key carries one or more scopes. **A new key gets `links` only** unless you choose more — least
privilege by default. On this bot that means a key needs `premium` and/or `users` to do anything
useful.

| Scope | Allows |
|---|---|
| `premium` | `POST /premium/add`, `POST /premium/remove`, `GET /premium/{user_id}`, `GET /premium` |
| `users` | `GET /users/{user_id}`, `POST /users/ban`, `POST /users/unban`, `GET /stats` |
| `links` | nothing on this bot (reserved for compatibility) |

`GET /ping` and `GET /usage` work for **any** valid key. Calling an endpoint without its scope →
`403 insufficient_scope`.

Manage keys from the bot (admins only):

| Command | What it does |
|---|---|
| `/newapikey <label> [scopes]` | Create a key. Scopes: `premium`, `users`, `links` (comma separated) or `all`. Default `links` |
| `/apikeys` | List keys — ID, label, scopes, last used |
| `/apikeyscopes <key_id> <scopes>` | Change a key's scopes — **takes effect immediately**, no new key |
| `/revokeapikey <key_id>` | Revoke a key — stops working immediately |

`/settings` → 🔑 API Keys does the same with buttons.

### Rate limit

**60 requests per minute per key.** Over it → `429 rate_limited` with a `Retry-After` header
(seconds). Repeated *failed* authentication from one IP is throttled the same way.

---

## 3. Endpoints

### `GET /api/v1/ping`
Check that your key works. *Any valid key.*

```json
{ "ok": true, "bot": "MangaRex_Bot", "key_label": "shopbot", "scopes": ["premium", "users"], "version": "v1" }
```

### `GET /api/v1/usage`
Your key's own details. *Any valid key.*

```json
{
  "ok": true,
  "key_id": "a1b2c3d4",
  "label": "shopbot",
  "links_created": 0,
  "ads_completed": 0,
  "scopes": ["premium", "users"],
  "created_at": "2026-09-28T09:00:00",
  "rate_limit_per_min": 60
}
```

`links_created` and `ads_completed` are always `0` here (no link generation or ads on this bot).

---

### `POST /api/v1/premium/add` — add, extend, replace or make permanent
*Scope: `premium`.*

```bash
curl -X POST https://YOUR-DOMAIN/api/v1/premium/add \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"user_id": 123456789, "days": 30, "request_id": "order-8841"}'
```

| Field | Required | Meaning |
|---|---|---|
| `user_id` | ✅ | Telegram user ID |
| `days` | — | Whole number `1`–`3650`. **Omit it for a permanent plan** |
| `mode` | — | `"extend"` *(default)* adds `days` on top of any time they still have. `"replace"` makes the plan end exactly `days` from now |
| `notify` | — | `true` to message the user ("💎 Premium activated…"). **Default `false`.** Never fails the request if they haven't started the bot |
| `request_id` | — | Your own unique id (1–128 characters). A repeat with the same id changes **nothing** and says `"duplicate": true` — see below |

Response:

```json
{
  "ok": true,
  "duplicate": false,
  "action": "activated",
  "user_id": 123456789,
  "premium": true,
  "permanent": false,
  "expires_at": "2026-10-30T12:00:00",
  "expires_at_unix": 1793361600,
  "days_left": 30,
  "previous_expires_at": null
}
```

| `action` | When |
|---|---|
| `activated` | They weren't premium (or their plan had expired) |
| `extended` | `mode: extend` on a running time-limited plan — time was added |
| `replaced` | `mode: replace` on a running plan — it now ends exactly `days` from now |
| `made_permanent` | `days` omitted and they had a time-limited plan |
| `unchanged` | Extending (or making permanent) a plan that is **already permanent** — nothing to add; nothing was written, nobody is messaged |

- A **permanent** plan has `"permanent": true` and `expires_at`, `expires_at_unix` and `days_left` are `null`.
- `previous_expires_at` is when the plan that was running before would have ended (`null` if they
  weren't premium or it was permanent).
- `days_left` is whole days, rounded **up** (a plan granted just now reads `30`).

#### Safe retries with `request_id`
If your request times out you can't tell whether it was applied — and retrying "add 30 days" could
give 60. Send a unique `request_id` (a payment id is ideal). A repeat from the same key returns the
**original response** with `"duplicate": true` and applies nothing. Duplicates arriving at the same
instant are serialized — exactly one is applied. Ids are remembered for 30 days and are per-key.

---

### `POST /api/v1/premium/remove`
*Scope: `premium`.* Body: `{"user_id": 123456789, "notify": false}` (`notify` default `false`).

```json
{
  "ok": true,
  "removed": true,
  "was_active": true,
  "user_id": 123456789,
  "premium": false,
  "permanent": false,
  "expires_at": null,
  "expires_at_unix": null,
  "days_left": null
}
```

Removing someone who isn't premium **succeeds** with `"removed": false` (safe to repeat).
`removed: true, was_active: false` means an already-expired record was cleaned up.

### `GET /api/v1/premium/{user_id}`
*Scope: `premium`.* Is this person premium, and until when?

```json
{
  "ok": true,
  "user_id": 123456789,
  "premium": true,
  "permanent": false,
  "expires_at": "2026-10-30T12:00:00",
  "expires_at_unix": 1793361600,
  "days_left": 29
}
```

Not premium is a normal `200`: `"premium": false, "permanent": false` and the rest `null`.

### `GET /api/v1/premium?limit=50&offset=0`
*Scope: `premium`.* Active premium users, **lowest user id first**. `limit` `1`–`200` (default `50`),
`offset` default `0`.

```json
{
  "ok": true,
  "total": 2,
  "limit": 50,
  "offset": 0,
  "users": [
    { "user_id": 111, "premium": true, "permanent": true,  "expires_at": null, "expires_at_unix": null, "days_left": null },
    { "user_id": 222, "premium": true, "permanent": false, "expires_at": "2026-10-30T12:00:00", "expires_at_unix": 1793361600, "days_left": 29 }
  ]
}
```

`total` counts all active users before paging.

---

### `GET /api/v1/users/{user_id}`
*Scope: `users`.*

```json
{
  "ok": true,
  "user_id": 123456789,
  "started_bot": true,
  "banned": false,
  "premium": {
    "premium": true,
    "permanent": false,
    "expires_at": "2026-10-30T12:00:00",
    "expires_at_unix": 1793361600,
    "days_left": 29
  }
}
```

`started_bot` is `true` once they've used the bot.

### `POST /api/v1/users/ban` · `POST /api/v1/users/unban`
*Scope: `users`.* Body: `{"user_id": 123456789}`

```json
{ "ok": true, "user_id": 123456789, "banned": true, "changed": true }
```

`changed` is `false` if nothing changed (already banned / wasn't banned). You can ban someone who
**hasn't started the bot** — it's in place when they do. **Admins and the owner can't be banned**
(`400 protected_user`). A banned user is refused everywhere: messages, buttons and inline mode.

### `GET /api/v1/stats`
*Scope: `users`.*

```json
{ "ok": true, "users": 1893, "banned": 12, "premium_active": 42 }
```

---

## 4. Errors

Every error has the same shape; branch on `error`, show `message` to humans:

```json
{ "ok": false, "error": "invalid_request", "message": "days must be between 1 and 3650." }
```

| HTTP | `error` | Meaning |
|---|---|---|
| 400 | `invalid_json` | The body is missing or isn't valid JSON |
| 400 | `invalid_request` | A field is missing or wrong (`user_id`, `days`, `mode`, `notify`, `request_id`, `limit`…) |
| 400 | `protected_user` | You tried to ban an admin or the owner |
| 401 | `missing_api_key` | No `Authorization` / `X-API-Key` header |
| 401 | `invalid_api_key` | The key is wrong or was revoked |
| 403 | `insufficient_scope` | The key doesn't have the scope this endpoint needs |
| 429 | `rate_limited` | Over 60 requests/minute — wait `Retry-After` seconds |
| 503 | `api_disabled` | The API switch is off |
| 503 | `server_starting` | The bot is still starting — retry in a few seconds |

Also possible, from the HTTP layer: `404 not_found`, `405 method_not_allowed` (with `Allow`),
`408 timeout`, `411 length_required`, `413 payload_too_large` (bodies are limited to 8 KB),
`431 headers_too_large`, and `500 internal_error` (details stay in the bot's logs).

**Retry guidance:** retry on network errors, `408`, `429` (after `Retry-After`), `500` and `503`,
with backoff — and always send a `request_id` on grants so a retry is safe. Don't retry other `4xx`.

---

## 5. Recipes

### A payment bot

```
payment confirmed
      │
      ▼
POST /premium/add  {"user_id": U, "days": 30, "request_id": "<payment id>", "notify": true}
      │
      ├── 200            → done   (also when "duplicate": true)
      ├── 429 / 5xx / network error → retry with the SAME request_id
      └── other 4xx      → a bug in your request; alert yourself
```

### Python

```python
import time, requests

BASE = "https://YOUR-DOMAIN/api/v1"
HEADERS = {"Authorization": "Bearer fsk_YOUR_KEY"}

def grant_premium(user_id: int, days: int | None, payment_id: str, attempts: int = 5) -> dict:
    """days=None -> permanent. Safe to call again with the same payment_id."""
    body = {"user_id": user_id, "request_id": payment_id, "notify": True}
    if days is not None:
        body["days"] = days
    for attempt in range(attempts):
        try:
            r = requests.post(f"{BASE}/premium/add", headers=HEADERS, json=body, timeout=15)
        except requests.RequestException:
            time.sleep(2 ** attempt); continue
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 503):
            time.sleep(int(r.headers.get("Retry-After", 2 ** attempt))); continue
        r.raise_for_status()               # other 4xx: a mistake - don't retry
    raise RuntimeError("premium grant did not go through")

def is_premium(user_id: int) -> bool:
    r = requests.get(f"{BASE}/premium/{user_id}", headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()["premium"]
```

### Node.js (18+)

```js
const BASE = "https://YOUR-DOMAIN/api/v1";
const headers = { Authorization: "Bearer fsk_YOUR_KEY", "Content-Type": "application/json" };

async function grantPremium(userId, days, paymentId) {
  const res = await fetch(`${BASE}/premium/add`, {
    method: "POST", headers,
    body: JSON.stringify({ user_id: userId, days, request_id: paymentId, notify: true }),
  });
  const body = await res.json();
  if (!res.ok) throw Object.assign(new Error(body.message), { code: body.error, status: res.status });
  return body;                           // body.duplicate === true on a retried request_id
}
```

### Refund / chargeback

```bash
curl -X POST https://YOUR-DOMAIN/api/v1/premium/remove \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"user_id": 123456789}'
```

---

## 6. The Premium link

Send people to **`https://t.me/<your_bot_username>?start=premium`** (your admin panel in
`/premium` shows the exact link).

- **They're premium** → *"✅ Premium is activated"*: validity (or *Permanent ♾*), time left, their perks.
- **They're not** → *"💎 Activate Premium"*: what Premium gives, the plans, and an
  **Activate Premium** button that opens your contact (set it in `/settings` → 💎 Premium →
  *Contact to buy* — a `@username`, a user id, or any `https://…` link such as your payment bot).

This is the natural place for a payment bot to send a user back to after `premium/add` succeeds.

---

## 7. Behaviour worth knowing

- **What Premium gives** depends on your setup: priority downloads, access to 🔒 Premium-only
  sources (`/settings` → 🔒 Source Access), and no access-token step when tokens are on. Changes
  apply immediately.
- **Expiry housekeeping** runs every 15 minutes: finished plans are removed and the user is told
  (when *Notify users* is on). Permanent plans never expire. Reminders before expiry are in
  *Settings → Premium*.
- **Everything is audited** — each grant/removal records who did it (`api:<key label>` or
  `admin:<id>`), kept 90 days.
- **Bans** take effect at once and survive restarts; `/ban`, `/unban` do the same from the bot.

## 8. Security checklist

- ✅ HTTPS only — a key in a header is as sensitive as a password.
- ✅ **One key per integration**, named after it; give each the **fewest scopes** it needs.
- ✅ Keys live in environment variables or a secrets manager — never in code or chats.
- ✅ Rotate a leaked key: make the new one, deploy it, then `/revokeapikey` the old one.
- ✅ Turn the API **off** when you aren't using it.

## 9. Troubleshooting

| Symptom | Likely cause |
|---|---|
| `503 api_disabled` | The API switch is off |
| `401 missing_api_key` | No `Authorization: Bearer …` / `X-API-Key` header |
| `401 invalid_api_key` | Typo, or the key was revoked |
| `403 insufficient_scope` | The key lacks the scope — `/apikeyscopes <id> premium,users` |
| Connection refused / timeout | The port isn't reachable: check the service uses the `PORT` it's given, and your proxy/firewall |
| User got premium but sees no change | Make sure it's the right `user_id` — check `GET /premium/{user_id}` |

## 10. Versioning

This is **v1** (`"version": "v1"` in `ping`). New fields and endpoints may be added within v1, so
clients should **ignore fields they don't recognise**. Breaking changes will ship as `/api/v2`.
