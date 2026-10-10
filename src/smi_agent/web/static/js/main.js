import { api, state, h, $, clear, t, toast, modal, confirmBox } from "./lib.js";

const VIEWS = [
  ["agenda", "agenda", () => import("./views/agenda.js")],
  ["proposals", "proposals", () => import("./views/proposals.js")],
  ["content", "content", () => import("./views/content.js")],
  ["publications", "publications", () => import("./views/publications.js")],
  ["analytics", "analytics", () => import("./views/analytics.js")],
  ["profile", "profile", () => import("./views/profile.js")],
  ["control", "control", () => import("./views/control.js")],
  ["system", "system", () => import("./views/system.js")],
];
const MODE_LABEL = { learning: "Режим 1 · Обучение", co_editor: "Режим 2 · Со-редактор", autopilot: "Режим 3 · Автопилот" };
const app = document.getElementById("app");
let pollTimer = null;
let routeToken = 0;
let listening = false;
export const shell = { autopilot: null, refreshHeader: async () => {} };

state.onUnauth = () => { clearInterval(pollTimer); showLogin(); };

async function boot() {
  try {
    const me = await api.get("/api/auth/me", { silent: true });
    await afterLogin(me);
  } catch { showLogin(); }
}

function showLogin(message = "") {
  clear(app);
  const err = h("div", { class: "notice bad hidden", role: "alert" });
  const u = h("input", { name: "username", autocomplete: "username", required: true, "aria-label": "Логин" });
  const p = h("input", { name: "password", type: "password", autocomplete: "current-password", required: true, "aria-label": "Пароль" });
  const c = h("input", { name: "totp", inputmode: "numeric", autocomplete: "one-time-code", maxlength: "6", placeholder: "000000", "aria-label": "Код MFA" });
  const form = h("form", { class: "col", onsubmit: async (e) => {
    e.preventDefault(); err.classList.add("hidden");
    try {
      const r = await api.post("/api/auth/login", { username: u.value, password: p.value, totp: c.value }, { silent: true });
      state.csrf = r.csrf;
      const me = await api.get("/api/auth/me");
      await afterLogin(me);
    } catch (ex) { err.textContent = ex.message || "Ошибка входа"; err.classList.remove("hidden"); }
  } },
    h("label", null, "Логин", u), h("label", null, "Пароль", p), h("label", null, "Код MFA (если включён)", c), err,
    h("button", { class: "btn primary", type: "submit" }, "Войти"),
    message ? h("div", { class: "notice info" }, message) : null);
  app.append(h("div", { class: "login" }, h("div", { class: "card" }, h("h1", null, "Smi-Agent"), h("p", { class: "muted" }, "AI-главный редактор: мониторинг, отбор тем, оригинальные материалы, публикация и аналитика."), form)));
  u.focus();
}

async function afterLogin(me) {
  state.csrf = me.csrf || state.csrf; state.user = me.user; state.demo = !!me.demo; state.env = me.env || "dev";
  const projects = me.user.projects;
  if (!projects.length) { clear(app); app.append(h("div", { class: "login" }, h("div", { class: "card" }, h("h2", null, "Нет доступа к проектам"), h("p", null, "Обратитесь к администратору."), h("button", { class: "btn", onclick: logout }, "Выйти")))); return; }
  state.projectId = Number(localStorage.getItem("project")) && projects.some((p) => p.project_id === Number(localStorage.getItem("project"))) ? Number(localStorage.getItem("project")) : projects[0].project_id;
  state.role = projects.find((p) => p.project_id === state.projectId).role;
  renderShell(projects);
  if (me.mfa_setup_required) { mfaSetup(true); return; }
  if (!listening) { window.addEventListener("hashchange", route); listening = true; }
  route();
  await shell.refreshHeader();
  clearInterval(pollTimer);
  pollTimer = setInterval(() => shell.refreshHeader(), 30000);
}

async function logout() {
  try { await api.post("/api/auth/logout"); } catch { /* сессия уже недействительна */ }
  state.csrf = ""; location.hash = ""; showLogin();
}

