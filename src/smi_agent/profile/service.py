"""Редакционный профиль (ТЗ §45, §61.22): явные настройки + то, что система поняла из действий и результатов."""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.errors import ValidationFailed
from ..db.models import EditorialCorrection, Event, PerformanceStat, ProfileFeature, ProfileSettings, Proposal, UserAction
from ..learning.service import decay_factor, event_features
from ..security.rbac import Actor

DEFAULT_PROFILE: dict[str, Any] = {
    "brand_voice": "Ясно, точно, по-человечески; без кликбейта, агитации и оценочных ярлыков.",
    "tone": "neutral",  # neutral | friendly | formal
    "priority_categories": [],
    "blocked_keywords": [],
    "forbidden_words": [],
    "signature": "",
    "default_hashtags": [],
    "max_hashtags": {"telegram": 3, "instagram": 8, "facebook": 3},
    "political_policy": "confirm_only",  # политика — только с ручным подтверждением (ТЗ §39, §53)
    "audience_notes": "",
}
_ALLOWED = set(DEFAULT_PROFILE)
POSTERIOR_PRIOR = 1.0


def posterior(alpha: float, beta: float) -> float:
    return (alpha + POSTERIOR_PRIOR) / (alpha + beta + 2 * POSTERIOR_PRIOR)


