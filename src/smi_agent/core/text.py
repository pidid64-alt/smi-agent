"""Лёгкие языковые утилиты для ru / kk / en: нормализация, язык, токены, simhash, транслитерация.

Без тяжёлых зависимостей: этого достаточно для дедупликации и оценок в офлайн-режиме.
Близость материалов — лексико-числовая (TF-IDF + сущности + числа + цитаты); векторные эмбеддинги в этой версии не используются
(точка расширения описана в docs/ARCHITECTURE.md).
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence

_WS = re.compile(r"\s+")
_ZW = {ord(c): None for c in "\u200b\u200c\u200d\u2060\ufeff\u00ad"}
_QUOTES = {ord(c): '"' for c in "«»“”„‟❝❞"}
_DASHES = {ord(c): "-" for c in "‐‑‒–—―−"}

KK_LETTERS = frozenset("әғқңөұүһ")
_TOKEN = re.compile(r"\d+(?:[.,]\d+)+|[^\W_]+(?:[-'’][^\W_]+)*", re.UNICODE)

STOP_RU = set(
    """и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее её мне было вот от
    меня еще ещё нет о из ему теперь когда даже ну вдруг ли если уже или ни быть был него до вас нибудь опять уж вам
    ведь там потом себя ничего ей может они тут где есть надо ней для мы тебя их чем была сам чтоб без будто чего раз
    тоже себе под будет ж тогда кто этот того потому этого какой совсем ним здесь этом один почти мой тем чтобы нее
    сейчас были куда зачем всех никогда можно при наконец два об другой хоть после над больше тот через эти нас про
    всего них какая много разве три эту моя впрочем хорошо свою этой перед иногда лучше чуть том нельзя такой им более
    всегда конечно всю между также который которые которая которого которым которых которое сообщает сообщил сообщили
    сообщила передает передал передали рассказал рассказали заявил заявили заявила отметил отметили отметила
    году года лет год будут стал стала станет свой своей своих вместе очень либо какие подчеркнул пояснил добавил
    отмечается сообщается информации информацию данным данных словам""".split()
)
STOP_KK = set(
    """және мен бен пен да де та те бір бұл сол осы ол олар біз сіз сен үшін деп деген туралы болып болды болады бойынша
    кейін дейін арқылы сондай сонымен тағы тек әр қандай қалай неге қай кім не айтты хабарлады мәлімдеді атап өтті
    жылы жыл жылдың""".split()
)
STOP_EN = set(
    """the a an and or but of to in on at for with by from as is are was were be been being it its this that these
    those he she they we you i his her their our your not no yes do does did have has had will would can could may
    might should also than then there here which who whom what when where why how into about over after before between
    during under again more most some any each other such only own same so very just said says say reported reports
    according told added noted year years""".split()
)
STOPWORDS = STOP_RU | STOP_KK | STOP_EN


def normalize_text(s: str | None) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s).translate(_ZW).translate(_QUOTES).translate(_DASHES)
    return _WS.sub(" ", s).strip()


def is_cyrillic(tok: str) -> bool:
    return any("\u0400" <= c <= "\u04ff" for c in tok)


def detect_language(text: str | None) -> str:
    """ru | kk | en | unknown. Казахский определяется по специфическим буквам, английский — по латинице."""
    if not text:
        return "unknown"
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 8:
        return "unknown"
    cyr = sum(1 for c in letters if "\u0400" <= c <= "\u04ff")
    lat = sum(1 for c in letters if c.isascii())
    if cyr / len(letters) > 0.5:
        kk = sum(1 for c in letters if c.lower() in KK_LETTERS)
        return "kk" if kk >= 2 and kk / max(cyr, 1) > 0.015 else "ru"
    if lat / len(letters) > 0.6:
        return "en"
    return "unknown"


_RU_END = tuple(
    sorted(
        "ами ями ого его ому ему ыми ими ах ях ов ев ей ой ий ый ая яя ое ее ые ие ых их ую юю ом ем ам ям а я ы и у ю е о ь й".split(),
        key=len,
        reverse=True,
    )
)
_EN_END = ("ing", "ed", "es", "ly", "s")


def stem(tok: str, lang: str = "ru") -> str:
    """Грубый стеммер: снятие окончания + усечение. Достаточно для кластеризации ru/kk/en."""
    if tok.isdigit() or len(tok) <= 4:
        return tok
    if is_cyrillic(tok):
        if lang == "kk":
            return tok[:5]
        for e in _RU_END:
            if tok.endswith(e) and len(tok) - len(e) >= 4:
                tok = tok[: -len(e)]
                break
        return tok[:6]
    for e in _EN_END:
        if tok.endswith(e) and len(tok) - len(e) >= 4:
            tok = tok[: -len(e)]
            break
    return tok[:6]


def tokenize(text: str | None, *, stopwords: bool = True, stemming: bool = True, lang: str | None = None) -> list[str]:
    norm = normalize_text(text)
    lang = lang or detect_language(norm)
    out: list[str] = []
    for m in _TOKEN.finditer(norm.lower()):
        t = m.group(0).replace("ё", "е")
        if stopwords and t in STOPWORDS:
            continue
        if len(t) < 2 and not t.isdigit():
            continue
        out.append(stem(t, lang) if stemming else t)
    return out


def ngrams(tokens: Sequence[str], n: int) -> list[tuple[str, ...]]:
    return [tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)]


def word_tokens(text: str | None) -> list[str]:
    """Все слова в нижнем регистре без стоп-слов и стемминга (для сравнения текстов дословно)."""
    return [m.group(0).replace("ё", "е") for m in _TOKEN.finditer(normalize_text(text).lower())]


# ---------------------------------------------------------------- simhash ---
def _h64(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big")


def simhash(tokens: Iterable[str]) -> int:
    counts = Counter(tokens)
    if not counts:
        return 0
    vec = [0] * 64
    for tok, w in counts.items():
        h = _h64(tok)
        for i in range(64):
            vec[i] += w if (h >> i) & 1 else -w
    out = 0
    for i in range(64):
        if vec[i] > 0:
            out |= 1 << i
    return out


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ------------------------------------------------------------ similarity ---
def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    dot = sum(w * b.get(t, 0.0) for t, w in a.items())
    na = math.sqrt(sum(w * w for w in a.values()))
    nb = math.sqrt(sum(w * w for w in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ------------------------------------------------------ предложения / ... ---
_ABBR = {
    "г", "гг", "тыс", "млн", "млрд", "трлн", "руб", "тг", "долл", "ул", "пр", "им", "др", "см", "рис", "напр", "обл",
    "р-н", "проф", "акад", "т.д", "т.п", "т.е", "т.к", "mr", "mrs", "dr", "inc", "ltd", "no", "vs", "etc", "u.s", "st",
    "jr", "sr", "co", "corp", "прим", "англ", "букв", "ок", "мл", "чел", "кв", "куб", "авт", "гос", "зам", "пос",
}  # fmt: skip
_SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+(?=[«\"(\[]?[A-ZА-ЯЁӘҒҚҢӨҰҮҺІ0-9])")


def sentences(text: str | None) -> list[str]:
    text = normalize_text(text)
    if not text:
        return []
    merged: list[str] = []
    for p in _SENT_SPLIT.split(text):
        if merged:
            prev = merged[-1]
            m = re.search(r"(\S+)\.$", prev)
            tail = m.group(1).lower().rstrip(".") if m else ""
            is_initial = bool(re.search(r"(?:^|\s)[A-ZА-ЯЁ]\.$", prev))
            if tail in _ABBR or is_initial:
                merged[-1] = prev + " " + p
                continue
        merged.append(p)
    return [m.strip() for m in merged if m.strip()]


# --------------------------------------------------------- транслитерация ---
_TR = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "i",
    "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "ә": "a", "ғ": "g", "қ": "k", "ң": "n", "ө": "o", "ұ": "u", "ү": "u", "һ": "h", "і": "i",
}  # fmt: skip


def transliterate(s: str) -> str:
    return "".join(_TR.get(c, c) for c in s.lower())


_VOWELS = re.compile(r"[aeiouyh]")


def skeleton(token: str) -> str:
    """Согласный «скелет» слова: Gates/Гейтс → gts, Tesla/Тесла → tsl. Для сопоставления имён между языками."""
    t = re.sub(r"[^a-z]", "", transliterate(token))
    t = _VOWELS.sub("", t)
    t = re.sub(r"(.)\1+", r"\1", t)
    if t.endswith("s") and len(t) > 3:
        t = t[:-1]
    return t


def clip(s: str, n: int, ellipsis: str = "…") -> str:
    s = s.strip()
    if len(s) <= n:
        return s
    cut = s[: n - len(ellipsis)]
    sp = cut.rfind(" ")
    if sp > n * 0.6:
        cut = cut[:sp]
    return cut.rstrip(" ,;:-—") + ellipsis
