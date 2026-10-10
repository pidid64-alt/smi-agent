"""Разбор лент: RSS/Atom (feedparser), JSON Feed, sitemap (в т.ч. news-sitemap), HTML-списки по CSS-селектору."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

import feedparser
from bs4 import BeautifulSoup
from lxml import etree

from .normalize import html_to_text, is_tracker_url


@dataclass
class RawItem:
    title: str
    url: str
    summary_html: str = ""
    body_html: str = ""
    published_raw: str = ""
    published_struct: Any = None
    author: str = ""
    section: str = ""
    tags: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    guid: str = ""


def _media_images(entry: Any) -> list[str]:
    out: list[str] = []
    for key in ("media_content", "media_thumbnail"):
        for m in entry.get(key, []) or []:
            u = m.get("url", "")
            if u.startswith("http") and (m.get("medium", "image") == "image" or "image" in m.get("type", "image")):
                out.append(u)
    for enc in entry.get("enclosures", []) or []:
        if "image" in (enc.get("type") or "") and (enc.get("href") or enc.get("url", "")).startswith("http"):
            out.append(enc.get("href") or enc.get("url"))
    return [u for u in dict.fromkeys(out) if not is_tracker_url(u)]


def parse_rss_atom(content: bytes) -> list[RawItem]:
    parsed = feedparser.parse(content)
    items: list[RawItem] = []
    for e in parsed.entries:
        link = e.get("link") or ""
        title = e.get("title") or ""
        if not link or not title:
            continue
        body = ""
        if e.get("content"):
            body = e["content"][0].get("value", "")
        summary = e.get("summary", "") or ""
        items.append(
            RawItem(
                title=title, url=link, summary_html=summary, body_html=body or "",
                published_raw=e.get("published") or e.get("updated") or "",
                published_struct=e.get("published_parsed") or e.get("updated_parsed"),
                author=e.get("author", "") or "",
                section=(e.get("tags") or [{}])[0].get("term", "") if e.get("tags") else "",
                tags=[t.get("term", "") for t in (e.get("tags") or []) if t.get("term")][:10],
                images=_media_images(e), guid=e.get("id", "") or link,
            )
        )
    return items


def parse_json_feed(content: bytes) -> list[RawItem]:
    data = json.loads(content.decode("utf-8", errors="replace"))
    items: list[RawItem] = []
    for it in data.get("items", []):
        url = it.get("url") or it.get("external_url") or ""
        title = it.get("title") or ""
        if not url or not title:
            continue
        items.append(
            RawItem(
                title=title, url=url, summary_html=it.get("summary", "") or "",
                body_html=it.get("content_html", "") or it.get("content_text", "") or "",
                published_raw=it.get("date_published", "") or "", author=((it.get("author") or {}).get("name", "") if isinstance(it.get("author"), dict) else ""),
                tags=list(it.get("tags", []))[:10], images=[it["image"]] if it.get("image") else [], guid=it.get("id", url),
            )
        )
    return items


_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9", "news": "http://www.google.com/schemas/sitemap-news/0.9", "image": "http://www.google.com/schemas/sitemap-image/1.1"}


def parse_sitemap(content: bytes) -> tuple[list[RawItem], list[tuple[str, str]]]:
    """Возвращает (items, children[(loc, lastmod)]). Для sitemapindex items пуст, children заполнен."""
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, recover=True)  # XXE-safe
    root = etree.fromstring(content, parser)
    if root is None:
        return [], []
    tag = etree.QName(root).localname
    if tag == "sitemapindex":
        kids = []
        for sm in root.findall("sm:sitemap", _NS):
            loc = (sm.findtext("sm:loc", default="", namespaces=_NS) or "").strip()
            mod = (sm.findtext("sm:lastmod", default="", namespaces=_NS) or "").strip()
            if loc:
                kids.append((loc, mod))
        kids.sort(key=lambda kv: kv[1], reverse=True)
        return [], kids
    items: list[RawItem] = []
    for u in root.findall("sm:url", _NS):
        loc = (u.findtext("sm:loc", default="", namespaces=_NS) or "").strip()
        nw = u.find("news:news", _NS)
        if not loc or nw is None:
            continue
        title = (nw.findtext("news:title", default="", namespaces=_NS) or "").strip()
        pub = (nw.findtext("news:publication_date", default="", namespaces=_NS) or "").strip()
        kw = (nw.findtext("news:keywords", default="", namespaces=_NS) or "").strip()
        imgs = [(i.findtext("image:loc", default="", namespaces=_NS) or "").strip() for i in u.findall("image:image", _NS)]
        if title:
            items.append(RawItem(title=title, url=loc, published_raw=pub, tags=[k.strip() for k in kw.split(",") if k.strip()][:10], images=[i for i in imgs if i.startswith("http")]))
    return items, []


def parse_html_list(content: bytes, base_url: str, cfg: dict[str, Any]) -> list[RawItem]:
    """Минимальный скрейпер списков: cfg = {item_selector: 'a.news', title_selector?: '...'}; даты берутся из time[datetime]."""
    soup = BeautifulSoup(content, "lxml")
    sel = cfg.get("item_selector")
    if not sel:
        return []
    items: list[RawItem] = []
    for a in soup.select(sel)[:80]:
        href = a.get("href") or (a.find("a") or {}).get("href", "") if hasattr(a, "find") else ""
        title = a.get_text(" ", strip=True)
        if not href or not title:
            continue
        t = a.find_parent().find("time") if a.find_parent() else None
        items.append(RawItem(title=title, url=urljoin(base_url, href), published_raw=(t.get("datetime", "") if t else "")))
    return items


def extract_main_text(content: bytes) -> str:
    """Главный текст страницы: <article> или блок с наибольшим числом абзацев."""
    soup = BeautifulSoup(content, "lxml")
    for t in soup(["script", "style", "noscript", "nav", "footer", "aside", "form"]):
        t.decompose()
    node = soup.find("article")
    if node is None:
        best, best_n = None, 0
        for div in soup.find_all(["div", "section", "main"]):
            n = len(div.find_all("p", recursive=False))
            if n > best_n:
                best, best_n = div, n
        node = best or soup.body
    if node is None:
        return ""
    text, _ = html_to_text(str(node))
    return text


def parse_items(kind: str, content: bytes, *, base_url: str = "", config: dict[str, Any] | None = None) -> tuple[list[RawItem], list[tuple[str, str]]]:
    kind = (kind or "rss").lower()
    if kind in ("rss", "atom"):
        return parse_rss_atom(content), []
    if kind == "json_feed":
        return parse_json_feed(content), []
    if kind == "sitemap":
        return parse_sitemap(content)
    if kind == "html_list":
        return parse_html_list(content, base_url, config or {}), []
    return [], []
