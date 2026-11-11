"""Post templates.

Six designs, selectable / previewable from /settings -> Post Design:

  1. Editorial - magazine layout: ✦ ornamental rules, quoted synopsis, ◈ labelled facts, hashtags.
  2. Noir      - dark spec-sheet: ▌accent bar, CAPS title, a framed monospace facts panel.
  3. Zen       - soft minimal: ❀ flower dividers, italic synopsis, one quiet facts line.
  4. Royal     - ♛ banner: ❝ title ❞, ╔═╗ framed synopsis, ❖ facts.
  5. Neon      - ⚡ high-energy: ▰ bars, ⟦ labels ⟧, ✧ tags.
  6. Card      - a ┏━┓ info card with a ┃ side bar, then the synopsis below.

Design rules (what keeps them readable): real Telegram formatting (bold / italic / blockquote /
monospace) for the text; special symbols and borders only for structure. Rules are ~20 characters so
they never wrap on a phone, and closed boxes are only drawn inside monospace <pre> blocks (the only
place alignment is guaranteed) - everywhere else the borders are top/bottom rules or a left bar, which
can't misalign when a long title wraps. "Stylish text" means small-caps labels.

Premium emoji: each accent slot below can be mapped to a custom-emoji id
(/settings -> Post Design -> Premium Emoji). Unmapped slots use the plain
emoji, so posts always render, even without Telegram Premium.
"""
import html
import re

from bot import Vars
from Tools.config import emoji_map
from Tools.fonts import stylize

# slot -> (label shown in settings, fallback emoji). Only the accents the
# templates actually use - deliberately few: restraint is what makes a post
# look premium, and every slot here is one you can swap for a custom emoji.
SLOTS = {
  "star": ("Editorial · title", "⭐"),
  "arrow": ("Editorial · footer", "📌"),
  "fire": ("Noir · hashtags", "🔥"),
  "sparkle": ("Zen · title", "✨"),
  "heart": ("Zen · closing", "🤍"),
  "crown": ("Royal · title & footer", "👑"),
  "bolt": ("Neon · title & footer", "⚡"),
  "book": ("Card · title", "📖"),
}
SLOT_ORDER = list(SLOTS)


def _emoji_tag():
  """pyrogram forks differ: <emoji id=".."> (pyrofork) vs <tg-emoji emoji-id=".."> (kurigram)."""
  try:
    import inspect
    import pyrogram.parser.html as parser
    if "tg-emoji" in inspect.getsource(parser):
      return "tg-emoji", "emoji-id"
  except Exception:
    pass
  return "emoji", "id"


_TAG, _ATTR = _emoji_tag()
_TAG_RE = re.compile(r"<[^>]+>")
_UTF16 = "utf-16-le"


def visible_len(text: str) -> int:
  return len(html.unescape(_TAG_RE.sub("", text)).encode(_UTF16)) // 2


def _tags(genres):
  out = []
  for g in genres:
    g = re.sub(r"\W+", "_", g.strip()).strip("_")
    if g:
      out.append("#" + g)
  return " ".join(out[:8])


def _split_genres(raw):
  if isinstance(raw, (list, tuple)):
    return [str(g) for g in raw]
  raw = str(raw or "")
  raw = raw.replace("#", " ")
  parts = raw.split(",") if "," in raw else raw.split()
  return [p for p in (x.strip() for x in parts) if p and p.upper() != "N/A"]


class _Ctx:
  """Formatting helpers bound to the current settings."""

  def __init__(self, premium=None, stylish=None, footer=None):
    self.stylish = Vars.POST_STYLISH if stylish is None else stylish
    self.premium = Vars.POST_PREMIUM_EMOJI if premium is None else premium
    self.footer = Vars.POST_FOOTER if footer is None else footer
    self._map = emoji_map() if self.premium else {}

  def e(self, slot):
    fallback = SLOTS[slot][1]
    cid = self._map.get(slot)
    if cid:
      return f'<{_TAG} {_ATTR}="{int(cid)}">{fallback}</{_TAG}>'
    return fallback

  def s(self, text, font):
    text = html.escape(str(text))
    return stylize(text, font) if self.stylish else text

  def label(self, text, font="smallcaps"):
    return f"<b>{self.s(text, font)}</b>"

  def cap(self, text):
    """A quiet label: small caps when stylish, plain otherwise."""
    text = html.escape(str(text))
    return stylize(text.lower(), "smallcaps") if self.stylish else text.capitalize()


# --------------------------------------------------------------------------- #
# templates - each returns HTML. `desc` is already escaped and length-limited.
# --------------------------------------------------------------------------- #
_NBSP = "\u00a0"


def _status_dot(status):
  """A purposeful colour cue - the one emoji that carries information."""
  s = (status or "").lower()
  if any(w in s for w in ("ongoing", "publishing", "releasing", "updating")):
    return "🟢"
  if any(w in s for w in ("complete", "finished", "ended")):
    return "🔵"
  if any(w in s for w in ("hiatus", "paused")):
    return "🟡"
  if any(w in s for w in ("cancel", "drop", "discontinu")):
    return "🔴"
  return ""


