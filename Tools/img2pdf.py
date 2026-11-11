"""Image download / conversion / PDF building.

Pipeline (one pass, low memory):
  download -> decode -> RGB JPEG at the chosen quality (written once to disk)
  PDF: reportlab embeds those JPEGs untouched and scales pages by PDF units (no re-encode)
  CBZ: zips the very same JPEGs

Copyright (c):-  Rahat4089 and VOATcb   Modified:- Dra-Sama   Optimised rewrite.
"""
import asyncio
import gc
import io
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from time import sleep

import pillow_avif  # noqa: F401  registers AVIF support with Pillow
import pillow_heif
import requests
from cloudscraper import create_scraper
from loguru import logger
from PIL import Image
from reportlab.lib.pdfencrypt import StandardEncryption
from reportlab.pdfgen import canvas

from bot import Vars

try:
  pillow_heif.register_heif_opener()
except Exception:
  pass

Image.MAX_IMAGE_PIXELS = 250_000_000   # webtoon strips are tall, but stop real decompression bombs

def get_headers(base_url: str):
    if "manhuaplus.com" in base_url:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://manhuaplus.com/",
            #"Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Cache-Control": "no-cache",
        }

    elif "mangakatana.com" in base_url:
        headers = {
            "accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            #"accept-encoding": "gzip, deflate, br, zstd"
            "accept-language": "en-GB,en;q=0.8",
            "connection": "keep-alive",
            #host: i.supernova22.click
            "referer": "https://mangakatana.com/",
            "sec-fetch-storage-access": "none",
            "sec-gpc": "1",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
        }
    elif "mangakakalot.gg" in base_url:
        headers = {
            "authority": "imgs-2.2xstorage.com",
            "method": "GET",
            "scheme": "https",
            "accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "accept-encoding": "gzip, deflate, br",
            "accept-language": "en-US,en;q=0.9",
            "cache-control": "no-cache",
            "pragma": "no-cache",
            "referer": "https://www.manganato.gg/",
            "sec-ch-ua": '"Chromium";v="137", "Not(A)Brand";v="24"',
            "sec-ch-ua-mobile": "?1",
            "sec-ch-ua-platform": '"Android"',
            "sec-fetch-dest": "image",
            "sec-fetch-mode": "no-cors",
            "sec-fetch-site": "cross-site",
            "user-agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36"
        }
    else:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Referer": base_url,
        }

    return headers


_POOL = ThreadPoolExecutor(max_workers=16, thread_name_prefix="img")
_tl = threading.local()
_sem = {"n": 0, "sem": None}


IMAGE_ACCEPT = "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"

# Client kinds, as passed via `webs.cs`:
#   False     plain `requests`                         - no browser metadata at all
#   True      cloudscraper                              - solves the old Cloudflare JS challenge
#   "chrome"  curl_cffi, Chrome TLS + Chrome headers   - passes Turnstile-protected PAGES, but it also sends
#                                                        Sec-Fetch-*/Priority headers, which some image CDNs reject
#   "plain"   curl_cffi, Chrome TLS, OUR headers only  - Chrome fingerprint without Sec-Fetch-* (some image CDNs reject browser fetch-metadata headers)
_FALLBACKS = {False: ("plain", "chrome", True), True: ("plain", "chrome", False),
              "chrome": ("plain", False, True), "plain": (False, "chrome", True)}


def _session(cs):
  """One HTTP session per thread (keeps connections alive; cloudscraper is expensive to build)."""
  name = str(cs) if cs else "rq"
  s = getattr(_tl, name, None)
  if s is None:
    if cs in ("chrome", "plain"):
      try:
        from curl_cffi import requests as _curl
        kw = {"default_headers": False} if cs == "plain" else {}
        s = _curl.Session(impersonate="chrome", **kw)
      except Exception as e:
        logger.warning(f"curl_cffi unavailable for '{cs}' ({e}); using requests for images")
        s = requests.Session()
    else:
      s = create_scraper() if cs else requests.Session()
    setattr(_tl, name, s)
  return s


