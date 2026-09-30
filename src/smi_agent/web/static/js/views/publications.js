import { api, h, clear, toast, modal, drawer, confirmBox, stateBadge, fmtDate, fmtAgo } from "../lib.js";

const STATES = [["", "Все"], ["awaiting_approval", "Ожидает подтверждения"], ["needs_review", "Требует проверки"], ["scheduled", "Запланировано"], ["publishing", "Публикуется"], ["published", "Опубликовано"], ["error", "Ошибка"], ["cancelled", "Отменено"], ["draft", "Черновик"]];
const PL = { telegram: "Telegram", instagram: "Instagram", facebook: "Facebook" };

export async function render(view) {
  let filter = "";
  const body = h("div"); const chips = h("div", { class: "row gap-s" });
  const load = async () => {
    const d = await api.get(api.p("/publications" + (filter ? `?state=${filter}` : "")));
    clear(chips); STATES.forEach(([v, l]) => chips.append(h("button", { class: "btn sm" + (filter === v ? " primary" : ""), onclick: () => { filter = v; load(); } }, l)));
    clear(body); body.append(table(d.publications, load));
  };
  view.append(h("div", { class: "row between" }, h("h1", null, "Публикации"), h("span", { class: "small muted" }, "Публикация — отдельный модуль: сбой одной платформы не блокирует остальные. Повторная отправка при неизвестном исходе запрещена до сверки.")), chips, h("div", { style: { height: "10px" } }), body);
  await load();
}

function table(pubs, reload) {
  if (!pubs.length) return h("div", { class: "empty" }, "Публикаций нет. Создайте их из раздела «Контент».");
  return h("div", { class: "table-wrap" }, h("table", null, h("thead", null, h("tr", null, ["Материал", "Платформа", "Состояние", "Время", "Попытки", "Действия"].map((x) => h("th", null, x)))),
    h("tbody", null, pubs.map((p) => h("tr", null,
      h("td", null, h("div", { class: "mono small muted" }, p.content_id), h("a", { href: "#/content?open=" + p.content_pk }, p.title.slice(0, 70)), p.origin !== "user" ? h("span", { class: "badge dark", style: { marginLeft: "6px" } }, p.origin) : null),
      h("td", null, PL[p.platform], h("div", { class: "small muted" }, p.account || "аккаунт не подключён")),
      h("td", null, stateBadge(p.state, p.state_label), p.needs_manual ? h("div", { class: "badge bad", style: { marginTop: "3px" } }, "нужна ручная проверка") : null, p.last_error?.message ? h("div", { class: "small", style: { color: "var(--bad)", maxWidth: "260px" } }, p.last_error.message) : null, p.external_url && !p.external_url.startsWith("sandbox") ? h("div", null, h("a", { href: p.external_url, target: "_blank", rel: "noopener noreferrer" }, "открыть пост")) : p.external_id ? h("div", { class: "mono small muted" }, p.external_id) : null),
      h("td", { class: "small" }, p.published_at ? "опубл. " + fmtDate(p.published_at) : p.scheduled_at ? fmtDate(p.scheduled_at) : "—", p.schedule?.reason ? h("div", { class: "muted", style: { maxWidth: "200px" } }, p.schedule.reason) : null),
      h("td", null, p.attempts), h("td", null, h("div", { class: "row gap-s" }, actions(p, reload), h("button", { class: "btn sm ghost", onclick: () => detail(p.id) }, "Журнал"))))))));
}

function actions(p, reload) {
  const run = async (fn, msg) => { try { const r = await fn(); toast(msg || "Готово", "ok"); reload(); return r; } catch { /* тост показан */ } };
  const b = [];
  if (p.state === "awaiting_approval") b.push(h("button", { class: "btn sm ok", onclick: () => approve(p, reload) }, "Подтвердить"));
  if (["awaiting_approval", "scheduled", "needs_review", "draft", "error"].includes(p.state)) b.push(h("button", { class: "btn sm danger", onclick: async () => { if (await confirmBox("Отменить публикацию?", "Состояние станет «Отменено».", "Отменить", "danger")) run(() => api.post(api.p(`/publications/${p.id}/cancel`), { reason: "отмена пользователем" }), "Отменено"); } }, "Отменить"));
  if (p.state === "error") {
    b.push(h("button", { class: "btn sm", onclick: async () => { const r = await run(() => api.post(api.p(`/publications/${p.id}/reconcile`)), "Сверка выполнена"); if (r) toast(`Сверка: ${r.status} — ${r.detail}`); } }, "Сверка"));
    b.push(h("button", { class: "btn sm", onclick: () => retry(p, reload) }, "Повторить"));
    b.push(h("button", { class: "btn sm", onclick: () => confirmPublished(p, reload) }, "Уже опубликовано"));
  }
  if (p.state === "needs_review") b.push(h("a", { class: "btn sm", href: "#/content?open=" + p.content_pk }, "Исправить"));
  return b;
}

