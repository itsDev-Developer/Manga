from .scraper import Scraper

import json
import re
from urllib.parse import quote, quote_plus, urljoin, urlparse

from bs4 import BeautifulSoup
from loguru import logger

from .utitls import T_MSG_FORMAT, extract_og_image

# --------------------------------------------------------------------------- #
# HentaiFox (https://hentaifox.com)
#
# A "gallery" is a one-shot, so each gallery is exposed to the bot as a manga
# with exactly one chapter ("Chapter 1") holding every page. That plugs straight
# into the existing chapter -> download -> PDF/CBZ pipeline.
#
# Nothing here depends on CSS class names. Galleries are found by the shape of
# their links (/gallery/<id>/), page images by the CDN folder their thumbnails
# live in (<cdn>/<dir>/<image id>/<n>t.<ext>  ->  <n>.<ext>), with the site's own
# reader data (load_dir / load_id / g_th) and the reader page as fallbacks.
#
# Not tested against the live site from the build environment (no network) -
# run `python -m Webs.diagnose hentaifox` on the server to check each step.
# --------------------------------------------------------------------------- #

BASE = "https://hentaifox.com/"
GALLERY_RE = re.compile(r"/gallery/(\d+)/?")
READER_RE = re.compile(r"/g/(\d+)/(\d+)/?")
THUMB_RE = re.compile(r"(?P<pre>(?:https?:)?//[^/\"'\s]+/(?P<dir>[^/\"'\s]+)/(?P<id>[^/\"'\s]+)/)(?P<n>\d+)t\.(?P<ext>jpe?g|png|gif|webp|bmp|avif)", re.I)
EXT_CODES = {"j": "jpg", "p": "png", "b": "bmp", "g": "gif", "w": "webp", "a": "avif"}
TAG_KINDS = ("tag", "artist", "group", "parody", "character", "language", "category")
CHAPTER_TITLE = "Chapter 1"


# --------------------------------------------------------------------------- #
# helpers (module level so they can be tested offline)
# --------------------------------------------------------------------------- #
def _abs(url: str) -> str:
  url = (url or "").strip()
  if not url:
    return ""
  if url.startswith("//"):
    return "https:" + url
  return url if url.startswith(("http://", "https://")) else urljoin(BASE, url)


def _img_src(img) -> str:
  if img is None:
    return ""
  for key in ("data-src", "data-lazy-src", "data-original", "src"):
    v = (img.get(key) or "").strip()
    if v and not v.startswith("data:"):
      return _abs(v)
  return ""


def gallery_id(text: str):
  """'12345', '/gallery/12345/', 'https://hentaifox.com/gallery/12345/' or 'https://hentaifox.com/g/12345/3/' -> '12345'."""
  text = (text or "").strip()
  if text.isdigit():
    return text
  m = GALLERY_RE.search(text) or READER_RE.search(text)
  return m.group(1) if m else None


def gallery_url(gid) -> str:
  return urljoin(BASE, f"gallery/{gid}/")


def parse_listing(bs):
  """Cards from a search / listing page -> [{title, poster, url}], page order, no duplicates."""
  seen, out = set(), []
  for a in bs.find_all("a", href=True):
    m = GALLERY_RE.search(a["href"])
    if not m:
      continue
    gid = m.group(1)
    img = a.find("img")
    # the card: nearest ancestor that also holds a picture and a title
    card = a
    for _ in range(4):
      if card.parent is None or card.parent.name in ("body", "html", "[document]"):
        break
      card = card.parent
      if card.find("img") is not None and (card.find(class_=re.compile("title|caption", re.I)) or card.find("h2")):
        break
    if img is None:
      img = card.find("img")

    title = (a.get("title") or "").strip()
    if not title:
      tnode = card.find(class_=re.compile("g_title|title|caption", re.I)) or card.find(["h2", "h3"])
      title = tnode.get_text(" ", strip=True) if tnode else ""
    if not title and img is not None:
      title = (img.get("alt") or "").strip()
    if not title:
      title = a.get_text(" ", strip=True)

    if gid in seen:
      # a later anchor of the same card may carry the title/poster the first one lacked
      prev = next(r for r in out if r["_id"] == gid)
      prev["title"] = prev["title"] or title
      prev["poster"] = prev["poster"] or _img_src(img)
      continue
    seen.add(gid)
    out.append({"_id": gid, "title": title, "poster": _img_src(img), "url": gallery_url(gid)})

  for r in out:
    r.pop("_id")
    r["title"] = r["title"] or "Untitled"
  return out


