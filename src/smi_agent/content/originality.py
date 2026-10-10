"""Проверка оригинальности (ТЗ §22, §62): длинные дословные совпадения и доля общих n-грамм с источниками."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.text import word_tokens


@dataclass
class CopyReport:
    max_run_words: int
    overlap_ratio: float
    worst_source: str = ""
    runs: list[str] = field(default_factory=list)


def _norm_words(text: str) -> list[str]:
    return [w.lower().replace("ё", "е") for w in word_tokens(text)]


def longest_common_run(a: list[str], b: list[str]) -> tuple[int, int]:
    """Длина и позиция (в a) самой длинной общей последовательности слов."""
    if not a or not b:
        return 0, 0
    index: dict[str, list[int]] = {}
    for j, w in enumerate(b):
        index.setdefault(w, []).append(j)
    best, best_pos = 0, 0
    prev: dict[int, int] = {}
    for i, w in enumerate(a):
        cur: dict[int, int] = {}
        for j in index.get(w, ()):
            k = prev.get(j - 1, 0) + 1
            cur[j] = k
            if k > best:
                best, best_pos = k, i - k + 1
        prev = cur
    return best, best_pos


def copy_report(text: str, sources: dict[str, str], *, ngram: int = 5) -> CopyReport:
    """sources: имя → полный текст источника (заголовок + текст)."""
    words = _norm_words(text)
    if not words:
        return CopyReport(0, 0.0)
    grams = {tuple(words[i : i + ngram]) for i in range(len(words) - ngram + 1)}
    worst = CopyReport(0, 0.0)
    for name, src in sources.items():
        sw = _norm_words(src)
        run, pos = longest_common_run(words, sw)
        sg = {tuple(sw[i : i + ngram]) for i in range(len(sw) - ngram + 1)}
        ratio = len(grams & sg) / len(grams) if grams else 0.0
        if run > worst.max_run_words or (run == worst.max_run_words and ratio > worst.overlap_ratio):
            worst = CopyReport(run, round(ratio, 3), name, [" ".join(words[pos : pos + run])] if run >= 6 else [])
        worst.overlap_ratio = max(worst.overlap_ratio, round(ratio, 3))
    return worst
