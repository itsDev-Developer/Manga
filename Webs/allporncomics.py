from .scraper import Scraper

import re
from urllib.parse import quote, quote_plus, urljoin, urlparse

from bs4 import BeautifulSoup
from loguru import logger

from .utitls import DEAULT_MSG_FORMAT, extract_og_image

# --------------------------------------------------------------------------- #
# AllPornComics (https://allporncomics.co)
#
# A WordPress comic site (Madara-style): /<prefix>/<series>/ is a comic page and
# /<prefix>/<series>/<chapter>/ a chapter. Nothing here depends on CSS class names:
#   * series are recognised by the SHAPE of their links (two path segments),
#   * chapters are the links that extend the series' own path,
#   * pictures are the images inside the reader area (lazy-load attributes included).
# Fallbacks: Madara's AJAX chapter list, `?style=list` (all pictures on one page),
# and treating a series page that already contains the pictures as a one-shot.
#
# The site sits behind bot protection, so every request uses the Chrome-fingerprint
# client (curl_cffi) - see Webs/scraper.py.
#
# Not tested against the live site from the build environment (no network, and the
# site blocks automated fetches) - run `python -m Webs.diagnose allporncomics`
# on the server to check each step.
# --------------------------------------------------------------------------- #

BASE = "https://allporncomics.co/"
HOSTS = ("allporncomics.co", "www.allporncomics.co")
SERIES_PREFIXES = ("porncomic", "porncomics", "comic", "comics", "manga", "series", "manhwa", "webtoon", "adult-comic")
NOT_SERIES = {"genre", "tag", "category", "author", "artist", "page", "feed", "wp-content", "wp-json", "tags",
              "genres", "authors", "artists", "porncomic-genre", "porncomic-tag", "porncomic-author", "manga-genre"}
READER_SELECTORS = (".reading-content", "#chapter-reader", ".chapter-content", ".entry-content", ".page-break", "article")
JUNK_IMG_RE = re.compile(r"(logo|avatar|favicon|icon|banner|placeholder|sprite|/ads?/|emoji|gravatar|loading|spinner|dmca)", re.I)
CHAPTER_NUM_RE = re.compile(r"(?:chapter|chap|ch|episode|ep|issue|part)\.?\s*[-_ ]?\s*(\d+(?:[.\-_]\d+)?)", re.I)
ANY_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")

# A path segment that is a taxonomy / utility page rather than a comic section (used by "loose" sites that
# don't tell us their series prefix: comicsvalley.com).
NOT_SECTION_RE = re.compile(
  r"^(?:[\w-]*-(?:genre|author|tag|artist|category|new|tags|authors|genres)|genre|tag|tags|category|author|artist|"
  r"authors?-genres|page|feed|wp-[\w-]+|home[\w-]*|sitemap[\w-]*|random[\w-]*|contact[\w-]*|about[\w-]*|dmca|"
  r"privacy[\w-]*|terms[\w-]*|cv-[\w-]+|adult-pc-games|comics-?new|comicsnew|search|login|register|user|my-account)$", re.I)
CARD_CLASS_RE = re.compile(r"(page-item-detail|post-title|item-thumb|c-tabs-item|popular-item|manga-item|tab-thumb|"
                           r"badge-pos|item-summary|comic-item|post-item|entry-title)", re.I)


class Site:
  """Where a Madara-style comic site lives. `loose=True` -> the series section name isn't known in advance:
  any two-segment link that sits inside a listing card counts as a series."""

  def __init__(self, base, hosts, prefixes=(), loose=False, label="AllPornComics"):
    self.base, self.hosts, self.prefixes, self.loose, self.label = base, tuple(hosts), tuple(prefixes), loose, label


DEFAULT_SITE = Site(BASE, HOSTS, SERIES_PREFIXES)


# --------------------------------------------------------------------------- #
# helpers (module level so they can be tested without the network)
# --------------------------------------------------------------------------- #
def _abs(url: str, base: str = BASE) -> str:
  url = (url or "").strip()
  if not url:
    return ""
  if url.startswith("//"):
    return "https:" + url
  return url if url.startswith(("http://", "https://")) else urljoin(base, url)


