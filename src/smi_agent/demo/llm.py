"""Демо-«LLM»: отдаёт заранее написанные оригинальные тексты для ВЫМЫШЛЕННЫХ демо-новостей (через тот же интерфейс и те же проверки).

Это не языковая модель и не имитация её возможностей: на любые другие события она честно не отвечает — тогда работает эвристическая заготовка.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..llm.testing import ScriptedLlm
from .corpus import demo_sample_texts

_SRC = re.compile(r"<source_material>\n(.*?)\n</source_material>", re.DOTALL)
CAT_TAGS = {"finance": "финансы", "economy": "экономика", "transport": "транспорт", "education": "образование", "energy": "энергетика", "auto": "авто", "ai": "ИИ", "science": "наука", "health": "здоровье", "tech": "технологии"}


def _payload(user: str) -> dict[str, Any]:
    m = _SRC.search(user)
    if not m:
        return {}
    try:
        return json.loads(m.group(1).replace("‹", "<").replace("›", ">"))
    except ValueError:
        return {}


def _sources(fb: dict[str, Any], limit: int = 3) -> str:
    seen: list[str] = []
    for s in fb.get("sources", []):
        name = re.sub(r"\s*[—–(].*$", "", s.get("name", "")).strip() or s.get("name", "")
        if name and name not in seen:
            seen.append(name)
    return ", ".join(seen[:limit])


class DemoLlm(ScriptedLlm):
    name = "demo"
    model = "scripted-demo"

    def __init__(self) -> None:
        self.samples = demo_sample_texts()
        super().__init__(responder=self._respond)

    def _respond(self, system: str, user: str):  # noqa: ANN202
        m = re.search(r"Платформа: (\w+)\.", user)
        data = _payload(user)
        if m is None:  # основной текст
            title = str(data.get("title", "")).lower()
            for key, text in self.samples.items():
                if key in title:
                    return text
            return None
        core, fb = data.get("core") or {}, data.get("fact_base") or {}
        if not core:
            return None
        src = _sources(fb)
        pts = core.get("points", [])
        tag = CAT_TAGS.get(fb.get("category", ""), "новости")
        plat = m.group(1)
        if plat == "telegram":
            body = f"<b>{core['headline']}</b>\n\n{core['lead']}\n\n" + "\n".join("• " + p for p in pts) + (f"\n\n{core['why_it_matters']}" if core.get("why_it_matters") else "") + (f"\n\nИсточники: {src}" if src else "")
            return {"title": core["headline"], "body": body, "hashtags": [f"#{tag}", "#Казахстан"]}
        if plat == "instagram":
            body = f"{core['headline']}\n\n{core['lead']}\n\n" + "\n".join("— " + p for p in pts[:2]) + (f"\n\nИсточники: {src}" if src else "")
            return {"title": core["headline"], "body": body, "hashtags": [f"#{tag}", "#новости"], "slides": [core["headline"][:90], *[p[:140] for p in pts[:4]], f"Источники: {src}"[:140]], "reels": {"hook": core["headline"][:70], "beats": [p[:80] for p in pts[:3]], "cta": f"Источники: {src}"[:90]}}
        body = f"{core['headline']}\n\n{core['lead']} {pts[0] if pts else ''}\n\n" + " ".join(pts[1:]) + (f"\n\n{core.get('context', '')} {core.get('why_it_matters', '')}".rstrip()) + (f"\n\nИсточники: {src}" if src else "")
        return {"title": core["headline"], "body": body, "hashtags": [f"#{tag}"]}
