// Мини-библиотека интерфейса: DOM-построитель, API-клиент (CSRF), i18n, графики SVG. Без внешних зависимостей, совместимо со строгим CSP.
export const state = { csrf: "", user: null, projectId: null, demo: false, env: "dev", lang: localStorage.getItem("lang") || "ru", onUnauth: null };

const DICT = {
  ru: { agenda: "Повестка", proposals: "5 предложений", content: "Контент", publications: "Публикации", analytics: "Аналитика", profile: "Редакционный профиль", control: "Управление", system: "Система",
        logout: "Выйти", stop: "ОСТАНОВИТЬ АВТОПИЛОТ", stopped: "Автопилот остановлен", take: "Беру", skip: "Неинтересно", replace: "Заменить", more: "Подробнее", angle: "Другой угол" },
  kk: { agenda: "Күн тәртібі", proposals: "5 ұсыныс", content: "Контент", publications: "Жарияланымдар", analytics: "Талдау", profile: "Редакциялық профиль", control: "Басқару", system: "Жүйе",
        logout: "Шығу", stop: "АВТОПИЛОТТЫ ТОҚТАТУ", stopped: "Автопилот тоқтатылды", take: "Аламын", skip: "Қызықсыз", replace: "Ауыстыру", more: "Толығырақ", angle: "Басқа қырынан" },
  en: { agenda: "Agenda", proposals: "5 proposals", content: "Content", publications: "Publications", analytics: "Analytics", profile: "Editorial profile", control: "Control", system: "System",
        logout: "Log out", stop: "STOP AUTOPILOT", stopped: "Autopilot stopped", take: "Take", skip: "Not interesting", replace: "Replace", more: "More", angle: "Other angle" },
};
export const t = (k) => (DICT[state.lang] && DICT[state.lang][k]) || DICT.ru[k] || k;

export function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k === "value") el.value = v;
    else if (k === "checked" || k === "disabled" || k === "selected") el[k] = !!v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v); // CSSOM разрешён строгим CSP
    else el.setAttribute(k, v === true ? "" : v);
  }
  const add = (c) => {
    if (c === null || c === undefined || c === false) return;
    if (Array.isArray(c)) c.forEach(add);
    else el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  };
  kids.forEach(add);
  return el;
}
export const $ = (s, r = document) => r.querySelector(s);
export const clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); return el; };

export function toast(msg, kind = "") {
  const box = document.getElementById("toasts");
  const el = h("div", { class: "toast " + kind, role: kind === "err" ? "alert" : "status" }, msg);
  box.append(el);
  setTimeout(() => el.remove(), kind === "err" ? 8000 : 4200);
}

export class ApiError extends Error { constructor(status, body) { super(body?.error?.message || "Ошибка"); this.status = status; this.code = body?.error?.code; this.details = body?.error?.details; } }

async function request(method, url, body, { silent = false } = {}) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && state.csrf) headers["X-CSRF-Token"] = state.csrf;
  let res;
  try { res = await fetch(url, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: "same-origin" }); }
  catch (e) { if (!silent) toast("Нет связи с сервером", "err"); throw e; }
  let data = null;
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("json")) data = await res.json().catch(() => null);
  else if (ct.startsWith("text/")) data = await res.text();
  if (!res.ok) {
    const err = new ApiError(res.status, data);
    if (res.status === 401 && state.onUnauth && !url.includes("/auth/login")) state.onUnauth();
    else if (!silent) toast(err.message, "err");
    throw err;
  }
  return data;
}
export const api = {
  get: (u, o) => request("GET", u, undefined, o), post: (u, b = {}, o) => request("POST", u, b, o),
  put: (u, b = {}, o) => request("PUT", u, b, o), del: (u, o) => request("DELETE", u, undefined, o),
  p: (path) => `/api/p/${state.projectId}${path}`,
};

// ---------- форматирование
const dtf = new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
export const fmtDate = (iso) => (iso ? dtf.format(new Date(iso)) : "—");
export function fmtAgo(iso) {
  if (!iso) return "—";
  const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (m < 1) return "только что";
  if (m < 60) return `${m} мин назад`;
  if (m < 48 * 60) return `${Math.round(m / 60)} ч назад`;
  return `${Math.round(m / 1440)} дн назад`;
}
export const fmtNum = (n) => (n === null || n === undefined ? "—" : new Intl.NumberFormat("ru-RU").format(n));
export const pct = (x, d = 0) => (x === null || x === undefined ? "—" : (x * 100).toFixed(d) + "%");

