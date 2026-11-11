from .scraper import Scraper

import json
import re
import time
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup
from loguru import logger

from .utitls import T_MSG_FORMAT

# --------------------------------------------------------------------------- #
# NHentai (https://nhentai.net)
#
# A gallery is a one-shot, so each gallery is exposed to the bot as a manga with exactly one
# chapter ("Chapter 1") holding every page - the same shape as Webs/hentaifox.py.
#
# Data comes from the site's JSON API, in this order of preference:
#     /api/gallery/<id>                       one gallery
#     /api/galleries/search?query=<q>&page=1  search   (/api/galleries/all for "no query")
# with HTML fallbacks if the API is unavailable:
#     /g/<id>/  (the gallery's embedded JSON, else its thumbnails)   /search/?q=<q>
# Page pictures:  https://i.nhentai.net/galleries/<media_id>/<n>.<ext>
# The site is behind Cloudflare, so requests use the Chrome-fingerprint client (curl_cffi),
# see Webs/scraper.py.
#
# Not tested against the live site from the build environment (no network) -
# run `python -m Webs.diagnose nhentai` on the server to check each step.
# --------------------------------------------------------------------------- #

BASE = "https://nhentai.net/"
IMG_HOST = "https://i.nhentai.net/"
THUMB_HOST = "https://t.nhentai.net/"
EXT = {"j": "jpg", "p": "png", "g": "gif", "w": "webp", "a": "avif", "b": "bmp"}
KINDS = ("tag", "artist", "group", "parody", "character", "language", "category")
CHAPTER_TITLE = "Chapter 1"
CACHE_TTL = 600


# --------------------------------------------------------------------------- #
# helpers (module level so they can be tested without the network)
# --------------------------------------------------------------------------- #
def gallery_id(text):
  """'177013', '/g/177013/', 'https://nhentai.net/g/177013/12/' -> '177013'."""
  text = str(text or "").strip()
  if text.isdigit():
    return text
  m = re.search(r"/g/(\d+)", text)
  return m.group(1) if m else None


def gallery_url(gid) -> str:
  return urljoin(BASE, f"g/{gid}/")


def _ext(code, default="jpg"):
  code = str(code or "").strip().lower()
  if code in EXT:
    return EXT[code]
  return code if code in EXT.values() else default


def _title(t) -> str:
  if isinstance(t, str):
    return t.strip()
  t = t or {}
  return (t.get("pretty") or t.get("english") or t.get("japanese") or "").strip()


def from_api(j: dict):
  """The site's gallery JSON -> the normalised gallery used everywhere below."""
  if not isinstance(j, dict) or not (j.get("id") and j.get("media_id")):
    return None
  imgs = j.get("images") or {}
  pages = [_ext((p or {}).get("t")) for p in (imgs.get("pages") or [])]
  tags = {k: [] for k in KINDS}
  for t in j.get("tags") or []:
    kind, name = (t or {}).get("type"), ((t or {}).get("name") or "").strip()
    if kind in tags and name and name not in tags[kind]:
      tags[kind].append(name)
  return {
    "id": str(j["id"]), "media_id": str(j["media_id"]), "title": _title(j.get("title")),
    "pages": pages, "num_pages": int(j.get("num_pages") or len(pages) or 0), "tags": tags,
    "cover_ext": _ext((imgs.get("cover") or {}).get("t"), pages[0] if pages else "jpg"),
  }


def _js_json(html: str):
  """The gallery JSON older/newer page layouts embed:  window._gallery = JSON.parse("…")."""
  m = re.search(r"_gallery\s*=\s*JSON\.parse\(\s*(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')\s*\)", html)
  if m:
    raw = m.group(1)
    try:
      lit = raw if raw[0] == '"' else '"' + raw[1:-1].replace('"', '\\"') + '"'
      return json.loads(json.loads(lit))
    except ValueError:
      pass
  m = re.search(r"_gallery\s*=\s*(\{.*?\})\s*;", html, re.S)
  if m:
    try:
      return json.loads(m.group(1))
    except ValueError:
      pass
  return None


