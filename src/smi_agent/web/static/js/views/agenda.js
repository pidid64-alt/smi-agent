import { api, h, clear, fmtAgo, fmtDate, pct, phaseBadge, verifBadge, geoChip, drawer, bars, lineChart, legend, toast } from "../lib.js";

const COMP = { freshness: "Свежесть", independent_sources: "Независимые источники", velocity: "Скорость роста", scale: "Масштаб", novelty: "Новизна", significance: "Значимость (КЗ/мир)", practical_value: "Практическая ценность", audience_interest: "Интерес аудитории", discussion: "Потенциал обсуждения", fact_sufficiency: "Достаточность фактов", source_quality: "Качество источников", feasibility: "Выполнимость", historical: "Историческая эффективность" };
const STAGE = { pool: "пул", s50: "50", s15: "15", s10: "10", s5: "5", published: "опубликовано", dropped: "отсеяно" };
const SRC_STATE = { ok: ["работает", "ok"], stale: ["устарела лента", "warn"], failing: ["ошибки", "bad"], never_polled: ["не опрашивался", ""], disabled: ["отключён", ""], manual: ["ручной", "info"] };
const srcOrder = (a, b) => (b.mandatory - a.mandatory) || ((a.state === "disabled") - (b.state === "disabled")) || a.name.localeCompare(b.name);

export async function render(view) {
  const [dash, evs] = await Promise.all([api.get(api.p("/dashboard")), api.get(api.p("/events?limit=60"))]);
  const f = dash.agenda.funnel;
  const box = (label, n, sub) => h("div", { class: "stage" }, h("span", { class: "muted small" }, label), h("b", null, n ?? "—"), sub ? h("span", { class: "small muted" }, sub) : null);
  const geoShare = (k) => { const g = f?.geo_ratio?.[k]; return g && g.kz_share !== null ? `КЗ ${pct(g.kz_share)}` : ""; };
  const run = async (ev, path, label) => {
    const b = ev.currentTarget; b.disabled = true;
    try { await api.post(api.p(path)); toast(label + " выполнено", "ok"); await new Promise((r) => setTimeout(r, path.includes("pipeline") ? 1200 : 0)); view.replaceChildren(); await render(view); } finally { b.disabled = false; }
  };
  view.append(
    h("div", { class: "row between" }, h("h1", null, "Информационная повестка"), h("div", { class: "row" },
      h("button", { class: "btn", onclick: (ev) => run(ev, "/pipeline/run", "Сбор и оценка") }, "Собрать и оценить"), h("button", { class: "btn primary", onclick: (ev) => run(ev, "/funnel/run", "Воронка") }, "Запустить воронку"))),
    h("div", { class: "card" }, h("h2", null, "Воронка отбора: 50 › 15 › 10 › 5"),
      f ? [h("div", { class: "funnel" }, box("Пул событий", f.counts.pool), h("span", { class: "arrow", "aria-hidden": "true" }), box("Этап 1 · значимые", f.counts.s50, geoShare("s50")), h("span", { class: "arrow", "aria-hidden": "true" }), box("Этап 2 · без шума", f.counts.s15, geoShare("s15")), h("span", { class: "arrow", "aria-hidden": "true" }), box("Этап 3 · проверено", f.counts.s10, geoShare("s10")), h("span", { class: "arrow", "aria-hidden": "true" }), box("Этап 4 · предложения", f.counts.s5, geoShare("s5"))),
        h("p", { class: "small muted" }, `Ориентир гео-баланса: КЗ ${pct(f.geo_ratio.target_kz)} / мир ${pct(1 - f.geo_ratio.target_kz)} — мягкий приор, а не квота. ${f.notes?.note || ""} Обновлено ${fmtAgo(f.finished_at)}.`)]
        : h("div", { class: "empty" }, "Воронка ещё не запускалась.")),
    h("div", { class: "card", style: { marginTop: "14px" } }, h("h2", null, "События по Trend Score"),
      h("p", { class: "small muted" }, "Число публикаций ≠ значимость: перепечатки и обновления одного источника не считаются независимыми подтверждениями и не ускоряют рост."),
      eventsTable(evs.events)),
    h("details", { class: "card", style: { marginTop: "14px" } }, h("summary", null, h("b", null, "Источники и их состояние"), ` · ${dash.agenda.sources.length}`), sourcesTable(dash.agenda.sources)));
}