class ProfileService:
    def __init__(self, ctx: Any):
        self.ctx = ctx

    # ---------------------------------------------------------------- явные настройки
    def get_settings(self, s: Session, project_id: int) -> dict[str, Any]:
        row = s.get(ProfileSettings, project_id)
        return {**DEFAULT_PROFILE, **(row.data if row else {})}

    def update_settings(self, s: Session, project_id: int, actor: Actor, patch: dict[str, Any]) -> dict[str, Any]:
        unknown = set(patch) - _ALLOWED
        if unknown:
            raise ValidationFailed(f"Неизвестные поля профиля: {', '.join(sorted(unknown))}")
        if "tone" in patch and patch["tone"] not in ("neutral", "friendly", "formal"):
            raise ValidationFailed("tone: neutral | friendly | formal")
        if "political_policy" in patch and patch["political_policy"] != "confirm_only":
            raise ValidationFailed("Политические темы публикуются только после ручного подтверждения (ТЗ §39)")
        for k in ("priority_categories", "blocked_keywords", "forbidden_words", "default_hashtags"):
            if k in patch and not (isinstance(patch[k], list) and all(isinstance(x, str) for x in patch[k])):
                raise ValidationFailed(f"{k}: ожидается список строк")
        row = s.get(ProfileSettings, project_id)
        data = {**(row.data if row else {}), **patch}
        if row is None:
            s.add(ProfileSettings(project_id=project_id, data=data, updated_at=self.ctx.clock.now(), updated_by=actor.label))
        else:
            row.data, row.updated_at, row.updated_by = data, self.ctx.clock.now(), actor.label
        s.flush()
        self.ctx.audit.log(s, actor, "profile.update", project_id=project_id, details={"fields": sorted(patch)})
        return {**DEFAULT_PROFILE, **data}

    def is_blocked(self, s: Session, project_id: int, ev: Event) -> str | None:
        words = [w.lower() for w in self.get_settings(s, project_id).get("blocked_keywords", []) if w.strip()]
        text = f"{ev.title} {ev.summary}".lower()
        for w in words:
            if w in text:
                return f"заблокированная тема (профиль): «{w}»"
        return None

    # ---------------------------------------------------------------- оценка близости к интересам
    def _features(self, s: Session, project_id: int, dim: str | None = None) -> dict[tuple[str, str], ProfileFeature]:
        q = select(ProfileFeature).where(ProfileFeature.project_id == project_id)
        if dim:
            q = q.where(ProfileFeature.dimension == dim)
        return {(f.dimension, f.key): f for f in s.scalars(q)}

    def affinity(self, s: Session, project_id: int, ev: Event) -> tuple[float, float, str]:
        now = self.ctx.clock.now()
        feats = self._features(s, project_id)
        total_n = 0
        num = den = 0.0
        notes: list[str] = []
        weights = {"category": 0.45, "subtopic": 0.25, "geo": 0.20}
        sub_means: list[float] = []
        for dim, key, _w in event_features(ev):
            f = feats.get((dim, key))
            if f is None or f.n == 0:
                continue
            d = decay_factor(f.last_signal_at, now)
            m = posterior(f.alpha * d, f.beta * d)
            total_n += f.n
            if dim == "subtopic":
                sub_means.append(m)
                continue
            num += weights[dim] * m
            den += weights[dim]
            if dim == "category" and abs(m - 0.5) > 0.12:
                notes.append(f"тема «{self.ctx.know.category_label(key)}» {'нравится' if m > 0.5 else 'реже выбирается'} ({m:.0%})")
        if sub_means:
            m = sum(sub_means) / len(sub_means)
            num += weights["subtopic"] * m
            den += weights["subtopic"]
        pri = self.get_settings(s, project_id).get("priority_categories", [])
        raw = num / den if den else 0.5
        if ev.category in pri:
            raw = min(1.0, raw + 0.1)
            notes.append("категория отмечена приоритетной в профиле")
        conf = min(1.0, total_n / 15)
        aud = 0.5 + conf * (raw - 0.5)
        hist = self._historical(s, project_id, ev)
        note = "; ".join(notes) if notes else "недостаточно данных профиля — нейтральная оценка"
        return round(aud, 4), round(hist, 4), note

    def _historical(self, s: Session, project_id: int, ev: Event) -> float:
        rows = s.scalars(select(PerformanceStat).where(PerformanceStat.project_id == project_id, PerformanceStat.dimension.in_(["category", "subtopic", "geo"])))
        keys = {(d, k) for d, k, _ in event_features(ev)}
        num = den = 0.0
        for r in rows:
            if (r.dimension, r.key) in keys and r.n:
                shrunk = (r.sum_log_ratio / r.n) * (r.n / (r.n + 5))
                num += r.n * shrunk
                den += r.n
        if not den:
            return 0.5
        return 0.5 + 0.5 * math.tanh(2 * num / den)

    # ---------------------------------------------------------------- стиль (из правок)
    def style_hints(self, s: Session, project_id: int) -> dict[str, Any]:
        now = self.ctx.clock.now()
        feats = self._features(s, project_id, "style")

        def strength(key: str) -> float:
            f = feats.get(("style", key))
            if f is None:
                return 0.5
            d = decay_factor(f.last_signal_at, now)
            return posterior(f.alpha * d, f.beta * d)

        shorter, longer = strength("shorter"), strength("longer")
        length_mult = 1.0 - 0.25 * max(0.0, shorter - 0.5) * 2 + 0.25 * max(0.0, longer - 0.5) * 2
        return {
            "length_multiplier": round(max(0.6, min(1.3, length_mult)), 2),
            "fewer_hashtags": strength("fewer_hashtags") > 0.6,
            "fewer_emoji": strength("fewer_emoji") > 0.6,
            "calmer_tone": strength("calmer_tone") > 0.6,
        }

    # ---------------------------------------------------------------- витрина профиля
    def snapshot(self, s: Session, project_id: int) -> dict[str, Any]:
        now = self.ctx.clock.now()
        know = self.ctx.know
        feats = self._features(s, project_id)
        total_signals = s.scalar(select(func.count()).select_from(UserAction).where(UserAction.project_id == project_id)) or 0

        def rows(dim: str, label=lambda k: k) -> list[dict[str, Any]]:
            out = []
            for (d, k), f in feats.items():
                if d != dim or f.n == 0:
                    continue
                dcy = decay_factor(f.last_signal_at, now)
                out.append({"key": k, "label": label(k), "score": round(posterior(f.alpha * dcy, f.beta * dcy), 3), "n": f.n, "positive": round(f.alpha * dcy, 2), "negative": round(f.beta * dcy, 2)})
            return sorted(out, key=lambda r: -r["score"])

        prefs = {
            "categories": rows("category", know.category_label), "subtopics": rows("subtopic"), "geo": rows("geo", lambda k: {"kz": "Казахстан", "ca": "Центральная Азия", "world": "Мир"}.get(k, k)),
            "formats": rows("format"), "language": rows("language"), "emphasis": rows("emphasis"),
        }
        style = self.style_hints(s, project_id)
        n_edits = s.scalar(select(func.count()).select_from(EditorialCorrection).where(EditorialCorrection.project_id == project_id)) or 0
        statements: list[str] = []
        likes = [r for r in prefs["categories"] if r["score"] >= 0.6 and r["n"] >= 2]
        dislikes = [r for r in prefs["categories"] if r["score"] <= 0.4 and r["n"] >= 2]
        if likes:
            statements.append("Чаще выбираете: " + ", ".join(r["label"] for r in likes[:4]) + ".")
        if dislikes:
            statements.append("Реже выбираете или отклоняете: " + ", ".join(r["label"] for r in dislikes[:4]) + ".")
        geo = {r["key"]: r for r in prefs["geo"]}
        if "kz" in geo and "world" in geo and abs(geo["kz"]["score"] - geo["world"]["score"]) > 0.12:
            statements.append("Предпочитаете " + ("казахстанскую" if geo["kz"]["score"] > geo["world"]["score"] else "мировую") + " повестку.")
        if style["length_multiplier"] < 0.95:
            statements.append("Вы обычно сокращаете тексты — черновики будут короче.")
        if style["fewer_hashtags"]:
            statements.append("Вы убираете хэштеги — их будет меньше.")
        if style["calmer_tone"]:
            statements.append("Вы смягчаете тон и убираете восклицания.")
        if not statements:
            statements.append("Пока слишком мало сигналов: выбирайте, отклоняйте и правьте предложения — профиль подстроится.")
        return {
            "signals": total_signals, "confidence": round(min(1.0, total_signals / 30), 2), "preferences": prefs, "style": style, "edits": n_edits,
            "statements": statements, "explicit": self.get_settings(s, project_id), "choice_analysis": self.choice_analysis(s, project_id),
        }  # fmt: skip

    def choice_analysis(self, s: Session, project_id: int) -> dict[str, Any]:
        """Анализ выбора пользователя (ТЗ §44): что показывали, что выбирали/отклоняли, какой слот, какая гео-доля."""
        props = list(s.scalars(select(Proposal).where(Proposal.project_id == project_id)))
        shown = Counter()
        sel = Counter()
        rej = Counter()
        geo_shown = Counter()
        geo_sel = Counter()
        slots = Counter()
        for p in props:
            cat = (p.card or {}).get("category", "other")
            bucket = (p.card or {}).get("geo_bucket", "KZ")
            shown[cat] += 1
            geo_shown[bucket] += 1
            if p.status in ("selected", "executed"):
                sel[cat] += 1
                geo_sel[bucket] += 1
                slots[p.slot] += 1
            elif p.status in ("rejected", "replaced"):
                rej[cat] += 1
        n_sel = sum(sel.values())
        know = self.ctx.know
        cats = [{"category": c, "label": know.category_label(c), "shown": n, "selected": sel[c], "rejected": rej[c], "selection_rate": round(sel[c] / n, 2) if n else 0} for c, n in shown.most_common()]
        return {
            "proposals_shown": len(props), "selected": n_sel, "categories": cats,
            "geo": {"shown": dict(geo_shown), "selected": dict(geo_sel), "selected_kz_share": round(geo_sel["KZ"] / n_sel, 2) if n_sel else None},
            "slot_distribution": {str(k): v for k, v in sorted(slots.items())},
            "picks_first_share": round(slots[1] / n_sel, 2) if n_sel else None,
        }  # fmt: skip
