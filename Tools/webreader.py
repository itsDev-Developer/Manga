"""Online PDF reader (the /read command).

A user replies to a PDF with /read; the bot stores the file under ./webreader/ and answers
with a link  <PUBLIC_URL>/read/<token>  that opens a mobile-friendly, vertical-scroll
reader page (PDF.js) served by the bot's own HTTP server (Tools/api.py routes /read/*
here).

* The link is the secret: a 192-bit random token, no listing anywhere.
* Files are stored under the token's name only (no user-supplied path ever touches disk).
* Links and files expire after WEB_READER_HOURS; a cleanup loop removes them (and
  anything left over from an earlier run).
* The PDF is streamed from disk in chunks with HTTP Range support, so a big file never
  sits in RAM.
* The page loads PDF.js from cdnjs (pinned version) and renders pages lazily, freeing
  the ones scrolled far away so phones don't run out of memory.
"""
import asyncio
import html
import json
import os
import re
import secrets
import time
from collections import defaultdict, deque

from loguru import logger

from bot import Vars
from Tools.config import hook

DIR = "webreader"
CHUNK = 256 * 1024
PDFJS = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174"
TOKEN_RE = re.compile(r"^/read/([A-Za-z0-9_-]{20,64})(/file\.pdf)?$")

_meta = {}                       # token -> {"name", "owner", "size", "exp"}
_miss = defaultdict(deque)       # ip -> timestamps of unknown-token requests (brute-force throttle)
MISS_LIMIT = 30                  # per minute


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #
def _pdf_path(token): return os.path.join(DIR, f"{token}.pdf")
def _meta_path(token): return os.path.join(DIR, f"{token}.json")


def load():
  """Re-read links that survived a restart (files live on disk)."""
  os.makedirs(DIR, exist_ok=True)
  for fn in os.listdir(DIR):
    if not fn.endswith(".json"):
      continue
    token = fn[:-5]
    try:
      with open(os.path.join(DIR, fn), encoding="utf-8") as f:
        m = json.load(f)
      if os.path.exists(_pdf_path(token)):
        _meta[token] = m
    except Exception as e:
      logger.warning(f"webreader: unreadable {fn}: {e}")


def public_base() -> str:
  """The bot's public address, from settings or the hosting platform's own variables."""
  url = (Vars.PUBLIC_URL or "").strip()
  if not url:
    url = os.environ.get("RENDER_EXTERNAL_URL", "")
  if not url and os.environ.get("KOYEB_PUBLIC_DOMAIN"):
    url = "https://" + os.environ["KOYEB_PUBLIC_DOMAIN"]
  if not url and os.environ.get("HEROKU_APP_NAME"):
    url = f"https://{os.environ['HEROKU_APP_NAME']}.herokuapp.com"
  if url and not url.startswith(("http://", "https://")):
    url = "https://" + url
  return url.rstrip("/")


def new_token() -> str:
  os.makedirs(DIR, exist_ok=True)
  return secrets.token_urlsafe(24)


def tmp_path(token: str) -> str:
  return os.path.abspath(_pdf_path(token))


def register(token: str, name: str, owner: int) -> dict:
  size = os.path.getsize(_pdf_path(token))
  m = {"name": name[:120], "owner": owner, "size": size, "exp": time.time() + max(1, Vars.WEB_READER_HOURS) * 3600}
  _meta[token] = m
  with open(_meta_path(token), "w", encoding="utf-8") as f:
    json.dump(m, f)
  return m


def link(token: str) -> str:
  return f"{public_base()}/read/{token}"


def active_for(owner: int) -> int:
  now = time.time()
  return sum(1 for m in _meta.values() if m["owner"] == owner and m["exp"] > now)


def drop(token: str):
  _meta.pop(token, None)
  for p in (_pdf_path(token), _meta_path(token)):
    try:
      os.remove(p)
    except FileNotFoundError:
      pass
    except OSError as e:
      logger.warning(f"webreader: cannot delete {p}: {e}")


