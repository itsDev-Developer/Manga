"""High-quality posters.

Why posters looked soft, and what this module does about each cause:

 1. Sites serve a *resized copy* of the poster (WordPress: `cover-193x278.jpg`, Jetpack/Next.js:
    `?w=300`, `/_next/image?url=..&w=256`). `upgrade_candidates()` rebuilds the URL of the original
    file and returns it first, with the URL as given as the last resort.
 2. Telegram re-fetches a poster given as a URL itself and recompresses whatever it finds (often
    refused by hotlink protection too). `poster_for_telegram()` downloads the best version with the
    bot's own client, keeps it at JPEG quality 95 (long side capped at 2560 px - what Telegram keeps
    for its largest photo size anyway) and gives Telegram that file.
 3. Re-encoding at low JPEG quality (was 70-80). Posters are now saved at 92-95.

Everything degrades gracefully: if no better version exists or the download fails, the original URL
is used exactly as before.
"""
import asyncio
import hashlib
import os
import re
import time
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urlparse, urlunparse

from loguru import logger

POSTER_DIR = os.path.join("Process", "posters")
KEEP_SECONDS = 12 * 3600
MAX_FILES = 300

# WordPress adds "-<w>x<h>" before the extension for every generated size
SIZE_SUFFIX = re.compile(r"-\d{2,4}x\d{2,4}(?=\.(?:jpe?g|png|webp|avif|gif)(?:$|[?#]))", re.I)
# query parameters that ask the server for a smaller copy
RESIZE_PARAMS = {"w", "h", "width", "height", "resize", "fit", "size", "crop", "zoom", "s"}
NH_PAGE1 = re.compile(r"^(https?://)i\d*\.nhentai\.net/galleries/(\d+)/1\.(\w+)", re.I)
NH_COVER = re.compile(r"^(https?://)t\d*\.nhentai\.net/galleries/(\d+)/cover\.(\w+)", re.I)


def _unwrap_next_image(url: str) -> str:
  """`/_next/image?url=<encoded real url>&w=..` -> the real url."""
  if "/_next/image" in url:
    inner = parse_qs(urlparse(url).query).get("url", [""])[0]
    if inner:
      inner = unquote(inner)
      if inner.startswith("//"):
        inner = "https:" + inner
      if inner.startswith("http"):
        return inner
  return url


def _drop_resize_params(url: str) -> str:
  parts = urlparse(url)
  if not parts.query:
    return url
  kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in RESIZE_PARAMS]
  if len(kept) == len(parse_qsl(parts.query, keep_blank_values=True)):
    return url
  return urlunparse(parts._replace(query=urlencode(kept)))


def upgrade_candidates(url: str) -> list:
  """URLs to try, best first. The LAST item is always the URL exactly as given."""
  url = (url or "").strip()
  if not url.startswith(("http://", "https://")):
    return [url] if url else []
  out = []

  def add(u):
    if u and u not in out:
      out.append(u)

  unwrapped = _unwrap_next_image(url)
  no_params = _drop_resize_params(unwrapped)
  full = SIZE_SUFFIX.sub("", no_params)

  m = NH_COVER.match(full)
  if m:                                         # nhentai: the cover is a 350px thumbnail; page 1 is the full picture
    add(f"{m.group(1)}i.nhentai.net/galleries/{m.group(2)}/1.{m.group(3)}")
  add(full)
  add(no_params)
  add(unwrapped)
  m = NH_PAGE1.match(url)
  if m:                                         # ...and if page 1 is refused, fall back to the cover
    add(f"{m.group(1)}t.nhentai.net/galleries/{m.group(2)}/cover.{m.group(3)}")
  add(url)
  return out


def referer_for(url: str) -> str:
  """'https://i.nhentai.net/x/1.jpg' -> 'https://nhentai.net/' (CDN hosts usually want the site as referer)."""
  host = urlparse(url).netloc.split(":")[0]
  labels = host.split(".")
  # keep two labels (example.com), or three for suffixes like co.uk / com.br / co.jp
  keep = 3 if len(labels) > 2 and len(labels[-1]) == 2 and labels[-2] in {"co", "com", "org", "net", "gov", "ac", "ne", "or"} else 2
  if len(labels) > keep:
    host = ".".join(labels[-keep:])
  return f"https://{host}/" if host else ""


def _sweep():
  try:
    files = [os.path.join(POSTER_DIR, f) for f in os.listdir(POSTER_DIR)]
  except FileNotFoundError:
    return
  now = time.time()
  files.sort(key=os.path.getmtime)
  for i, f in enumerate(files):
    try:
      if now - os.path.getmtime(f) > KEEP_SECONDS or len(files) - i > MAX_FILES:
        os.remove(f)
    except OSError:
      pass


async def poster_for_telegram(poster, base_url=None, quality: int = 95, timeout: int = 30):
  """An http(s) poster URL -> a local high-quality JPEG path to upload; anything else (a Telegram file id,
  a local path, a failed download) is returned unchanged so the caller's old behaviour still works."""
  if not isinstance(poster, str) or not poster.startswith(("http://", "https://")):
    return poster
  key = hashlib.sha1(poster.encode()).hexdigest()
  path = os.path.join(POSTER_DIR, f"{key}.jpg")
  try:
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < KEEP_SECONDS:
      return path
    from Tools.img2pdf import thumbnali_images          # local import: it pulls in Pillow, reportlab...
    os.makedirs(POSTER_DIR, exist_ok=True)
    await asyncio.to_thread(_sweep)
    got = await asyncio.wait_for(
      thumbnali_images(poster, POSTER_DIR, base_url or referer_for(poster), quality, key), timeout)
    return got or poster
  except Exception as e:
    logger.warning(f"poster: high-quality download failed ({type(e).__name__}: {e}); using the URL as given")
    return poster