function approve(p, reload) {
  const mode = h("select", null, [["now", "Сразу"], ["optimal", "Оптимальное время"], ["at", "Указать время"]].map(([a, l]) => h("option", { value: a, selected: (p.account_mode === "scheduled" ? "optimal" : "now") === a }, l)));
  const at = h("input", { type: "datetime-local", class: "hidden" });
  mode.addEventListener("change", () => at.classList.toggle("hidden", mode.value !== "at"));
  modal("Подтвердить публикацию", h("div", { class: "col" }, h("p", null, `${PL[p.platform]} · ${p.title}`), h("p", { class: "small muted" }, "Перед отправкой проверки выполнятся ещё раз. К тексту добавится маркировка участия ИИ («Проверено редактором.» — после вашего подтверждения)."), h("label", null, "Когда публиковать", mode), at),
    [{ label: "Отмена" }, { label: "Подтвердить", kind: "ok", fn: async () => {
      const schedule = mode.value === "at" ? (at.value ? { mode: "at", at: new Date(at.value).toISOString() } : null) : { mode: mode.value };
      if (!schedule) { toast("Укажите время", "err"); return true; }
      try { const r = await api.post(api.p(`/publications/${p.id}/approve`), { schedule }); toast(`Подтверждено: ${r.state_label}${r.schedule?.reason ? " — " + r.schedule.reason : ""}`, "ok"); reload(); } catch { return true; }
    } }]);
}
function retry(p, reload) {
  const unknown = (p.last_error?.outcome || "") === "unknown";
  const cb = h("input", { type: "checkbox" });
  modal("Повторить отправку", h("div", { class: "col" }, unknown ? h("div", { class: "notice bad" }, "Исход прошлой попытки НЕИЗВЕСТЕН: пост мог выйти. Повтор может создать дубль. Сначала нажмите «Сверка» или проверьте аккаунт вручную.") : h("p", null, "Повторная отправка будет поставлена в очередь."),
    unknown ? h("label", { class: "inline" }, cb, "Я проверил аккаунт: поста на платформе нет") : null),
    [{ label: "Отмена" }, { label: "Повторить", kind: "primary", fn: async () => { try { await api.post(api.p(`/publications/${p.id}/retry`), { confirm_not_published: cb.checked }); toast("Поставлено в очередь", "ok"); reload(); } catch { return true; } } }]);
}
function confirmPublished(p, reload) {
  const url = h("input", { placeholder: "Ссылка на пост (необязательно)" });
  modal("Пост уже опубликован", h("label", null, "Ссылка", url), [{ label: "Отмена" }, { label: "Подтвердить", kind: "primary", fn: async () => { try { await api.post(api.p(`/publications/${p.id}/confirm-published`), { url: url.value }); toast("Отмечено как опубликованное", "ok"); reload(); } catch { return true; } } }]);
}
async function detail(id) {
  const d = await api.get(api.p(`/publications/${id}`));
  drawer(`Публикация ${d.content_id} · ${PL[d.platform]}`, h("div", { class: "col" },
    h("div", { class: "row" }, stateBadge(d.state, d.state_label), h("span", { class: "chip" }, "попыток: " + d.attempts), d.approved_by ? h("span", { class: "chip" }, "подтвердил: " + d.approved_by) : null),
    h("h3", null, "История состояний"), h("div", { class: "col gap-s" }, d.events.map((e) => h("div", { class: "small" }, h("span", { class: "mono muted" }, fmtDate(e.ts) + " "), h("b", null, (e.from ? e.from + " › " : "") + e.to), " · ", e.actor, e.note ? h("div", { class: "muted" }, e.note) : null))),
    h("h3", null, "Попытки отправки"), d.attempts_log.length ? h("div", { class: "table-wrap" }, h("table", null, h("thead", null, h("tr", null, ["№", "Исход", "Код", "Сообщение"].map((x) => h("th", null, x)))), h("tbody", null, d.attempts_log.map((a) => h("tr", null, h("td", null, a.no), h("td", null, h("span", { class: "badge " + ({ success: "ok", unknown: "warn", failed_permanent: "bad", rate_limited: "warn", not_sent: "info" }[a.outcome] || "") }, a.outcome)), h("td", { class: "mono small" }, a.error_code || "—"), h("td", { class: "small" }, a.message || "", a.reconcile?.status ? h("div", { class: "muted" }, "сверка: " + a.reconcile.status + " — " + (a.reconcile.detail || "")) : null)))))) : h("div", { class: "muted small" }, "Попыток ещё не было.")));
}
