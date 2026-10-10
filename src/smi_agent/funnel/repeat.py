"""Повтор или новая стадия (ТЗ §51): сравнение кандидатов с недавно опубликованными материалами."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.text import skeleton, tokenize
from ..db.models import Content, Event
from ..settings_model import load_project_settings


def _ent_keys(ev: Event) -> set[str]:
    keys = set()
    for name in ev.keywords or []:
        for w in str(name).replace("-", " ").split():
            k = skeleton(w.lower())
            if len(k) >= 3:
                keys.add(k)
    return keys


def _num_keys(ev: Event) -> set[str]:
    return {f"{n.get('u', '')}:{float(n['v']):.3g}" for n in (ev.features or {}).get("numbers", [])}


def _jac(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b and (a | b) else 0.0


def event_similarity(a: Event, b: Event) -> float:
    ta, tb = set(tokenize(a.title)), set(tokenize(b.title))
    lang_mismatch = set(a.languages or []) != set(b.languages or []) and not (set(a.languages or []) & set(b.languages or []))
    tj = 0.0 if lang_mismatch else _jac(ta, tb)
    ej = _jac(_ent_keys(a), _ent_keys(b))
    nj = _jac(_num_keys(a), _num_keys(b))
    return 0.45 * tj + 0.35 * ej + 0.20 * nj


def annotate_repeats(ctx: Any, s: Session, project_id: int, events: list[Event]) -> int:
    """Выставляет flags.repeat_sim / repeat_of / repeat_kind (duplicate | new_stage) по материалам за repeat_window_days."""
    now = ctx.clock.now()
    from ..db.models import Project

    proj = s.get(Project, project_id)
    win = load_project_settings(proj.settings).funnel.repeat_window_days
    rows = list(s.execute(select(Content.id, Content.content_id, Content.event_id, Content.title).where(Content.project_id == project_id, Content.created_at >= now - timedelta(days=win), Content.status != "cancelled", Content.event_id.is_not(None))))
    if not rows:
        for ev in events:
            ev.flags = {k: v for k, v in (ev.flags or {}).items() if not k.startswith("repeat")}
        return 0
    prev_events = {e.id: e for e in s.scalars(select(Event).where(Event.id.in_({r.event_id for r in rows})))}
    cid_by_event = {r.event_id: r.content_id for r in rows}
    marked = 0
    for ev in events:
        best, best_prev = 0.0, None
        for pid, pe in prev_events.items():
            if pid == ev.id:
                # тот же самый event: новые факты = новая стадия
                best, best_prev = 1.0, pe
                break
            sim = event_similarity(ev, pe)
            if sim > best:
                best, best_prev = sim, pe
        flags = {k: v for k, v in (ev.flags or {}).items() if not k.startswith("repeat")}
        if best_prev is not None and best >= 0.45:
            new_nums = _num_keys(ev) - _num_keys(best_prev)
            grew = ev.n_independent >= 2 * max(1, best_prev.n_independent) and ev.n_independent - best_prev.n_independent >= 3
            has_stage = bool(ctx.know.count("followup", f"{ev.title} {ev.summary}"))
            if new_nums or grew or has_stage:
                flags.update(repeat_kind="new_stage", repeat_of=cid_by_event[best_prev.id], repeat_sim=0.0, stage_sim=round(best, 2), new_numbers=len(new_nums))
            else:
                flags.update(repeat_kind="duplicate", repeat_of=cid_by_event[best_prev.id], repeat_sim=round(min(1.0, best), 2))
            marked += 1
        ev.flags = flags
    return marked
