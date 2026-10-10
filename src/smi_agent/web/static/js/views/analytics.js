import { api, h, clear, toast, md, modal, barChart, lineChart, scatter, legend, bars, pct, fmtDate, fmtNum } from "../lib.js";

const PERIODS = [["today", "Сегодня"], ["7d", "7 дней"], ["30d", "30 дней"], ["3m", "3 месяца"], ["6m", "6 месяцев"], ["year", "Год"], ["custom", "Свой период"]];
const CH = [["posts", "Публикации", "bar"], ["views", "Просмотры", "bar"], ["reach", "Охват", "bar"], ["engagement", "Вовлечённость", "bar"], ["kz_share", "Доля КЗ в публикациях и ориентир 60%", "line"], ["events", "Новые события в повестке", "bar"], ["trend_avg", "Средний Trend Score", "line"], ["selected", "Выбор пользователя по предложениям", "bar"], ["followers", "Подписчики", "line"]];

export async function render(view) {
  let period = "7d", platform = "";
  const s0 = h("input", { type: "date", "aria-label": "Начало" }), s1 = h("input", { type: "date", "aria-label": "Конец" });
  const sel = h("select", { "aria-label": "Период", onchange: () => { period = sel.value; custom.classList.toggle("hidden", period !== "custom"); if (period !== "custom") load(); } }, PERIODS.map(([v, l]) => h("option", { value: v, selected: v === period }, l)));
  const psel = h("select", { "aria-label": "Платформа", onchange: () => { platform = psel.value; load(); } }, [["", "Все платформы"], ["telegram", "Telegram"], ["instagram", "Instagram"], ["facebook", "Facebook"]].map(([v, l]) => h("option", { value: v }, l)));
  const custom = h("span", { class: "row hidden" }, s0, "—", s1, h("button", { class: "btn sm", onclick: () => load() }, "Показать"));
  const charts = h("div", { class: "grid g2" }), extra = h("div", { class: "col" });
  view.append(h("h1", null, "Аналитика"), h("div", { class: "card row" }, sel, custom, psel, h("span", { class: "small muted" }, "Время — по часовому поясу проекта. Метрики, недоступные через API платформы, показываются как «нет данных», а не нулями.")), h("div", { style: { height: "12px" } }), charts, h("div", { style: { height: "12px" } }), extra);
  async function load() {
    clear(charts);
    const q = (m) => `/analytics/series?metric=${m}&period=${period}${platform ? "&platform=" + platform : ""}${period === "custom" && s0.value && s1.value ? `&start=${new Date(s0.value).toISOString()}&end=${new Date(s1.value + "T23:59:59").toISOString()}` : ""}`;
    if (period === "custom" && !(s0.value && s1.value)) return;
    const res = await Promise.all(CH.map(([m]) => api.get(api.p(q(m)), { silent: true }).catch(() => null)));
    CH.forEach(([m, title, kind], i) => {
      const d = res[i]; const card = h("div", { class: "card" }, h("h2", null, title));
      if (!d) card.append(h("div", { class: "muted" }, "Не удалось загрузить"));
      else if (!d.series.length || d.series.every((s) => s.points.every((p) => !p))) card.append(h("div", { class: "empty" }, d.unavailable_platforms?.length ? `Нет данных: платформа не отдаёт эту метрику (${d.unavailable_platforms.join(", ")}). Внесите вручную или импортируйте статистику.` : "За период данных нет"));
      else { const names = d.series.map((s) => s.name); card.append(kind === "bar" ? barChart(d.labels, d.series, { ariaLabel: title }) : lineChart(d.labels, d.series, { ariaLabel: title, max: m === "kz_share" ? 1 : null, dashed: ["Цель"] }), legend(names)); if (d.unavailable_platforms?.length) card.append(h("div", { class: "small muted" }, "Нет метрики у: " + d.unavailable_platforms.join(", "))); }
      charts.append(card);
    });
    await sideData(extra);
  }
  await load();
}

