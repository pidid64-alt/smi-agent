import { api, h, clear, toast, modal, verifBadge, geoChip, fmtDate, t, state } from "../lib.js";

const EMPH = [["", "— без акцента —"], ["kazakhstan", "Казахстан"], ["numbers", "Цифры и данные"], ["practical", "Практическая польза"], ["world", "Мировой контекст"]];
const EXAMPLES = ["Беру №1", "№2, но сделай акцент на Казахстан", "№3 неинтересна — слишком много политики", "Замени №4", "Раскрой №5 подробнее"];

export async function render(view) {
  const data = await api.get(api.p("/proposals"));
  const log = h("div", { class: "col gap-s" });
  const input = h("input", { placeholder: "Например: «Беру №2, но на казахском и покороче»", style: { flex: "1", minWidth: "260px" }, "aria-label": "Команда" });
  const send = async (text) => {
    if (!text.trim()) return;
    input.disabled = true;
    try {
      const r = await api.post(api.p("/commands"), { text });
      if (r.hint) log.prepend(h("div", { class: "notice warn" }, r.hint));
      r.results.forEach((x) => log.prepend(h("div", { class: "notice " + (x.ok ? "ok" : "bad") }, h("b", null, `№${x.slot} · ${x.action}: `), x.message)));
      input.value = ""; await refresh();
    } finally { input.disabled = false; input.focus(); }
  };
  const list = h("div", { class: "grid g2" });
  const refresh = async () => { const d = await api.get(api.p("/proposals")); fill(list, d.proposals, refresh); };
  view.append(h("div", { class: "row between" }, h("h1", null, "5 предложений"), h("button", { class: "btn primary", onclick: async () => { await api.post(api.p("/funnel/run")); toast("Воронка запущена", "ok"); await refresh(); } }, "Обновить подборку")),
    h("div", { class: "card" }, h("div", { class: "row" }, input, h("button", { class: "btn primary", onclick: () => send(input.value) }, "Отправить")),
      h("div", { class: "row gap-s", style: { marginTop: "8px" } }, h("span", { class: "small muted" }, "Примеры:"), EXAMPLES.map((e) => h("button", { class: "chip", style: { cursor: "pointer" }, onclick: () => { input.value = e; input.focus(); } }, e))),
      h("div", { style: { marginTop: "10px" } }, log)),
    h("div", { style: { height: "14px" } }), list);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") send(input.value); });
  fill(list, data.proposals, refresh);
}

function fill(list, proposals, refresh) {
  clear(list);
  if (!proposals.length) { list.append(h("div", { class: "empty", style: { gridColumn: "1/-1" } }, "Пока нет предложений: достойных тем не нашлось или воронка не запускалась. Слабые темы система не добавляет ради заполнения списка.")); return; }
  proposals.forEach((p) => list.append(card(p, refresh)));
}

function card(p, refresh) {
  const c = p.card, done = p.status !== "proposed";
  const act = (label, fn, kind = "") => h("button", { class: "btn sm " + kind, disabled: done, onclick: fn }, label);
  const call = async (path, body) => { try { const r = await api.post(api.p(`/proposals/${p.slot}/${path}`), body || {}); toast(r.message, r.ok ? "ok" : ""); await refresh(); return r; } catch { /* тост показан */ } };
  return h("article", { class: "card prop" + (done ? " done" : ""), "aria-label": `Предложение ${p.slot}` },
    h("div", { class: "row between", style: { alignItems: "flex-start" } }, h("div", { class: "row", style: { flexWrap: "nowrap", alignItems: "flex-start" } }, h("div", { class: "no" }, "№" + p.slot), h("h3", null, c.title)),
      h("div", { class: "col gap-s", style: { alignItems: "flex-end" } }, h("span", { class: "score" }, "Оценка " + c.trend_score.toFixed(0)), done ? h("span", { class: "badge " + (p.status === "rejected" ? "bad" : "info") }, { selected: "выбрано", executed: "опубликовано", rejected: "отклонено", replaced: "заменено", expired: "устарело" }[p.status] || p.status) : null)),
    h("div", { class: "row gap-s" }, verifBadge(c.verification.status, c.verification.label), h("span", { class: "chip" }, c.category_label), geoChip(c.geo), c.sensitive ? h("span", { class: "badge warn", title: "Только с ручным подтверждением" }, "чувствительная тема") : null, c.political ? h("span", { class: "badge warn" }, "политика: нейтральность") : null, c.repeat ? h("span", { class: "badge info" }, c.repeat.kind === "new_stage" ? "новая стадия" : "повтор") : null),
    sec("Почему сейчас", c.why_now),
    h("div", { class: "sec" }, h("b", null, "Что произошло"), h("ul", null, c.what_happened.map((x) => h("li", null, x)))),
    sec("Почему интересно", c.why_interesting),
    h("div", { class: "sec" }, h("b", null, `Источники (${c.n_independent} независимых из ${c.n_articles} публикаций)`), h("div", { class: "col gap-s" }, c.sources.slice(0, 6).map((s) => h("div", { class: "src" }, h("span", { class: "badge " + (s.independent ? "ok" : "") }, s.relation_label), h("a", { href: s.url, target: "_blank", rel: "noopener noreferrer" }, s.name), s.official ? h("span", { class: "badge dark" }, "первичный") : null, h("span", { class: "muted small" }, fmtDate(s.published_at)))))),
    c.verification.warnings?.length ? h("div", { class: "notice warn" }, c.verification.warnings.map((w) => h("div", null, "⚠ " + w))) : null,
    sec("Угол", c.angle), sec("Формат", c.format.label),
    p.expanded ? expanded(p.expanded) : null,
    h("div", { class: "row", style: { marginTop: "4px" } }, act(t("take"), () => takeDialog(p, refresh), "primary"), act(t("skip"), () => reasonDialog(p, call), ""), act(t("replace"), () => call("replace")), act(t("more"), () => call("more")), act(t("angle"), () => call("angle"))),
    done && p.content_pk ? h("a", { href: "#/content?open=" + p.content_pk }, "Открыть материал ›") : null);
}
const sec = (l, v) => h("div", { class: "sec" }, h("b", null, l), h("div", null, v));