// ---------- бейджи
export const PHASES = { emerging: ["Зарождается", "info"], rising: ["Растёт", "ok"], peak: ["Пик", "warn"], fading: ["Затухает", ""], stale: ["Устарело", "bad"] };
export const phaseBadge = (p) => { const [l, c] = PHASES[p] || [p, ""]; return h("span", { class: "badge " + c }, l); };
export const VERIF = { confirmed: "подтверждено", multi_confirmed: "подтверждено несколькими источниками", needs_check: "требуется дополнительная проверка", rejected: "не подтверждено" };
export const verifBadge = (status, label) => h("span", { class: "badge " + ({ confirmed: "ok", multi_confirmed: "ok", needs_check: "warn", rejected: "bad" }[status] || "") }, label || VERIF[status] || status || "не проверено");
export const stateBadge = (state, label) => h("span", { class: "badge s-" + state }, label || state);
export const GEO = { kz: "Казахстан", ca: "Центральная Азия", world: "Мир" };
export const geoChip = (g) => h("span", { class: "chip" }, GEO[g] || g);

// ---------- модальное окно
export function modal(title, body, actions = []) {
  const root = document.getElementById("modal-root");
  const close = () => { back.remove(); document.removeEventListener("keydown", esc); };
  const esc = (e) => { if (e.key === "Escape") close(); };
  const back = h("div", { class: "backdrop", onclick: (e) => { if (e.target === back) close(); } },
    h("div", { class: "modal", role: "dialog", "aria-modal": "true", "aria-label": title },
      h("h2", null, title), body, h("div", { class: "row", style: { marginTop: "14px", justifyContent: "flex-end" } }, actions.map((a) => h("button", { class: "btn " + (a.kind || ""), onclick: async (e) => { if (a.fn) { const keep = await a.fn(e, close); if (keep === true) return; } if (a.close !== false) close(); } }, a.label)))));
  root.append(back);
  document.addEventListener("keydown", esc);
  const f = back.querySelector("input,select,textarea,button");
  if (f) f.focus();
  return close;
}
export function confirmBox(title, text, okLabel = "Подтвердить", kind = "primary") {
  return new Promise((res) => modal(title, h("p", null, text), [{ label: "Отмена", fn: () => res(false) }, { label: okLabel, kind, fn: () => res(true) }]));
}
export function drawer(title, body) {
  const root = document.getElementById("modal-root");
  const close = () => { wrap.remove(); };
  const wrap = h("div", null, h("div", { class: "backdrop", style: { alignItems: "stretch", justifyContent: "flex-end", padding: "0", background: "rgba(16,24,40,.35)" }, onclick: (e) => { if (e.target.classList.contains("backdrop")) close(); } },
    h("aside", { class: "drawer", style: { position: "relative" }, role: "dialog", "aria-label": title }, h("div", { class: "row between" }, h("h2", null, title), h("button", { class: "btn sm", onclick: close, "aria-label": "Закрыть" }, "×")), body)));
  root.append(wrap);
  return close;
}

