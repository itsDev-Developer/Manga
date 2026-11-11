"""
Manga info cards.

Every source module hands back a pre-formatted `msg` blob (Webs/utitls.py).
`parse_manga_msg` pulls the facts out of it - the post flows use that - and
`info_card` re-renders them in the bot's current look, so all sources share
one clean card without touching ten scrapers. If a source's blob can't be
parsed the original text is shown untouched: a card can degrade, never vanish.
"""
import html
import re

from Tools.templates import _status_dot as status_dot
from Tools.ui import esc, quote

_STATUS_RE = re.compile(r"\*\*Status\*\*:\s*`([^`]*)`")
_GENRE_RE = re.compile(r"\*\*Genres\*\*:\s*`([^`]*)`")
_LANG_RE = re.compile(r"\*\*Language\*\*:\s*`([^`]*)`")
_DESC_RE = re.compile(r"\*\*Description\*\*:\s*<blockquote expandable><i>(.*?)</i></blockquote>", re.S)


def parse_manga_msg(bio_list: dict):
  """Best-effort extraction of status/genre/description from the scraper's
  pre-formatted `msg` blob. Every site module in Webs/ builds this from the
  same shared template (Webs/utitls.py), so one parser covers all of them.
  Returns (title, genre, status, description) - any of which may be ""."""
  msg = bio_list.get("msg", "") or ""
  title = bio_list.get("title", "Unknown")

  status_m = _STATUS_RE.search(msg)
  genre_m = _GENRE_RE.search(msg)
  desc_m = _DESC_RE.search(msg)

  status = status_m.group(1).strip() if status_m else ""
  genre = genre_m.group(1).strip() if genre_m else ""
  description = desc_m.group(1).strip() if desc_m else ""
  if description.endswith("..."):
    description = description[:-3].strip()

  return title, genre, status, description


def _clip(text: str, limit: int) -> str:
  text = " ".join(text.split())
  if len(text) <= limit:
    return text
  cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:.-")
  return cut + "…"


def info_card(bio_list: dict, source: str = "") -> str:
  """HTML caption (<= 1024 chars) for a manga's info screen."""
  raw = bio_list.get("msg") or ""
  title, genre, status, description = parse_manga_msg(bio_list)
  if not (status or genre or description):          # unknown layout: show the source's own text
    return (raw or f"<b>{html.escape(title)}</b>")[:1024]

  lang_m = _LANG_RE.search(raw)
  genres = " · ".join(g.strip() for g in genre.split(",") if g.strip() and g.strip().upper() != "N/A")
  out = [f"<b>{esc(title)}</b>"]
  if genres:
    out.append(f"<i>{esc(genres[:140])}</i>")

  facts = []
  if status and status.upper() != "N/A":
    dot = status_dot(status)
    facts.append(f"{dot + ' ' if dot else ''}{esc(status)}")
  if lang_m and lang_m.group(1).strip():
    facts.append(f"🌐 {esc(lang_m.group(1).strip())}")
  if source:
    facts.append(f"via {esc(source)}")
  if facts:
    out += ["", "  ·  ".join(facts)]

  if description:
    plain = re.sub(r"<[^>]+>", " ", html.unescape(description))      # some sources leak markup into summaries
    out += ["", quote(esc(_clip(plain, 520)), expandable=True)]
  if bio_list.get("url"):
    out += ["", f'<a href="{html.escape(bio_list["url"], quote=True)}">Open on the website ›</a>']
  return "\n".join(out)[:1024]