def _headers_for(cs, headers: dict) -> dict:
  """Headers to send with a given client kind."""
  h = dict(headers or {})
  low = {k.lower() for k in h}
  if "accept" not in low:
    h["Accept"] = IMAGE_ACCEPT
  if cs == "chrome":
    # curl_cffi supplies a matching Chrome User-Agent / client hints itself; a different UA
    # (or Sec-*) than its TLS fingerprint is a classic bot signal.
    for k in list(h):
      if k.lower() in ("user-agent", "authority", "method", "scheme") or k.lower().startswith("sec-"):
        del h[k]
  for k in list(h):
    if k.lower() == "origin":
      del h[k]
  return h


def _semaphore():
  n = max(1, int(Vars.DL_THREADS))
  if _sem["n"] != n:
    _sem["n"], _sem["sem"] = n, asyncio.Semaphore(n)
  return _sem["sem"]


class Aborted(Exception):
  pass


def _to_jpeg_bytes(raw: bytes, quality: int, max_side: int = 0) -> bytes:
  with Image.open(io.BytesIO(raw)) as img:
    img.load()
    if max_side and max(img.size) > max_side:       # only ever shrinks (posters); chapter pages pass 0
      img.thumbnail((max_side, max_side), Image.LANCZOS)
    if img.mode not in ("RGB", "L"):
      if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
      else:
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, "JPEG", quality=quality, optimize=False, progressive=False)
    return out.getvalue()


def download_image(idx: str, image_url: str, download_dir: str, headers: dict = None,
                   cs=False, quality: int = 70, abort: threading.Event = None,
                   attempts: int = 3, max_side: int = 0):
  """Download one image and store it as <idx>.jpg. Returns (path, bytes_downloaded).
  `attempts` < 3 fails faster (used for "maybe it exists" poster candidates); `max_side` > 0 caps the longest side."""
  img_path = os.path.join(download_dir, f"{idx.zfill(5)}.jpg")
  headers = headers or {}
  last_error = None
  # attempt 1 uses the client the source asked for; if the CDN refuses it (403 etc.) the next
  # attempts rotate through the other client kinds instead of repeating the same failing request
  ladder = [cs] + [k for k in _FALLBACKS.get(cs, (False, True)) if k != cs]
  for attempt in range(attempts):
    kind = ladder[attempt % len(ladder)]
    if abort is not None and abort.is_set():
      raise Aborted()
    try:
      if not os.path.isdir(download_dir):
        raise Aborted("task cancelled")
      resp = _session(kind).get(image_url, headers=_headers_for(kind, headers), timeout=(10, 45))
      if resp.status_code == 200 and resp.content:
        data = _to_jpeg_bytes(resp.content, quality, max_side)
        with open(img_path, "wb") as f:
          f.write(data)
        return str(img_path), len(resp.content)
      if resp.status_code == 200:
        last_error = f"empty or non-image response ({resp.headers.get('content-type', '?')})"
      elif "Attention Required! | Cloudflare" in resp.text[:2000]:
        last_error = "Cloudflare protection triggered"
      else:
        last_error = f"HTTP {resp.status_code}"
      last_error += f" via {kind or 'requests'}"
    except Aborted:
      raise
    except Exception as e:      # network error or the file is not a valid image
      last_error = repr(e)
    logger.warning(f"Download attempt {attempt + 1}/{attempts} failed ({last_error}) [{kind or 'requests'}]: {image_url}")
    if attempt == attempts - 1:
      break                                       # no point waiting after the last try
    for _ in range(3 * (attempt + 1)):            # interruptible back-off
      if abort is not None and abort.is_set():
        raise Aborted()
      sleep(1)
  raise Exception(f"Failed to download image after {attempts} attempts ({last_error}): {image_url}")