// ---------- безопасный разбор Telegram-HTML и markdown (без innerHTML)
export function safeTelegram(src) {
  const doc = new DOMParser().parseFromString("<div>" + src + "</div>", "text/html");
  const out = document.createDocumentFragment();
  const walk = (node, parent) => {
    for (const n of node.childNodes) {
      if (n.nodeType === 3) parent.append(document.createTextNode(n.textContent));
      else if (n.nodeType === 1) {
        const tag = n.tagName.toLowerCase();
        if (["b", "strong", "i", "em", "u", "s", "code"].includes(tag)) { const el = document.createElement(tag); walk(n, el); parent.append(el); }
        else if (tag === "a") { const el = h("span", { class: "mono" }); walk(n, el); el.append(document.createTextNode(" (ссылка)")); parent.append(el); }
        else parent.append(document.createTextNode(n.textContent));
      }
    }
  };
  walk(doc.body.firstChild, out);
  return out;
}
export function md(text) {
  const root = h("div", { class: "md" });
  let ul = null;
  for (const raw of (text || "").split("\n")) {
    const line = raw.trimEnd();
    if (/^#{1,3} /.test(line)) { ul = null; const lvl = line.match(/^#+/)[0].length; root.append(h("h" + Math.min(lvl + 1, 4), null, line.replace(/^#+ /, ""))); }
    else if (/^[-*] /.test(line)) { if (!ul) { ul = h("ul"); root.append(ul); } inline(ul.appendChild(h("li")), line.slice(2)); }
    else if (line === "") ul = null;
    else { ul = null; inline(root.appendChild(h("p")), line); }
  }
  return root;
}
function inline(el, s) { s.split(/(\*\*[^*]+\*\*)/).forEach((p) => el.append(p.startsWith("**") ? h("b", null, p.slice(2, -2)) : p)); }

// ---------- графики SVG
const NS = "http://www.w3.org/2000/svg";
const svg = (tag, attrs = {}, ...kids) => { const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); kids.forEach((c) => e.append(c)); return e; };
export const PALETTE = ["#1c4ea1", "#0aa6a6", "#f5b301", "#c2410c", "#7c3aed", "#15803d", "#be185d", "#475569"];
function frame(labels, maxv, W, H, pad) {
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img" });
  const steps = 4;
  for (let i = 0; i <= steps; i++) {
    const y = pad.t + ((H - pad.t - pad.b) * i) / steps;
    s.append(svg("line", { x1: pad.l, x2: W - pad.r, y1: y, y2: y, class: "grid" }));
    const v = maxv * (1 - i / steps);
    const tx = svg("text", { x: pad.l - 6, y: y + 4, "text-anchor": "end" }); tx.textContent = v >= 100 ? Math.round(v) : Math.round(v * 10) / 10; s.append(tx);
  }
  const every = Math.ceil(labels.length / 7);
  labels.forEach((l, i) => { if (i % every) return; const x = pad.l + ((W - pad.l - pad.r) * (i + 0.5)) / labels.length; const tx = svg("text", { x, y: H - pad.b + 14, "text-anchor": "middle" }); tx.textContent = l; s.append(tx); });
  return s;
}
export function legend(names) { return h("div", { class: "legend" }, names.map((n, i) => h("span", null, h("i", { style: { background: PALETTE[i % PALETTE.length] } }), n))); }
export function barChart(labels, series, { stacked = true, height = 210, ariaLabel = "График" } = {}) {
  const W = 480, H = height, pad = { l: 38, r: 8, t: 10, b: 26 };
  const n = labels.length || 1;
  const totals = labels.map((_, i) => (stacked ? series.reduce((a, s) => a + (s.points[i] || 0), 0) : Math.max(...series.map((s) => s.points[i] || 0))));
  const maxv = Math.max(1, ...totals) * 1.1;
  const s = frame(labels, maxv, W, H, pad); s.setAttribute("aria-label", ariaLabel);
  const bw = ((W - pad.l - pad.r) / n) * 0.72, plotH = H - pad.t - pad.b;
  labels.forEach((_, i) => {
    let acc = 0;
    series.forEach((se, k) => {
      const v = se.points[i] || 0; if (!v) return;
      const x0 = pad.l + ((W - pad.l - pad.r) * i) / n + ((W - pad.l - pad.r) / n - bw) / 2;
      const w = stacked ? bw : bw / series.length, x = stacked ? x0 : x0 + k * w;
      const hh = (v / maxv) * plotH, y = pad.t + plotH - (stacked ? acc + hh : hh);
      const r = svg("rect", { x, y, width: Math.max(1, w - 1), height: hh, fill: PALETTE[k % PALETTE.length], rx: 2 }); r.append(svg("title", {}, document.createTextNode(`${se.name}: ${v} (${labels[i]})`))); s.append(r);
      if (stacked) acc += hh;
    });
  });
  return s;
}
export function lineChart(labels, series, { height = 210, max = null, ariaLabel = "График", dashed = [] } = {}) {
  const W = 480, H = height, pad = { l: 38, r: 8, t: 10, b: 26 };
  const vals = series.flatMap((s) => s.points.filter((x) => x !== null && x !== undefined));
  const maxv = (max ?? Math.max(1, ...vals)) * 1.1;
  const s = frame(labels, maxv, W, H, pad); s.setAttribute("aria-label", ariaLabel);
  const plotH = H - pad.t - pad.b, n = labels.length;
  series.forEach((se, k) => {
    let d = "", pen = false;
    se.points.forEach((v, i) => {
      if (v === null || v === undefined) { pen = false; return; }
      const x = pad.l + ((W - pad.l - pad.r) * (i + 0.5)) / n, y = pad.t + plotH - (v / maxv) * plotH;
      d += `${pen ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)} `; pen = true;
      if (!dashed.includes(se.name)) { const c = svg("circle", { cx: x, cy: y, r: 3, fill: PALETTE[k % PALETTE.length] }); c.append(svg("title", {}, document.createTextNode(`${se.name}: ${v} (${labels[i]})`))); s.append(c); }
    });
    const p = svg("path", { d, fill: "none", stroke: PALETTE[k % PALETTE.length], "stroke-width": 2.2, "stroke-linejoin": "round" });
    if (dashed.includes(se.name)) p.setAttribute("stroke-dasharray", "5 4");
    s.insertBefore(p, s.querySelector("circle"));
  });
  return s;
}
export function sparkline(points, { w = 140, hgt = 34, color = "#1c4ea1" } = {}) {
  if (!points.length) return h("span", { class: "muted" }, "—");
  const max = Math.max(1, ...points), s = svg("svg", { viewBox: `0 0 ${w} ${hgt}`, width: w, height: hgt, role: "img", "aria-label": "динамика" });
  const d = points.map((v, i) => `${i ? "L" : "M"}${((w - 4) * i) / Math.max(1, points.length - 1) + 2},${hgt - 3 - (v / max) * (hgt - 6)}`).join(" ");
  s.append(svg("path", { d, fill: "none", stroke: color, "stroke-width": 2 })); return s;
}
export function scatter(points, { height = 260, ariaLabel = "Прогноз и факт" } = {}) {
  const W = 480, H = height, pad = { l: 44, r: 12, t: 12, b: 30 };
  const lim = Math.max(1, ...points.flatMap((p) => [Math.abs(p.predicted), Math.abs(p.actual)])) * 1.1;
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img", "aria-label": ariaLabel });
  const X = (v) => pad.l + ((v + lim) / (2 * lim)) * (W - pad.l - pad.r), Y = (v) => pad.t + (1 - (v + lim) / (2 * lim)) * (H - pad.t - pad.b);
  s.append(svg("line", { x1: X(-lim), y1: Y(-lim), x2: X(lim), y2: Y(lim), stroke: "#9aa7ba", "stroke-dasharray": "4 4" }));
  s.append(svg("line", { x1: X(0), y1: pad.t, x2: X(0), y2: H - pad.b, class: "axis" })); s.append(svg("line", { x1: pad.l, y1: Y(0), x2: W - pad.r, y2: Y(0), class: "axis" }));
  points.forEach((p) => { const c = svg("circle", { cx: X(p.predicted), cy: Y(p.actual), r: 4.5, fill: "#1c4ea1", "fill-opacity": 0.7 }); c.append(svg("title", {}, document.createTextNode(`${p.category || ""}: прогноз ${p.predicted}, факт ${p.actual}`))); s.append(c); });
  const a = svg("text", { x: W / 2, y: H - 4, "text-anchor": "middle" }); a.textContent = "Прогноз (log-отношение к обычному результату)"; s.append(a);
  const b = svg("text", { x: 10, y: H / 2, transform: `rotate(-90 10 ${H / 2})`, "text-anchor": "middle" }); b.textContent = "Факт"; s.append(b);
  return s;
}
export function bars(items, { max = null, cls = "" } = {}) {
  const m = max ?? Math.max(0.0001, ...items.map((i) => i.value));
  return h("div", { class: "col gap-s" }, items.map((i) => h("div", { class: "row", style: { flexWrap: "nowrap" } },
    h("div", { style: { width: "150px", flex: "none" }, class: "small" }, i.label),
    h("div", { class: "bar " + (i.cls || cls), style: { flex: "1" }, title: i.title || "" }, h("i", { style: { width: Math.max(1, (i.value / m) * 100) + "%" } })),
    h("div", { class: "small mono", style: { width: "64px", textAlign: "right" } }, i.text ?? i.value))));
}