async function sideData(box) {
  clear(box);
  const [geo, fc, ins, reps] = await Promise.all([api.get(api.p("/analytics/geo?days=30")), api.get(api.p("/analytics/forecast")), api.get(api.p("/analytics/insights")), api.get(api.p("/reports"))]);
  const row = (label, g) => ({ label, value: g.kz_share ?? 0, text: g.kz_share === null ? "нет" : `КЗ ${pct(g.kz_share)} (${g.kz}/${g.world})`, cls: "kz" });
  const geoCard = h("div", { class: "card" }, h("h2", null, "Гео-баланс: Казахстан / мир (30 дней)"),
    bars([{ label: "Ориентир", value: geo.target.kz, text: pct(geo.target.kz), cls: "ok" }, row("Предложено", geo.proposed), row("Опубликовано", geo.published)], { max: 1 }),
    h("p", { class: "small muted" }, geo.note, " Центральная Азия относится к казахстанской стороне."));
  const fcCard = h("div", { class: "card" }, h("h2", null, "Прогноз и факт"), fc.n
    ? [scatter(fc.points, { height: 220 }), h("div", { class: "row gap-s small" }, h("span", { class: "chip" }, "оценено: " + fc.n), h("span", { class: "chip" }, "средняя ошибка (log): " + fc.mae_log), h("span", { class: "chip" }, "смещение: " + fc.bias_log), fc.correlation !== null ? h("span", { class: "chip" }, "корреляция: " + fc.correlation) : null), h("p", { class: "small muted" }, fc.note)]
    : h("div", { class: "empty" }, fc.note));
  const insCard = h("div", { class: "card" }, h("h2", null, "Закономерности"),
    ins.findings.length ? h("div", { class: "col gap-s" }, ins.findings.map((f) => h("div", { class: "row" }, h("span", { class: "badge " + (f.effect > 0 ? "ok" : "warn") }, (f.effect > 0 ? "выше ×" : "ниже ×") + f.ratio), h("span", null, f.text), h("span", { class: "chip" }, "уверенность: " + f.confidence)))) : h("div", { class: "empty" }, ins.note),
    ins.findings.length ? h("p", { class: "small muted" }, ins.note) : null);
  const openReport = async (id) => { const r = await api.get(api.p(`/reports/${id}`)); modal("Отчёт", md(r.markdown), [{ label: "Закрыть" }]); };
  const repCard = h("div", { class: "card" }, h("h2", null, "Отчёты"),
    h("div", { class: "row gap-s" }, [["weekly", "Недельный"], ["monthly", "Месячный"], ["strategy", "Обзор стратегии"]].map(([k, l]) => h("button", { class: "btn sm", onclick: async () => { const r = await api.post(api.p("/reports"), { kind: k }); await openReport(r.id); await sideData(box); } }, "+ " + l))),
    h("div", { class: "col gap-s", style: { marginTop: "10px" } }, reps.reports.length
      ? reps.reports.map((r) => h("button", { class: "list-item", onclick: () => openReport(r.id) }, h("b", null, { weekly: "Недельный", monthly: "Месячный", strategy: "Стратегия" }[r.kind]), " · ", fmtDate(r.period_start), " — ", fmtDate(r.period_end)))
      : h("div", { class: "muted small" }, "Отчётов пока нет: воркер формирует недельный (пн) и месячный (1-е число) автоматически.")));
  box.append(h("div", { class: "grid g2" }, geoCard, fcCard, insCard, repCard, importCard()));
}

function importCard() {
  const ta = h("textarea", { placeholder: '[{"publication_id": 12, "views": 1500, "likes": 40, "shares": 6}]', style: { minHeight: "90px" }, "aria-label": "JSON со статистикой" });
  const src = h("select", null, [["import", "Импорт"], ["manual", "Ручной ввод"]].map(([v, l]) => h("option", { value: v }, l)));
  return h("div", { class: "card" }, h("h2", null, "Ручной ввод / импорт статистики"), h("p", { class: "small muted" }, "У Telegram Bot API нет статистики постов канала — внесите просмотры из аналитики канала. Поля: publication_id (или content_id + platform), views, reach, likes, comments, shares, saves, clicks, follows."), ta,
    h("div", { class: "row", style: { marginTop: "8px" } }, src, h("button", { class: "btn primary", onclick: async () => { let rows; try { rows = JSON.parse(ta.value); } catch { toast("Некорректный JSON", "err"); return; } const r = await api.post(api.p("/analytics/import"), { rows, source: src.value }); toast(`Импортировано: ${r.imported}, ошибок: ${r.errors.length}` + (r.errors[0] ? ` (${r.errors[0].error})` : ""), r.errors.length ? "" : "ok"); } }, "Загрузить")));
}