function renderShell(projects) {
  clear(app);
  const killBtn = h("button", { class: "kill", id: "kill", onclick: killDialog }, t("stop"));
  const modeChip = h("button", { class: "modechip", id: "mode", title: "Открыть «Управление»", onclick: () => { location.hash = "#/control"; } }, "…");
  const psel = h("select", { "aria-label": "Проект", onchange: (e) => { localStorage.setItem("project", e.target.value); location.reload(); } }, projects.map((p) => h("option", { value: p.project_id, selected: p.project_id === state.projectId }, p.name)));
  const lang = h("select", { "aria-label": "Язык интерфейса", onchange: (e) => { state.lang = e.target.value; localStorage.setItem("lang", state.lang); location.reload(); } }, [["ru", "RU"], ["kk", "ҚАЗ"], ["en", "EN"]].map(([v, l]) => h("option", { value: v, selected: v === state.lang }, l)));
  const nav = h("nav", { class: "tabs", "aria-label": "Разделы" }, VIEWS.map(([key]) => h("a", { href: "#/" + key, dataset: { key } }, t(key), key === "system" ? h("span", { class: "badge-n hidden", id: "nbadge" }, "0") : null)));
  app.append(
    state.demo ? h("div", { class: "banner demo", role: "note" }, "ДЕМО-РЕЖИМ: все новости вымышлены, публикации уходят в песочницу (никуда не отправляются), метрики и история — искусственные. Тексты — заранее подготовленные.") : null,
    state.env === "production" ? null : h("div", { class: "banner warn hidden", id: "envwarn" }),
    h("header", { class: "topbar" }, h("div", { class: "brand" }, "Smi-Agent", h("small", null, "AI-главный редактор")), psel, modeChip, h("span", { class: "grow" }), killBtn, lang,
      h("span", { class: "small", style: { opacity: ".9" } }, state.user.display_name || state.user.username), h("button", { class: "btn sm", onclick: logout }, t("logout"))),
    nav, h("main", { id: "view", tabindex: "-1" }));
  shell.refreshHeader = async () => {
    try {
      const a = await api.get(api.p("/autopilot"), { silent: true });
      shell.autopilot = a;
      $("#mode").textContent = MODE_LABEL[a.mode] || a.mode;
      const k = $("#kill"); const engaged = a.kill_switches.length > 0;
      k.classList.toggle("engaged", engaged); k.textContent = engaged ? `${t("stopped")} (${a.kill_switches.length})` : t("stop");
      const n = await api.get(api.p("/notifications?unread=true&limit=50"), { silent: true });
      const b = $("#nbadge"); b.textContent = n.notifications.length; b.classList.toggle("hidden", !n.notifications.length);
    } catch { /* фоновое обновление */ }
  };
}

function killDialog() {
  const scope = h("select", null, [["project", "Весь проект"], ["platform", "Платформа"], ["category", "Категория"], ["account", "Аккаунт (ID)"], ...(state.role === "admin" ? [["system", "Вся система"]] : [])].map(([v, l]) => h("option", { value: v }, l)));
  const val = h("input", { placeholder: "telegram / economy / ID аккаунта" });
  const reason = h("input", { placeholder: "Причина (необязательно)" });
  modal("Остановить автопилот", h("div", { class: "col" }, h("p", { class: "small muted" }, "Автопубликации в выбранной области немедленно прекращаются; запланированные посты автопилота вернутся в «Ожидает подтверждения». Снять остановку может только администратор."), h("label", null, "Область", scope), h("label", null, "Значение (для платформы/категории/аккаунта)", val), h("label", null, "Причина", reason)),
    [{ label: "Отмена" }, { label: "ОСТАНОВИТЬ", kind: "danger", fn: async () => {
      try { await api.post(api.p("/killswitch"), { scope_type: scope.value, scope_value: val.value.trim(), reason: reason.value }); toast("Автопилот остановлен", "ok"); await shell.refreshHeader(); route(); } catch { return true; }
    } }]);
}

async function mfaSetup(forced) {
  let info;
  try { info = await api.post("/api/auth/mfa/begin"); } catch { return; }
  const code = h("input", { inputmode: "numeric", maxlength: "6", placeholder: "000000", "aria-label": "Код из приложения" });
  modal("Включение двухфакторной аутентификации", h("div", { class: "col" },
    forced ? h("div", { class: "notice warn" }, "Для администратора MFA обязательна. Пока она не включена, доступны только настройки безопасности.") : null,
    h("p", null, "Добавьте ключ в приложение-аутентификатор (Google Authenticator, Aegis, 1Password…) и введите код."),
    h("label", null, "Секрет (показывается один раз)", h("input", { readonly: true, value: info.secret, class: "mono" })),
    h("label", null, "Ссылка otpauth://", h("input", { readonly: true, value: info.uri, class: "mono" })), h("label", null, "Код подтверждения", code)),
    [{ label: "Позже", close: true }, { label: "Подтвердить", kind: "primary", fn: async () => {
      try { await api.post("/api/auth/mfa/confirm", { code: code.value }); toast("MFA включена", "ok"); setTimeout(() => location.reload(), 600); } catch { return true; }
    } }]);
}

async function route() {
  const token = ++routeToken; // устаревшие отрисовки (быстрые переходы) отбрасываются, чтобы не дублировать содержимое
  const key = (location.hash.replace(/^#\//, "").split("?")[0]) || "agenda";
  const entry = VIEWS.find((v) => v[0] === key) || VIEWS[0];
  document.querySelectorAll("nav.tabs a").forEach((a) => a.classList.toggle("active", a.dataset.key === entry[0]));
  const view = $("#view");
  clear(view);
  view.append(h("div", { class: "muted" }, h("span", { class: "spinner" }), " Загрузка…"));
  const box = h("div");
  try {
    const mod = await entry[2]();
    await mod.render(box, { shell, route, query: new URLSearchParams(location.hash.split("?")[1] || "") });
    if (token !== routeToken) return;
    clear(view); view.append(box);
  } catch (e) {
    if (token !== routeToken) return;
    clear(view);
    view.append(h("div", { class: "notice bad", role: "alert" }, "Не удалось загрузить раздел: " + (e.message || e)));
  }
  view.focus({ preventScroll: true });
}

boot();
