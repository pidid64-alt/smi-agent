"""События и дедупликация (ТЗ §8, §61.3–4): одно событие + N источников, независимые подтверждения считаются по происхождению."""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.enums import Relation
from ..core.errors import NotFound
from ..core.text import hamming, skeleton
from ..db.models import Article, Event, EventSnapshot, Project, Source
from ..settings_model import load_project_settings
from .similarity import ArtVec, EventProf, make_idf, make_vec, member_cos, num_specificity, pair_score

log = logging.getLogger(__name__)
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass
class ClusterStats:
    new_articles: int = 0
    attached: int = 0
    new_events: int = 0
    merged: int = 0
    touched: list[int] = field(default_factory=list)


def _chunks(seq: list[int], n: int = 400):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


class EventService:
    def __init__(self, ctx: Any):
        self.ctx = ctx

    # ---------------------------------------------------------------- происхождение
    def _origin_fn(self, sources: list[Source]):
        know = self.ctx.know
        group_by_key = {s.key: (s.independence_group or s.key) for s in sources}
        wire = know.lex.get("wire_names")

        def origin_of(a: Article) -> str:
            own_group = a.source.independence_group or a.source.key
            attr = (a.features or {}).get("attribution", {})
            for key in attr.get("keys", []):
                g = group_by_key.get(key)
                if g and g != own_group:
                    return "g:" + g
            if wire is not None:
                for name in attr.get("names", []):
                    hit = wire.matched(name)
                    if hit and skeleton(hit[0].replace(" ", "")) != skeleton(a.source.name.lower().replace(" ", "")):
                        return "w:" + skeleton(hit[0].replace(" ", ""))
            return "g:" + own_group

        return origin_of

    # ------------------------------------------------------------------- кластеризация
    def process_new(self, project_id: int) -> ClusterStats:
        ctx = self.ctx
        stats = ClusterStats()
        with ctx.db.session() as s:
            now = ctx.clock.now()
            proj = s.get(Project, project_id)
            mon = load_project_settings(proj.settings).monitoring
            new_arts = list(s.scalars(select(Article).where(Article.project_id == project_id, Article.event_id.is_(None)).order_by(Article.published_at, Article.id).limit(3000)))
            if not new_arts:
                return stats
            stats.new_articles = len(new_arts)
            sources = list(s.scalars(select(Source).where(Source.project_id == project_id)))
            origin_of = self._origin_fn(sources)
            window = timedelta(hours=mon.dedup_window_hours)
            events = list(s.scalars(select(Event).where(Event.project_id == project_id, Event.merged_into_id.is_(None), Event.last_update_at >= now - window)))
            ev_by_id = {e.id: e for e in events}
            members: dict[int, list[Article]] = defaultdict(list)
            for ids in _chunks(list(ev_by_id)):
                for a in s.scalars(select(Article).where(Article.event_id.in_(ids))):
                    members[a.event_id].append(a)
            profs: dict[int, EventProf] = {}
            vec_by_art: dict[int, ArtVec] = {}
            for eid, arts in members.items():
                p = EventProf(eid)
                for a in sorted(arts, key=lambda x: (x.published_at, x.id)):
                    v = make_vec(a, origin=origin_of(a), group=a.source.independence_group or a.source.key)
                    vec_by_art[a.id] = v
                    p.add(v)
                profs[eid] = p
            new_vecs = {a.id: make_vec(a, origin=origin_of(a), group=a.source.independence_group or a.source.key) for a in new_arts}
            idf = make_idf([set(p.tf) for p in profs.values()] + [set(v.tf) for v in new_vecs.values()])

            # инвертированные индексы для выбора кандидатов
            idx_stem: dict[str, set[int]] = defaultdict(set)
            idx_ent: dict[str, set[int]] = defaultdict(set)
            idx_num: dict[str, set[int]] = defaultdict(set)

            def index(p: EventProf, v: ArtVec | None = None) -> None:
                vs = [v] if v is not None else p.members
                for mv in vs:
                    for st in mv.tf:
                        idx_stem[st].add(p.id)
                    for k in mv.ent:
                        idx_ent[k].add(p.id)
                    for nk in mv.nums:
                        if num_specificity(nk) >= 0.75:
                            idx_num[nk].add(p.id)

            for p in profs.values():
                index(p)

            touched: set[int] = set()
            by_event_new: dict[int, list[Article]] = defaultdict(list)
            for a in new_arts:
                v = new_vecs[a.id]
                hits: Counter = Counter()
                for st in sorted(v.tf, key=lambda x: -idf(x))[:16]:
                    for eid in idx_stem.get(st, ()):
                        hits[eid] += 1
                for k in v.ent:
                    for eid in idx_ent.get(k, ()):
                        hits[eid] += 2
                for nk in v.nums:
                    for eid in idx_num.get(nk, ()):
                        hits[eid] += 3
                best_id, best = None, None
                for eid, _n in hits.most_common(14):
                    sc = pair_score(v, profs[eid], idf)
                    if sc.attach and (best is None or sc.score > best.score):
                        best_id, best = eid, sc
                if best_id is None:
                    ev = Event(project_id=project_id, title=a.title, first_seen_at=now, first_published_at=a.published_at, last_update_at=a.published_at, stage="pool")
                    s.add(ev)
                    s.flush()
                    p = EventProf(ev.id)
                    profs[ev.id] = p
                    ev_by_id[ev.id] = ev
                    best_id = ev.id
                    stats.new_events += 1
                else:
                    stats.attached += 1
                    a.similarity = best.cos
                a.event_id = best_id
                members[best_id].append(a)
                by_event_new[best_id].append(a)
                profs[best_id].add(v)
                vec_by_art[a.id] = v
                index(profs[best_id], v)
                touched.add(best_id)
            s.flush()

            # слияние событий, оказавшихся одним и тем же
            stats.merged = self._merge_pass(s, profs, members, ev_by_id, touched, idx_stem, idx_ent, idx_num, idf, vec_by_art)
            for eid in list(touched):
                ev = ev_by_id[eid]
                if ev.merged_into_id is not None:
                    touched.discard(eid)
                    continue
                self.refresh_event(s, ev, members[eid], vec_by_art, idf, now, sources)
            stats.touched = sorted(touched)
        log.info("cluster project=%s new=%d attached=%d new_events=%d merged=%d", project_id, stats.new_articles, stats.attached, stats.new_events, stats.merged)
        return stats

    def _merge_pass(self, s, profs, members, ev_by_id, touched, idx_stem, idx_ent, idx_num, idf, vec_by_art) -> int:
        merged = 0
        for eid in sorted(touched, key=lambda i: -len(members[i])):
            if ev_by_id[eid].merged_into_id is not None:
                continue
            p = profs[eid]
            hits: Counter = Counter()
            for st in p.tf:
                for other in idx_stem.get(st, ()):
                    if other != eid:
                        hits[other] += 1
            for k in p.ent:
                for other in idx_ent.get(k, ()):
                    if other != eid:
                        hits[other] += 2
            for nk in p.nums:
                for other in idx_num.get(nk, ()):
                    if other != eid:
                        hits[other] += 3
            pseudo = self._pseudo_vec(p)
            for oid, _n in hits.most_common(6):
                o = ev_by_id.get(oid)
                if o is None or o.merged_into_id is not None or oid == eid:
                    continue
                op = profs[oid]
                if abs(((p.first_pub or EPOCH) - (op.first_pub or EPOCH)).total_seconds()) > 60 * 3600:
                    continue
                sc = pair_score(pseudo, op, idf)
                strong = sc.attach and sc.score >= 0.42 and (sc.cos >= 0.3 or sc.cross_lang)
                if not strong:
                    continue
                big, small = (eid, oid) if len(members[eid]) >= len(members[oid]) else (oid, eid)
                self._merge(s, big, small, profs, members, ev_by_id, vec_by_art)
                touched.add(big)
                merged += 1
                if small == eid:
                    break
        return merged

    @staticmethod
    def _pseudo_vec(p: EventProf) -> ArtVec:
        first = p.members[0]
        return ArtVec(
            id=-p.id, source_id=first.source_id, source_key="", group="", origin="", reliability=0.7, tier="quality", country="", pub=p.first_pub or first.pub,
            lang=p.dominant_lang, title_stems=frozenset(p.title_stems), tf={k: min(v, 2.0) for k, v in p.tf.items()}, ent=dict(p.ent), title_ent=frozenset(p.title_ent),
            nums=frozenset(p.nums), numd={k: frozenset(v) for k, v in p.numd.items()}, quotes=frozenset(p.quotes), dates=frozenset(p.dates), sim=first.sim,
        )  # fmt: skip

    def _merge(self, s: Session, big: int, small: int, profs, members, ev_by_id, vec_by_art) -> None:
        for a in members[small]:
            a.event_id = big
            members[big].append(a)
        for v in profs[small].members:
            profs[big].add(v)
        members[small] = []
        profs[small] = EventProf(small)
        sm = ev_by_id[small]
        sm.merged_into_id = big
        sm.stage = "dropped"
        sm.flags = {**(sm.flags or {}), "merged": True}
        s.flush()

    # ------------------------------------------------------------------- агрегаты события
    def refresh_event(self, s: Session, ev: Event, arts: list[Article], vec_by_art: dict[int, ArtVec], idf, now: datetime, sources: list[Source]) -> None:
        know = self.ctx.know
        arts = sorted(arts, key=lambda a: (a.published_at, a.id))
        vecs = [(a, vec_by_art[a.id]) for a in arts]
        # «первый носитель» происхождения: предпочтительно не производная публикация, затем самая ранняя
        first_for_origin: dict[str, tuple[tuple, int]] = {}
        for a, v in vecs:
            key = (v.origin != "g:" + v.group, a.published_at, a.id)
            cur = first_for_origin.get(v.origin)
            if cur is None or key < cur[0]:
                first_for_origin[v.origin] = (key, a.id)
        seen_origin = {o: vec_by_art[aid] for o, (_k, aid) in first_for_origin.items()}
        prior: list[ArtVec] = []
        for i, (a, v) in enumerate(vecs):
            deriv = v.origin != "g:" + v.group  # ссылается на чужой источник («передает …», «со ссылкой на …»)
            best_cos, best_id = 0.0, None
            for pv in prior:
                c = member_cos(v, pv, idf)
                if hamming(v.sim, pv.sim) <= 3:
                    c = max(c, 0.95)
                if c > best_cos:
                    best_cos, best_id = c, pv.id
            a.similarity = round(best_cos, 3)
            fid = first_for_origin[v.origin][1]
            if a.id == fid:
                if i == 0:
                    a.relation = Relation.ORIGINAL.value
                elif deriv:
                    a.relation = Relation.TRANSLATION.value if prior and v.lang not in {pv.lang for pv in prior} else Relation.REPRINT.value
                else:
                    a.relation = Relation.INDEPENDENT.value
                a.relation_to = best_id if (best_cos >= 0.5 and i > 0) else None
                a.independent = not deriv
            else:
                first = vec_by_art[fid]
                a.relation_to = fid
                if v.group == first.group:
                    a.relation = Relation.DUPLICATE.value if best_cos >= 0.9 else Relation.UPDATE.value
                elif deriv and v.lang != first.lang:
                    a.relation = Relation.TRANSLATION.value
                else:
                    a.relation = Relation.REPRINT.value
                a.independent = False
            prior.append(v)

        first, last = arts[0], arts[-1]
        ev.first_published_at = first.published_at
        ev.last_update_at = last.published_at
        ev.n_articles = len(arts)
        ev.n_sources = len({a.source_id for a in arts})
        direct = {v.origin for _a, v in vecs if v.origin == "g:" + v.group}
        cited_only = sorted({v.origin for _a, v in vecs if v.origin != "g:" + v.group} - direct)
        # независимые = происхождения с собственным материалом в событии; «заявленные» (только по ссылке) учитываются, лишь если других нет
        ev.n_independent = len(direct) if direct else len(cited_only)
        ev.languages = sorted({a.lang for a in arts})
        carriers = []
        for o, v in sorted(seen_origin.items(), key=lambda kv: kv[1].pub):
            carriers.append({"t": v.pub.isoformat(), "r": v.reliability, "tier": v.tier, "c": v.country, "k": v.source_key, "cited_only": o in cited_only and bool(direct)})

        # представитель события: лучший по надёжности/полноте, язык проекта предпочтителен
        default_lang = "ru"

        def rep_key(a: Article):
            f = a.features or {}
            fl = f.get("flags", {})
            return (
                a.source.reliability * 2 + (1.0 if a.source.is_official else 0) + (0.5 if a.lang == default_lang else 0) + (0.4 if a.has_full_text else 0)
                - 0.3 * min(3, fl.get("clickbait", 0)) - 0.5 * min(2, fl.get("ad", 0)),
                -a.published_at.timestamp(),
            )  # fmt: skip

        rep = max(arts, key=rep_key)
        ev.title = rep.title
        ev.summary = (rep.summary or rep.body)[:600]
        official = [a for a in arts if a.source.is_official or know.is_official_url(a.url)]
        prim = official[0] if official else max((a for a in arts if a.independent), key=lambda a: (a.source.reliability, -a.published_at.timestamp()), default=first)
        ev.primary_article_id = prim.id
        ev.primary_source_url = prim.url

        # тема и география
        cat_scores: Counter = Counter()
        subtopics: Counter = Counter()
        kz = ca = world = 0.0
        kz_hits: set[str] = set()
        for a in sorted(arts, key=lambda a: -a.source.reliability)[:8]:
            cr = know.classify(a.title, a.summary, a.body, hint=a.source.category_hint, tags=a.tags)
            for cid, sc in cr.scores.items():
                cat_scores[cid] += sc * (0.6 + 0.4 * a.source.reliability)
            subtopics.update(cr.subtopics)
            g = know.geo_signals(a.title, a.summary, a.body, source_country=a.source.country)
            kz, ca, world = max(kz, g.kz), max(ca, g.ca), max(world, g.world)
            kz_hits.update(g.kz_hits)
        ev.category = cat_scores.most_common(1)[0][0] if cat_scores and cat_scores.most_common(1)[0][1] >= 2.5 else (rep.source.category_hint or "other")
        ev.subtopics = [k for k, _ in subtopics.most_common(4)]
        # местная новость без явных топонимов: все независимые источники — казахстанские и мировых маркеров нет → казахстанская повестка
        direct_vecs = [v for o, v in seen_origin.items() if o not in cited_only] or list(seen_origin.values())
        if direct_vecs and sum(1 for v in direct_vecs if v.country == "KZ") / len(direct_vecs) >= 0.75 and world < 0.2:
            kz = max(kz, 0.5)
        foreign_indep = len({v.origin for v in seen_origin.values() if v.country and v.country != "KZ"})
        world = min(1.0, world + 0.08 * max(0, foreign_indep - 1)) if foreign_indep else world
        ev.kz_relevance, ev.world_relevance = round(kz, 3), round(world, 3)
        if kz >= 0.45 and kz >= 0.75 * world:
            ev.geo = "kz"
        elif ca >= 0.5 and ca >= kz:
            ev.geo = "ca"
        elif kz >= 0.55:
            ev.geo = "kz"
        else:
            ev.geo = "world"
        ev.geo_bucket = "KZ" if ev.geo in ("kz", "ca") else "WORLD"

        # флаги качества и чувствительности
        n = len(arts)
        shares = lambda key: sum(1 for a in arts if (a.features or {}).get("flags", {}).get(key, 0)) / n  # noqa: E731
        text_for_sens = f"{rep.title} {rep.summary} {(rep.body or '')[:1200]}"
        sens = know.sensitive_hits(text_for_sens)
        ev.flags = {
            "hedge_share": round(shares("hedge"), 2), "clickbait_share": round(shares("clickbait"), 2), "ad_share": round(shares("ad"), 2),
            "opinion_share": round(shares("opinion"), 2),
            "injection": any((a.features or {}).get("flags", {}).get("injection") for a in arts),
            "sensitive": sens, "political": know.is_political_category(ev.category) or "elections" in sens or "political_figures" in sens,
            "sensitive_category": know.is_sensitive_category(ev.category), "official_present": bool(official),
            "paywalled_only": all(a.source.paywalled for a in arts),
            **{k: v for k, v in (ev.flags or {}).items() if k in ("merged",)},
        }  # fmt: skip

        # сводные признаки для проверки и карточки
        nums: dict[str, dict[str, Any]] = {}
        quotes: dict[str, dict[str, Any]] = {}
        ents: Counter = Counter()
        ent_names: dict[str, str] = {}
        facts: list[dict[str, Any]] = []
        for a in arts:
            f = a.features or {}
            for nn in f.get("numbers", []):
                key = f"{nn['u']}:{nn['v']:.3g}"
                d = nums.setdefault(key, {**nn, "sources": []})
                if a.source.key not in d["sources"]:
                    d["sources"].append(a.source.key)
            for q in f.get("quotes", []):
                d = quotes.setdefault(q["norm"], {"text": q["text"], "sources": []})
                if a.source.key not in d["sources"]:
                    d["sources"].append(a.source.key)
            for e in f.get("entities", []):
                ents[e["k"]] += 1 + (2 if e.get("title") else 0)
                ent_names.setdefault(e["k"], e["t"])
        for a in sorted(arts, key=lambda x: (x.id != rep.id, -x.source.reliability)):
            for ft in (a.features or {}).get("facts", [])[:4]:
                if all(ft != x["text"] for x in facts):
                    facts.append({"text": ft, "source": a.source.key, "article_id": a.id})
            if len(facts) >= 8:
                break
        ev.keywords = [ent_names[k] for k, _ in ents.most_common(8)]
        ev.features = {
            "numbers": sorted(nums.values(), key=lambda d: -len(d["sources"]))[:12],
            "quotes": sorted(quotes.values(), key=lambda d: -len(d["sources"]))[:5],
            "dates": sorted({d for a in arts for d in (a.features or {}).get("dates", [])})[:8],
            "facts": facts,
            "cited_names": sorted({n_ for a in arts for n_ in (a.features or {}).get("attribution", {}).get("names", [])})[:8],
            "official_urls": [a.url for a in official][:5],
            "rep_article_id": rep.id,
            "carriers": carriers,
            "cited_only_origins": cited_only if direct else [],
        }  # fmt: skip
        ev.event_type = self._event_type(ev, rep)
        ev.last_clustered_at = now
        # снимок для истории роста
        last = s.scalars(select(EventSnapshot).where(EventSnapshot.event_id == ev.id).order_by(EventSnapshot.ts.desc()).limit(1)).first()
        if last is None or (last.n_independent, last.n_articles) != (ev.n_independent, ev.n_articles) or now - last.ts > timedelta(minutes=30):
            s.add(EventSnapshot(event_id=ev.id, ts=now, n_articles=ev.n_articles, n_sources=ev.n_sources, n_independent=ev.n_independent, trend_score=ev.trend_score, velocity=ev.velocity))
        s.flush()

    def _event_type(self, ev: Event, rep: Article) -> str:
        know = self.ctx.know
        text = f"{rep.title} {rep.summary}"
        if ev.category == "incident":
            return "incident"
        if (ev.flags or {}).get("official_present") or know.count("official_markers", text):
            return "official_statement"
        if know.count("novelty", rep.title):
            return "launch_or_first"
        if ev.category == "science":
            return "research"
        if ev.category in ("economy", "finance") and (ev.features or {}).get("numbers"):
            return "data_release"
        return "report"

    # ------------------------------------------------------------------- чтение
    def timeline(self, s: Session, event: Event) -> list[dict[str, Any]]:
        """Накопительное число независимых источников по времени публикации (пример ТЗ: 08:00→3, 10:00→15, 12:00→40)."""
        arts = list(s.scalars(select(Article).where(Article.event_id == event.id).order_by(Article.published_at)))
        out, n = [], 0
        for a in arts:
            if a.independent:
                n += 1
            out.append({"t": a.published_at.isoformat(), "independent": n, "articles": len(out) + 1})
        return out

    def detail(self, s: Session, project_id: int, event_id: int) -> dict[str, Any]:
        ev = s.get(Event, event_id)
        if ev is None or ev.project_id != project_id:
            raise NotFound("Событие не найдено")
        arts = list(s.scalars(select(Article).where(Article.event_id == ev.id).order_by(Article.published_at)))
        return {
            "id": ev.id, "title": ev.title, "summary": ev.summary, "category": ev.category, "geo": ev.geo, "trend_score": ev.trend_score,
            "phase": ev.phase, "n_independent": ev.n_independent, "n_sources": ev.n_sources, "n_articles": ev.n_articles, "components": ev.components,
            "verification_status": ev.verification_status, "stage": ev.stage, "flags": ev.flags, "features": ev.features,
            "articles": [
                {"id": a.id, "source": a.source.name, "source_key": a.source.key, "title": a.title, "url": a.url, "published_at": a.published_at.isoformat(),
                 "relation": a.relation, "independent": a.independent, "similarity": a.similarity, "lang": a.lang}
                for a in arts
            ],
            "timeline": self.timeline(s, ev),
        }  # fmt: skip