def _first_src(img) -> str:
  if img is None:
    return ""
  for key in ("data-src", "data-lazy-src", "data-original", "data-cfsrc", "src"):
    v = (img.get(key) or "").strip()
    if v and not v.startswith("data:"):
      return v
  srcset = (img.get("data-srcset") or img.get("srcset") or "").strip()
  if srcset:
    return srcset.split(",")[0].strip().split(" ")[0]
  return ""


def _same_site(href: str, site: Site = DEFAULT_SITE) -> bool:
  host = urlparse(href).netloc.lower()
  return not host or host in site.hosts


def _segments(href: str, site: Site = DEFAULT_SITE):
  if not href or href.startswith(("#", "javascript:", "mailto:")) or not _same_site(href, site):
    return []
  return [p for p in urlparse(href).path.split("/") if p]


def series_slug(href: str, site: Site = DEFAULT_SITE):
  """'/porncomic/foo-bar/' -> ('porncomic', 'foo-bar'); anything else -> None."""
  seg = _segments(href, site)
  if len(seg) != 2 or seg[1].lower() in NOT_SERIES or "." in seg[1]:
    return None
  if site.loose:
    return None if NOT_SECTION_RE.match(seg[0]) else (seg[0], seg[1])
  return (seg[0], seg[1]) if seg[0].lower() in site.prefixes else None


def series_url(prefix: str, slug: str, site: Site = DEFAULT_SITE) -> str:
  return urljoin(site.base, f"{prefix}/{slug}/")


def _in_card(a) -> bool:
  """Does this link sit in a listing card (picture / heading / Madara card class)?"""
  if a.find("img") is not None:
    return True
  node = a
  for _ in range(4):
    if node is None:
      break
    cls = " ".join(node.get("class") or []) if hasattr(node, "get") else ""
    if CARD_CLASS_RE.search(cls):
      return True
    node = node.parent
  return False


def _num(text):
  if not text:
    return None
  m = CHAPTER_NUM_RE.search(str(text)) or ANY_NUM_RE.search(str(text))
  if not m:
    return None
  try:
    return float(m.group(1).replace("-", ".").replace("_", "."))
  except ValueError:
    return None


def _title_from_slug(slug: str) -> str:
  return (slug or "").replace("-", " ").replace("_", " ").strip().title() or "Chapter"


def _series_links(node, site: Site = DEFAULT_SITE) -> set:
  out = set()
  for a in node.find_all("a", href=True):
    ss = series_slug(a["href"], site)
    if ss and (not site.loose or _in_card(a)):
      out.add(ss)
  return out


def parse_listing(bs, site: Site = DEFAULT_SITE):
  """Cards of a search / listing page -> [{title, poster, url}] (page order, no duplicates)."""
  seen, out = {}, []
  for a in bs.find_all("a", href=True):
    ss = series_slug(a["href"], site)
    if not ss or (site.loose and not _in_card(a)):
      continue
    url = series_url(*ss, site=site)
    img = a.find("img")
    card = a
    for _ in range(4):           # nearest ancestor that holds a picture and a heading
      parent = card.parent
      if parent is None or parent.name in ("body", "html", "[document]"):
        break
      if len(_series_links(parent, site)) > 1:      # that container holds other comics too: stop, or we'd borrow their title/poster
        break
      card = parent
      if card.find("img") is not None and card.find(["h2", "h3", "h4"]):
        break
    if img is None:
      img = card.find("img")

    title = (a.get("title") or "").strip()
    if not title:
      node = card.find(["h2", "h3", "h4"]) or card.find(class_=re.compile("title", re.I))
      title = node.get_text(" ", strip=True) if node else ""
    if not title and img is not None:
      title = (img.get("alt") or "").strip()
    if not title:
      title = a.get_text(" ", strip=True)

    poster = _abs(_first_src(img), site.base)
    if url in seen:
      prev = seen[url]
      prev["title"] = prev["title"] or title
      prev["poster"] = prev["poster"] or poster
      continue
    seen[url] = {"title": title, "poster": poster, "url": url}
    out.append(seen[url])
  for r in out:
    r["title"] = r["title"] or _title_from_slug(r["url"].rstrip("/").rsplit("/", 1)[-1])
  return out


