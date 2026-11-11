import asyncio
import html as _html
import json as _json
import re
import time
from urllib.parse import urlencode

import requests
from cloudscraper import create_scraper
from asyncio import to_thread
from loguru import logger

# Optional: curl_cffi impersonates a real Chrome TLS/HTTP2 fingerprint, which gets
# through bot-protection that rejects plain `requests`/cloudscraper. It's only used
# as a fallback when the normal path is blocked, and only if it's installed.
try:
  from curl_cffi import requests as _curl
except Exception:  # pragma: no cover - optional dependency
  _curl = None

# Without a timeout, a slow/dead site leaves the underlying request hanging
# forever, which blocks whichever thread picked it up from asyncio's shared
# default thread-pool (the same pool Tools/img2pdf.py's image downloads use
# via to_thread) - enough of those and the whole bot silently stops being
# able to download anything, with no error, no crash, just a growing queue.
DEFAULT_TIMEOUT = 20  # seconds

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_BLOCKED = (403, 429, 503)

# Browser fingerprints to try, in order, when a site blocks the first one. Names unknown to the installed
# curl_cffi are skipped. The one that works is remembered per source.
PROFILES = ("chrome", "safari", "firefox", "chrome_android", "edge")


class _Solved:
  """A response produced by FlareSolverr (a real browser), shaped like requests'/curl_cffi's."""

  def __init__(self, status, text):
    self.status_code, self.text, self.headers = status, text, {}

  def json(self):
    m = re.search(r"<pre[^>]*>(.*?)</pre>", self.text, re.S | re.I)      # browsers wrap raw JSON in <pre>
    return _json.loads(_html.unescape(m.group(1)) if m else self.text)


def looks_blocked(status: int, text: str = "") -> bool:
  """True for a bot-protection / Cloudflare challenge (as opposed to a real 404 etc.)."""
  if status in _BLOCKED:
    return True
  head = (text or "")[:3000].lower()
  if status == 200:
    # Cloudflare puts its challenge-platform script on perfectly normal pages: only the interstitial's title counts
    return "<title>just a moment" in head
  return "just a moment" in head or "cf-chl" in head or "challenge-platform" in head


