"""База фактов (ТЗ §21, §50): единственный источник утверждений для текста. Нет в базе фактов — нет в тексте."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.text import sentences
from ..db.models import Article, Event, Verification
from ..ingestion.features import extract_numbers


@dataclass
class Fact:
    id: int
    text: str
    sources: list[str]
    support: int
    attributed_to: str | None  # None — подтверждено несколькими/первоисточником, иначе «по данным …»
    numbers: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Quote:
    text: str
    speaker: str | None
    source: str
    source_name: str


@dataclass
class FactBase:
    event_id: int
    title: str
    category: str
    geo: str
    facts: list[Fact]
    quotes: list[Quote]
    dates: list[str]
    entities: list[str]
    sources: list[dict[str, Any]]
    unknowns: list[str]
    interpretations: list[str]
    verification: dict[str, Any]
    languages: list[str]
    sensitive: dict[str, Any]
    political: bool
    source_texts: dict[str, str]  # для проверки оригинальности
    subtopics: list[str] = field(default_factory=list)
    all_numbers: list[dict[str, Any]] = field(default_factory=list)

    def for_prompt(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("source_texts", None)
        d["facts"] = [{"id": f.id, "text": f.text, "attributed_to": f.attributed_to, "support": f.support} for f in self.facts]
        d["quotes"] = [{"text": q.text, "speaker": q.speaker, "source": q.source_name} for q in self.quotes]
        return d


_SPEAKER = re.compile(r"(?:—|-|,)\s*(?:заявил[а]?|сказал[а]?|отметил[а]?|подчеркнул[а]?|рассказал[а]?|добавил[а]?|пояснил[а]?|said|says|added|noted|stated)\s+([^.]{3,70})", re.IGNORECASE)
_SPEAKER_BEFORE = re.compile(r"([A-ZА-ЯЁ][^.«\"“]{3,70}?)\s+(?:заявил[а]?|сказал[а]?|отметил[а]?|подчеркнул[а]?|said|says)\s*[:,]?\s*$", re.IGNORECASE)


def _find_speaker(body: str, quote: str) -> str | None:
    i = body.find(quote[:40])
    if i < 0:
        return None
    after = body[i + len(quote) : i + len(quote) + 120]
    m = _SPEAKER.search(after[:100])
    if m:
        return m.group(1).strip(" ,.—-")[:70]
    before = body[max(0, i - 100) : i].rstrip(" «\"“:")
    m = _SPEAKER_BEFORE.search(before)
    return m.group(1).strip()[:70] if m else None


_STRAIGHT_Q = re.compile(r'"([^"]{3,})"')


def typo_quotes(text: str) -> str:
    return _STRAIGHT_Q.sub(r"«\1»", text)


def brand_name(name: str) -> str:
    """«Kursiv.media (Курсив)» → «Kursiv.media»; «BBC News — World» → «BBC News»: в публикациях — название бренда."""
    return re.sub(r"\s*[—–(].*$", "", name).strip() or name


def build_factbase(s: Session, ev: Event, ver: Verification | None, *, max_facts: int = 8) -> FactBase:
    arts = list(s.scalars(select(Article).where(Article.event_id == ev.id).order_by(Article.published_at)))
    by_key = {a.source.key: a for a in arts}
    src_names = {a.source.key: brand_name(a.source.name) for a in arts}
    facts: list[Fact] = []
    raw_facts = list(ver.facts) if ver and ver.facts else [{"text": f["text"], "source": f.get("source"), "support": 1} for f in (ev.features or {}).get("facts", [])]
    for i, f in enumerate(raw_facts[:max_facts], 1):
        key = f.get("source") or ""
        support = int(f.get("support", 1))
        attributed = None if (support >= 2 or (ver and (ver.primary_source or {}).get("found"))) else (f"по данным {src_names.get(key, key)}" if key else None)
        facts.append(Fact(i, typo_quotes(f["text"]), [key] if key else [], support, attributed, extract_numbers(f["text"])))
    quotes: list[Quote] = []
    for q in (ev.features or {}).get("quotes", [])[:4]:
        key = (q.get("sources") or [""])[0]
        a = by_key.get(key)
        quotes.append(Quote(q["text"], _find_speaker(a.body, q["text"]) if a else None, key, src_names.get(key, key)))
    sources = []
    for a in arts:
        sources.append({"key": a.source.key, "name": brand_name(a.source.name), "url": a.url, "tier": a.source.tier, "country": a.source.country, "independent": a.independent, "official": a.source.is_official, "relation": a.relation})
    unknowns = list(ver.warnings) if ver else []
    if ver:
        unknowns += [c["detail"] for c in ver.checks if c["status"] == "warn" and c["detail"] not in unknowns][:3]
    return FactBase(
        event_id=ev.id, title=ev.title, category=ev.category, geo=ev.geo, facts=facts, quotes=quotes, dates=list((ev.features or {}).get("dates", [])),
        entities=list(ev.keywords or [])[:8], sources=sources, unknowns=unknowns[:6], interpretations=[i["text"] for i in (ver.interpretations if ver else [])][:4],
        verification={"status": ev.verification_status, "warnings": list(ver.warnings) if ver else []}, languages=list(ev.languages or []),
        sensitive=dict((ev.flags or {}).get("sensitive", {})), political=bool((ev.flags or {}).get("political")),
        source_texts={a.source.key + f"#{a.id}": f"{a.title}. {a.body or a.summary}" for a in arts},
        subtopics=list(ev.subtopics or []),
        all_numbers=[{"raw": n["raw"], "value": n["v"], "unit": n["u"]} for n in (ev.features or {}).get("numbers", [])],
    )  # fmt: skip


def split_sentences_safe(text: str) -> list[str]:
    return sentences(text)