def sweep():
  now = time.time()
  for token in [t for t, m in _meta.items() if m["exp"] <= now]:
    drop(token)
  # orphans: files without a live token (a download that failed half-way, an old run)
  try:
    for fn in os.listdir(DIR):
      token = fn.rsplit(".", 1)[0]
      if token not in _meta and now - os.path.getmtime(os.path.join(DIR, fn)) > 3600:
        os.remove(os.path.join(DIR, fn))
  except FileNotFoundError:
    pass


async def cleanup_loop():
  await asyncio.to_thread(load)
  while True:
    try:
      await asyncio.to_thread(sweep)
    except Exception as e:
      logger.warning(f"webreader cleanup: {e}")
    await asyncio.sleep(600)


@hook("webreader")
def _toggled():
  try:
    from Tools.api import ensure_server
    asyncio.get_running_loop().create_task(ensure_server())
  except RuntimeError:
    pass


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
SEC = {
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "no-referrer",
  "X-Robots-Tag": "noindex, nofollow",
  "Cache-Control": "private, no-store",
}
PAGE_CSP = ("default-src 'none'; script-src 'unsafe-inline' https://cdnjs.cloudflare.com blob:; "
            "worker-src blob: https://cdnjs.cloudflare.com; style-src 'unsafe-inline'; img-src 'self' blob: data:; "
            "connect-src 'self' https://cdnjs.cloudflare.com; frame-ancestors 'none'; base-uri 'none'")

_REASON = {200: "OK", 206: "Partial Content", 304: "Not Modified", 404: "Not Found", 405: "Method Not Allowed",
           416: "Range Not Satisfiable", 429: "Too Many Requests", 503: "Service Unavailable"}


async def _send(writer, status, body: bytes, ctype, extra=None, head=False):
  hdr = {"Content-Type": ctype, "Content-Length": str(len(body)), "Connection": "close", **SEC, **(extra or {})}
  writer.write((f"HTTP/1.1 {status} {_REASON.get(status, 'OK')}\r\n"
                + "".join(f"{k}: {v}\r\n" for k, v in hdr.items()) + "\r\n").encode("latin-1"))
  if not head:
    writer.write(body)
  await writer.drain()


def _text_page(title, msg):
  return (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
          f"<title>{html.escape(title)}</title><body style='font:16px system-ui;background:#111;color:#ddd;"
          f"display:grid;place-items:center;height:100vh;margin:0;text-align:center'><div><h2>{html.escape(title)}</h2>"
          f"<p>{html.escape(msg)}</p></div></body>").encode()


def _throttled(ip) -> bool:
  q, now = _miss[ip], time.monotonic()
  while q and now - q[0] > 60:
    q.popleft()
  if len(_miss) > 2000:                       # don't let the table itself grow forever
    for k in [k for k, v in _miss.items() if not v]:
      _miss.pop(k, None)
  return len(q) >= MISS_LIMIT


async def handle(writer, method, path, headers, ip):
  head = method == "HEAD"
  if method not in ("GET", "HEAD"):
    return await _send(writer, 405, b"Method not allowed", "text/plain", {"Allow": "GET, HEAD"})
  if not Vars.WEB_READER_ENABLED:
    return await _send(writer, 503, _text_page("Reader is off", "The web reader is switched off."), "text/html; charset=utf-8", head=head)
  if _throttled(ip):
    return await _send(writer, 429, b"Too many requests", "text/plain", {"Retry-After": "60"})

  m = TOKEN_RE.match(path)
  meta = _meta.get(m.group(1)) if m else None
  if not m or meta is None or meta["exp"] <= time.time() or not os.path.exists(_pdf_path(m.group(1))):
    _miss[ip].append(time.monotonic())
    if m and meta is not None and meta["exp"] <= time.time():
      drop(m.group(1))
    return await _send(writer, 404, _text_page("Link expired", "This reading link doesn't exist or has expired. Ask the bot for a new one with /read."),
                       "text/html; charset=utf-8", head=head)
  token = m.group(1)

  if m.group(2):
    return await _send_pdf(writer, token, meta, headers, head)
  body = render_page(token, meta).encode("utf-8")
  return await _send(writer, 200, body, "text/html; charset=utf-8", {"Content-Security-Policy": PAGE_CSP}, head=head)