async def thumbnali_images(image_url, download_dir, base_url=None, quality=92, file_name="thumb"):
  """A manga poster / banner image. Tries the full-resolution version of the URL first (see Tools/poster.py),
  then the URL as given. Saved at a high JPEG quality because banners end up inside the chapter files."""
  from Tools.poster import upgrade_candidates
  os.makedirs(download_dir, exist_ok=True)
  headers = get_headers(base_url) if base_url else get_headers("")
  loop = asyncio.get_running_loop()
  candidates = upgrade_candidates(image_url)
  for i, url in enumerate(candidates):
    last = i == len(candidates) - 1
    try:
      path, _ = await loop.run_in_executor(
        _POOL, partial(download_image, file_name, url, download_dir, headers, True, quality, None,
                       3 if last else 2, 2560))
      return path
    except Exception:
      continue
  return None


async def download_and_convert_images(images, download_dir, base_url: str, quality: int = 70,
                                      cs: bool = False, progress=None):
  """Download every image concurrently (bounded by Vars.DL_THREADS).

  progress: optional `async def cb(done, total, bytes_downloaded)`.
  Stops all workers as soon as one image ultimately fails.
  """
  images = [u for u in images if u]
  os.makedirs(download_dir, exist_ok=True)
  headers = get_headers(base_url)
  loop = asyncio.get_running_loop()
  abort = threading.Event()
  sem = _semaphore()
  state = {"done": 0, "bytes": 0}
  total = len(images)

  async def one(idx, url):
    async with sem:
      if abort.is_set():
        raise Aborted()
      path, size = await loop.run_in_executor(
        _POOL, partial(download_image, str(idx), url, download_dir, headers, cs, quality, abort))
      state["done"] += 1
      state["bytes"] += size
      if progress:
        await progress(state["done"], total, state["bytes"])
      return path

  tasks = [asyncio.create_task(one(i, u)) for i, u in enumerate(images, 1)]
  try:
    paths = await asyncio.gather(*tasks)
  except BaseException:
    abort.set()
    for t in tasks:
      t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    raise
  paths.sort()
  return paths


def normalize_jpeg(path: str) -> str:
  """Make sure a user-supplied banner is an RGB JPEG (any format in, .jpg out)."""
  try:
    with open(path, "rb") as f:
      data = _to_jpeg_bytes(f.read(), 85)
    out = os.path.splitext(path)[0] + ".jpg"
    with open(out, "wb") as f:
      f.write(data)
    return out
  except Exception as e:
    logger.warning(f"normalize_jpeg({path}): {e}")
    return None


def make_thumb(path: str) -> str:
  """Telegram thumbnails must be JPEG, <=320px and <200KB."""
  try:
    with Image.open(path) as img:
      img = img.convert("RGB")
      img.thumbnail((320, 320))
      out = os.path.splitext(path)[0] + "_t.jpg"
      img.save(out, "JPEG", quality=80)
    return out
  except Exception as e:
    logger.warning(f"make_thumb({path}): {e}")
    return None


def convert_images_to_pdf(image_files, pdf_output_path, password=None):
  """Build the PDF. Returns None on success or an error string (kept for compatibility)."""
  if not image_files:
    return "No images provided for PDF conversion."

  sizes = []
  for f in image_files:
    with Image.open(f) as im:          # header only, no pixel decoding
      sizes.append(im.size)
  target_width = min(w for w, _ in sizes)

  encrypt = None
  if password:
    encrypt = StandardEncryption(str(password), canPrint=1, canModify=0, canCopy=1, canAnnotate=0, strength=128)

  c = canvas.Canvas(str(pdf_output_path), pagesize=(target_width, target_width), encrypt=encrypt, pageCompression=0)
  for f, (w, h) in zip(image_files, sizes):
    new_height = max(1, int(target_width * h / w))
    c.setPageSize((target_width, new_height))
    c.drawImage(str(f), 0, 0, width=target_width, height=new_height)
    c.showPage()
  c.save()
  gc.collect()
  logger.info(f"PDF created at {pdf_output_path}")
  return None
