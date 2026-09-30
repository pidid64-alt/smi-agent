"""Разбор команд пользователя (ТЗ §17): «Беру №2», «№4, но сделай акцент на Казахстан», «№1 неинтересна», «Замени №3», «Раскрой №5 подробнее»."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..core.enums import ActionKind

_ORD = {
    "перв": 1, "втор": 2, "трет": 3, "четв": 4, "пят": 5, "шест": 6, "седьм": 7, "восьм": 8, "девят": 9,
    "бірінші": 1, "екінші": 2, "үшінші": 3, "төртінші": 4, "бесінші": 5,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
}  # fmt: skip
_NUM = re.compile(r"(?:№|#|номер\w*|вариант\w*|тем\w*|предложени\w*|number|no\.?)\s*(\d{1,2})|(?<![\w.,:])(\d{1,2})(?:-?(?:й|я|е|ю|го|ую|ая|ое|ый|ой|ні|ші|st|nd|rd|th))?(?![\w.,:%])", re.IGNORECASE)
_ORD_RX = re.compile(r"\b(" + "|".join(_ORD) + r")\w*", re.IGNORECASE)

_VERBS: dict[ActionKind, re.Pattern[str]] = {
    ActionKind.MORE_INFO: re.compile(r"раскро\w+|расскаж\w+\s+(?:больше|подробн\w+)|больше\s+информаци\w+|что\s+там\s+(?:по|с)|детал\w+|\bmore\s+(?:on|about|info)|\bexpand\b|\bdetails?\b|толығырақ", re.IGNORECASE),
    ActionKind.CHANGE_ANGLE: re.compile(r"(?:друго\w+|ино\w+|новы\w+)\s+(?:угол|ракурс|подач\w+)|под\s+друг\w+\s+угл\w+|\bchange\s+angle\b|\bdifferent\s+angle\b", re.IGNORECASE),
    ActionKind.REPLACE: re.compile(r"замени\w*|заменить|поменяй\w*|друго\w+\s+(?:вариант|тему|новост\w+)|\breplace\b|\bswap\b|ауыстыр\w*", re.IGNORECASE),
    ActionKind.REJECT: re.compile(r"не\s*интересн\w*|не\s+надо|не\s+нужн\w*|не\s+то\b|не\s+подход\w+|убери\w*|убрать|пропусти\w*|\bмимо\b|отклон\w+|не\s+(?:беру|берём|берем|возьму|хочу|выбираю)|\bskip\b|\breject\b|\bdrop\b|қызықсыз|керек\s+емес", re.IGNORECASE),
    ActionKind.SELECT: re.compile(r"беру|берём|берем|возьм\w+|взять|выбира\w+|выберу|пишем|готовь\w*|запускай|принято|\bок(?:ей)?\b|\bgo\s+with\b|\btake\b|\bchoose\b|\bpick\b|\bselect\b|аламын|таңдаймын|\bделаем\b|\bдавай\b", re.IGNORECASE),
}
_VERB_ANY = "|".join(p.pattern for p in _VERBS.values())
_SPLIT = re.compile(rf"[;\n]+|\.\s+|,\s*а\s+|,\s*(?:и\s+)?(?=(?:{_VERB_ANY}))", re.IGNORECASE)

_LANG = [(re.compile(r"на\s+казахск\w+|қазақша|по-?казахски|in\s+kazakh", re.I), "kk"), (re.compile(r"на\s+английск\w+|in\s+english|по-?английски", re.I), "en"), (re.compile(r"на\s+русск\w+|по-?русски|in\s+russian", re.I), "ru")]
_FORMAT = [(re.compile(r"reels?|рилс\w*|рилз\w*", re.I), "reels_script"), (re.compile(r"карусел\w+", re.I), "carousel_numbers"), (re.compile(r"карточк\w+", re.I), "carousel_numbers"), (re.compile(r"сторис|stories", re.I), "stories"), (re.compile(r"коротк\w+\s+пост|только\s+пост", re.I), "short_post")]
_LENGTH = [(re.compile(r"короче|покороче|кратк\w+|лаконичн\w+|shorter", re.I), "shorter"), (re.compile(r"длиннее|развёрнут\w+|развернут\w+|лонгрид|longer", re.I), "longer")]
_TONE = [(re.compile(r"нейтральн\w+", re.I), "neutral"), (re.compile(r"строже|формальн\w+|официальн\w+", re.I), "formal"), (re.compile(r"дружелюбн\w+|живее|легче|эмоциональнее", re.I), "friendly")]
_PLATFORM = {"телеграм": "telegram", "telegram": "telegram", "инстаграм": "instagram", "instagram": "instagram", "фейсбук": "facebook", "facebook": "facebook"}
_EMPH = re.compile(r"(?:акцент\w*|упор\w*|фокус\w*|сфокусируй\w*|больше\s+про|сделай\s+упор)\s+(?:на\s+|про\s+)?([^,.;]{2,60})", re.IGNORECASE)
_EMPH_TAGS = [(re.compile(r"казахстан|қазақстан|кз\b|местн\w+|локальн\w+", re.I), "kazakhstan"), (re.compile(r"цифр\w+|данн\w+|статистик\w+", re.I), "numbers"), (re.compile(r"практическ\w+|польз\w+|что\s+делать", re.I), "practical"), (re.compile(r"мир\w*|глобальн\w+|международн\w+", re.I), "world")]
_ANGLE_TXT = re.compile(r"угол\s*[:—-]\s*([^;\n]{3,200})", re.IGNORECASE)


@dataclass
class Command:
    kind: ActionKind
    slot: int
    params: dict[str, Any] = field(default_factory=dict)
    raw: str = ""


@dataclass
class ParseResult:
    commands: list[Command] = field(default_factory=list)
    unrecognized: list[str] = field(default_factory=list)

    @property
    def hint(self) -> str | None:
        if self.commands:
            return None
        return "Не понял команду. Примеры: «Беру №2», «№4, но сделай акцент на Казахстан», «№1 неинтересна», «Замени №3», «Раскрой №5 подробнее»."


def _slots(chunk: str) -> list[int]:
    out: list[int] = []
    for m in _NUM.finditer(chunk):
        n = int(m.group(1) or m.group(2))
        if n not in out:
            out.append(n)
    for m in _ORD_RX.finditer(chunk):
        n = _ORD[m.group(1).lower()]
        if n not in out:
            out.append(n)
    return out


def _modifiers(chunk: str) -> dict[str, Any]:
    p: dict[str, Any] = {}
    m = _EMPH.search(chunk)
    if m:
        txt = m.group(1).strip(" .,;").lower()
        p["emphasis_text"] = txt
        p["emphasis"] = next((tag for rx, tag in _EMPH_TAGS if rx.search(txt)), re.sub(r"\s+", "_", txt)[:40])
    for rx, code in _LANG:
        if rx.search(chunk):
            p["language"] = code
            break
    for rx, code in _FORMAT:
        if rx.search(chunk):
            p["format"] = code
            break
    for rx, code in _LENGTH:
        if rx.search(chunk):
            p["length"] = code
            break
    for rx, code in _TONE:
        if rx.search(chunk):
            p["tone"] = code
            break
    only = re.search(r"только\s+(?:для\s+|в\s+)?(телеграм\w*|telegram|инстаграм\w*|instagram|фейсбук\w*|facebook)", chunk, re.I)
    if only:
        key = next(v for k, v in _PLATFORM.items() if only.group(1).lower().startswith(k))
        p["platforms"] = [key]
    a = _ANGLE_TXT.search(chunk)
    if a:
        p["angle"] = a.group(1).strip()
    return p


def parse_commands(text: str) -> ParseResult:
    res = ParseResult()
    text = (text or "").strip()
    if not text:
        return res
    for raw_chunk in [c.strip() for c in _SPLIT.split(text) if c and c.strip()]:
        slots = _slots(raw_chunk)
        first: tuple[int, ActionKind] | None = None
        for kind, rx in _VERBS.items():
            m = rx.search(raw_chunk)
            if m and (first is None or m.start() < first[0] or (m.start() == first[0] and kind in (ActionKind.REJECT, ActionKind.MORE_INFO))):
                first = (m.start(), kind)
        # отрицание выбора перекрывает «беру»
        if first and first[1] == ActionKind.SELECT and re.search(r"не\s+(?:беру|берём|берем|возьму|хочу|выбираю)", raw_chunk, re.I):
            first = (0, ActionKind.REJECT)
        mods = _modifiers(raw_chunk)
        kind: ActionKind | None = first[1] if first else None
        if kind is None and slots and (mods or re.search(r"\bно\b|\bbut\b", raw_chunk, re.I)):
            kind = ActionKind.MODIFY
        elif kind is None and slots and len(raw_chunk.strip(" №#.,")) <= 4:
            kind = ActionKind.SELECT  # «2» — выбор (создаётся только черновик, публикация — после проверок)
        if kind is None or not slots:
            res.unrecognized.append(raw_chunk)
            continue
        if kind == ActionKind.SELECT and mods:
            kind = ActionKind.MODIFY
        if kind == ActionKind.REJECT:
            reason = _VERBS[ActionKind.REJECT].sub("", raw_chunk)
            reason = _NUM.sub("", reason)
            reason = re.sub(r"^\s*(?:и|а|но)\s+", "", reason.strip(" ,.-—:;"), flags=re.I).strip(" ,.-—:;")
            if len(reason) >= 4:
                mods["reason"] = reason[:200]
        for n in slots:
            res.commands.append(Command(kind, n, dict(mods), raw_chunk))
    return res
