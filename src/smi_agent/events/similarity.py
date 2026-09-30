"""Семантическая близость материалов и событий: TF-IDF + сущности + числа + цитаты + время; кросс-языковое сопоставление."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..core.text import hamming, skeleton, tokenize

_STOP_ENT = {"kz", "nur", "kazinform", "reuters", "bbc", "cnbc", "tengrinews"}


@dataclass
class ArtVec:
    id: int
    source_id: int
    source_key: str
    group: str
    origin: str
    reliability: float
    tier: str
    country: str
    pub: datetime
    lang: str
    title_stems: frozenset[str]
    tf: dict[str, float]
    ent: dict[str, float]
    title_ent: frozenset[str]
    nums: frozenset[str]
    numd: dict[str, frozenset[str]]
    quotes: frozenset[str]
    dates: frozenset[str]
    sim: int
    cites: tuple[str, ...] = ()
    has_full_text: bool = False
    is_official: bool = False


def _num_key(n: dict[str, Any]) -> str:
    return f"{float(n['v']):.3g}"


def unit_compatible(a: frozenset[str] | set[str], b: frozenset[str] | set[str]) -> bool:
    """«7,8 млн тонн» и «7.8 million tonnes» — одно число: единица может быть не распознана на одном из языков."""
    return not a or not b or "" in a or "" in b or bool(a & b)


def num_specificity(key: str) -> float:
    """Круглые числа (4%, 500 тыс.) менее специфичны, чем 16,5% или 12,3%."""
    try:
        val = key.split(":", 1)[-1]
        digits = val.replace(".", "").replace("-", "").lstrip("0")
        if "e" in val:
            digits = val.split("e")[0].replace(".", "")
        sig = len(digits.rstrip("0")) if digits else 1
    except Exception:  # noqa: BLE001
        sig = 1
    return min(1.0, 0.25 + 0.25 * max(1, sig))


def entity_word_keys(entities: list[dict[str, Any]]) -> tuple[dict[str, float], frozenset[str]]:
    ent: dict[str, float] = {}
    title: set[str] = set()
    for e in entities:
        w = 2.0 if e.get("title") else 1.0
        words = [x for x in str(e.get("t", "")).replace("-", " ").split() if len(x) >= 2]
        for word in words or [str(e.get("t", ""))]:
            k = skeleton(word.lower())
            if len(k) < 2 or k in _STOP_ENT:
                continue
            ent[k] = max(ent.get(k, 0.0), w)
            if e.get("title"):
                title.add(k)
    return ent, frozenset(title)


def make_vec(a: Any, *, origin: str, group: str) -> ArtVec:
    f = a.features or {}
    lang = f.get("lang") or a.lang or "ru"
    title_stems = frozenset(tokenize(a.title, lang=lang))
    tf: dict[str, float] = {}
    for st in f.get("stems", []):
        tf[st] = 2.0 if st in title_stems else 1.0
    for st in title_stems:
        tf.setdefault(st, 2.0)
    ent, tent = entity_word_keys(f.get("entities", []))
    numd: dict[str, set[str]] = {}
    for n in f.get("numbers", []):
        numd.setdefault(_num_key(n), set()).add(n.get("u", ""))
    return ArtVec(
        id=a.id, source_id=a.source_id, source_key=a.source.key, group=group, origin=origin, reliability=a.source.reliability, tier=a.source.tier,
        country=a.source.country, pub=a.published_at, lang=lang, title_stems=title_stems, tf=tf, ent=ent, title_ent=tent,
        nums=frozenset(numd), numd={k: frozenset(v) for k, v in numd.items()}, quotes=frozenset(q["norm"] for q in f.get("quotes", [])),
        dates=frozenset(f.get("dates", [])), sim=int(a.simhash or "0", 16), cites=tuple(f.get("attribution", {}).get("keys", [])),
        has_full_text=bool(a.has_full_text), is_official=bool(a.source.is_official),
    )  # fmt: skip


@dataclass
class EventProf:
    id: int
    members: list[ArtVec] = field(default_factory=list)
    tf: Counter = field(default_factory=Counter)
    tf_by_lang: dict[str, Counter] = field(default_factory=dict)
    numd: dict[str, set[str]] = field(default_factory=dict)
    ent: dict[str, float] = field(default_factory=dict)
    title_ent: set[str] = field(default_factory=set)
    title_stems: Counter = field(default_factory=Counter)
    nums: set[str] = field(default_factory=set)
    quotes: set[str] = field(default_factory=set)
    dates: set[str] = field(default_factory=set)
    langs: Counter = field(default_factory=Counter)
    first_pub: datetime | None = None
    last_pub: datetime | None = None

    def add(self, v: ArtVec) -> None:
        self.members.append(v)
        lang_tf = self.tf_by_lang.setdefault(v.lang, Counter())
        for s, w in v.tf.items():
            self.tf[s] += w
            lang_tf[s] += w
        for k, units in v.numd.items():
            self.numd.setdefault(k, set()).update(units)
        for k, w in v.ent.items():
            self.ent[k] = max(self.ent.get(k, 0.0), w)
        self.title_ent |= v.title_ent
        self.title_stems.update(v.title_stems)
        self.nums |= v.nums
        self.quotes |= v.quotes
        self.dates |= v.dates
        self.langs[v.lang] += 1
        self.first_pub = v.pub if self.first_pub is None or v.pub < self.first_pub else self.first_pub
        self.last_pub = v.pub if self.last_pub is None or v.pub > self.last_pub else self.last_pub

    @property
    def dominant_lang(self) -> str:
        return self.langs.most_common(1)[0][0] if self.langs else "ru"

    def stems(self) -> set[str]:
        return set(self.tf)


@dataclass
class PairScore:
    score: float
    cos: float
    title_j: float
    ent: float
    shared_ent: int
    shared_title_ent: int
    num: float
    shared_num: int
    quote: bool
    date: bool
    cross_lang: bool
    attach: bool
    reasons: list[str] = field(default_factory=list)


IdfFn = Callable[[str], float]


def _tfidf(tf: dict[str, float], idf: IdfFn, sublinear: bool) -> dict[str, float]:
    return {s: ((1 + math.log(w)) if sublinear and w > 1 else w) * idf(s) for s, w in tf.items()}


def cosine_sparse(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    dot = sum(w * b.get(s, 0.0) for s, w in a.items())
    na = math.sqrt(sum(w * w for w in a.values()))
    nb = math.sqrt(sum(w * w for w in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def weighted_overlap(a: dict[str, float], b: dict[str, float]) -> float:
    keys = set(a) | set(b)
    if not keys:
        return 0.0
    num = sum(min(a.get(k, 0.0), b.get(k, 0.0)) for k in keys)
    den = sum(max(a.get(k, 0.0), b.get(k, 0.0)) for k in keys)
    return num / den if den else 0.0


def pair_score(v: ArtVec, prof: EventProf, idf: IdfFn) -> PairScore:
    """Близость материала к событию. Два независимых пути: (1) лексика на языке материала, (2) сущности+числа (не зависит от языка).
    Материал присоединяется, если сработал любой из них со своими порогами."""
    same_lang = v.lang in prof.langs
    cos = cosine_sparse(_tfidf(v.tf, idf, False), _tfidf(dict(prof.tf_by_lang.get(v.lang, {})), idf, True)) if same_lang else 0.0
    tj_a, tj_b = set(v.title_stems), set(prof.title_stems)
    union = tj_a | tj_b
    title_j = (sum(idf(s) for s in tj_a & tj_b) / sum(idf(s) for s in union)) if union and same_lang else 0.0
    shared_keys = set(v.ent) & set(prof.ent)
    ent = weighted_overlap(v.ent, prof.ent)
    shared_title = len(set(v.title_ent) & (set(prof.title_ent) | set(prof.ent)))
    shared_nums = {k for k in (v.nums & prof.nums) if unit_compatible(v.numd.get(k, frozenset()), prof.numd.get(k, set()))}
    shared_num = len(shared_nums)
    shared_num_w = sum(num_specificity(k) for k in shared_nums)
    num = min(1.0, shared_num_w / max(1.0, min(len(v.nums), len(prof.nums)) * 0.75)) if v.nums and prof.nums else 0.0
    quote = bool(v.quotes & prof.quotes)
    date = bool(v.dates & prof.dates)
    center = prof.first_pub or v.pub
    gap_h = abs((v.pub - center).total_seconds()) / 3600
    tfac = 1.0 if gap_h <= 24 else max(0.35, 1.0 - (gap_h - 24) / 70)
    reasons: list[str] = []
    score_lex, attach_lex = 0.0, False
    if same_lang:
        score_lex = 0.44 * cos + 0.14 * title_j + 0.18 * ent + 0.16 * num + 0.06 * quote
        if quote and shared_keys:
            score_lex += 0.10
        score_lex *= 0.75 + 0.25 * tfac
        attach_lex = score_lex >= 0.30 and cos >= 0.15 and (len(shared_keys) >= 2 or shared_num_w >= 0.75 or cos >= 0.5) and (shared_title >= 1 or title_j >= 0.15 or cos >= 0.4 or shared_num_w >= 1.5)
    score_ent = (0.40 * ent + 0.35 * num + 0.10 * date + 0.15 * min(1.0, shared_title / 3)) * (0.8 + 0.2 * tfac)
    attach_ent = score_ent >= 0.30 and (
        (len(shared_keys) >= 2 and shared_num_w >= 1.2)
        or (len(shared_keys) >= 1 and shared_num_w >= 1.75)
        or (len(shared_keys) >= 3 and shared_num_w >= 0.75)
        or (len(shared_keys) >= 5 and shared_title >= 1)
    )
    cross = not same_lang
    if attach_ent and not attach_lex:
        reasons.append("entities_numbers")
    if cross:
        reasons.append("cross_lang")
    if quote:
        reasons.append("shared_quote")
    if shared_num:
        reasons.append(f"shared_numbers:{shared_num}")
    score = max(score_lex, score_ent)
    return PairScore(round(score, 4), round(cos, 4), round(title_j, 4), round(ent, 4), len(shared_keys), shared_title, round(num, 4), shared_num, quote, date, cross, attach_lex or attach_ent, reasons)


def member_cos(a: ArtVec, b: ArtVec, idf: IdfFn) -> float:
    if a.lang != b.lang:
        return 0.0
    return cosine_sparse(_tfidf(a.tf, idf, False), _tfidf(b.tf, idf, False))


def make_idf(event_stem_sets: list[set[str]]) -> IdfFn:
    n = len(event_stem_sets) + 1
    df: Counter = Counter()
    for s in event_stem_sets:
        df.update(s)

    def idf(stem: str) -> float:
        return math.log((n + 1) / (df.get(stem, 0) + 1)) + 1.0

    return idf


__all__ = ["ArtVec", "EventProf", "PairScore", "make_vec", "pair_score", "member_cos", "make_idf", "hamming", "cosine_sparse", "weighted_overlap"]