def parse_details(bs, fallback_title: str = ""):
  title = ""
  h1 = bs.find("h1")
  if h1:
    title = h1.get_text(" ", strip=True)
  if not title:
    og = bs.find("meta", attrs={"property": "og:title"})
    title = re.split(r"\s+[-|–]\s+", (og.get("content", "") if og else "").strip())[0]
  title = title or fallback_title

  genres, artists = [], []
  for a in bs.find_all("a", href=True):
    path = urlparse(a["href"]).path.lower()
    name = a.get_text(" ", strip=True)
    name = re.sub(r"\s*\(?\d[\d,\.]*\)?$", "", name).strip()
    if not name or len(name) > 40:
      continue
    if re.search(r"/(?:[\w-]*genre|[\w-]*tag|[\w-]*category|tags?)/[^/]+/?$", path):
      if name.lower() not in (g.lower() for g in genres):
        genres.append(name)
    elif re.search(r"/(?:[\w-]*artist|[\w-]*author)s?/[^/]+/?$", path):
      if name.lower() not in (g.lower() for g in artists):
        artists.append(name)

  desc = ""
  for sel in (".summary__content", ".description-summary", ".manga-excerpt", "[class*=summary]", "[class*=description]"):
    node = bs.select_one(sel)
    if node and len(node.get_text(strip=True)) > 20:
      desc = node.get_text(" ", strip=True)
      break
  if not desc:
    meta = bs.find("meta", attrs={"name": "description"}) or bs.find("meta", attrs={"property": "og:description"})
    desc = (meta.get("content", "") if meta else "").strip()

  status = "N/A"
  m = re.search(r"Status\s*[:\-]?\s*(Completed|Complete|Ongoing|OnGoing|Hiatus|Cancelled|Canceled)", bs.get_text(" ", strip=True), re.I)
  if m:
    status = m.group(1).capitalize()

  return {"title": title, "poster": extract_og_image(bs) or "", "genres": genres, "artists": artists,
          "desc": desc, "status": status}


def parse_chapters(bs, prefix: str, slug: str, site: Site = DEFAULT_SITE):
  """Links that extend the series' own path: /<prefix>/<slug>/<chapter>/ ."""
  out, seen = [], set()
  for a in bs.find_all("a", href=True):
    seg = _segments(a["href"], site)
    if len(seg) < 3 or seg[0].lower() != prefix.lower() or seg[1] != slug:
      continue
    if seg[2].lower() in ("feed", "ajax", "comments", "page"):
      continue
    url = urljoin(site.base, "/".join(seg[:3]) + "/")
    if url in seen:
      continue
    seen.add(url)
    texts = list(a.stripped_strings)
    title = texts[0] if texts else ""
    if not title or len(title) > 80 or title.lower() in ("new", "hot", "read", "read now", "read online"):
      title = _title_from_slug(seg[2])
    out.append({"title": title, "url": url, "_n": _num(title) if _num(title) is not None else _num(seg[2])})
  # newest first when numbers are known (a Madara list already is - then this is a no-op)
  if out and all(c["_n"] is not None for c in out):
    out.sort(key=lambda c: c["_n"], reverse=True)
  for c in out:
    c.pop("_n")
  return out


PRECISE_READERS = (".reading-content", "#chapter-reader", ".chapter-content", ".page-break", "#readerarea", ".reader-area")
BROAD_READERS = (".entry-content", "article", "main")
IMG_EXT = r"(?:jpe?g|png|webp|gif|avif|bmp)"
THUMB_SUFFIX_RE = re.compile(r"-\d{2,4}x\d{2,4}\." + IMG_EXT + r"$", re.I)      # WordPress thumbnail sizes
SCRIPT_IMG_RE = re.compile(r"https?:(?:\\?/){2}[^\s\"'<>\\]+(?:\\/[^\s\"'<>\\]+)*?\." + IMG_EXT + r"(?![\w])", re.I)


def _img_urls(imgs, base: str = BASE):
  out = []
  for img in imgs:
    src = _first_src(img)
    if src:
      out.append(_abs(src.strip(), base))
  return out


def _usable(urls):
  seen, out = set(), []
  for u in urls:
    if not u or u in seen or JUNK_IMG_RE.search(u) or THUMB_SUFFIX_RE.search(u.split("?")[0]):
      continue
    seen.add(u)
    out.append(u)
  return out


