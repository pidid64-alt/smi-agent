"""Обучение (ТЗ §18–19, §44–45): каждое действие — сигнал; профиль — бета-распределения по признакам с затуханием."""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import (
    Content,
    EditorialCorrection,
    Event,
    LearningEvent,
    PerformanceStat,
    ProfileFeature,
    Proposal,
)

HALF_LIFE_DAYS = 90.0
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")
_TAG = re.compile(r"#\w+")


def decay_factor(last: datetime | None, now: datetime) -> float:
    if last is None:
        return 1.0
    days = max(0.0, (now - last).total_seconds() / 86400)
    return 0.5 ** (days / HALF_LIFE_DAYS)


def event_features(ev: Event) -> list[tuple[str, str, float]]:
    feats = [("category", ev.category, 1.0), ("geo", ev.geo, 0.7)]
    feats += [("subtopic", st, 0.6) for st in (ev.subtopics or [])[:3]]
    return feats


class LearningService:
    def __init__(self, ctx: Any):
        self.ctx = ctx

    # ---------------------------------------------------------------- ядро
    def apply(self, s: Session, project_id: int, deltas: list[tuple[str, str, float, float]], *, source: str, signal: str, ref_type: str = "", ref_id: Any = "", note: str = "") -> None:
        now = self.ctx.clock.now()
        for dim, key, da, db_ in deltas:
            row = s.execute(select(ProfileFeature).where(ProfileFeature.project_id == project_id, ProfileFeature.dimension == dim, ProfileFeature.key == key)).scalar_one_or_none()
            if row is None:
                row = ProfileFeature(project_id=project_id, dimension=dim, key=key, alpha=0.0, beta=0.0, n=0, recent=[])
                s.add(row)
            f = decay_factor(row.last_signal_at, now)
            row.alpha, row.beta = row.alpha * f + da, row.beta * f + db_
            row.n += 1
            row.last_signal_at = now
            if da > 0:
                row.recent = ([*(row.recent or []), now.isoformat()])[-10:]
        s.add(LearningEvent(project_id=project_id, ts=now, source=source, signal=signal, deltas=[[d, k, round(a, 3), round(b, 3)] for d, k, a, b in deltas], ref_type=ref_type, ref_id=str(ref_id), note=note[:300]))
        s.flush()

    # ------------------------------------------------------------- действия пользователя
    def record_selection(self, s: Session, project_id: int, proposal: Proposal, ev: Event, siblings: list[tuple[Proposal, Event]], *, overrides: dict[str, Any] | None = None) -> None:
        deltas = [(d, k, w, 0.0) for d, k, w in event_features(ev)]
        ov = overrides or {}
        signal = "select"
        if ov.get("emphasis"):
            deltas.append(("emphasis", str(ov["emphasis"])[:60].lower(), 0.8, 0.0))
            signal = "select_modified"
        if ov.get("language"):
            deltas.append(("language", ov["language"], 0.5, 0.0))
        if ov.get("format"):
            deltas.append(("format", ov["format"], 0.8, 0.0))
        else:
            deltas.append(("format", (proposal.card.get("format") or {}).get("code", "post_card"), 0.4, 0.0))
        # неявный слабый минус для показанных, но не выбранных предложений
        for sp, sev in siblings:
            if sp.id == proposal.id:
                continue
            same_cat = sev.category == ev.category
            deltas += [("category", sev.category, 0.0, 0.0 if same_cat else 0.12)]
        self.apply(s, project_id, [d for d in deltas if d[2] or d[3]], source="user_action", signal=signal, ref_type="proposal", ref_id=proposal.id, note=f"№{proposal.slot} {ev.title[:80]}")

    def record_rejection(self, s: Session, project_id: int, proposal: Proposal, ev: Event, reason: str = "") -> None:
        deltas = [(d, k, 0.0, w) for d, k, w in event_features(ev)]
        self.apply(s, project_id, deltas, source="user_action", signal="reject", ref_type="proposal", ref_id=proposal.id, note=(reason or ev.title)[:200])

    def record_replace(self, s: Session, project_id: int, proposal: Proposal, ev: Event) -> None:
        deltas = [(d, k, 0.0, 0.5 * w) for d, k, w in event_features(ev)]
        self.apply(s, project_id, deltas, source="user_action", signal="replace", ref_type="proposal", ref_id=proposal.id, note=ev.title[:200])

    def record_more_info(self, s: Session, project_id: int, proposal: Proposal, ev: Event) -> None:
        deltas = [(d, k, 0.3 * w, 0.0) for d, k, w in event_features(ev)]
        self.apply(s, project_id, deltas, source="user_action", signal="more_info", ref_type="proposal", ref_id=proposal.id, note=ev.title[:200])

    # ---------------------------------------------------------------- правки текста
    @staticmethod
    def diff_stats(before: str, after: str) -> dict[str, Any]:
        wb, wa = len(before.split()), len(after.split())
        return {
            "words_before": wb, "words_after": wa, "ratio": round(wa / wb, 2) if wb else 1.0,
            "hashtags_before": len(_TAG.findall(before)), "hashtags_after": len(_TAG.findall(after)),
            "emoji_before": len(_EMOJI.findall(before)), "emoji_after": len(_EMOJI.findall(after)),
            "exclaim_before": before.count("!"), "exclaim_after": after.count("!"),
            "changed": before.strip() != after.strip(),
        }  # fmt: skip

    def record_edit(self, s: Session, project_id: int, content: Content, platform: str, field: str, before: str, after: str, *, user_id: int | None = None) -> dict[str, Any]:
        st = self.diff_stats(before, after)
        if not st["changed"]:
            return st
        s.add(EditorialCorrection(project_id=project_id, content_pk=content.id, platform=platform, field=field, before=before, after=after, stats=st, user_id=user_id))
        deltas: list[tuple[str, str, float, float]] = []
        if st["ratio"] <= 0.85:
            deltas.append(("style", "shorter", 1.0, 0.0))
            deltas.append(("style", "longer", 0.0, 0.5))
        elif st["ratio"] >= 1.15:
            deltas.append(("style", "longer", 1.0, 0.0))
            deltas.append(("style", "shorter", 0.0, 0.5))
        if st["hashtags_after"] < st["hashtags_before"]:
            deltas.append(("style", "fewer_hashtags", 1.0, 0.0))
        if st["emoji_after"] < st["emoji_before"]:
            deltas.append(("style", "fewer_emoji", 1.0, 0.0))
        if st["exclaim_after"] < st["exclaim_before"]:
            deltas.append(("style", "calmer_tone", 1.0, 0.0))
        if deltas:
            self.apply(s, project_id, deltas, source="edit", signal="edit", ref_type="content", ref_id=content.id, note=f"{platform}/{field}")
        s.flush()
        return st

    # ---------------------------------------------------------------- результативность
    def record_performance(self, s: Session, project_id: int, ev: Event | None, content: Content, log_ratio: float, *, confidence: float = 1.0) -> None:
        """log_ratio = ln(результат / базовый уровень аккаунта); храним сумму и сумму квадратов для усадки и дисперсии."""
        now = self.ctx.clock.now()
        feats = [("category", content.category), ("geo", content.geo), *[("subtopic", t) for t in (content.subtopics or [])[:3]]]
        fmt = (content.draft or {}).get("format_code")
        if fmt:
            feats.append(("format", fmt))
        feats.append(("language", content.language))
        up = []
        for dim, key in feats:
            row = s.execute(select(PerformanceStat).where(PerformanceStat.project_id == project_id, PerformanceStat.dimension == dim, PerformanceStat.key == key)).scalar_one_or_none()
            if row is None:
                row = PerformanceStat(project_id=project_id, dimension=dim, key=key, n=0, sum_log_ratio=0.0, sum_sq=0.0)
                s.add(row)
            row.n += 1
            row.sum_log_ratio += log_ratio * confidence
            row.sum_sq += (log_ratio * confidence) ** 2
            row.updated_at = now
            up.append((dim, key, max(0.0, math.tanh(log_ratio)) * confidence, max(0.0, -math.tanh(log_ratio)) * confidence))
        self.apply(s, project_id, [u for u in up if u[0] in ("category", "subtopic", "format") and (u[2] or u[3])], source="performance", signal="performance", ref_type="content", ref_id=content.id, note=f"ln-ratio {log_ratio:+.2f}")