class Scraper:
  def __init__(self):
    self.scraper = create_scraper()
    # Persistent session for the non-Cloudflare path too, so repeated
    # requests to the same site (very common - a chapter list fetch is
    # followed by dozens of image requests) reuse the TCP/TLS connection
    # instead of paying a fresh handshake every single call.
    self.session = requests.Session()
    # Image downloads (Tools/cworker.py) read `webs.cs`; sites that need the
    # Cloudflare-aware client for their images set this to True.
    self.cs = False
    # Set to "chrome" by sources behind Cloudflare Turnstile: every request (and, via
    # `cs`, every image download) then goes through curl_cffi with a real Chrome fingerprint.
    self.impersonate = None
    self._profile = "chrome"            # the fingerprint that last worked
    self._flare_cache = {}              # url -> (time, _Solved)
    self._flare_sem = None
    # What happened on the most recent request: shown by `python -m Webs.diagnose` and in the logs.
    self.last_diag = ""

  # ----------------------------------------------------------------------- #
  def _curl_request(self, method, url, profile="chrome", **kwargs):
    kwargs.pop("allow_redirects", None)
    return _curl.request(method, url, impersonate=profile, allow_redirects=True, **kwargs)

  def _note(self, response, via, url=""):
    status = getattr(response, "status_code", "?")
    blocked = looks_blocked(status, getattr(response, "text", "") if response is not None else "")
    self.last_diag = f"HTTP {status} via {via}" + (" - blocked by bot protection (Cloudflare-style)" if blocked and status != 200 else "")
    return response

  async def _impersonated(self, method, url, **kwargs):
    """curl_cffi with a real-browser fingerprint. If the site blocks it, rotate through other browsers'
    fingerprints, then (if configured) hand the page to FlareSolverr."""
    order = [self._profile] + [p for p in PROFILES if p != self._profile]
    last, tried = None, []
    for i, prof in enumerate(order):
      kw = dict(kwargs)
      if i:
        kw["timeout"] = min(kw.get("timeout", DEFAULT_TIMEOUT), 12)     # retries shouldn't stack 20 s each
      try:
        r = await to_thread(self._curl_request, method, url, prof, **kw)
      except Exception as e:                  # unknown profile name for this curl_cffi, or a network error
        tried.append(f"{prof}: {type(e).__name__}")
        continue
      tried.append(f"{prof}: {r.status_code}")
      last = r
      if not looks_blocked(r.status_code, getattr(r, "text", "")):
        if prof != self._profile:
          logger.info(f"{url}: the '{prof}' browser fingerprint works - remembering it")
          self._profile = prof
        return self._note(r, prof)
    solved = await self._flaresolverr(method, url, **kwargs)
    if solved is not None:
      return self._note(solved, "FlareSolverr")
    r = self._note(last, "all fingerprints (" + ", ".join(tried) + ")") if last is not None else None
    if r is None:
      self.last_diag = "no response (" + ", ".join(tried) + ")"
      raise ConnectionError(self.last_diag)
    return r

  async def _flaresolverr(self, method, url, **kwargs):
    """Optional: a FlareSolverr server (https://github.com/FlareSolverr/FlareSolverr) renders the page in a real
    browser and passes the challenge. Set its address in /settings -> Source Access (or FLARESOLVERR_URL)."""
    try:
      from bot import Vars
      base = (getattr(Vars, "FLARESOLVERR_URL", "") or "").strip()
    except Exception:
      base = ""
    if not base:
      return None
    hit = self._flare_cache.get(url)
    if hit and method == "get" and time.time() - hit[0] < 120:
      return hit[1]
    if self._flare_sem is None:
      self._flare_sem = asyncio.Semaphore(2)
    payload = {"cmd": "request.get" if method == "get" else "request.post", "url": url, "maxTimeout": 60000}
    if method == "post":
      data = kwargs.get("data")
      payload["postData"] = urlencode(data) if isinstance(data, dict) else (data or "")
    try:
      async with self._flare_sem:
        r = await to_thread(lambda: requests.post(base.rstrip("/") + "/v1", json=payload, timeout=80))
      j = r.json()
    except Exception as e:
      logger.warning(f"FlareSolverr at {base} failed: {type(e).__name__}: {e}")
      return None
    sol = j.get("solution") or {}
    if j.get("status") != "ok" or not sol:
      logger.warning(f"FlareSolverr could not solve {url}: {j.get('message', '?')}")
      return None
    out = _Solved(int(sol.get("status") or 200), sol.get("response") or "")
    if method == "get":
      if len(self._flare_cache) > 100:
        self._flare_cache.clear()
      self._flare_cache[url] = (time.time(), out)
    logger.info(f"{url}: solved by FlareSolverr")
    return out

  async def _request(self, method, url, rjson=None, cs=None, *args, **kwargs):
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    if self.impersonate and _curl is None:
      logger.error("This source needs curl_cffi (Cloudflare Turnstile) but it is not installed: "
                   "run `pip install curl_cffi` (it is in requirements.txt)")
    if self.impersonate and _curl is not None:
      response = await self._impersonated(method, url, **kwargs)
    elif cs:
      response = await to_thread(getattr(self.scraper, method), url, *args, **kwargs)
      # cloudscraper can't beat every protection level. If it got blocked, retry once
      # with a real-Chrome fingerprint (when curl_cffi is installed).
      if _curl is not None and looks_blocked(response.status_code, getattr(response, "text", "")):
        logger.info(f"{method.upper()} {url}: blocked ({response.status_code}), retrying with curl_cffi")
        try:
          alt = await to_thread(self._curl_request, method, url, **kwargs)
          if alt.status_code == 200:
            response = alt
        except Exception as e:
          logger.warning(f"curl_cffi fallback failed for {url}: {type(e).__name__}: {e}")
    else:
      response = await to_thread(getattr(self.session, method), url, *args, **kwargs)
      response.raise_for_status()

    if response.status_code == 200:
      if rjson:
        try:
          return response.json()
        except ValueError:
          logger.warning(f"{method.upper()} {url}: 200 but body is not JSON (first 120 chars: {response.text[:120]!r})")
          return None
      return response.text

    logger.warning(f"{method.upper()} {url}: HTTP {response.status_code}" + (f" ({self.last_diag})" if self.last_diag else ""))
    return None

  async def get(self, url, rjson=None, cs=None, *args, **kwargs):
    return await self._request("get", url, rjson, cs, *args, **kwargs)

  async def post(self, url, rjson=None, cs=None, *args, **kwargs):
    return await self._request("post", url, rjson, cs, *args, **kwargs)
