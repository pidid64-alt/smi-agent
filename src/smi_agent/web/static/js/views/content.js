import { api, h, clear, toast, modal, fmtDate, verifBadge, safeTelegram, state } from "../lib.js";

const genBadge = (g) => (g === "heuristic" ? h("span", { class: "badge warn" }, "заготовка (без LLM)") : String(g).startsWith("llm") ? h("span", { class: "badge info" }, "LLM") : g === "demo" ? h("span", { class: "badge" }, "история (демо)") : h("span", { class: "badge" }, g));
const PLAT = { telegram: "Telegram", instagram: "Instagram", facebook: "Facebook" };
const ST = { pass: "", warn: "!", fail: "×" };

export async function render(view, { query }) {
  const list = await api.get(api.p("/content?limit=60"));
  const box = h("div"); const side = h("div");
  view.append(h("h1", null, "Контент"), h("div", { class: "layout-2" }, h("div", null, side), box));
  list.content.sort((a, b) => (a.generator === "demo") - (b.generator === "demo") || b.id - a.id);
  let current = Number(query.get("open")) || list.content[0]?.id || null;
  if (list.content.length && list.content.every((c) => c.generator === "demo") && !query.get("open")) side.append(h("div", { class: "notice info", style: { marginBottom: "8px" } }, "Пока есть только демо-история. Выберите тему в разделе «5 предложений» — здесь появится ваш материал."));
  const drawList = () => { [...side.querySelectorAll(".list-item,.empty")].forEach((x) => x.remove()); if (!list.content.length) side.append(h("div", { class: "empty" }, "Материалов пока нет. Выберите тему в разделе «5 предложений».")); list.content.forEach((c) => side.append(h("button", { class: "list-item" + (c.id === current ? " active" : ""), onclick: () => open(c.id) },
    h("div", { class: "small muted mono" }, c.content_id), h("div", null, c.title), h("div", { class: "row gap-s", style: { marginTop: "4px" } }, h("span", { class: "chip" }, c.status), genBadge(c.generator), c.origin !== "user" ? h("span", { class: "badge dark" }, c.origin) : null, c.requires_manual ? h("span", { class: "badge warn" }, "только вручную") : null)))); };
  const open = async (id) => { current = id; drawList(); clear(box); box.append(h("div", { class: "muted" }, h("span", { class: "spinner" }), " Загрузка…")); history.replaceState(null, "", "#/content?open=" + id); await editor(box, id, () => open(id)); };
  drawList();
  if (current) await open(current);
}

async function editor(box, id, reload) {
  const d = await api.get(api.p(`/content/${id}`));
  clear(box);
  const notes = [...(d.draft.notes || [])];
  const tabs = h("div", { class: "tabs-s", role: "tablist" }); const panel = h("div");
  let active = d.versions[0]?.platform;
  const show = async (plat) => { active = plat; [...tabs.children].forEach((b) => b.classList.toggle("active", b.dataset.p === plat)); clear(panel); await versionPanel(panel, d, d.versions.find((v) => v.platform === plat), reload); };
  d.versions.forEach((v) => tabs.append(h("button", { role: "tab", dataset: { p: v.platform }, onclick: () => show(v.platform), class: v.platform === active ? "active" : "" }, PLAT[v.platform], " ", badge(v.checks))));
  box.append(h("div", { class: "card" },
    h("div", { class: "row between" }, h("div", null, h("div", { class: "mono small muted" }, d.content_id), h("h2", { style: { margin: 0 } }, d.title)), h("button", { class: "btn primary", onclick: () => publishDialog(d) }, "Создать публикации")),
    h("div", { class: "row gap-s", style: { margin: "8px 0" } }, h("span", { class: "chip" }, d.category), h("span", { class: "chip" }, d.geo), h("span", { class: "chip" }, "язык: " + d.language), d.generator === "heuristic" ? h("span", { class: "badge warn" }, "черновик-заготовка (без LLM)") : h("span", { class: "badge info" }, d.generator), d.supersedes_event_stage ? h("span", { class: "badge info" }, d.supersedes_event_stage) : null),
    d.requires_manual ? h("div", { class: "notice warn" }, "Чувствительная/политическая тема: только с ручным подтверждением. Автопубликация запрещена. Тон — нейтральный, спорные утверждения с атрибуцией.") : null,
    notes.map((n) => h("div", { class: "notice warn", style: { marginTop: "8px" } }, n)),
    d.draft.unknowns?.length ? h("div", { class: "notice info", style: { marginTop: "8px" } }, "Обратите внимание (проверка фактов): " + d.draft.unknowns.join("; ")) : null,
    d.forecast ? h("p", { class: "small muted" }, `Прогноз: ожидаем ~×${d.forecast.expected_ratio} к обычному результату аккаунта (уверенность: ${d.forecast.confidence}; калибровочных публикаций: ${d.forecast.calibration_samples}). ${d.forecast.note}`) : null),
    h("div", { style: { height: "12px" } }), h("div", { class: "card" }, tabs, panel));
  if (active) await show(active);
}
const badge = (c) => !c ? null : h("span", { class: "badge " + (!c.passed ? "bad" : c.results.some((r) => r.status === "warn") ? "warn" : "ok") }, !c.passed ? "не пройдено" : c.results.some((r) => r.status === "warn") ? "замечания" : "готово");