function expanded(e) {
  return h("details", { class: "sec", open: true }, h("summary", null, h("b", null, "Подробности")),
    h("div", { class: "col" },
      h("div", null, h("b", { class: "small muted" }, "Возможные углы: "), e.angles.map((a) => h("div", { class: "small" }, "• " + a))),
      h("div", null, h("b", { class: "small muted" }, "Проверки: "), e.verification.checks.map((c) => h("span", { class: "badge " + ({ pass: "ok", warn: "warn", fail: "bad" }[c.status]), style: { marginRight: "4px" }, title: c.detail }, c.label))),
      e.unknowns.length ? h("div", { class: "notice warn" }, "Неизвестно/нужно проверить: " + e.unknowns.join("; ")) : null,
      h("div", { class: "small muted" }, "Факты с подтверждением: ", e.facts.map((f) => h("div", null, `• ${f.text} `, h("span", { class: "chip" }, "×" + f.support)))),
      h("div", { class: "small muted" }, "Хронология: " + e.timeline.map((x) => `${x.independent} нез.`).join(" › "))));
}

function reasonDialog(p, call) {
  const r = h("input", { placeholder: "Причина (необязательно) — учтётся при обучении" });
  modal(`№${p.slot}: неинтересно`, h("label", null, "Причина", r), [{ label: "Отмена" }, { label: "Отклонить", kind: "danger", fn: async () => { await call("reject", { reason: r.value }); } }]);
}

function takeDialog(p, refresh) {
  const emph = h("select", null, EMPH.map(([v, l]) => h("option", { value: v }, l)));
  const lang = h("select", null, [["", "по умолчанию (ru)"], ["ru", "Русский"], ["kk", "Қазақша"], ["en", "English"]].map(([v, l]) => h("option", { value: v }, l)));
  const len = h("select", null, [["", "обычная"], ["shorter", "короче"], ["longer", "подробнее"]].map(([v, l]) => h("option", { value: v }, l)));
  const plats = ["telegram", "instagram", "facebook"].map((x) => [x, h("input", { type: "checkbox", checked: true })]);
  modal(`Беру №${p.slot}`, h("div", { class: "col" }, h("p", { class: "small muted" }, "Система подготовит оригинальные версии для каждой платформы, карточки и проверки. Публикация — только после вашего подтверждения (режимы 1–2)."),
    h("div", { class: "field-row" }, h("label", null, "Акцент", emph), h("label", null, "Язык", lang), h("label", null, "Объём", len)),
    h("div", { class: "row" }, plats.map(([n, cb]) => h("label", { class: "inline" }, cb, n)))),
    [{ label: "Отмена" }, { label: "Подготовить материал", kind: "primary", fn: async (_e, close) => {
      const body = {}; if (emph.value) body.emphasis = emph.value; if (lang.value) body.language = lang.value; if (len.value) body.length = len.value;
      const pl = plats.filter(([, cb]) => cb.checked).map(([n]) => n); if (pl.length && pl.length < 3) body.platforms = pl;
      try { const r = await api.post(api.p(`/proposals/${p.slot}/select`), body); toast(r.message, "ok"); close(); location.hash = "#/content?open=" + r.data.content_pk; } catch { return true; }
    }, close: false }]);
}