async def _send_pdf(writer, token, meta, headers, head):
  size = os.path.getsize(_pdf_path(token))
  start, end, status = 0, size - 1, 200
  rng = headers.get("range", "")
  rm = re.match(r"^bytes=(\d*)-(\d*)$", rng.strip())
  if rm and (rm.group(1) or rm.group(2)):
    if rm.group(1):
      start = int(rm.group(1))
      end = int(rm.group(2)) if rm.group(2) else size - 1
    else:                                      # suffix range: last N bytes
      start = max(0, size - int(rm.group(2)))
    end = min(end, size - 1)
    if start > end or start >= size:
      return await _send(writer, 416, b"", "application/pdf", {"Content-Range": f"bytes */{size}"})
    status = 206
  length = end - start + 1
  hdr = {"Content-Type": "application/pdf", "Content-Length": str(length), "Accept-Ranges": "bytes",
         "Content-Disposition": "inline", "Connection": "close", **SEC}
  if status == 206:
    hdr["Content-Range"] = f"bytes {start}-{end}/{size}"
  writer.write((f"HTTP/1.1 {status} {_REASON[status]}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdr.items()) + "\r\n").encode("latin-1"))
  if head:
    return await writer.drain()
  with open(_pdf_path(token), "rb") as f:
    f.seek(start)
    left = length
    while left > 0:
      chunk = await asyncio.to_thread(f.read, min(CHUNK, left))
      if not chunk:
        break
      writer.write(chunk)
      await writer.drain()
      left -= len(chunk)


# --------------------------------------------------------------------------- #
# the page
# --------------------------------------------------------------------------- #
PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex,nofollow"><meta name="color-scheme" content="dark">
<title>__TITLE__</title>
<style>
*{box-sizing:border-box}html,body{margin:0;background:#0f0f12;color:#e6e6ea;font:15px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
#bar{position:sticky;top:0;z-index:5;display:flex;gap:10px;align-items:center;padding:8px 12px;padding-top:max(8px,env(safe-area-inset-top));
background:rgba(20,20,24,.94);backdrop-filter:blur(8px);border-bottom:1px solid #2a2a31}
#t{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:600}
#bar button,#bar a{background:#26262d;color:#e6e6ea;border:0;border-radius:8px;padding:6px 11px;font:inherit;text-decoration:none;cursor:pointer}
#pg{color:#9aa;white-space:nowrap;font-variant-numeric:tabular-nums}
#pages{margin:0 auto;padding:8px 0 40vh}
.pg{margin:0 auto 6px;background:#1a1a20;display:block;position:relative}
.pg canvas{display:block;width:100%;height:100%}
#msg{text-align:center;padding:40px 16px;color:#9aa}
</style></head><body>
<div id="bar"><div id="t">__TITLE__</div><span id="pg"></span>
<button id="zo" aria-label="Narrower">&minus;</button><button id="zi" aria-label="Wider">+</button>
<a href="/read/__TOKEN__/file.pdf" download="__FILE__" aria-label="Download">&#11015;</a></div>
<div id="msg">Loading&hellip;</div><div id="pages"></div>
<script src="__PDFJS__/pdf.min.js"></script>
<script>
(function(){
var TOKEN="__TOKEN__",KEY="pdfread:"+TOKEN,box=document.getElementById("pages"),msg=document.getElementById("msg"),pgEl=document.getElementById("pg");
var width=0,pdf=null,holders=[],ratio=1.414,total=0,rendering={};
function ls(k,v){try{if(v===undefined)return localStorage.getItem(k);localStorage.setItem(k,v)}catch(e){return null}}
function maxW(){var w=parseInt(ls("pdfread:w"),10);return w>=400&&w<=1600?w:Math.min(900,window.innerWidth)}
function layout(){width=Math.min(maxW(),window.innerWidth);box.style.width=width+"px";
 holders.forEach(function(h){h.style.height=Math.round(width*(h._r||ratio))+"px"})}
function renderPage(i){var h=holders[i];if(h._on||rendering[i])return;rendering[i]=1;
 pdf.getPage(i+1).then(function(p){
  var v1=p.getViewport({scale:1});h._r=v1.height/v1.width;h.style.height=Math.round(width*h._r)+"px";
  var dpr=Math.min(window.devicePixelRatio||1,2),sc=(width*dpr)/v1.width,v=p.getViewport({scale:sc});
  var c=document.createElement("canvas");c.width=Math.floor(v.width);c.height=Math.floor(v.height);
  return p.render({canvasContext:c.getContext("2d"),viewport:v}).promise.then(function(){
   if(h._gone){c.width=c.height=0;return}h.textContent="";h.appendChild(c);h._on=1;p.cleanup()})
 }).catch(function(e){console.warn(e)}).then(function(){rendering[i]=0})}
function release(i){var h=holders[i];if(!h._on)return;h._on=0;var c=h.firstChild;if(c&&c.tagName==="CANVAS"){c.width=c.height=0}h.textContent=""}
var io=new IntersectionObserver(function(es){es.forEach(function(e){var i=e.target._i;
 if(e.isIntersecting){e.target._gone=0;renderPage(i)}else{e.target._gone=1;release(i)}})},{rootMargin:"1400px 0px"});
var tick=0;function onScroll(){if(tick)return;tick=requestAnimationFrame(function(){tick=0;
 var mid=window.innerHeight*.4,cur=0;for(var i=0;i<holders.length;i++){if(holders[i].getBoundingClientRect().bottom>mid){cur=i;break}}
 pgEl.textContent=(cur+1)+" / "+total;ls(KEY,String(cur+1))})}
addEventListener("scroll",onScroll,{passive:true});
var rt;addEventListener("resize",function(){clearTimeout(rt);rt=setTimeout(function(){
 var old=width;layout();if(width!==old){holders.forEach(function(h,i){if(h._on){release(i);renderPage(i)}})}},200)});
function zoom(d){var w=Math.max(400,Math.min(1600,(width||maxW())+d));ls("pdfread:w",String(w));
 var old=width;layout();if(width!==old){holders.forEach(function(h,i){if(h._on){release(i);renderPage(i)}})}}
document.getElementById("zi").onclick=function(){zoom(120)};document.getElementById("zo").onclick=function(){zoom(-120)};
pdfjsLib.GlobalWorkerOptions.workerSrc="__PDFJS__/pdf.worker.min.js";
pdfjsLib.getDocument({url:"/read/"+TOKEN+"/file.pdf",rangeChunkSize:262144}).promise.then(function(d){
 pdf=d;total=d.numPages;return d.getPage(1)}).then(function(p){
 var v=p.getViewport({scale:1});ratio=v.height/v.width;msg.remove();layout();
 for(var i=0;i<total;i++){var h=document.createElement("div");h.className="pg";h._i=i;box.appendChild(h);holders.push(h)}
 layout();holders.forEach(function(h){io.observe(h)});
 var last=parseInt(ls(KEY),10);if(last>1&&last<=total){holders[last-1].scrollIntoView()}onScroll()
}).catch(function(e){msg.textContent="Could not open this PDF ("+(e&&e.message||e)+")."});
})();
</script></body></html>"""


def render_page(token: str, meta: dict) -> str:
  name = meta.get("name") or "PDF"
  title = html.escape(re.sub(r"\.pdf$", "", name, flags=re.I))
  fname = html.escape(re.sub(r'[^\w .()\-\[\]]', "_", name) or "file.pdf", quote=True)
  return (PAGE.replace("__TITLE__", title).replace("__TOKEN__", token)
          .replace("__FILE__", fname).replace("__PDFJS__", PDFJS))