def from_html(html: str, gid: str):
  """A gallery page -> the normalised gallery (embedded JSON first, else its thumbnails)."""
  j = _js_json(html)
  g = from_api(j) if j else None
  if g:
    return g
  bs = BeautifulSoup(html, "html.parser")
  thumbs = {}
  media_id = None
  for m in re.finditer(r"(?:https?:)?//t\d*\.nhentai\.net/galleries/(\d+)/(\d+)t\.(\w+)", html):
    media_id = media_id or m.group(1)
    if m.group(1) == media_id:
      thumbs.setdefault(int(m.group(2)), _ext(m.group(3)))
  if not media_id or not thumbs:
    return None
  title = ""
  node = bs.select_one("#info h1") or bs.select_one("h1.title") or bs.find("h1")
  if node:
    title = node.get_text(" ", strip=True)
  tags = {k: [] for k in KINDS}
  for a in bs.find_all("a", href=True):
    m = re.match(r"^/(tag|artist|group|parody|character|language|category)/([^/]+)/?$", a["href"])
    if not m:
      continue
    name = a.find(class_="name")
    name = (name.get_text(" ", strip=True) if name else re.sub(r"\s*\d[\d,\.Kk]*$", "", a.get_text(" ", strip=True))).strip()
    if name and name not in tags[m.group(1)]:
      tags[m.group(1)].append(name)
  n = max(thumbs)
  pages = [thumbs.get(i, "jpg") for i in range(1, n + 1)]
  cover = re.search(r"galleries/%s/cover\.(\w+)" % media_id, html)
  return {"id": gid, "media_id": media_id, "title": title, "pages": pages, "num_pages": n, "tags": tags,
          "cover_ext": _ext(cover.group(1)) if cover else pages[0]}


def page_urls(g: dict) -> list:
  return [f"{IMG_HOST}galleries/{g['media_id']}/{i}.{ext}" for i, ext in enumerate(g["pages"], 1)]


def cover_url(g: dict) -> str:
  return f"{THUMB_HOST}galleries/{g['media_id']}/cover.{g['cover_ext']}"


def card_from_api(j: dict):
  g = from_api(j)
  if not g:
    return None
  return {"title": g["title"] or f"Gallery {g['id']}", "poster": cover_url(g), "url": gallery_url(g["id"])}


def parse_listing(bs):
  """Cards of an HTML search / home page -> [{title, poster, url}]."""
  out, seen = [], set()
  for a in bs.find_all("a", href=True):
    m = re.match(r"^/g/(\d+)/?$", a["href"])
    if not m or m.group(1) in seen:
      continue
    seen.add(m.group(1))
    img = a.find("img")
    src = ""
    if img is not None:
      src = (img.get("data-src") or img.get("src") or "").strip()
      if src.startswith("data:"):
        src = (img.get("data-src") or "")
      src = ("https:" + src) if src.startswith("//") else src
    cap = a.find(class_="caption")
    title = (cap.get_text(" ", strip=True) if cap else "") or (img.get("alt", "").strip() if img is not None else "")
    out.append({"title": title or f"Gallery {m.group(1)}", "poster": src, "url": gallery_url(m.group(1))})
  return out


