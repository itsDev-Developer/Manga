"""Check a source against the live site, step by step.

    python -m Webs.diagnose hentaifox  [search text | gallery id]
    python -m Webs.diagnose allporncomics [search text]
    python -m Webs.diagnose comicsvalley  [search text]
    python -m Webs.diagnose nhentai       [search text | gallery id]

Prints what each stage returned (comic list / search, chapters, pictures) so a
broken selector or a blocked request is obvious. Paste the output when reporting a bug.
"""
import asyncio
import sys


def _site(name):
  from . import AllPornComicsWebs, ComicsValleyWebs, HentaiFoxWebs, NHentaiWebs
  return {"hentaifox": HentaiFoxWebs, "allporncomics": AllPornComicsWebs,
          "comicsvalley": ComicsValleyWebs, "nhentai": NHentaiWebs}[name]()


async def main(name, query):
  web = _site(name)
  import bot as _bot
  flare = (getattr(_bot.Vars, "FLARESOLVERR_URL", "") or "").strip()
  print(f"[0] can the bot reach {web.url} ?   (FlareSolverr: {flare or 'not configured'})")
  try:
    page = await web.get(web.url, cs=True)
  except Exception as e:
    page = None
    print(f"    request error: {type(e).__name__}: {e}")
  print(f"    -> {'OK, ' + str(len(page)) + ' bytes' if page else 'NO PAGE'}   [{getattr(web, 'last_diag', '')}]")
  if not page and "blocked" in getattr(web, "last_diag", ""):
    print("    !! The site is blocking the bot (bot protection). Update curl_cffi (pip install -U curl_cffi), or set a FlareSolverr")
    print("       address in /settings -> Source Access (a real browser the bot hands blocked pages to).")
  elif not page and "curl_cffi" in getattr(web, "last_diag", "") + str(getattr(web, "impersonate", "")):
    print("    !! pip install curl_cffi")
  print(f"[1] search({query!r})")
  results = await web.search(query)
  print(f"    -> {len(results)} results")
  for r in results[:5]:
    print("      ", r.get("title"), "|", r.get("url"), "| poster:", bool(r.get("poster")))
  if not results:
    return print(f"    !! nothing found: blocked, wrong query, or layout changed   [{getattr(web, 'last_diag', '')}]")

  print("[2] get_chapters(first result)")
  info = await web.get_chapters(dict(results[0]))
  chapters = web.iter_chapters(info, page=1)
  print(f"    -> {len(chapters)} chapters; msg ok: {bool(info.get('msg'))}")
  for c in chapters[:3]:
    print("      ", c["title"], "|", c["url"])
  if not chapters:
    return print("    !! no chapters")

  print("[3] get_pictures(first chapter)")
  pics = await web.get_pictures(chapters[0]["url"], chapters[0])
  print(f"    -> {len(pics)} images")
  for p in pics[:3]:
    print("      ", p)
  if pics:
    import tempfile
    from Tools.img2pdf import download_image, get_headers
    d = tempfile.mkdtemp()
    try:     # the same code path the bot uses, incl. its client fallbacks (watch the warnings above)
      path, size = await asyncio.to_thread(download_image, "1", pics[0], d, get_headers(web.url), getattr(web, "cs", False))
      print(f"[4] first image downloaded OK: {size} bytes")
    except Exception as e:
      print(f"[4] first image FAILED: {e}")

if __name__ == "__main__":
  if len(sys.argv) < 2 or sys.argv[1] not in ("hentaifox", "allporncomics", "comicsvalley", "nhentai"):
    sys.exit(__doc__)
  asyncio.run(main(sys.argv[1], " ".join(sys.argv[2:])))