def parse_details(bs):
  """Gallery page -> dict(title, poster, tags{kind: [names]}, pages)."""
  title = ""
  h1 = bs.find("h1")
  if h1:
    title = h1.get_text(" ", strip=True)
  if not title:
    og = bs.find("meta", attrs={"property": "og:title"})
    title = (og.get("content", "") if og else "").strip()
    title = re.sub(r"\s*[-|–]\s*HentaiFox.*$", "", title, flags=re.I)

  tags = {k: [] for k in TAG_KINDS}
  for a in bs.find_all("a", href=True):
    m = re.search(r"/(tag|artist|group|parody|character|language|category)/([^/?#]+)/?$", a["href"], re.I)
    if not m:
      continue
    kind = m.group(1).lower()
    # visible name without the little "(1,234)" counter badge
    name = a.find(string=True, recursive=False)
    name = (str(name).strip() if name and str(name).strip() else a.get_text(" ", strip=True))
    name = re.sub(r"\s*\(?\d[\d,\.]*[kKmM]?\)?$", "", name).strip() or m.group(2).replace("-", " ")
    if name and name.lower() not in (n.lower() for n in tags[kind]):
      tags[kind].append(name)

  pages = None
  m = re.search(r"Pages?\s*:\s*(\d+)", bs.get_text(" ", strip=True), re.I)
  if m:
    pages = int(m.group(1))
  node = bs.find(id="load_pages") or bs.find("input", attrs={"name": "load_pages"})
  if node and str(node.get("value", "")).isdigit():
    pages = int(node["value"])

  cover = bs.find(class_=re.compile(r"cover", re.I))
  poster = _img_src(cover.find("img")) if cover and cover.find("img") else ""
  poster = extract_og_image(bs) or poster
  return {"title": title, "poster": _abs(poster), "tags": tags, "pages": pages}


def _ext_map(text: str) -> dict:
  """The reader's `g_th` JSON ({"1":"j,1280,1840", ...}) -> {1: 'jpg', ...}."""
  out = {}
  m = re.search(r"g_th\s*=\s*[^'\"{]*['\"]?(\{.*?\})['\"]?\s*\)", text, re.S) or re.search(r"g_th\s*=\s*(\{.*?\})", text, re.S)
  if not m:
    return out
  raw = m.group(1).replace("\\'", "'").replace('\\"', '"')
  try:
    data = json.loads(raw)
  except ValueError:
    data = dict(re.findall(r'"(\d+)"\s*:\s*"([a-z])', raw))
  for k, v in data.items():
    code = str(v).split(",")[0].strip().lower()
    if str(k).isdigit() and code in EXT_CODES:
      out[int(k)] = EXT_CODES[code]
  return out


def build_images(html: str, bs, pages=None, reader_html: str = ""):
  """Ordered full-size image URLs of a gallery page (see module docstring)."""
  exts = _ext_map(html) or _ext_map(reader_html or "")

  # 1. thumbnails: .../<dir>/<id>/<n>t.<ext>  ->  .../<dir>/<id>/<n>.<ext>
  thumbs = {}
  prefix = None
  for m in THUMB_RE.finditer(html):
    n = int(m.group("n"))
    if prefix is None:
      prefix = m.group("pre")
    elif m.group("pre") != prefix:
      continue                                   # a different gallery's thumb (e.g. "related")
    thumbs.setdefault(n, m.group("ext").lower().replace("jpeg", "jpg"))

  # 2. the site's own reader variables (cover galleries that only show some thumbs)
  load_dir = load_id = None
  for key in ("load_dir", "load_id", "load_pages"):
    node = bs.find(id=key) or bs.find("input", attrs={"name": key})
    val = str(node.get("value", "")).strip() if node else ""
    if key == "load_dir":
      load_dir = val
    elif key == "load_id":
      load_id = val
    elif val.isdigit() and not pages:
      pages = int(val)

  host = None
  if prefix:
    base = _abs(prefix)
  elif load_dir and load_id:
    cdn = re.search(r"//(i\d*\.hentaifox\.com)/", html)
    host = cdn.group(1) if cdn else "i.hentaifox.com"
    base = f"https://{host}/{load_dir}/{load_id}/"
  else:
    return []

  total = max([pages or 0, max(thumbs) if thumbs else 0])
  if not total:
    return []
  default_ext = (max(set(thumbs.values()), key=list(thumbs.values()).count) if thumbs else "jpg")
  urls = []
  for n in range(1, total + 1):
    ext = exts.get(n) or thumbs.get(n) or default_ext
    urls.append(f"{base}{n}.{ext}")
  return urls


