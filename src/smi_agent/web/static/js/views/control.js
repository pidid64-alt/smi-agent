import { api, h, clear, toast, modal, confirmBox, stateBadge, fmtDate, state } from "../lib.js";

const MODES = [
  ["learning", "Режим 1 · ОБУЧЕНИЕ", "Система предлагает 5 тем и учится на ваших действиях. Ничего не создаёт и не публикует без вас."],
  ["co_editor", "Режим 2 · СО-РЕДАКТОР", "Система сама готовит черновики лучшей темы и предлагает время. Публикуете вы — после подтверждения."],
  ["autopilot", "Режим 3 · АВТОПИЛОТ", "Система выбирает, пишет и публикует сама — только в рамках политик, лимитов и пройденных проверок. Политика и чувствительные темы — всегда вручную."],
];
const PL = { telegram: "Telegram", instagram: "Instagram", facebook: "Facebook" };
const AST = { connected: ["подключён", "ok"], pending: ["ожидает", ""], needs_reauth: ["нужна повторная авторизация", "bad"], error: ["ошибка", "bad"], revoked: ["отозван", "dark"] };

export async function render(view, { shell }) {
  const admin = state.role === "admin";
  const [ap, acc] = await Promise.all([api.get(api.p("/autopilot")), api.get(api.p("/accounts"))]);
  const reload = async () => { await shell.refreshHeader(); view.replaceChildren(); await render(view, { shell }); };
  const modeCards = MODES.map(([k, title, text]) => h("div", { class: "card flat", style: ap.mode === k ? { borderColor: "var(--brand)", boxShadow: "0 0 0 2px #1c4ea122" } : {} },
    h("h3", null, title), h("p", { class: "small muted" }, text),
    ap.mode === k ? h("span", { class: "badge ok" }, "включён сейчас") : h("button", { class: "btn sm " + (k === "autopilot" ? "danger" : ""), disabled: !admin && k === "autopilot", onclick: () => setMode(k, reload) }, "Включить")));
  const modeCard = h("div", { class: "card" }, h("h2", null, "Режим работы агента"),
    !ap.llm_enabled ? h("div", { class: "notice warn" }, "LLM не подключена: тексты — честные заготовки для редактора, а автопилот не будет публиковать такие тексты. Подключите LLM (см. docs/LLM.md).") : null,
    h("div", { class: "grid g3", style: { marginTop: "10px" } }, modeCards),
    !admin ? h("p", { class: "small muted" }, "Автопилот включает администратор.") : null);
  const gap = () => h("div", { style: { height: "14px" } });
  view.append(h("h1", null, "Управление автономностью"), modeCard, gap(), killCard(ap, reload, admin), gap(), policyCard(ap, reload, admin), gap(), accountsCard(acc.accounts, reload, admin));
}

function setMode(mode, reload) {
  const go = async () => { try { await api.put(api.p("/autopilot/mode"), { mode }); toast("Режим изменён", "ok"); await reload(); } catch { return true; } };
  if (mode !== "autopilot") { go(); return; }
  const t = h("input", { placeholder: "ВКЛЮЧИТЬ" });
  modal("Включить автопилот?", h("div", { class: "col" }, h("div", { class: "notice warn" }, "Автопилот публикует без вашего подтверждения — только если: режим аккаунта «auto», политика включена, текст создан LLM, все проверки пройдены, тема не политическая и не чувствительная, лимиты не исчерпаны. Кнопка «ОСТАНОВИТЬ АВТОПИЛОТ» всегда доступна."), h("label", null, "Введите ВКЛЮЧИТЬ для подтверждения", t)),
    [{ label: "Отмена" }, { label: "Включить автопилот", kind: "danger", fn: async () => { if (t.value.trim().toUpperCase() !== "ВКЛЮЧИТЬ") { toast("Подтверждение не введено", "err"); return true; } return go(); } }]);
}

