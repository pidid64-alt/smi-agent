import { api, h, toast, bars, pct, state } from "../lib.js";

export async function render(view) {
  const p = await api.get(api.p("/profile"));
  const prefBars = (rows, label) => rows.length ? bars(rows.slice(0, 8).map((r) => ({ label: r.label, value: r.score, text: `${(r.score * 100).toFixed(0)}% · n=${r.n}`, cls: r.score >= 0.5 ? "ok" : "bad", title: `+${r.positive} / −${r.negative}` })), { max: 1 }) : h("div", { class: "muted small" }, "Данных пока нет");
  const ca = p.choice_analysis;
  view.append(h("h1", null, "Редакционный профиль"),
    h("div", { class: "card" }, h("h2", null, "Что система поняла о ваших предпочтениях"), h("ul", null, p.statements.map((s) => h("li", null, s))), h("p", { class: "small muted" }, `Сигналов (действий): ${p.signals} · уверенность профиля: ${pct(p.confidence)}. Каждое действие — выбор, отклонение, замена, правка текста, результат публикации — является сигналом обучения; давние сигналы затухают.`)),
    h("div", { style: { height: "14px" } }),
    h("div", { class: "grid g2" },
      h("div", { class: "card" }, h("h2", null, "Темы"), prefBars(p.preferences.categories)),
      h("div", { class: "card" }, h("h2", null, "География"), prefBars(p.preferences.geo), h("h2", { style: { marginTop: "14px" } }, "Подтемы"), prefBars(p.preferences.subtopics)),
      h("div", { class: "card" }, h("h2", null, "Формат, язык, акценты"), prefBars([...p.preferences.formats, ...p.preferences.language, ...p.preferences.emphasis]), h("h3", { style: { marginTop: "14px" } }, "Стиль (по вашим правкам)"), h("div", { class: "row gap-s" }, h("span", { class: "chip" }, "длина ×" + p.style.length_multiplier), p.style.fewer_hashtags ? h("span", { class: "chip" }, "меньше хэштегов") : null, p.style.fewer_emoji ? h("span", { class: "chip" }, "меньше эмодзи") : null, p.style.calmer_tone ? h("span", { class: "chip" }, "спокойнее тон") : null), h("p", { class: "small muted" }, `Правок текста учтено: ${p.edits}.`)),
      h("div", { class: "card" }, h("h2", null, "Анализ выбора пользователя"), h("p", { class: "small" }, `Показано предложений: ${ca.proposals_shown}; выбрано: ${ca.selected}` + (ca.picks_first_share !== null ? `; №1 выбирается в ${pct(ca.picks_first_share)} случаев` : "") + (ca.geo.selected_kz_share !== null ? `; доля КЗ среди выбранных ${pct(ca.geo.selected_kz_share)}` : "") + "."),
        ca.categories.length ? h("div", { class: "table-wrap" }, h("table", null, h("thead", null, h("tr", null, ["Тема", "Показано", "Выбрано", "Отклонено", "Доля выбора"].map((x) => h("th", null, x)))), h("tbody", null, ca.categories.map((c) => h("tr", null, h("td", null, c.label), h("td", null, c.shown), h("td", null, c.selected), h("td", null, c.rejected), h("td", null, pct(c.selection_rate))))))) : null)),
    h("div", { style: { height: "14px" } }), settingsCard(p.explicit));
}

function settingsCard(e) {
  const f = { tone: h("select", null, [["neutral", "Нейтральный"], ["friendly", "Дружелюбный"], ["formal", "Формальный"]].map(([v, l]) => h("option", { value: v, selected: e.tone === v }, l))),
    brand: h("textarea", { style: { minHeight: "70px" } }), prio: h("input"), blocked: h("input"), forbidden: h("input"), tags: h("input"), sig: h("input") };
  f.brand.value = e.brand_voice; f.prio.value = e.priority_categories.join(", "); f.blocked.value = e.blocked_keywords.join(", "); f.forbidden.value = e.forbidden_words.join(", "); f.tags.value = e.default_hashtags.join(" "); f.sig.value = e.signature;
  const list = (s) => s.split(/[,;\n]/).map((x) => x.trim()).filter(Boolean);
  return h("div", { class: "card" }, h("h2", null, "Явные настройки профиля"),
    h("div", { class: "notice info" }, "Политические и чувствительные темы публикуются только после ручного подтверждения — это правило нельзя отключить. Тон политических материалов — нейтральный, без агитации и прогнозов выборов."),
    h("div", { class: "col", style: { marginTop: "10px" } }, h("div", { class: "field-row" }, h("label", null, "Тон", f.tone), h("label", null, "Приоритетные категории (id через запятую: economy, ai, auto…)", f.prio), h("label", null, "Заблокированные темы (слова)", f.blocked)),
      h("label", null, "Голос бренда", f.brand), h("div", { class: "field-row" }, h("label", null, "Запрещённые слова в текстах", f.forbidden), h("label", null, "Хэштеги по умолчанию", f.tags), h("label", null, "Подпись", f.sig)),
      h("div", null, h("button", { class: "btn primary", onclick: async () => { await api.put(api.p("/profile/settings"), { tone: f.tone.value, brand_voice: f.brand.value, priority_categories: list(f.prio.value), blocked_keywords: list(f.blocked.value), forbidden_words: list(f.forbidden.value), default_hashtags: f.tags.value.split(/\s+/).filter(Boolean), signature: f.sig.value }); toast("Профиль сохранён", "ok"); } }, "Сохранить"))));
}