# --------------------------------------------------------------------------- #
class NHentaiWebs(Scraper):
  def __init__(self):
    super().__init__()
    self.url = BASE
    self.bg = None
    self.sf = "nh"
    self.cs = False               # images: plain headers first; the downloader falls back to Chrome fingerprints on 403
    self.impersonate = "chrome"   # Cloudflare: real-Chrome fingerprint for the site and its API
    self.headers = {"Referer": self.url, "Accept-Language": "en-US,en;q=0.9"}
    self.api_headers = {**self.headers, "Accept": "application/json, text/plain, */*"}
    self._cache = {}              # gid -> (time, gallery)

  # ----------------------------------------------------------------------- #
  async def _api(self, path):
    for attempt in range(2):
      try:
        data = await self.get(urljoin(self.url, path), cs=True, rjson=True, headers=self.api_headers)
        if isinstance(data, dict):
          return data
      except Exception as e:
        logger.warning(f"NHentai: {path} attempt {attempt + 1} failed: {type(e).__name__}: {e}")
    return None

  async def _html(self, path):
    for attempt in range(2):
      try:
        html = await self.get(urljoin(self.url, path), cs=True, headers=self.headers)
        if html:
          return html
      except Exception as e:
        logger.warning(f"NHentai: {path} attempt {attempt + 1} failed: {type(e).__name__}: {e}")
    return None

  async def _gallery(self, gid):
    hit = self._cache.get(gid)
    if hit and time.time() - hit[0] < CACHE_TTL:
      return hit[1]
    g = from_api(await self._api(f"api/gallery/{gid}") or {})
    if not g:
      html = await self._html(f"g/{gid}/")
      g = from_html(html, gid) if html else None
    if g:
      if len(self._cache) > 200:
        self._cache.clear()
      self._cache[gid] = (time.time(), g)
    else:
      logger.error(f"NHentai: could not load gallery {gid} (blocked, removed, or the layout changed)")
    return g

  # ----------------------------------------------------------------------- #
  async def search(self, query: str = ""):
    query = (query or "").strip()

    gid = gallery_id(query) if (query.isdigit() or "nhentai" in query.lower()) else None
    if gid:                                   # a pasted link / bare number opens that gallery directly
      g = await self._gallery(gid)
      return [{"title": g["title"] or f"Gallery {gid}", "poster": cover_url(g), "url": gallery_url(gid)}] if g else []

    path = f"api/galleries/search?query={quote_plus(query)}&page=1" if query else "api/galleries/all?page=1"
    data = await self._api(path)
    results = [c for c in (card_from_api(j) for j in (data or {}).get("result", [])) if c]
    if results:
      return results

    html = await self._html(f"search/?q={quote_plus(query)}" if query else "")
    results = parse_listing(BeautifulSoup(html, "html.parser")) if html else []
    if not results:
      logger.warning(f"NHentai: no results for {query!r} (blocked, or the layout changed)")
    return results

  # ----------------------------------------------------------------------- #
  async def get_chapters(self, data, page: int = 1):
    results = data
    gid = gallery_id(results.get("url"))
    g = await self._gallery(gid) if gid else None
    if not g:
      results["chapters"] = []
      return results

    title = g["title"] or results.get("title") or f"Gallery {gid}"
    results["title"] = title
    results["url"] = gallery_url(gid)
    # page 1 is the full-size cover (the "cover" file is a ~350px thumbnail); Tools.poster falls back to the cover if page 1 is refused
    results["poster"] = page_urls(g)[0] if g["pages"] else cover_url(g)
    t = g["tags"]
    bits = []
    for label, kind in (("Artists", "artist"), ("Groups", "group"), ("Parodies", "parody"), ("Characters", "character")):
      if t[kind]:
        bits.append(f"{label}: {', '.join(t[kind][:6])}")
    if g["num_pages"]:
      bits.append(f"Pages: {g['num_pages']}")
    langs = [x for x in t["language"] if x.lower() != "translated"] or t["language"]
    results["msg"] = T_MSG_FORMAT.format(
      title=title, status="Completed", genres=", ".join(t["tag"][:10]) or "N/A",
      language=", ".join(langs) or "N/A", summary=(" | ".join(bits) or "N/A")[:400], url=results["url"])
    results["chapters"] = [{"title": CHAPTER_TITLE, "url": results["url"], "manga_title": title[:120],
                            "poster": results["poster"]}]
    return results

  def iter_chapters(self, data, page: int = 1):
    chapters = data.get("chapters") or []
    return chapters[(page - 1) * 60:page * 60] if page != 1 else chapters

  # ----------------------------------------------------------------------- #
  async def get_pictures(self, url, data=None):
    gid = gallery_id(url)
    g = await self._gallery(gid) if gid else None
    if not g or not g["pages"]:
      logger.error(f"NHentai: no pages for {url}")
      return []
    return page_urls(g)