def _largest_folder(urls):
  """Chapter pictures live together in one folder (…/WP-manga/data/<id>/<hash>/001.jpg); related-comic
  thumbnails, ads and logos are scattered elsewhere. Keep the biggest folder, in page order."""
  groups, order = {}, []
  for u in urls:
    p = urlparse(u)
    key = (p.netloc, p.path.rsplit("/", 1)[0])
    if key not in groups:
      groups[key] = []
      order.append(key)
    groups[key].append(u)
  if not groups:
    return []
  best = max(order, key=lambda k: (len(groups[k]), -order.index(k)))
  return groups[best]


def _natural_key(url):
  name = urlparse(url).path.rsplit("/", 1)[-1]
  return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def _add_script_siblings(bs, urls):
  """Some themes put a few pictures only in a script array: add any from the SAME folder
  that the markup didn't have, then order the folder naturally (2 before 10)."""
  p = urlparse(urls[0])
  folder = (p.netloc, p.path.rsplit("/", 1)[0])
  have = set(urls)
  extra = []
  for tag in bs.find_all("script"):
    text = (tag.string or tag.get_text() or "")
    if "http" not in text:
      continue
    for m in SCRIPT_IMG_RE.finditer(text):
      u = m.group(0).replace("\\/", "/")
      q = urlparse(u)
      if (q.netloc, q.path.rsplit("/", 1)[0]) == folder and u not in have and _usable([u]):
        have.add(u)
        extra.append(u)
  return sorted(urls + extra, key=_natural_key) if extra else urls


def reader_images(bs, base: str = BASE):
  """Ordered picture URLs of a chapter page, using the most specific evidence available:
  1. the reader container itself (trusted as-is),
  2. a broader content area, 3. every image + image URLs in scripts - the last two are narrowed
  to the single biggest folder so related thumbnails can't leak in."""
  for sel in PRECISE_READERS:
    found = bs.select(sel)
    imgs = [i for x in found for i in x.find_all("img")]
    urls = _usable(_img_urls(imgs, base))
    if urls:
      return urls

  for sel in BROAD_READERS:
    found = bs.select(sel)
    urls = _usable(_img_urls([i for x in found for i in x.find_all("img")], base))
    if len(urls) >= 1:
      narrowed = _largest_folder([u for u in urls if "/wp-content/uploads/" in u] or urls)
      if narrowed:
        return _add_script_siblings(bs, narrowed)

  urls = _img_urls(bs.find_all("img"), base)
  for tag in bs.find_all("script"):
    text = (tag.string or tag.get_text() or "")
    if "http" in text:
      urls += [m.group(0).replace("\\/", "/") for m in SCRIPT_IMG_RE.finditer(text)]
  urls = _usable([u for u in urls if "/wp-content/uploads/" in u] or urls)
  return _largest_folder(urls)