function killCard(ap, reload, admin) {
  return h("div", { class: "card" }, h("h2", null, "Аварийный выключатель «ОСТАНОВИТЬ АВТОПИЛОТ»"), h("p", { class: "small muted" }, "Уровни: система · проект · аккаунт · платформа · категория. Снятие — только явным действием администратора; автоматически не снимается."),
    ap.kill_switches.length ? h("div", { class: "table-wrap" }, h("table", null, h("thead", null, h("tr", null, ["Область", "Причина", "Кто / когда", ""].map((x) => h("th", null, x)))), h("tbody", null, ap.kill_switches.map((k) => h("tr", null, h("td", null, h("b", null, k.scope), k.value ? " · " + k.value : ""), h("td", null, k.reason || "—"), h("td", { class: "small" }, k.engaged_by, h("div", { class: "muted" }, fmtDate(k.engaged_at))),
      h("td", null, h("button", { class: "btn sm", disabled: !admin, title: admin ? "" : "Только администратор", onclick: async () => { const note = prompt("Комментарий к снятию остановки"); if (note === null) return; await api.post(api.p(`/killswitch/${k.id}/release`), { note }); toast("Остановка снята", "ok"); reload(); } }, "Снять"))))))) : h("div", { class: "notice ok" }, "Активных остановок нет."));
}

function policyCard(ap, reload, admin) {
  const plat = h("select", null, [["", "Все платформы"], ...Object.entries(PL)].map(([v, l]) => h("option", { value: v }, l)));
  const s = ap.settings;
  return h("div", { class: "card" }, h("h2", null, "Политики автопилота"),
    h("p", { class: "small muted" }, `Лимиты: ${s.max_posts_per_day} пост(а)/день, интервал ≥ ${s.min_gap_minutes} мин, Trend Score ≥ ${s.min_trend_score}, проверка ≥ «${s.min_verification}», текст от LLM: ${s.require_llm_generator ? "обязателен" : "нет"}. Меняются в «Система › Настройки».`),
    ap.policies.length ? h("div", { class: "table-wrap" }, h("table", null, h("thead", null, h("tr", null, ["Платформа", "Категория", "Состояние", ""].map((x) => h("th", null, x)))), h("tbody", null, ap.policies.map((p) => h("tr", null, h("td", null, PL[p.platform] || "все"), h("td", null, p.category || "все"), h("td", null, h("span", { class: "badge " + (p.enabled ? "ok" : "") }, p.enabled ? "включена" : "выключена")), h("td", null, h("button", { class: "btn sm", disabled: !admin, onclick: async () => { await api.put(api.p("/autopilot/policy"), { enabled: !p.enabled, platform: p.platform, category: p.category, constraints: p.constraints }); reload(); } }, p.enabled ? "Выключить" : "Включить"))))))) : h("div", { class: "muted small" }, "Политик нет — автопилот выключен на всех платформах (состояние по умолчанию)."),
    h("div", { class: "row", style: { marginTop: "10px" } }, plat, h("button", { class: "btn", disabled: !admin, onclick: async () => { await api.put(api.p("/autopilot/policy"), { enabled: true, platform: plat.value || null }); toast("Политика включена", "ok"); reload(); } }, "Включить автопилот для платформы")));
}