def _status_text(d):
  status = (d.get("status") or "").strip()
  if not status or status.upper() == "N/A":
    return ""
  dot = _status_dot(status)
  return f"{dot} {html.escape(status)}" if dot else html.escape(status)


def _chapters_text(d):
  n = d.get("chapters")
  if not n:
    return ""
  return f"{n} chapter" + ("" if str(n) == "1" else "s")


def _formats_text(d):
  return html.escape(d["formats"]).replace(" · ", " + ") if d.get("formats") else ""


def _genre_line(d, sep=" · ", n=4):
  return sep.join(html.escape(g) for g in d["genres"][:n])


def _hashtags(d):
  return " ".join(_tags(d["genres"]).split()[:6])


def _facts(d):
  """[(label, value)] of the facts that exist - already HTML-safe."""
  rows = [
    ("Status", _status_text(d)),
    ("Chapters", str(d["chapters"]) if d.get("chapters") else ""),
    ("Format", _formats_text(d)),
    ("Source", html.escape(d["source"]) if d.get("source") else ""),
  ]
  return [(k, v) for k, v in rows if v]


def _fit(text: str, n: int) -> str:
  text = text or ""
  return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def t_editorial(c: _Ctx, d, desc):
  """Magazine layout: ornamental rules around a quoted synopsis, ◈ facts."""
  out = [f"{c.e('star')} <b>{html.escape(d['title'])}</b>"]
  genres = _genre_line(d)
  if genres:
    out.append(f"╰┈➤ <i>{genres}</i>")
  out.append("")
  if desc:
    out += ["✦━━━━━━━━━━━━━━━━━━✦", f"<blockquote expandable>{desc}</blockquote>", "✦━━━━━━━━━━━━━━━━━━✦", ""]
  for label, value in _facts(d):
    out.append(f"◈ {c.cap(label)}{_NBSP}{_NBSP}<b>{value}</b>")
  tags = _hashtags(d)
  if tags:
    out += ["", tags]
  if c.footer:
    out += ["", f"{c.e('arrow')} <i>{html.escape(c.footer)}</i>"]
  return "\n".join(out)


def t_noir(c: _Ctx, d, desc):
  """Dark spec-sheet: accent bar, CAPS title, a framed monospace facts panel."""
  out = [f"▌<b>{html.escape(d['title'].upper())}</b>"]
  genres = _genre_line(d, sep=" / ")
  if genres:
    out.append(f"▌<i>{genres}</i>")
  out.append("")
  raw = [
    ("STATUS", (d.get("status") or "").strip() if _status_text(d) else ""),
    ("CHAPTERS", str(d["chapters"]) if d.get("chapters") else ""),
    ("FORMAT", (d.get("formats") or "").replace(" · ", " + ")),
    ("SOURCE", d.get("source") or ""),
  ]
  raw = [(k, v) for k, v in raw if v]
  if raw:
    lab, val = 9, 17                                   # 30 columns in all: fits a phone without scrolling
    lines = ["┏" + "━" * (lab + val + 2) + "┓"]
    for k, v in raw:
      lines.append("┃ " + f"{k:<{lab}}{_fit(v, val):<{val}}" + " ┃")
    lines.append("┗" + "━" * (lab + val + 2) + "┛")
    out.append("<pre>" + html.escape("\n".join(lines)) + "</pre>")
  if desc:
    out += ["", f"<blockquote expandable>{desc}</blockquote>"]
  tags = _hashtags(d)
  if tags:
    out += ["", f"{c.e('fire')} {tags}"]
  if c.footer:
    out += ["", f"▌<b>{html.escape(c.footer)}</b>"]
  return "\n".join(out)


def t_zen(c: _Ctx, d, desc):
  """Soft minimal: flower dividers, italic synopsis, one quiet facts line."""
  out = [f"{c.e('sparkle')} <b>{html.escape(d['title'])}</b>"]
  genres = _genre_line(d)
  if genres:
    out.append(f"<i>{genres}</i>")
  out.append("")
  if desc:
    out += ["❀ ─── ✿ ─── ❀", f"<blockquote expandable><i>{desc}</i></blockquote>", ""]
  facts = [x for x in (_status_text(d), _chapters_text(d), _formats_text(d)) if x]
  if d.get("source"):
    facts.append(f"via {html.escape(d['source'])}")
  if facts:
    out.append(f"⋆｡˚ {f'{_NBSP}{_NBSP}·{_NBSP}{_NBSP}'.join(facts)} ˚｡⋆")
    out.append("❀ ─── ✿ ─── ❀")
    out.append("")
  closing = html.escape(c.footer) if c.footer else c.cap("happy reading")
  out.append(f"{c.e('heart')} <i>{closing}</i>")
  return "\n".join(out)