# --------------------------------------------------------------------------- #
class AllPornComicsWebs(Scraper):
  def __init__(self):
    super().__init__()
    self.site = DEFAULT_SITE
    self.url = self.site.base
    self.bg = None
    self.sf = "apc"
    self.cs = False               # images: plain headers first; the downloader falls back to Chrome fingerprints on 403
    self.impersonate = "chrome"   # the site rejects plain clients
    self.headers = {"Referer": self.url, "Accept-Language": "en-US,en;q=0.9"}

  async def _page(self, url, **kw):
    for attempt in range(2):
      try:
        html = await self.get(url, cs=True, headers=self.headers, **kw)
        if html:
          return html
      except Exception as e:
        logger.warning(f"{self.site.label}: {url} attempt {attempt + 1} failed: {type(e).__name__}: {e}")
    return None

  # ----------------------------------------------------------------------- #
  async def search(self, query: str = ""):
    query = (query or "").strip()
    urls = [urljoin(self.url, f"?s={quote_plus(query)}&post_type=wp-manga"),
            urljoin(self.url, f"?s={quote_plus(query)}")] if query else [self.url]
    for url in urls:
      html = await self._page(url)
      if not html:
        continue
      results = parse_listing(BeautifulSoup(html, "html.parser"), self.site)
      if results:
        return results
    logger.warning(f"{self.site.label}: no results for {query!r} (blocked, or the layout changed)")
    return []

  # ----------------------------------------------------------------------- #
  async def get_chapters(self, data, page: int = 1):
    results = data
    ss = series_slug(results["url"], self.site)
    if not ss:
      logger.error(f"{self.site.label}: not a series url: {results['url']}")
      results["chapters"] = []
      return results
    prefix, slug = ss
    results["url"] = series_url(prefix, slug, self.site)

    html = await self._page(results["url"])
    if not html:
      logger.error(f"{self.site.label}: could not load {results['url']}")
      results["chapters"] = []
      return results
    bs = BeautifulSoup(html, "html.parser")
    d = parse_details(bs, results.get("title", ""))
    if d["poster"]:
      results["poster"] = d["poster"]
    results["title"] = d["title"] or results.get("title") or _title_from_slug(slug)

    summary = d["desc"][:200] or "N/A"
    if d["artists"]:
      summary = f"Artist: {', '.join(d['artists'][:3])} | {summary}"
    results["msg"] = DEAULT_MSG_FORMAT.format(
      title=results["title"], status=d["status"],
      genres=", ".join(d["genres"][:10]) or "N/A", summary=summary[:260], url=results["url"])

    chapters = parse_chapters(bs, prefix, slug, self.site)

    if not chapters:      # Madara loads the list lazily through this endpoint
      ajax = await self._ajax_chapters(results["url"], bs)
      if ajax:
        chapters = parse_chapters(BeautifulSoup(ajax, "html.parser"), prefix, slug, self.site)

    if not chapters and reader_images(bs, self.url):      # a one-shot: the pictures are on the series page itself
      chapters = [{"title": "Chapter 1", "url": results["url"]}]

    if not chapters:
      logger.error(f"{self.site.label}: no chapters found on {results['url']} ({len(html)} bytes)")

    for c in chapters:
      c["manga_title"] = results["title"]
      c["poster"] = results.get("poster")
    results["chapters"] = chapters
    return results

  @staticmethod
  def _post_id(bs):
    """The WordPress post id of a comic page (Madara exposes it in several places)."""
    node = bs.find(id="manga-chapters-holder") or bs.find(attrs={"data-id": True})
    if node and str(node.get("data-id", "")).isdigit():
      return node["data-id"]
    node = bs.find("input", class_="rating-post-id") or bs.find("input", id="post_ID")
    if node and str(node.get("value", "")).isdigit():
      return node["value"]
    body = bs.find("body")
    m = re.search(r"\bpostid-(\d+)\b", " ".join(body.get("class", [])) if body else "")
    if m:
      return m.group(1)
    m = re.search(r"[\"']manga_id[\"']\s*[:=]\s*[\"']?(\d+)", str(bs))
    return m.group(1) if m else None

  async def _ajax_chapters(self, series, bs=None):
    """Madara loads the chapter list lazily: /<series>/ajax/chapters/ (new) or admin-ajax.php (old)."""
    hdr = {**self.headers, "X-Requested-With": "XMLHttpRequest"}
    try:
      html = await self.post(urljoin(series, "ajax/chapters/"), cs=True, headers=hdr)
      if html and "<a" in html:
        return html
    except Exception as e:
      logger.warning(f"{self.site.label}: ajax chapter list failed: {type(e).__name__}: {e}")
    pid = self._post_id(bs) if bs is not None else None
    if pid:
      try:
        return await self.post(urljoin(self.url, "wp-admin/admin-ajax.php"), cs=True, headers=hdr,
                               data={"action": "manga_get_chapters", "manga": pid})
      except Exception as e:
        logger.warning(f"{self.site.label}: admin-ajax chapter list failed: {type(e).__name__}: {e}")
    return None

  def iter_chapters(self, data, page: int = 1):
    chapters = data.get("chapters") or []
    return chapters[(page - 1) * 60:page * 60] if page != 1 else chapters

  # ----------------------------------------------------------------------- #
  async def get_pictures(self, url, data=None):
    base = url.split("?")[0]
    for candidate in (base, base + "?style=list"):       # ?style=list: every picture on one page
      html = await self._page(candidate)
      if not html:
        continue
      images = reader_images(BeautifulSoup(html, "html.parser"), self.url)
      if images:
        logger.info(f"{self.site.label}: {len(images)} images at {candidate} (first: {images[0]})")
        return [quote(u, safe=":/%?=&+,()~@!$'*;[]#") for u in images]
    logger.error(f"{self.site.label}: no images found at {url}")
    return []