function accountsCard(accounts, reload, admin) {
  const f = { platform: h("select", null, Object.entries(PL).map(([v, l]) => h("option", { value: v }, l))), ext: h("input", { placeholder: "ID канала (-100…) / Page ID / IG business ID" }), token: h("input", { type: "password", autocomplete: "off", placeholder: "Токен API (не пароль!)" }), name: h("input"), mode: h("select", null, [["manual", "Ручное подтверждение"], ["scheduled", "По расписанию"], ["auto", "Автоматически (автопилот)"]].map(([v, l]) => h("option", { value: v }, l))), sandbox: h("input", { type: "checkbox" }) };
  return h("div", { class: "card" }, h("h2", null, "Аккаунты платформ"), h("p", { class: "small muted" }, "Первый этап: Telegram, Instagram, Facebook (WhatsApp не поддерживается). Публикация — только через официальные API. Пароли внешних платформ не запрашиваются и не хранятся; токены шифруются (AES-256-GCM) и не показываются после сохранения."),
    accounts.length ? h("div", { class: "table-wrap" }, h("table", null, h("thead", null, h("tr", null, ["Платформа", "Аккаунт", "Статус", "Режим", "Действия"].map((x) => h("th", null, x)))), h("tbody", null, accounts.map((a) => h("tr", null, h("td", null, PL[a.platform]), h("td", null, a.display_name, a.sandbox ? h("span", { class: "badge info", style: { marginLeft: "6px" } }, "песочница") : null, h("div", { class: "small muted mono" }, a.handle || a.external_id), a.last_error ? h("div", { class: "small", style: { color: "var(--bad)" } }, a.last_error) : null),
      h("td", null, h("span", { class: "badge " + (AST[a.status]?.[1] || "") }, AST[a.status]?.[0] || a.status), h("div", { class: "small muted" }, "проверен " + (a.last_checked_at ? fmtDate(a.last_checked_at) : "—"))),
      h("td", null, h("select", { disabled: !admin || a.status === "revoked", "aria-label": "Режим", onchange: async (e) => { try { await api.put(api.p(`/accounts/${a.id}/mode`), { mode: e.target.value }); toast("Режим аккаунта изменён", "ok"); } catch { reload(); } } }, [["manual", "Ручное"], ["scheduled", "По расписанию"], ["auto", "Авто"]].map(([v, l]) => h("option", { value: v, selected: a.mode === v }, l)))),
      h("td", null, h("div", { class: "row gap-s" }, h("button", { class: "btn sm", disabled: a.status === "revoked", onclick: async () => { await api.post(api.p(`/accounts/${a.id}/recheck`)); toast("Проверено", "ok"); reload(); } }, "Проверить"),
        h("button", { class: "btn sm", disabled: !admin || a.status === "revoked", onclick: () => reauth(a, reload) }, "Новый токен"), h("button", { class: "btn sm danger", disabled: !admin || a.status === "revoked", onclick: async () => { const reason = prompt("Причина отзыва доступа (запишется в аудит)"); if (reason === null) return; await api.post(api.p(`/accounts/${a.id}/revoke`), { reason }); toast("Доступ отозван, токен уничтожен", "ok"); reload(); } }, "Отозвать")))))))) : h("div", { class: "empty" }, "Аккаунты не подключены. Для знакомства подключите «песочницу»."),
    admin ? h("details", { style: { marginTop: "12px" } }, h("summary", null, h("b", null, "+ Подключить аккаунт")), h("div", { class: "col", style: { marginTop: "10px" } }, h("div", { class: "field-row" }, h("label", null, "Платформа", f.platform), h("label", null, "Идентификатор", f.ext), h("label", null, "Токен доступа", f.token)), h("div", { class: "field-row" }, h("label", null, "Название", f.name), h("label", null, "Режим", f.mode), h("label", { class: "inline", style: { marginTop: "22px" } }, f.sandbox, "Песочница (без реальной отправки)")),
      h("div", { class: "row" }, h("button", { class: "btn primary", onclick: async () => { try { await api.post(api.p("/accounts"), { platform: f.platform.value, token: f.token.value, external_id: f.ext.value, display_name: f.name.value, mode: f.mode.value, sandbox: f.sandbox.checked }); toast("Аккаунт подключён", "ok"); reload(); } catch { /* тост показан */ } } }, "Подключить"), h("button", { class: "btn", onclick: async () => { try { const r = await api.get(api.p("/accounts/oauth/meta/start")); modal("OAuth Meta", h("p", null, h("a", { href: r.url, rel: "noopener" }, "Перейти к авторизации Facebook/Instagram")), [{ label: "Закрыть" }]); } catch { /* не настроено */ } } }, "OAuth Meta")))) : null);
}
function reauth(a, reload) {
  const t = h("input", { type: "password", autocomplete: "off" });
  modal("Новый токен для " + a.display_name, h("label", null, "Токен доступа", t), [{ label: "Отмена" }, { label: "Сохранить", kind: "primary", fn: async () => { try { await api.post(api.p(`/accounts/${a.id}/reauthorize`), { token: t.value }); toast("Токен обновлён", "ok"); reload(); } catch { return true; } } }]);
}