# --------------------------------------------------------------------------- #
class HentaiFoxWebs(Scraper):
  def __init__(self):
    super().__init__()
    self.url = BASE
    self.bg = None
    self.sf = "hf"
    self.cs = True          # use the Cloudflare-aware client for the image CDN as well
    self.headers = {"Referer": self.url, "Accept-Language": "en-US,en;q=0.9"}

  async def _page(self, url):
    """Fetch an HTML page (Cloudflare-aware, one retry)."""
    for attempt in range(2):
      try:
        html = await self.get(url, cs=True, headers=self.headers)
        if html:
          return html
      except Exception as e:
        logger.warning(f"HentaiFox: {url} attempt {attempt + 1} failed: {type(e).__name__}: {e}")
    return None

  # ----------------------------------------------------------------------- #
  async def search(self, query: str = ""):
    query = (query or "").strip()

    # A pasted gallery link / bare number opens that gallery directly.
    gid = gallery_id(query) if (query.isdigit() or "hentaifox.com" in query.lower()) else None
    if gid:
      html = await self._page(gallery_url(gid))
      if html:
        d = parse_details(BeautifulSoup(html, "html.parser"))
        if d["title"]:
          return [{"title": d["title"], "poster": d["poster"], "url": gallery_url(gid)}]

    url = urljoin(self.url, f"search/?q={quote_plus(query)}") if query else self.url
    html = await self._page(url)
    if not html:
      logger.error(f"HentaiFox: search page failed ({url})")
      return []
    results = parse_listing(BeautifulSoup(html, "html.parser"))
    if not results:
      logger.warning(f"HentaiFox: no galleries parsed from {url} ({len(html)} bytes)")
    return results

  # ----------------------------------------------------------------------- #
  async def get_chapters(self, data, page: int = 1):
    results = data
    results["url"] = gallery_url(gallery_id(results["url"]) or "")
    html = await self._page(results["url"])
    if not html:
      logger.error(f"HentaiFox: could not load {results['url']}")
      results["chapters"] = []
      return results

    bs = BeautifulSoup(html, "html.parser")
    d = parse_details(bs)
    if d["poster"]:
      results["poster"] = d["poster"]
    title = d["title"] or results.get("title") or "Untitled"
    results["title"] = title
    results["pages"] = d["pages"]

    t = d["tags"]
    genres = ", ".join(t["tag"][:10]) or "N/A"
    bits = []
    for label, kind in (("Artists", "artist"), ("Groups", "group"), ("Parodies", "parody"), ("Characters", "character")):
      if t[kind]:
        bits.append(f"{label}: {', '.join(t[kind][:6])}")
    if d["pages"]:
      bits.append(f"Pages: {d['pages']}")
    results["msg"] = T_MSG_FORMAT.format(
      title=title,
      status="Completed",
      genres=genres,
      language=", ".join(t["language"]) or "N/A",
      summary=(" | ".join(bits) or "N/A")[:400],
      url=results["url"],
    )

    # The page is already in hand - keep its pictures so opening the chapter needs no second request.
    results["_images"] = build_images(html, bs, d["pages"])
    if results["_images"]:
      results["poster"] = results["_images"][0]      # page 1 is the full-size cover; the gallery's own cover is a thumbnail
    results["chapters"] = [{
      "title": CHAPTER_TITLE,
      "url": results["url"],
      "manga_title": title,
      "poster": results.get("poster"),
    }]
    return results

  def iter_chapters(self, data, page: int = 1):
    chapters = data.get("chapters") or []
    return chapters[(page - 1) * 60:page * 60] if page != 1 else chapters

  # ----------------------------------------------------------------------- #
  async def get_pictures(self, url, data=None):
    gid = gallery_id(url)
    if not gid:
      return []
    gurl = gallery_url(gid)

    html = await self._page(gurl)
    if not html:
      logger.error(f"HentaiFox: could not load {gurl}")
      return []
    bs = BeautifulSoup(html, "html.parser")
    pages = parse_details(bs)["pages"]
    images = build_images(html, bs, pages)

    # Thumbnails can be partial / missing: the reader page carries the real per-page extensions.
    reader_html = ""
    if not images or (pages and len(images) < pages):
      reader_html = await self._page(urljoin(self.url, f"g/{gid}/1/")) or ""
      if reader_html:
        rbs = BeautifulSoup(reader_html, "html.parser")
        images = build_images(html + "\n" + reader_html, bs, pages, reader_html) or images
        if not images:
          first = rbs.find(id="gimg")
          src = _img_src(first)
          m = re.match(r"^(.*/)(\d+)\.(\w+)$", src or "")
          if m and pages:
            images = [f"{m.group(1)}{n}.{m.group(3)}" for n in range(1, pages + 1)]

    if not images:
      logger.error(f"HentaiFox: no images found for {gurl}")
      return []
    return [quote(u, safe=":/%") for u in images]
