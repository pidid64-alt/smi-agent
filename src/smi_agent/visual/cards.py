"""Визуал (ТЗ §30–31): оригинальные карточки (Pillow) — обложка, «цифра дня», текстовая, источники. JPEG для Instagram."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
# (верх фона, низ фона, акцент) — тёмные фоны: белый текст даёт контраст > 7:1
PALETTES: dict[str, tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]]] = {
    "finance": ((14, 38, 79), (28, 82, 150), (255, 196, 61)),
    "economy": ((14, 38, 79), (28, 82, 150), (255, 196, 61)),
    "business": ((33, 37, 41), (73, 80, 87), (255, 159, 28)),
    "energy": ((52, 30, 16), (120, 66, 18), (255, 183, 77)),
    "ai": ((45, 20, 90), (98, 52, 172), (130, 240, 255)),
    "tech": ((20, 33, 61), (52, 73, 122), (128, 237, 153)),
    "auto": ((28, 28, 30), (72, 72, 78), (255, 99, 71)),
    "transport": ((18, 50, 62), (36, 107, 128), (255, 214, 102)),
    "science": ((12, 58, 50), (24, 120, 98), (200, 255, 130)),
    "unusual": ((74, 24, 60), (142, 48, 110), (255, 214, 102)),
    "health": ((10, 70, 82), (16, 128, 140), (255, 255, 255)),
    "politics": ((36, 36, 48), (78, 78, 98), (220, 220, 230)),
    "incident": ((58, 20, 20), (120, 34, 34), (255, 220, 120)),
    "default": ((18, 42, 66), (34, 86, 120), (255, 196, 61)),
}
SIZES = {"portrait": (1080, 1350), "square": (1080, 1080), "wide": (1200, 675)}


def _font(bold: bool, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_DIR / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")), size)


def _gradient(size: tuple[int, int], top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    w, h = size
    img = Image.new("RGB", size, top)
    px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        c = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        for x in range(w):
            px[x, y] = c
    return img


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split():
            trial = f"{cur} {word}".strip()
            if draw.textlength(trial, font=font) <= max_w or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    return lines


def _fit(draw: ImageDraw.ImageDraw, text: str, box: tuple[int, int], *, bold: bool, max_size: int, min_size: int, spacing: float = 1.22) -> tuple[ImageFont.FreeTypeFont, list[str], int]:
    w, h = box
    for size in range(max_size, min_size - 1, -2):
        font = _font(bold, size)
        lines = _wrap(draw, text, font, w)
        lh = int(size * spacing)
        if len(lines) * lh <= h and all(draw.textlength(ln, font=font) <= w for ln in lines):
            return font, lines, lh
    font = _font(bold, min_size)
    lines = _wrap(draw, text, font, w)
    lh = int(min_size * spacing)
    max_lines = max(1, h // lh)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(" ,.;:") + "…"
    return font, lines, lh


class CardRenderer:
    def _base(self, category: str, size: tuple[int, int]):
        top, bottom, accent = PALETTES.get(category, PALETTES["default"])
        img = _gradient(size, top, bottom)
        return img, ImageDraw.Draw(img), accent

    def _frame(self, draw: ImageDraw.ImageDraw, size: tuple[int, int], accent, kicker: str, brand: str, footer: str = "") -> int:
        w, h = size
        m = int(w * 0.075)
        draw.rectangle([m, m, m + int(w * 0.09), m + 10], fill=accent)
        kf = _font(True, int(w * 0.028))
        draw.text((m, m + 28), kicker.upper()[:40], font=kf, fill=accent)
        bf = _font(True, int(w * 0.03))
        draw.text((m, h - m - int(w * 0.03) - 6), brand[:40], font=bf, fill=(255, 255, 255))
        if footer:
            ff = _font(False, int(w * 0.022))
            tw = draw.textlength(footer, font=ff)
            draw.text((w - m - tw, h - m - int(w * 0.022) - 8), footer, font=ff, fill=(214, 220, 230))
        return m

    def cover(self, headline: str, *, kicker: str, brand: str, category: str, size: str = "portrait", footer: str = "") -> Image.Image:
        sz = SIZES[size]
        img, d, accent = self._base(category, sz)
        m = self._frame(d, sz, accent, kicker, brand, footer)
        w, h = sz
        font, lines, lh = _fit(d, headline, (w - 2 * m, int(h * 0.55)), bold=True, max_size=int(w * 0.088), min_size=int(w * 0.04))
        y = int(h * 0.22)
        for ln in lines:
            d.text((m, y), ln, font=font, fill=(255, 255, 255))
            y += lh
        return img

    def stat(self, number: str, caption: str, *, kicker: str, brand: str, category: str, size: str = "portrait", footer: str = "") -> Image.Image:
        sz = SIZES[size]
        img, d, accent = self._base(category, sz)
        m = self._frame(d, sz, accent, kicker, brand, footer)
        w, h = sz
        font, lines, lh = _fit(d, number, (w - 2 * m, int(h * 0.26)), bold=True, max_size=int(w * 0.24), min_size=int(w * 0.07))
        y = int(h * 0.25)
        for ln in lines:
            d.text((m, y), ln, font=font, fill=accent)
            y += lh
        cf, clines, clh = _fit(d, caption, (w - 2 * m, int(h * 0.25)), bold=False, max_size=int(w * 0.05), min_size=int(w * 0.03))
        y += int(h * 0.03)
        for ln in clines:
            d.text((m, y), ln, font=cf, fill=(255, 255, 255))
            y += clh
        return img

    def text_card(self, text: str, *, kicker: str, brand: str, category: str, size: str = "portrait", footer: str = "") -> Image.Image:
        sz = SIZES[size]
        img, d, accent = self._base(category, sz)
        m = self._frame(d, sz, accent, kicker, brand, footer)
        w, h = sz
        font, lines, lh = _fit(d, text, (w - 2 * m, int(h * 0.55)), bold=False, max_size=int(w * 0.062), min_size=int(w * 0.034))
        y = int(h * 0.24)
        for ln in lines:
            d.text((m, y), ln, font=font, fill=(255, 255, 255))
            y += lh
        return img

    def sources_card(self, names: list[str], note: str, *, kicker: str, brand: str, category: str, size: str = "portrait") -> Image.Image:
        body = "\n".join(f"• {n}" for n in names[:6])
        if note:
            body += "\n\n" + note
        return self.text_card(body, kicker=kicker, brand=brand, category=category, size=size)


def to_jpeg(img: Image.Image, *, ai_generated: bool = False, quality: int = 88) -> bytes:
    """JPEG (Instagram принимает только JPEG). Для ИИ-изображений добавляет XMP DigitalSourceType=trainedAlgorithmicMedia."""
    buf = io.BytesIO()
    kwargs: dict[str, Any] = {"quality": quality, "optimize": True}
    if ai_generated:
        kwargs["xmp"] = AI_XMP
    img.convert("RGB").save(buf, "JPEG", **kwargs)
    return buf.getvalue()


AI_XMP = (
    b'<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>'
    b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    b'<rdf:Description rdf:about="" xmlns:Iptc4xmpExt="http://iptc.org/std/Iptc4xmpExt/2008-02-29/" '
    b'Iptc4xmpExt:DigitalSourceType="http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"/>'
    b'</rdf:RDF></x:xmpmeta><?xpacket end="w"?>'
)


def draw_ai_label(img: Image.Image, text: str) -> Image.Image:
    """Видимая метка «Создано ИИ» в нижнем углу (ТЗ §31: ИИ-изображения не должны вводить аудиторию в заблуждение)."""
    img = img.convert("RGB").copy()
    d = ImageDraw.Draw(img)
    w, h = img.size
    f = _font(True, max(18, int(w * 0.028)))
    tw = d.textlength(text, font=f)
    pad = int(w * 0.012)
    x1, y1 = w - tw - 3 * pad, h - f.size - 3 * pad
    d.rounded_rectangle([x1 - pad, y1 - pad, w - pad, h - pad], radius=pad, fill=(0, 0, 0))
    d.text((x1, y1 - 2), text, font=f, fill=(255, 255, 255))
    return img


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
