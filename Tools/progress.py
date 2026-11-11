"""Live progress bar shown to the user while a chapter downloads / converts / uploads."""
import html
import time

from bot import Vars

STYLES = {
  "smooth": ("▰", "▱"),
  "blocks": ("█", "░"),
  "dots": ("●", "○"),
  "squares": ("■", "□"),
}


def bar(pct: float, width: int = 12, style: str = None) -> str:
  full, empty = STYLES.get(style or Vars.PROGRESS_STYLE, STYLES["smooth"])
  pct = max(0.0, min(100.0, pct))
  filled = int(round(width * pct / 100))
  return full * filled + empty * (width - filled)


def human_size(n) -> str:
  n = float(n or 0)
  for unit in ("B", "KB", "MB", "GB"):
    if n < 1024 or unit == "GB":
      return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
    n /= 1024


def human_time(seconds) -> str:
  seconds = int(max(0, seconds))
  m, s = divmod(seconds, 60)
  h, m = divmod(m, 60)
  return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class BatchState:
  """Shared by every chapter of one 'download all' / post job."""
  __slots__ = ("total", "done", "failed")

  def __init__(self, total):
    self.total = total
    self.done = 0
    self.failed = 0


class Reporter:
  """Edits one status message, throttled so we never hit Telegram flood limits."""

  def __init__(self, message, title="", batch: BatchState = None):
    self.message = message
    self.title = title
    self.batch = batch
    self._last = 0.0
    self._start = time.time()
    self._stage = None
    self._stage_start = self._start

  @property
  def enabled(self):
    return bool(self.message) and Vars.PROGRESS

  async def update(self, stage, done=0, total=0, nbytes=None, byte_total=None, force=False, note=""):
    """stage: text like '📥 Downloading'.  Either done/total (items) or nbytes/byte_total (bytes)."""
    if not self.enabled:
      return
    now = time.time()
    if stage != self._stage:
      self._stage, self._stage_start, force = stage, now, True
    if not force and now - self._last < max(2, Vars.PROGRESS_INTERVAL):
      return
    self._last = now

    if byte_total:
      pct, count = nbytes * 100 / byte_total, f"{human_size(nbytes)} / {human_size(byte_total)}"
      rate_units = nbytes
      remaining_units = byte_total - nbytes
    elif total:
      pct, count = done * 100 / total, f"{done}/{total} pages"
      rate_units, remaining_units = done, total - done
    else:
      pct, count, rate_units, remaining_units = 0, "", 0, 0

    elapsed = max(now - self._stage_start, 0.001)
    extras = []
    if rate_units and elapsed > 1:
      speed = rate_units / elapsed
      if byte_total or nbytes:
        extras.append(f"{human_size(speed)}/s")
      if speed > 0 and remaining_units > 0:
        extras.append(f"ETA {human_time(remaining_units / speed)}")

    lines = [f"<b>{stage}</b>"]
    if self.title:
      lines.append(f"<blockquote>{html.escape(str(self.title))}</blockquote>")
    if self.batch:
      lines.append(f"📦 Chapter <b>{min(self.batch.done + 1, self.batch.total)}/{self.batch.total}</b>"
                   + (f"  ·  ⚠️ {self.batch.failed} failed" if self.batch.failed else ""))
    lines.append("")
    lines.append(f"<code>{bar(pct)}</code> <b>{pct:.0f}%</b>")
    detail = " · ".join(x for x in [count, *extras] if x)
    if detail:
      lines.append(f"<code>{detail}</code>")
    if note:
      lines.append(f"<i>{html.escape(str(note))}</i>")
    try:
      await self.message.edit_text("\n".join(lines))
    except Exception:
      pass   # MessageNotModified / message deleted / flood - never break the job for a progress edit

  async def finish(self, text):
    if not self.message:
      return
    try:
      await self.message.edit_text(text)
    except Exception:
      pass