function eventsTable(events) {
  if (!events.length) return h("div", { class: "empty" }, "Событий пока нет. Нажмите «Собрать и оценить».");
  const max = Math.max(...events.map((e) => e.trend_score), 1);
  return h("div", { class: "table-wrap" }, h("table", null, h("thead", null, h("tr", null, ["Trend Score", "Событие", "Публ. / независимых", "Динамика", "Проверка", "Этап"].map((x) => h("th", null, x)))),
    h("tbody", null, events.map((e) => h("tr", { class: "click", tabindex: "0", onclick: () => detail(e.id), onkeydown: (ev) => { if (ev.key === "Enter") detail(e.id); } },
      h("td", { style: { width: "130px" } }, h("div", { class: "score" }, e.trend_score.toFixed(1)), h("div", { class: "bar" }, h("i", { style: { width: (e.trend_score / max) * 100 + "%" } }))),
      h("td", null, h("div", null, e.title), h("div", { class: "row gap-s", style: { marginTop: "3px" } }, h("span", { class: "chip" }, e.category_label), geoChip(e.geo))),
      h("td", { class: "nowrap" }, h("b", null, e.n_articles), " / ", h("b", null, e.n_independent), e.n_articles > e.n_independent * 2 + 1 ? h("div", { class: "small muted" }, "много перепечаток") : null),
      h("td", null, phaseBadge(e.phase), h("div", { class: "small muted" }, e.velocity ? `+${e.velocity.toFixed(1)} ист./ч` : "")),
      h("td", null, e.verification ? verifBadge(e.verification) : h("span", { class: "muted small" }, "—")),
      h("td", null, h("span", { class: "chip" }, STAGE[e.stage] || e.stage)))))));
}

async function detail(id) {
  const d = await api.get(api.p(`/events/${id}`));
  const vals = d.components?.values || {}, ex = d.components?.explain || {};
  const tl = d.timeline || [];
  const body = h("div", { class: "col" },
    h("div", { class: "row" }, phaseBadge(d.phase), d.verification_status ? verifBadge(d.verification_status) : null, geoChip(d.geo), h("span", { class: "chip" }, "Trend Score " + d.trend_score.toFixed(1))),
    h("p", null, d.summary),
    h("div", { class: "card flat" }, h("h3", null, "Из чего сложилась оценка"),
      bars(Object.keys(COMP).map((k) => ({ label: COMP[k], value: vals[k] || 0, text: ((vals[k] || 0) * 100).toFixed(0), title: ex[k] || "" })), { max: 1 }),
      h("ul", { class: "small muted" }, Object.keys(COMP).filter((k) => ex[k]).map((k) => h("li", null, h("b", null, COMP[k] + ": "), ex[k])))),
    Object.keys(d.components?.penalties || {}).length ? h("div", { class: "notice warn" }, "Штрафы: " + Object.entries(d.components.penalties).map(([k, v]) => `${k} ×${v}`).join(", ")) : null,
    tl.length > 1 ? h("div", { class: "card flat" }, h("h3", null, "Рост числа независимых источников"), lineChart(tl.map((p) => fmtDate(p.t)), [{ name: "Независимые источники", points: tl.map((p) => p.independent) }, { name: "Публикации", points: tl.map((p) => p.articles) }], { height: 180, ariaLabel: "Рост источников" }), legend(["Независимые источники", "Публикации (вкл. перепечатки)"])) : null,
    h("div", { class: "card flat" }, h("h3", null, `Материалы (${d.articles.length})`), h("div", { class: "table-wrap" }, h("table", null, h("thead", null, h("tr", null, ["Источник", "Тип", "Время"].map((x) => h("th", null, x)))),
      h("tbody", null, d.articles.map((a) => h("tr", null, h("td", null, a.source, h("div", { class: "small" }, a.title.slice(0, 80))), h("td", null, h("span", { class: "badge " + (a.independent ? "ok" : "") }, RELATION[a.relation] || a.relation), a.independent ? null : h("div", { class: "small muted" }, "не независимый")), h("td", { class: "small" }, fmtDate(a.published_at)))))))),
    d.features?.facts?.length ? h("div", { class: "card flat" }, h("h3", null, "Факты"), h("ul", null, d.features.facts.slice(0, 6).map((x) => h("li", null, x.text)))) : null);
  drawer(d.title, body);
}
const RELATION = { original: "оригинал", duplicate: "дубль", reprint: "перепечатка", translation: "перевод", update: "обновление", independent: "независимая" };

function sourcesTable(rows) {
  return h("div", { class: "table-wrap", style: { marginTop: "10px" } }, h("table", null, h("thead", null, h("tr", null, ["Источник", "Страна", "Статус", "Последний материал", "Результат", "Лента"].map((x) => h("th", null, x)))),
    h("tbody", null, [...rows].sort(srcOrder).map((r) => { const [l, c] = SRC_STATE[r.state] || [r.state, ""]; return h("tr", null, h("td", null, r.name, r.mandatory ? h("span", { class: "badge dark", style: { marginLeft: "6px" } }, "обязательный") : null, h("div", { class: "small muted" }, r.tier + " · " + r.independence_group)), h("td", null, r.country), h("td", null, h("span", { class: "badge " + c }, l)), h("td", { class: "small" }, fmtAgo(r.last_item_at)), h("td", { class: "small" }, r.last_status || "—"), h("td", null, r.verified_url ? h("span", { class: "badge ok" }, "проверена") : h("span", { class: "badge warn", title: "Адрес ленты не проверен вручную" }, "не проверена"))); }))));
}
