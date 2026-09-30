"""Знания предметной области: таксономия тем, лексиконы эвристик, геотопонимы (из YAML, без правки кода)."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

_WS = re.compile(r"\s+")


def load_yaml_default(name: str, override_dir: str | Path | None = None) -> dict[str, Any]:
    """Читает defaults/<name>.yaml; если в override_dir есть одноимённый файл — он заменяет верхнеуровневые ключи."""
    base = yaml.safe_load(resources.files("smi_agent.defaults").joinpath(f"{name}.yaml").read_text(encoding="utf-8")) or {}
    if override_dir:
        p = Path(override_dir) / f"{name}.yaml"
        if p.exists():
            over = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            base.update(over)
    return base


class Matcher:
    """Набор шаблонов: совпадение с началом слова; «$» на конце — слово целиком; многословные шаблоны терпимы к флексиям."""

    def __init__(self, patterns: list[str]):
        self.patterns: list[str] = []
        pieces: list[str] = []
        for raw in patterns:
            p = str(raw).strip().lower()
            if not p:
                continue
            whole = p.endswith("$")
            core = p.rstrip("$").strip()
            if not core:
                continue
            words = core.split()
            body = r"\w*\s+".join(re.escape(w) for w in words)
            piece = r"(?<!\w)(?:" + body + (r")(?!\w)" if whole else r")")
            self.patterns.append(core)
            pieces.append(f"({piece})")
        self._rx = re.compile("|".join(pieces), re.IGNORECASE | re.UNICODE) if pieces else None

    def hits(self, text: str) -> list[int]:
        """Индексы сработавших шаблонов (с повторами)."""
        if not self._rx or not text:
            return []
        t = _WS.sub(" ", text.lower())
        return [m.lastindex - 1 for m in self._rx.finditer(t) if m.lastindex]

    def matched(self, text: str) -> list[str]:
        seen: dict[int, None] = {}
        for i in self.hits(text):
            seen.setdefault(i)
        return [self.patterns[i] for i in seen]

    def count(self, text: str) -> int:
        return len(self.hits(text))

    def any(self, text: str) -> bool:
        if not self._rx or not text:
            return False
        return self._rx.search(_WS.sub(" ", text.lower())) is not None


@dataclass
class CategoryResult:
    category: str
    scores: dict[str, float]
    secondary: list[str] = field(default_factory=list)
    subtopics: list[str] = field(default_factory=list)


@dataclass
class GeoSignals:
    kz: float
    ca: float
    world: float
    kz_hits: list[str] = field(default_factory=list)
    world_hits: list[str] = field(default_factory=list)


def _sat(x: float, k: float = 3.0) -> float:
    return 1.0 - math.exp(-x / k)


class Knowledge:
    def __init__(self, override_dir: str | Path | None = None):
        tax = load_yaml_default("taxonomy", override_dir)
        lex = load_yaml_default("lexicons", override_dir)
        gaz = load_yaml_default("gazetteer", override_dir)
        self.categories: dict[str, dict[str, Any]] = tax["categories"]
        self.themes: list[dict[str, Any]] = tax.get("themes", [])
        self._cat = {cid: Matcher(c.get("keywords", [])) for cid, c in self.categories.items()}
        self._sub = {sid: Matcher(words) for sid, words in tax.get("subtopics", {}).items()}
        self.lex: dict[str, Matcher] = {}
        self.sensitive: dict[str, Matcher] = {}
        for key, val in lex.items():
            if key == "sensitive_topics":
                self.sensitive = {k: Matcher(v) for k, v in val.items()}
            elif isinstance(val, list):
                self.lex[key] = Matcher(val)
        self.kz = Matcher(gaz["kz_markers"])
        self.ca = Matcher(gaz["central_asia_markers"])
        self.world = Matcher(gaz["world_markers"])
        self.official_domains: list[str] = [d.lower() for d in gaz.get("official_domains", [])]

    def is_official_url(self, url: str) -> bool:
        from urllib.parse import urlsplit

        host = (urlsplit(url or "").hostname or "").lower()
        return any(host == d or host.endswith("." + d) for d in self.official_domains)

    # --------------------------------------------------------------- lexicons
    def count(self, lexicon: str, text: str) -> int:
        m = self.lex.get(lexicon)
        return m.count(text) if m else 0

    def matched(self, lexicon: str, text: str) -> list[str]:
        m = self.lex.get(lexicon)
        return m.matched(text) if m else []

    def sensitive_hits(self, text: str) -> dict[str, list[str]]:
        out = {}
        for topic, m in self.sensitive.items():
            hit = m.matched(text)
            if hit:
                out[topic] = hit[:5]
        return out

    # --------------------------------------------------------------- category
    def category_label(self, cid: str) -> str:
        return self.categories.get(cid, {}).get("name", cid)

    def is_sensitive_category(self, cid: str) -> bool:
        return bool(self.categories.get(cid, {}).get("sensitive"))

    def is_political_category(self, cid: str) -> bool:
        return bool(self.categories.get(cid, {}).get("political"))

    def classify(self, title: str, summary: str = "", body: str = "", *, hint: str = "", tags: list[str] | None = None) -> CategoryResult:
        parts = [(title or "", 3.0), (" ".join(tags or []) + " " + (summary or ""), 1.6), ((body or "")[:1500], 1.0)]
        scores: dict[str, float] = {}
        for cid, matcher in self._cat.items():
            if cid == "other" or not matcher.patterns:
                continue
            total = 0.0
            for text, w in parts:
                hits = matcher.hits(text)
                if hits:
                    distinct = len(set(hits))
                    total += w * (distinct + 0.3 * (len(hits) - distinct))
            if total:
                scores[cid] = total
        if hint and hint in self.categories and hint != "other":
            scores[hint] = scores.get(hint, 0.0) + 2.0
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        if not ranked or ranked[0][1] < 2.5:
            category = hint if hint in self.categories else "other"
            secondary: list[str] = []
        else:
            category = ranked[0][0]
            secondary = [cid for cid, sc in ranked[1:3] if sc >= 0.6 * ranked[0][1]]
        text_all = f"{title} {summary} {(body or '')[:1500]}"
        subtopics = [sid for sid, m in self._sub.items() if m.any(text_all)][:5]
        return CategoryResult(category, {k: round(v, 2) for k, v in ranked[:6]}, secondary, subtopics)

    # -------------------------------------------------------------------- geo
    def geo_signals(self, title: str, summary: str = "", body: str = "", *, source_country: str = "") -> GeoSignals:
        def weighted(m: Matcher) -> tuple[float, list[str]]:
            total, names = 0.0, []
            for text, w in ((title or "", 3.0), (summary or "", 1.5), ((body or "")[:2500], 1.0)):
                h = m.hits(text)
                if h:
                    total += w * (len(set(h)) + 0.25 * (len(h) - len(set(h))))
                    names += [m.patterns[i] for i in set(h)]
            return total, names

        kz_x, kz_names = weighted(self.kz)
        ca_x, _ = weighted(self.ca)
        w_x, w_names = weighted(self.world)
        kz = _sat(kz_x, 3.0)
        ca = _sat(ca_x, 3.0)
        world = _sat(w_x, 4.0)
        if (source_country or "").upper() == "KZ":
            kz = min(1.0, kz + 0.22 * (1 - kz))  # местное издание — слабый приор, не заменяющий упоминания
        return GeoSignals(round(kz, 3), round(ca, 3), round(world, 3), sorted(set(kz_names))[:8], sorted(set(w_names))[:8])


@lru_cache(maxsize=4)
def get_knowledge(override_dir: str | None = None) -> Knowledge:
    return Knowledge(override_dir)
