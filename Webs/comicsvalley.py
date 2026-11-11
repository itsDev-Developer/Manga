import html as _html
import re
import time
from urllib.parse import quote_plus, urljoin, urlparse

from bs4 import BeautifulSoup
from loguru import logger

from .allporncomics import AllPornComicsWebs, Site, _title_from_slug, parse_listing, series_slug, series_url

# --------------------------------------------------------------------------- #
# Comics Valley (https://comicsvalley.com)
#
# The sister site of AllPornComics and the same engine: a WordPress "Madara" comic theme
# (search is /?s=<q>&post_type=wp-manga, taxonomies are /comic-genre/, /comic-author/, /comic-tag/,
# sitemap is Yoast's /sitemap_index.xml). Chapter list, reader pictures, AJAX fallbacks are shared
# with Webs/allporncomics.py; this class adds what is specific to finding comics:
#
#   search, three independent routes (the first that returns anything wins):
#     1. the site's own search page          /?s=<q>&post_type=wp-manga
#     2. the WordPress REST API              /wp-json/wp/v2/wp-manga?search=<q>
#     3. the Yoast sitemap                   /sitemap_index.xml -> the comic sitemaps -> slugs matching the query
#
# The name of the section that holds the comics (/<section>/<comic>/) is not assumed
# (`loose=True`): any two-segment link inside a listing card counts, taxonomy / utility pages
# (/comic-genre/3d/, /comics-new/, /home-3/ ...) are skipped.
#
# Not tested against the live site from the build environment (no network) -
# run `python -m Webs.diagnose comicsvalley` on the server to check each step.
# --------------------------------------------------------------------------- #

BASE = "https://comicsvalley.com/"
SITEMAP_TTL = 900
_SKIP_SITEMAP = re.compile(r"(chapter|genre|tag|author|artist|category|post-sitemap|page-sitemap|attachment|product|user|local)", re.I)


class ComicsValleyWebs(AllPornComicsWebs):
  def __init__(self):
    super().__init__()
    self.site = Site(BASE, ("comicsvalley.com", "www.comicsvalley.com"), loose=True, label="ComicsValley")
    self.url = BASE
    self.sf = "cv"
    self.headers = {"Referer": self.url, "Accept-Language": "en-US,en;q=0.9"}
    self._sitemap = (0.0, [])        # (time, [series urls])

  # ----------------------------------------------------------------------- #
  async def search(self, query: str = ""):
    query = (query or "").strip()

    results = await super().search(query)                 # 1. the site's search page
    if results:
      return results

    results = await self._rest_search(query)              # 2. REST API
    if results:
      logger.info(f"ComicsValley: {len(results)} results for {query!r} from the REST API")
      return results

    results = await self._sitemap_search(query)           # 3. sitemap
    if results:
      logger.info(f"ComicsValley: {len(results)} results for {query!r} from the sitemap")
    else:
      logger.error(f"ComicsValley: nothing found for {query!r} by search page, REST API or sitemap "
                   "(blocked? run `python -m Webs.diagnose comicsvalley` on the server)")
    return results

  async def _rest_search(self, query):
    url = urljoin(self.url, f"wp-json/wp/v2/wp-manga?per_page=20&search={quote_plus(query)}")
    try:
      data = await self.get(url, cs=True, rjson=True, headers={**self.headers, "Accept": "application/json"})
    except Exception as e:
      logger.warning(f"ComicsValley: REST search failed: {type(e).__name__}: {e}")
      return []
    out = []
    for item in data if isinstance(data, list) else []:
      link = (item or {}).get("link") or ""
      ss = series_slug(link, self.site)
      if not ss:
        continue
      title = _html.unescape(re.sub(r"<[^>]+>", "", ((item.get("title") or {}).get("rendered") or ""))).strip()
      og = ((item.get("yoast_head_json") or {}).get("og_image") or [{}])[0].get("url", "")
      out.append({"title": title or _title_from_slug(ss[1]), "poster": og, "url": series_url(*ss, site=self.site)})
    return out

  async def _sitemap_urls(self):
    stamp, cached = self._sitemap
    if cached and time.time() - stamp < SITEMAP_TTL:
      return cached
    urls, seen = [], set()
    try:
      index = await self.get(urljoin(self.url, "sitemap_index.xml"), cs=True, headers=self.headers) or ""
    except Exception as e:
      logger.warning(f"ComicsValley: sitemap index failed: {type(e).__name__}: {e}")
      return cached
    children = [u for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", index)
                if u.endswith(".xml") and not _SKIP_SITEMAP.search(urlparse(u).path)]
    if not children and index:
      children = [urljoin(self.url, "sitemap_index.xml")]       # a flat sitemap: read it directly
    for child in children[:8]:
      try:
        xml = index if child.endswith("sitemap_index.xml") else await self.get(child, cs=True, headers=self.headers)
      except Exception:
        continue
      for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml or ""):
        ss = series_slug(loc, self.site)
        if ss and ss not in seen:
          seen.add(ss)
          urls.append(series_url(*ss, site=self.site))
    if urls:
      self._sitemap = (time.time(), urls)
    return urls or cached

  async def _sitemap_search(self, query):
    urls = await self._sitemap_urls()
    words = [w for w in re.split(r"[^a-z0-9]+", query.lower()) if w]
    hits = [u for u in urls if all(w in u.rstrip("/").rsplit("/", 1)[-1] for w in words)] if words else urls[-20:][::-1]
    return [{"title": _title_from_slug(u.rstrip("/").rsplit("/", 1)[-1]), "poster": "", "url": u} for u in hits[:20]]