async function versionPanel(panel, d, v, reload) {
  const body = h("textarea", { "aria-label": "Текст", spellcheck: "true" }); body.value = v.body;
  const title = h("input", { "aria-label": "Заголовок" }); title.value = v.title;
  const tags = h("input", { "aria-label": "Хэштеги" }); tags.value = (v.hashtags || []).join(" ");
  const counter = h("span", { class: "counter" });
  const upd = () => { const n = v.platform === "telegram" ? body.value.replace(/<[^>]+>/g, "").length : body.value.length; counter.textContent = `${n} знаков (лимит платформы ${v.limit}, итог с маркировкой ИИ: ${v.length})`; counter.classList.toggle("over", v.length > v.limit); };
  body.addEventListener("input", upd); upd();
  const save = async () => {
    const hashtags = tags.value.split(/\s+/).filter(Boolean);
    const r = await api.put(api.p(`/content/${d.id}/versions/${v.platform}`), { body: body.value, title: title.value, hashtags });
    toast(`Сохранена версия ${r.version}. Проверки выполнены заново; прежние подтверждения отменены.`, "ok"); await reload();
  };
  const pv = await api.get(api.p(`/content/${d.id}/preview/${v.platform}?reviewed=false`));
  const pvr = await api.get(api.p(`/content/${d.id}/preview/${v.platform}?reviewed=true`));
  const mediaBlock = v.media?.length ? h("div", null, h("b", { class: "small muted" }, "МЕДИА"), h("div", { class: "thumbs" }, v.media.map((m) => h("img", { src: api.p(`/assets/${m.asset_id}`), alt: m.role, loading: "lazy" })))) : null;
  const reels = v.extras?.reels;
  const slidesBlock = v.extras?.slides?.length ? h("details", null, h("summary", null, "Слайды карусели (" + v.extras.slides.length + ") и сценарий Reels"),
    h("ol", null, v.extras.slides.map((s) => h("li", null, s))),
    reels?.hook ? h("div", { class: "small" }, h("b", null, "Reels: "), reels.hook, " › ", (reels.beats || []).join(" › "), " › ", reels.cta, h("div", { class: "muted" }, "Для публикации Reels нужен видеофайл: генерация видео не выполняется.")) : null) : null;
  const previewBlock = h("div", null, h("b", { class: "small muted" }, "ИТОГОВЫЙ ТЕКСТ ПУБЛИКАЦИИ"), h("div", { class: "preview" }, v.platform === "telegram" ? safeTelegram(pv.text) : pv.text),
    h("p", { class: "small muted" }, "После вашего подтверждения маркировка будет дополнена: «" + (pvr.disclosure || "—") + "»"));
  const checksBlock = h("div", null, h("b", { class: "small muted" }, "ПРОВЕРКИ ПЕРЕД ПУБЛИКАЦИЕЙ"),
    v.checks ? h("div", { class: "checks", style: { marginTop: "6px" } }, v.checks.results.map((r) => h("div", { class: "check " + r.status }, h("span", { class: "ic" }, ST[r.status]),
      h("div", null, h("b", null, r.label), ": ", r.detail, r.blocks_autopilot && r.status !== "fail" ? h("span", { class: "chip", style: { marginLeft: "6px" } }, "блокирует автопубликацию") : null)))) : h("div", { class: "muted" }, "Не выполнялись"),
    h("button", { class: "btn sm", style: { marginTop: "8px" }, onclick: async () => { await api.post(api.p(`/content/${d.id}/checks`)); toast("Проверки выполнены", "ok"); await reload(); } }, "Перезапустить проверки"));
  panel.append(h("div", { class: "col" },
    h("div", { class: "field-row" }, h("label", null, "Заголовок (внутренний)", title), h("label", null, "Хэштеги", tags)),
    h("label", null, v.platform === "telegram" ? "Текст (HTML Telegram: <b>, <i>, <a href>)" : "Текст", body), counter,
    h("div", { class: "row" }, h("button", { class: "btn primary", onclick: save }, "Сохранить как новую версию"), h("span", { class: "small muted" }, `версия ${v.version} · формат ${v.format}`)),
    mediaBlock, slidesBlock, previewBlock, checksBlock));
}

function publishDialog(d) {
  const plats = d.versions.map((v) => [v.platform, h("input", { type: "checkbox", checked: true })]);
  const mode = h("select", null, [["", "по режиму аккаунта"], ["now", "сразу после подтверждения"], ["optimal", "оптимальное время"], ["at", "указать дату и время"]].map(([a, b]) => h("option", { value: a }, b)));
  const at = h("input", { type: "datetime-local", class: "hidden" });
  mode.addEventListener("change", () => at.classList.toggle("hidden", mode.value !== "at"));
  modal("Создать публикации", h("div", { class: "col" }, h("p", { class: "small muted" }, "Публикации появятся в разделе «Публикации» в состоянии «Ожидает подтверждения» (или «Требует проверки», если проверки не пройдены). Опубликовать можно только официальным API."),
    h("div", { class: "row" }, plats.map(([n, cb]) => h("label", { class: "inline" }, cb, PLAT[n]))), h("label", null, "Время", mode), at),
    [{ label: "Отмена" }, { label: "Создать", kind: "primary", fn: async () => {
      const body = { platforms: plats.filter(([, cb]) => cb.checked).map(([n]) => n) };
      if (mode.value === "at") { if (!at.value) { toast("Укажите дату и время", "err"); return true; } body.schedule = { mode: "at", at: new Date(at.value).toISOString() }; } else if (mode.value) body.schedule = { mode: mode.value };
      try { await api.post(api.p(`/content/${d.id}/publications`), body); toast("Публикации созданы", "ok"); location.hash = "#/publications"; } catch { return true; }
    } }]);
}
