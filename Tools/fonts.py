"""Unicode 'stylish' fonts. Only letters/digits are mapped; everything else is untouched."""

_LOWER = "abcdefghijklmnopqrstuvwxyz"
_UPPER = _LOWER.upper()
_DIGITS = "0123456789"


def _offset_map(upper=None, lower=None, digits=None):
  m = {}
  if upper:
    m.update({c: chr(upper + i) for i, c in enumerate(_UPPER)})
  if lower:
    m.update({c: chr(lower + i) for i, c in enumerate(_LOWER)})
  if digits:
    m.update({c: chr(digits + i) for i, c in enumerate(_DIGITS)})
  return m


_SMALLCAPS = dict(zip(_LOWER, "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ"))
_SMALLCAPS.update({c.upper(): v for c, v in _SMALLCAPS.items()})

FONTS = {
  "bold": _offset_map(0x1D400, 0x1D41A, 0x1D7CE),          # 𝐀𝐛𝐜 𝟏𝟐𝟑
  "bolditalic": _offset_map(0x1D468, 0x1D482, 0x1D7CE),    # 𝑨𝒃𝒄
  "script": _offset_map(0x1D4D0, 0x1D4EA, 0x1D7CE),        # 𝓐𝓫𝓬
  "gothic": _offset_map(0x1D56C, 0x1D586, 0x1D7CE),        # 𝕬𝖇𝖈
  "sans": _offset_map(0x1D5D4, 0x1D5EE, 0x1D7EC),          # 𝗔𝗯𝗰
  "mono": _offset_map(0x1D670, 0x1D68A, 0x1D7F6),          # 𝙰𝚋𝚌
  "wide": _offset_map(0xFF21, 0xFF41, 0xFF10),             # ＡＢＣ
  "smallcaps": _SMALLCAPS,                                 # ᴀʙᴄ
}


def stylize(text: str, font: str = "smallcaps") -> str:
  table = FONTS.get(font)
  return "".join(table.get(ch, ch) for ch in text) if table else text
