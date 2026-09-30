"""Извлечение признаков статьи: сущности, числа, даты, цитаты, ссылки на источники, флаги качества."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..core.text import detect_language, normalize_text, sentences, skeleton, stem, tokenize
from ..knowledge import Knowledge

_UP = "A-ZА-ЯЁӘҒҚҢӨҰҮҺІ"
_LOW = "a-zа-яёәғқңөұүһі"
_ENTITY = re.compile(rf"(?<![\w-])([{_UP}][\w\-]*(?:\s+(?:of|de|la|van|von|bin|ibn|al|и|в)?\s*[{_UP}][\w\-]*)*)")
_ACRONYM = re.compile(rf"(?<![\w-])([{_UP}]{{2,}}[0-9]*)(?![\w-])")
_STOP_CAPS = {
    "января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря",
    "понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье", "january", "february", "march", "april", "may",
    "june", "july", "august", "september", "october", "november", "december", "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday", "the", "this", "that", "they", "according", "while", "after", "before", "когда", "после", "также", "однако",
    "как", "это", "его", "она", "они", "по", "об", "в", "на", "для", "что", "при", "фото", "видео", "читайте", "сообщает", "передает",
    "по словам", "по данным", "новости", "источник", "there", "here", "what", "why", "how", "who", "new", "says", "said", "reuters", "ap",
    "on", "in", "at", "by", "for", "from", "with", "as", "but", "and", "if", "it", "its", "he", "she", "we", "our", "their", "a", "an",
    "of", "to", "is", "are", "was", "were", "be", "в", "на", "из", "за", "от", "до", "об", "для", "про", "со", "во", "но", "и", "а",
    "да", "же", "уже", "еще", "ещё", "так", "вот", "тоже", "все", "всё", "наш", "наша", "этот", "эта", "эти", "тот", "та", "те",
}  # fmt: skip
_MONTHS = {
    **{m: i for i, m in enumerate(["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"], 1)},
    **{m: i for i, m in enumerate(["қаңтар", "ақпан", "наурыз", "сәуір", "мамыр", "маусым", "шілде", "тамыз", "қыркүйек", "қазан", "қараша", "желтоқсан"], 1)},
    **{m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1)},
}
_MONTH_RX = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DATE_DM = re.compile(rf"(?<!\d)(\d{{1,2}})\s+({_MONTH_RX})\w*(?:\s+(\d{{4}}))?", re.IGNORECASE)
_EN_MONTH_RX = "|".join(m for m in _MONTHS if m.isascii())
_DATE_MD = re.compile(rf"\b({_EN_MONTH_RX})\s+(\d{{1,2}})(?!\d)(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?", re.IGNORECASE)
_DATE_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_DATE_DOT = re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})\.(\d{4})(?![\d.])")

_UNIT_MAP = [
    (r"%|процент\w*|percent|пайыз", "pct"), (r"пункт\w*|п\.п\.|bps|basis points|points?", "pts"),
    (r"тг\b|тенге|теңге|₸", "kzt"), (r"\$|долл\w*|usd", "usd"), (r"€|евро|eur\b", "eur"), (r"руб\w*|₽|rub\b", "rub"), (r"юан\w*|cny", "cny"),
    (r"км/ч", "kmh"), (r"км\b|км²|километр\w*", "km"), (r"кг\b|килограмм\w*", "kg"), (r"тонн\w*|\bт\b", "t"), (r"барр\w*|barrel\w*|bbl", "bbl"),
    (r"человек|чел\b\.?|people|persons", "people"), (r"лет\b|года\b|году\b|years?", "years"), (r"квартир\w*|apartments?", "apt"),
]  # fmt: skip
_SCALE = [
    (r"трлн|триллион\w*|trillion", 1e12), (r"млрд|миллиард\w*|billion", 1e9), (r"млн|миллион\w*|million", 1e6), (r"тыс\b\.?|тысяч\w*|thousand", 1e3),
]  # fmt: skip
_NUM_RX = re.compile(
    r"(?<![\w.,/-])(\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:([.,])(\d{1,3}(?!\d)|\d+))?"
    r"(?:\s*(трлн|триллион\w*|trillion|млрд|миллиард\w*|billion|млн|миллион\w*|million|тыс\b\.?|тысяч\w*|thousand))?"
    r"\s*(%|процент\w*|percent|пайыз|пункт\w*|bps|тг\b|тенге|теңге|₸|\$|долл\w*|usd|€|евро|eur\b|руб\w*|₽|юан\w*|км/ч|км\b|кг\b|тонн\w*|барр\w*|barrel\w*|человек|чел\b\.?|people|лет\b|года\b|году\b|years?|квартир\w*)?",
    re.IGNORECASE,
)
_CURRENCY_PREFIX = re.compile(r"([$€₽])\s*$")
_QUOTE_RX = [re.compile(r"«([^»]{20,420})»"), re.compile(r"“([^”]{20,420})”"), re.compile(r'"([^"]{20,420})"')]
_DOMAIN = re.compile(r"\b[\w\-]+\.(?:kz|com|ru|org|net|media|info|uz|kg|gov)\b", re.IGNORECASE)
_ATTR_NAMES = re.compile(rf"[\w\-]+\.(?i:kz|com|ru|org|net|media|info)\b|[{_UP}][\w\-]*(?:\s+[{_UP}][\w\-]*){{0,3}}")
_SENT_END = re.compile(rf"(?<=[{_LOW}0-9])[.!?]\s+(?=[{_UP}])")


def _to_float(int_part: str, sep: str | None, frac: str | None, lang: str) -> float | None:
    ip = re.sub(r"[ \u00a0\u202f]", "", int_part)
    try:
        if sep and frac:
            if len(frac) == 3 and lang == "en" and sep == ",":
                return float(ip + frac)  # en: 1,234 → тысячи
            return float(f"{ip}.{frac}")
        return float(ip)
    except ValueError:
        return None


def extract_numbers(text: str, lang: str = "ru") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    t = text or ""
    # en: 1,234,567 → без запятых
    t = re.sub(r"(?<!\d)(\d{1,3}(?:,\d{3}){2,})(?!\d)", lambda m: m.group(1).replace(",", ""), t)
    for m in _NUM_RX.finditer(t):
        int_part, sep, frac, scale_w, unit_w = m.groups()
        val = _to_float(int_part, sep, frac, lang)
        if val is None:
            continue
        unit = ""
        if unit_w:
            for rx, name in _UNIT_MAP:
                if re.fullmatch(rx, unit_w.lower()) or re.match(rx, unit_w.lower()):
                    unit = name
                    break
        if not unit:
            pre = _CURRENCY_PREFIX.search(t[max(0, m.start() - 2) : m.start()])
            if pre:
                unit = {"$": "usd", "€": "eur", "₽": "rub"}[pre.group(1)]
        if scale_w:
            for rx, mult in _SCALE:
                if re.match(rx, scale_w.lower()):
                    val *= mult
                    break
        # отсекаем «голые» числа: годы, дни месяца, мелкие значения без единиц
        nxt = t[m.end() : m.end() + 14].lower().strip()
        if unit == "years" and 1900 <= val <= 2100:
            continue
        if not unit and not scale_w:
            if 1900 <= val <= 2100 and float(int(val)) == val:
                continue
            if val < 100:
                continue
            if re.match(rf"({_MONTH_RX})", nxt):
                continue
        out.append({"v": round(val, 4), "u": unit, "raw": m.group(0).strip()[:40]})
    seen, uniq = set(), []
    for n in out:
        key = (n["u"], round(n["v"], 2))
        if key not in seen:
            seen.add(key)
            uniq.append(n)
    return uniq[:20]


def extract_dates(text: str) -> list[str]:
    out: list[str] = []
    t = text or ""
    for d, mon, y in _DATE_DM.findall(t):
        m = _MONTHS.get(mon.lower())
        if m and 1 <= int(d) <= 31:
            out.append(f"{y + '-' if y else ''}{m:02d}-{int(d):02d}")
    for mon, d, y in _DATE_MD.findall(t):
        m = _MONTHS.get(mon.lower())
        if m and 1 <= int(d) <= 31:
            out.append(f"{y + '-' if y else ''}{m:02d}-{int(d):02d}")
    for y, m, d in _DATE_ISO.findall(t):
        out.append(f"{y}-{m}-{d}")
    for d, m, y in _DATE_DOT.findall(t):
        if 1 <= int(m) <= 12 and 1 <= int(d) <= 31:
            out.append(f"{y}-{int(m):02d}-{int(d):02d}")
    return list(dict.fromkeys(out))[:10]


def extract_quotes(text: str) -> list[dict[str, str]]:
    out, seen = [], set()
    for rx in _QUOTE_RX:
        for m in rx.finditer(text or ""):
            q = m.group(1).strip()
            if len(q.split()) < 4:
                continue
            norm = re.sub(r"[^\w\s]", "", q.lower())
            norm = re.sub(r"\s+", " ", norm)
            if norm in seen:
                continue
            seen.add(norm)
            out.append({"text": q[:420], "norm": norm[:300]})
    return out[:8]


def extract_entities(title: str, body: str, *, limit: int = 14) -> list[dict[str, Any]]:
    counts: dict[str, dict[str, Any]] = {}

    def add(raw: str, in_title: bool, sentence_start: bool):
        words = raw.strip(" -").split()
        while words and words[0].lower() in _STOP_CAPS:
            words.pop(0)
            sentence_start = False
        while words and words[-1].lower() in _STOP_CAPS:
            words.pop()
        ent = " ".join(words)
        low = ent.lower()
        if len(ent) < 2 or low in _STOP_CAPS or ent.isdigit():
            return
        if sentence_start and " " not in ent and not ent.isupper():
            # первое слово предложения — сущность, только если это латиница либо слово ≥5 букв, встречающееся в тексте (по основе)
            if not (in_title and (ent.isascii() or (len(ent) >= 5 and ent[:5].lower() in (body or "").lower()))):
                return
        key = skeleton(low.replace(" ", ""))
        if not key or len(key) < 2:
            return
        d = counts.setdefault(key, {"t": ent, "k": key, "n": 0, "title": False, "acr": ent.isupper() and len(ent) <= 6, "multi": " " in ent})
        d["n"] += 1
        d["title"] = d["title"] or in_title
        if len(ent) > len(d["t"]) and d["t"].lower() in low:
            d["t"] = ent

    for src, in_title in ((title or "", True), ((body or "")[:4000], False)):
        src = _DOMAIN.sub(" ", src)
        for sent in sentences(src) or [src]:
            for m in _ENTITY.finditer(sent):
                add(m.group(1), in_title, m.start() == 0)
            for m in _ACRONYM.finditer(sent):
                add(m.group(1), in_title, False)
    ranked = sorted(counts.values(), key=lambda d: (-(d["title"] * 3 + d["n"] + d["multi"]), -len(d["t"])))
    return ranked[:limit]


def extract_attribution(text: str, know: Knowledge, alias_index: dict[str, str]) -> dict[str, Any]:
    """«передает X со ссылкой на Y» → список ключей известных источников и «сырых» имён."""
    cited_keys: list[str] = []
    names: list[str] = []
    t = text or ""
    m_rx = know.lex.get("attribution_markers")
    if not m_rx or not m_rx._rx:  # noqa: SLF001
        return {"keys": [], "names": [], "marker_count": 0}
    t_low = re.sub(r"\s+", " ", t.lower())
    count = 0
    for m in m_rx._rx.finditer(t_low):  # noqa: SLF001
        count += 1
        window_low = t_low[m.end() : m.end() + 90]
        window = re.sub(r"\s+", " ", t)[m.end() : m.end() + 90] if len(t_low) == len(re.sub(r"\s+", " ", t)) else window_low
        window = _SENT_END.split(window, 1)[0]
        for alias, key in alias_index.items():
            if alias in window_low and key not in cited_keys:
                cited_keys.append(key)
        for nm in _ATTR_NAMES.findall(window)[:3]:
            nm = nm.strip(" .,-")
            if nm.lower() not in _STOP_CAPS and nm not in names and len(nm) > 2:
                names.append(nm)
    return {"keys": cited_keys[:6], "names": names[:6], "marker_count": count}


def pick_facts(title: str, body: str, know: Knowledge, *, limit: int = 4) -> list[str]:
    """Кандидаты «что произошло»: информативные предложения без оценочных и слабых конструкций."""
    cands: list[tuple[float, str]] = []
    sents = sentences(body or "")[:14]
    title_stems = set(tokenize(title))
    for i, s in enumerate(sents):
        s = s.strip()
        if not (35 <= len(s) <= 260):
            continue
        if know.count("hedges", s) or know.count("opinion_in_own_text", s) or know.count("ads", s) or know.count("clickbait", s):
            continue
        score = 1.0 / (1 + 0.25 * i)
        if extract_numbers(s):
            score += 0.9
        if extract_dates(s):
            score += 0.4
        if extract_entities("", s, limit=3):
            score += 0.4
        if know.count("official_markers", s) or know.count("quote_intro", s):
            score += 0.3
        toks = set(tokenize(s))
        if title_stems and len(toks & title_stems) / max(1, len(title_stems)) > 0.85:
            score -= 0.6  # пересказ заголовка — не новый факт
        cands.append((score, s))
    cands.sort(key=lambda x: -x[0])
    chosen = [s for _, s in cands[: limit + 2]]
    chosen.sort(key=lambda s: sents.index(s) if s in sents else 99)
    return chosen[:limit]


@dataclass
class ArticleFeatures:
    lang: str
    entities: list[dict[str, Any]]
    numbers: list[dict[str, Any]]
    dates: list[str]
    quotes: list[dict[str, str]]
    attribution: dict[str, Any]
    flags: dict[str, Any]
    facts: list[str]
    stems: list[str] = field(default_factory=list)
    word_count: int = 0
    official: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "lang": self.lang, "entities": self.entities, "numbers": self.numbers, "dates": self.dates, "quotes": self.quotes,
            "attribution": self.attribution, "flags": self.flags, "facts": self.facts, "stems": self.stems,
            "word_count": self.word_count, "official": self.official,
        }  # fmt: skip


def compute_features(title: str, summary: str, body: str, *, know: Knowledge, alias_index: dict[str, str] | None = None, section: str = "", lang: str | None = None) -> ArticleFeatures:
    body_all = body or summary or ""
    lead = f"{summary or ''}\n{body_all[:2500]}"
    full = f"{title}\n{lead}"
    lang = lang or detect_language(f"{title} {summary} {body_all[:800]}")
    caps_letters = [c for c in title if c.isalpha()]
    caps_ratio = (sum(1 for c in caps_letters if c.isupper()) / len(caps_letters)) if len(caps_letters) >= 12 else 0.0
    flags: dict[str, Any] = {
        "clickbait": know.count("clickbait", title) * 2 + know.count("clickbait", summary[:300] if summary else "") + (2 if caps_ratio > 0.6 else 0)
        + (1 if re.search(r"[!?]{2,}|…$", title) else 0),
        "ad": know.count("ads", title) * 2 + know.count("ads", (summary or body_all)[:500]),
        "opinion": know.count("opinion", title) + know.count("opinion", lead[:500]) // 2,
        "hedge": know.count("hedges", full),
        "injection": know.matched("injection_patterns", full),
        "low_value_section": bool(know.count("low_value_sections", section)),
        "caps_ratio": round(caps_ratio, 2),
        "official_markers": know.count("official_markers", lead),
    }
    if re.search(r"(?i)\b(opinion|мнение|колонк|блог|авторская колонка|пікір)\b", section or ""):
        flags["opinion"] += 2
    entities = extract_entities(title, body_all)
    stems = list(dict.fromkeys(tokenize(f"{title} {title} {summary or ''} {body_all[:1800]}", lang=lang)))[:140]
    return ArticleFeatures(
        lang=lang,
        entities=entities,
        numbers=extract_numbers(f"{title}. {lead}", lang),
        dates=extract_dates(full),
        quotes=extract_quotes(lead),
        attribution=extract_attribution(lead, know, alias_index or {}),
        flags=flags,
        facts=pick_facts(title, body_all, know),
        stems=stems,
        word_count=len(normalize_text(body_all).split()),
        official=bool(flags["official_markers"]),
    )


def build_alias_index(sources: list[Any]) -> dict[str, str]:
    """alias (нижний регистр) → ключ источника; по названию и алиасам."""
    idx: dict[str, str] = {}
    for s in sources:
        for a in [s.name, *(s.aliases or [])]:
            a = (a or "").strip().lower()
            if len(a) >= 3:
                idx[a] = s.key
    return idx


__all__ = ["compute_features", "build_alias_index", "extract_numbers", "extract_dates", "extract_quotes", "extract_entities", "stem"]