def t_royal(c: _Ctx, d, desc):
  """Royal banner: ❝ title ❞, a ╔═╗ framed synopsis, ❖ facts."""
  out = [f"{c.e('crown')} ❝ <b>{html.escape(d['title'])}</b> ❞"]
  genres = _genre_line(d, sep=" ✧ ")
  if genres:
    out.append(f"✧･ﾟ: <i>{genres}</i> :･ﾟ✧")
  out.append("")
  if desc:
    out += ["╔═══════ ✦ ═══════╗", f"<blockquote expandable>{desc}</blockquote>", "╚═══════ ✦ ═══════╝", ""]
  for label, value in _facts(d):
    out.append(f"❖ {c.cap(label)} ⟶ <b>{value}</b>")
  tags = _hashtags(d)
  if tags:
    out += ["", f"✦ {tags}"]
  closing = html.escape(c.footer) if c.footer else ""
  if closing:
    out += ["", f"{c.e('crown')} <i>{closing}</i>"]
  return "\n".join(out)


def t_neon(c: _Ctx, d, desc):
  """High-energy: ▰ bars, ⟦ labels ⟧, ✧ tags."""
  bar = "▰" * 14
  out = [f"{c.e('bolt')} <b>{html.escape(d['title'].upper())}</b>", bar]
  genres = _genre_line(d, sep=" ✦ ")
  if genres:
    out.append(f"〔 <i>{genres}</i> 〕")
  out.append("")
  if desc:
    out += [f"<blockquote expandable>{desc}</blockquote>", ""]
  for label, value in _facts(d):
    out.append(f"⟦ {c.cap(label)} ⟧ <b>{value}</b>")
  out += ["", bar]
  tags = _hashtags(d)
  if tags:
    out.append(f"✧ {tags}")
  if c.footer:
    out.append(f"{c.e('bolt')} <b>{html.escape(c.footer)}</b>")
  return "\n".join(out)


def t_card(c: _Ctx, d, desc):
  """An info card: ┏━┓ frame with a ┃ side bar, synopsis below."""
  rule = "━" * 20
  out = [f"┏{rule}┓", f"┃ {c.e('book')} <b>{html.escape(d['title'])}</b>"]
  genres = _genre_line(d)
  if genres:
    out.append(f"┃ <i>{genres}</i>")
  facts = _facts(d)
  if facts:
    out.append(f"┣{rule}")
    for label, value in facts:
      out.append(f"┃ ▸ {c.cap(label)}: <b>{value}</b>")
  out.append(f"┗{rule}┛")
  if desc:
    out += ["", f"<blockquote expandable>{desc}</blockquote>"]
  tags = _hashtags(d)
  if tags:
    out += ["", tags]
  if c.footer:
    out += ["", f"<i>{html.escape(c.footer)}</i>"]
  return "\n".join(out)


TEMPLATES = {
  1: ("Editorial", t_editorial),
  2: ("Noir", t_noir),
  3: ("Zen", t_zen),
  4: ("Royal", t_royal),
  5: ("Neon", t_neon),
  6: ("Card", t_card),
}

SAMPLE = {
  "title": "Solo Leveling",
  "genres": ["Action", "Fantasy", "Adventure"],
  "status": "Completed",
  "chapters": 200,
  "formats": "PDF · CBZ",
  "source": "Manga Source",
  "description": ("Ten years ago, a portal connecting our world to a world of monsters opened. "
                  "Sung Jin-Woo, the weakest of all hunters, gets a second chance to level up "
                  "in a way no one else can."),
}

MAX_VISIBLE = 1000   # Telegram caption limit is 1024; keep a margin


def _clip(text: str, n: int) -> str:
  text = re.sub(r"\s+", " ", text or "").strip()
  return text if len(text) <= n else text[: max(n - 1, 0)].rstrip() + "…"


def render_post(data: dict, template: int = None, premium=None, stylish=None, footer=None) -> str:
  """data: title, genres|genre, status, description, chapters, formats, source,
  hide_description (True = leave the synopsis out of this post)."""
  template = template or Vars.POST_TEMPLATE
  _, fn = TEMPLATES.get(int(template), TEMPLATES[1])
  c = _Ctx(premium=premium, stylish=stylish, footer=footer)
  d = dict(data)
  d["title"] = _clip(d.get("title") or "Unknown", 90)
  d["genres"] = _split_genres(d.get("genres", d.get("genre")))
  d["status"] = _clip(d.get("status") or "", 30)
  limit = max(40, int(Vars.POST_DESC_LEN))
  hide_desc = bool(d.get("hide_description"))        # the preview's "📝 Description" switch
  while True:
    desc = html.escape(_clip(d.get("description", ""), limit)) if d.get("description") and not hide_desc else ""
    text = fn(c, d, desc).strip()
    if visible_len(text) <= MAX_VISIBLE or limit <= 40:
      return text
    limit = max(40, limit - 40)


def plain_emoji_version(data, template=None):
  """Same post without premium emoji (used if Telegram rejects the custom emoji)."""
  return render_post(data, template, premium=False)
