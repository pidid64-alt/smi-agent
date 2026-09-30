"""Нормализация: URL, HTML → текст, даты, заголовки (модуль «Сбор и нормализация», ТЗ §61.2)."""

from __future__ import annotations

import hashlib
import html
import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

_TRACKING = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id", "fbclid", "gclid", "yclid", "ysclid",
    "mc_cid", "mc_eid", "ref", "ref_src", "from", "_ga", "igshid", "cmpid", "at_medium", "at_campaign", "ocid", "smid",
    "sr_share", "spm", "share", "mkt_tok", "wt_mc", "ito", "xtor",
}  # fmt: skip
_TRACKER_HOSTS = ("pixel.", "counter.", "stat.", "analytics.", "doubleclick.", "mc.yandex", "top-fwz", "googletagmanager")
_JUNK_CLASS = re.compile(r"(?i)\b(ad|ads|advert|banner|promo|share|social|related|subscribe|comment|newsletter|widget|sidebar|breadcrumb|read-?more|tags?)\b")
_JUNK_PARA = re.compile(r"(?i)^(читайте также|читайте ещё|читайте еще|фото:|фото —|источник фото|подписывайтесь|подпишитесь|смотрите также|"
                        r"материалы по теме|следите за|присоединяйтесь|read more|related:|subscribe|follow us|photo:|источник:\s*$)")


def canonical_url(url: str) -> str:
    parts = urlsplit((url or "").strip())
    host = (parts.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    port = f":{parts.port}" if parts.port and parts.port not in (80, 443) else ""
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False) if k.lower() not in _TRACKING]
    q.sort()
    scheme = "https" if parts.scheme in ("http", "https", "") else parts.scheme
    return urlunsplit((scheme, host + port, path, urlencode(q), ""))


def url_hash(url: str) -> str:
    c = canonical_url(url)
    no_scheme = c.split("://", 1)[-1]
    return hashlib.sha1(no_scheme.encode("utf-8")).hexdigest()  # noqa: S324 — не криптография, ключ дедупликации


def is_tracker_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host.startswith(t) or t in host for t in _TRACKER_HOSTS)


def html_to_text(raw: str | None, *, keep_images: bool = False) -> tuple[str, list[str]]:
    """Возвращает (чистый текст с абзацами, список ссылок на изображения)."""
    if not raw:
        return "", []
    if "<" not in raw and "&" not in raw:
        return re.sub(r"[ \t\u00a0]+", " ", raw).strip(), []
    soup = BeautifulSoup(raw, "lxml")
    images: list[str] = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        w, h = str(img.get("width", "")), str(img.get("height", ""))
        if src.startswith("http") and not is_tracker_url(src) and not (w in ("0", "1") or h in ("0", "1")):
            images.append(src)
    for t in soup(["script", "style", "noscript", "iframe", "form", "nav", "footer", "aside", "svg", "button", "template"]):
        t.decompose()
    for t in soup.find_all(True):
        cls = " ".join(t.get("class", [])) + " " + str(t.get("id", ""))
        if cls.strip() and _JUNK_CLASS.search(cls):
            t.decompose()
    blocks = soup.find_all(["p", "li", "h1", "h2", "h3", "h4", "blockquote"])
    paras: list[str] = []
    if blocks:
        for b in blocks:
            txt = re.sub(r"\s+", " ", b.get_text(" ", strip=True)).strip()
            if txt and not _JUNK_PARA.match(txt):
                paras.append(txt)
    else:
        txt = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
        if txt:
            paras.append(txt)
    text = "\n\n".join(paras)
    text = re.sub(r"\s+([,.;:!?»)])", r"\1", text)
    return html.unescape(text).strip(), images


def clean_title(title: str | None, site_names: list[str] | None = None) -> str:
    t = html.unescape(re.sub(r"\s+", " ", (title or "")).strip())
    t = re.sub(r"<[^>]+>", "", t)
    for name in site_names or []:
        n = re.escape(name.strip())
        t = re.sub(rf"\s*[|\-–—•:]\s*{n}\s*$", "", t, flags=re.IGNORECASE)
    return t.strip(" \u00a0")


def parse_datetime(value: str | None, *, default_tz: str = "UTC", now: datetime | None = None, struct_time=None) -> tuple[datetime | None, list[str]]:
    """Возвращает (aware UTC | None, флаги). Будущие даты обрезаются до «сейчас» (флаг date_future)."""
    flags: list[str] = []
    dt: datetime | None = None
    if value:
        v = value.strip()
        try:
            dt = parsedate_to_datetime(v)
        except (TypeError, ValueError, IndexError):
            dt = None
        if dt is None:
            try:
                dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                dt = None
        if dt is not None and dt.tzinfo is None:
            try:
                dt = dt.replace(tzinfo=ZoneInfo(default_tz))
            except Exception:  # noqa: BLE001
                dt = dt.replace(tzinfo=UTC)
            flags.append("date_naive_tz_assumed")
    if dt is None and struct_time:
        try:
            dt = datetime(*struct_time[:6], tzinfo=UTC)
        except Exception:  # noqa: BLE001
            dt = None
    if dt is None:
        return None, ["date_missing"]
    dt = dt.astimezone(UTC)
    if now is not None and dt > now + timedelta(minutes=10):
        dt = now
        flags.append("date_future")
    return dt, flags


def first_paragraphs(text: str, n: int = 2, max_chars: int = 600) -> str:
    paras = [p for p in (text or "").split("\n\n") if p.strip()]
    return " ".join(paras[:n])[:max_chars]
