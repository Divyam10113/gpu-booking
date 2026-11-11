"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (k === "style") n.style.cssText = v;
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) n.append(kid);
  return n;
};

const state = {
  cfg: null,
  me: { user: null, admin: false },
  weekStart: startOfWeek(new Date()),
  bookings: [],
  status: null,
  mine: [],
  pending: [],
  activity: [],
  usage: { people: [], unbooked: [] },
  waiting: [],
  passwordRequests: [],
};

// Through ngrok every request counts against a monthly cap, so refresh less often there.
const VIA_TUNNEL = /\.ngrok(-free)?\.(app|dev|io)$/.test(location.hostname);
const REFRESH_MS = VIA_TUNNEL ? 120_000 : 15_000;

const PX_PER_MIN = 56 / 60; // timeline width: 56px per hour
const HOUR_PX = PX_PER_MIN * 60;

// ------------------------------------------------------------------ api

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
    credentials: "same-origin",
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && path !== "/api/login") { state.me = { user: null, admin: false }; showLogin(); throw new Error("Log in first"); }
  if (!res.ok) {
    const d = data.detail;
    throw new Error(typeof d === "string" ? d : Array.isArray(d) ? d.map((x) => x.msg).join("; ") : `Request failed (${res.status})`);
  }
  return data;
}

// ------------------------------------------------------------------ time helpers

function startOfWeek(d) {
  const x = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const dow = (x.getDay() + 6) % 7; // Monday = 0
  x.setDate(x.getDate() - dow);
  return x;
}
const addDays = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
const secs = (d) => Math.floor(d.getTime() / 1000);
const nowSecs = () => Math.floor(Date.now() / 1000);
const pad = (n) => String(n).padStart(2, "0");

const fmtClock = (ts) => { const d = new Date(ts * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
const fmtDay = (ts) => new Date(ts * 1000).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
function fmtWhen(ts) {
  const d = new Date(ts * 1000), today = new Date();
  const tmr = addDays(today, 1);
  const same = (a, b) => a.toDateString() === b.toDateString();
  const day = same(d, today) ? "Today" : same(d, tmr) ? "Tomorrow" : fmtDay(ts);
  return `${day} ${fmtClock(ts)}`;
}
function fmtRange(a, b) {
  const sameDay = new Date(a * 1000).toDateString() === new Date(b * 1000).toDateString()
    || (b - a <= 86400 && fmtClock(b) === "00:00" && new Date((b - 1) * 1000).toDateString() === new Date(a * 1000).toDateString());
  return sameDay ? `${fmtWhen(a)}–${fmtClock(b)}` : `${fmtWhen(a)} → ${fmtWhen(b)}`;
}
function fmtDur(s) {
  s = Math.max(0, Math.round(s / 60));
  const d = Math.floor(s / 1440), h = Math.floor((s % 1440) / 60), m = s % 60;
  return [d && `${d}d`, h && `${h}h`, m && `${m}m`].filter(Boolean).join(" ") || "0m";
}
const fmtGpus = (gpus) => gpus.length === state.cfg.num_gpus && gpus.length > 1
  ? `All GPUs (${gpus.join(", ")})` : gpus.length > 1 ? `GPUs ${gpus.join(", ")}` : `GPU ${gpus[0]}`;
const gib = (mib) => (mib / 1024).toFixed(1);
/** Each person keeps the same color everywhere, by their position in config.toml. */
function personClass(name) {
  const i = state.cfg.users.findIndex((u) => u.name === name);
  return i < 0 ? "" : `p${i % 8}`;
}

function toLocalInput(ts) {
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function fromLocalInput(v) {
  if (!v) return null;
  const slot = state.cfg.slot_minutes * 60;
  return Math.round(secs(new Date(v)) / slot) * slot;
}
const slotFloor = (ts) => ts - (ts % (state.cfg.slot_minutes * 60));
/** Whether this person's bookings go straight through, without waiting for an admin. */
const bookInstantly = () => state.me.admin || !state.cfg.require_approval;

// ------------------------------------------------------------------ toasts

function toast(msg, kind = "") {
  const t = el("div", { class: `toast ${kind}` }, msg);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), kind === "error" ? 6000 : 3500);
}
async function act(fn, okMsg) {
  try {
    const r = await fn();
    if (okMsg) toast(typeof okMsg === "function" ? okMsg(r) : okMsg);
    await refreshAll();
    return r;
  } catch (e) {
    toast(e.message, "error");
    throw e;
  }
}

// ------------------------------------------------------------------ login

function showLogin() {
  $("#app").hidden = true;
  $("#who").hidden = true;
  const sel = $("#login-name");
  sel.replaceChildren(el("option", { value: "" }, "Choose your name"),
    ...state.cfg.users.map((u) => el("option", { value: u.name }, u.name)));
  for (const id of ["#login-password", "#login-confirm"]) $(id).value = "";
  updateLoginForm();
  showLoginStep("#login-form");
  $("#login").hidden = false;
  sel.focus();
}

/** The login overlay shows one of: log in, sign up, or "waiting for approval". */
function showLoginStep(id) {
  for (const x of ["#login-form", "#signup-form", "#forgot-form", "#signup-done"]) $(x).hidden = x !== id;
}
function showDone(text) {
  $("#done-text").textContent = text;
  showLoginStep("#signup-done");
}

$("#to-signup").addEventListener("click", async () => {
  for (const id of ["#su-name", "#su-password", "#su-confirm"]) $(id).value = "";
  $("#su-error").hidden = true;
  const accounts = await api("GET", "/api/accounts").catch(() => []);
  $("#su-unix").replaceChildren(el("option", { value: "" }, "Choose your account"),
    ...accounts.map((a) => el("option", { value: a }, a)));
  showLoginStep("#signup-form");
  $("#su-name").focus();
});
$("#to-forgot").addEventListener("click", () => {
  $("#fp-name").replaceChildren(el("option", { value: "" }, "Choose your name"),
    ...state.cfg.users.map((u) => el("option", { value: u.name }, u.name)));
  $("#fp-name").value = $("#login-name").value;
  for (const id of ["#fp-password", "#fp-confirm"]) $(id).value = "";
  $("#fp-error").hidden = true;
  showLoginStep("#forgot-form");
  ($("#fp-name").value ? $("#fp-password") : $("#fp-name")).focus();
});
for (const b of document.querySelectorAll("[data-to-login]")) {
  b.addEventListener("click", () => { showLoginStep("#login-form"); $("#login-name").focus(); });
}

$("#forgot-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const showError = (msg) => { $("#fp-error").textContent = msg; $("#fp-error").hidden = false; };
  if ($("#fp-password").value !== $("#fp-confirm").value) { showError("The two passwords don't match."); return; }
  try {
    await api("POST", "/api/forgot-password", { name: $("#fp-name").value, password: $("#fp-password").value });
    for (const id of ["#fp-password", "#fp-confirm"]) $(id).value = "";
    showDone("Your new password starts working once an admin approves it; they've been told. Until then your old one still works.");
  } catch (err) {
    showError(err.message);
  }
});
$("#su-done-ok").addEventListener("click", () => showLogin());

$("#signup-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const showError = (msg) => { $("#su-error").textContent = msg; $("#su-error").hidden = false; };
  if ($("#su-password").value !== $("#su-confirm").value) { showError("The two passwords don't match."); return; }
  try {
    const r = await api("POST", "/api/register", {
      name: $("#su-name").value, unix: $("#su-unix").value, password: $("#su-password").value,
    });
    for (const id of ["#su-password", "#su-confirm"]) $(id).value = "";
    showDone(`Your account ${r.name} is waiting for an admin to approve it. You can log in once they do; the admins have been told.`);
  } catch (err) {
    showError(err.message);
  }
});

function updateLoginForm() {
  const u = state.cfg.users.find((x) => x.name === $("#login-name").value);
  const firstTime = !!u && !u.has_password;
  $("#login-first-time").hidden = !firstTime;
  $("#login-confirm-row").hidden = !firstTime;
  $("#login-password-label").textContent = firstTime ? "Choose a password" : "Password";
  $("#login-password").autocomplete = firstTime ? "new-password" : "current-password";
  $("#login-error").hidden = true;
}

$("#login-name").addEventListener("change", updateLoginForm);

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const showError = (msg) => { $("#login-error").textContent = msg; $("#login-error").hidden = false; };
  if (!$("#login-confirm-row").hidden && $("#login-password").value !== $("#login-confirm").value) {
    showError("The two passwords don't match.");
    return;
  }
  try {
    state.me = await api("POST", "/api/login", {
      name: $("#login-name").value,
      password: $("#login-password").value,
    });
    for (const id of ["#login-password", "#login-confirm"]) $(id).value = "";
    $("#login").hidden = true;
    enterApp();
  } catch (err) {
    showError(err.message);
  }
});

$("#change-pw").addEventListener("click", () => {
  for (const id of ["#pw-current", "#pw-new", "#pw-confirm"]) $(id).value = "";
  $("#pw-error").hidden = true;
  $("#password-dialog").hidden = false;
  $("#pw-current").focus();
});

$("#password-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const showError = (msg) => { $("#pw-error").textContent = msg; $("#pw-error").hidden = false; };
  if ($("#pw-new").value !== $("#pw-confirm").value) return showError("The two new passwords don't match.");
  try {
    await api("POST", "/api/password", { current: $("#pw-current").value, new: $("#pw-new").value });
    closeDialogs();
    toast("Password changed");
  } catch (err) {
    showError(err.message);
  }
});

$("#logout").addEventListener("click", async () => {
  await api("POST", "/api/logout");
  state.me = { user: null, admin: false };
  state.cfg = await api("GET", "/api/config");
  showLogin();
});

function enterApp() {
  $("#who-name").textContent = state.me.user;
  $("#admin-pill").hidden = !state.me.admin;
  $("#admin-panel").hidden = !state.me.admin;
  $("#who").hidden = false;
  $("#app").hidden = false;
  refreshAll().then(scrollCalendarToNow);
}

// ------------------------------------------------------------------ data refresh

/** Everything the page shows comes from one request, so each refresh costs one request. */
async function refreshAll() {
  if (!state.me.user) return;
  const ws = secs(state.weekStart), we = secs(addDays(state.weekStart, 7));
  let s;
  try {
    s = await api("GET", `/api/state?start=${ws}&end=${we}`);
  } catch { return; /* polling stays quiet; actions show their own errors */ }
  Object.assign(state, { status: s.status, bookings: s.bookings, mine: s.mine });
  if (state.me.admin) {
    Object.assign(state, { pending: s.pending, activity: s.activity, usage: s.usage, waiting: s.waiting,
      passwordRequests: s.password_requests });
    state.cfg.users = s.users;
  }
  renderNow();
  renderCalendar();
  renderMine();
  if (state.me.admin) renderAdmin();
}

// ------------------------------------------------------------------ now strip

function renderNow() {
  const s = state.status;
  if (!s) return;
  const err = $("#smi-error");
  // The server reads the GPUs every 10 s; a much older reading means something is wrong there.
  const stale = !s.error && s.taken_at && s.now - s.taken_at > 120;
  err.hidden = !s.error && !stale;
  err.textContent = s.error ? `Live usage unavailable: ${s.error}`
    : stale ? `Live usage stopped updating at ${fmtWhen(Math.floor(s.taken_at))}. Tell an admin; restarting the app fixes it.` : "";
  $("#poll-age").textContent = s.taken_at ? `Live usage, updated ${fmtClock(Math.floor(s.taken_at))}` : "";

  $("#now-grid").replaceChildren(...s.gpus.map((g) => {
    const cur = g.current;
    const mine = cur && cur.user === state.me.user;
    const util = g.util ?? 0;
    const memPct = g.mem_total_mib ? (100 * g.mem_used_mib) / g.mem_total_mib : 0;

    const holder = cur
      ? el("div", { class: "holder" },
          el("div", { class: `holder-name ${personClass(cur.user)}` }, el("span", { class: "dot" }), mine ? "You" : cur.user),
          el("div", { class: "holder-sub" },
            `Booked until ${fmtClock(cur.end)} (${fmtDur(cur.end - s.now)} left)${cur.gpus.length > 1 ? ", both GPUs" : ""}`))
      : el("div", { class: "holder" },
          el("div", { class: "holder-name" }, "Free"),
          el("div", { class: g.unbooked_use ? "holder-sub" : "holder-sub free" },
            g.unbooked_use ? "Not booked, but someone is using it" : "Not booked right now"));

    const alert = g.violation && el("div", { class: "alert-line" }, "Someone without the booking is running a job here");

    // Running booking with none of the booker's jobs on it: say when it'll be freed automatically.
    const limit = s.idle_release_minutes;
    const idle = cur && limit && cur.idle_min >= 10 && el("div", { class: "idle-line" },
      `No GPU activity from ${mine ? "you" : cur.user} for ${fmtDur(cur.idle_min * 60)}. `,
      `It's freed automatically after ${fmtDur(limit * 60)} idle.`);

    const done = mine && el("button", {
      class: "btn small", type: "button",
      onclick: () => act(() => api("POST", `/api/bookings/${cur.id}/cancel`), "Thanks! The GPU is free for others."),
    }, "I'm done, free it");

    const env = mine && el("div", { class: "env-line" },
      el("code", {}, `export CUDA_VISIBLE_DEVICES=${cur.gpus.join(",")}`),
      el("button", {
        class: "btn ghost small", type: "button",
        onclick: (e) => copy(`export CUDA_VISIBLE_DEVICES=${cur.gpus.join(",")}`, e.currentTarget),
      }, "Copy"),
      done);

    const meters = g.util == null ? null : el("div", { class: "meters" },
      el("span", { class: "label" }, "Load"),
      el("div", { class: "meter" }, el("i", { style: `width:${util}%` })),
      el("span", {}, `${util}%`),
      el("span", { class: "label" }, "Memory"),
      el("div", { class: "meter" }, el("i", { style: `width:${memPct.toFixed(1)}%` })),
      el("span", {}, `${gib(g.mem_used_mib)} of ${gib(g.mem_total_mib)} GB`));

    const procs = g.procs.length
      ? el("ul", { class: "procs" }, g.procs.map((p) => el("li", { class: p.violation ? "violation" : "", title: `PID ${p.pid}: ${p.cmd}` },
          el("span", { class: `p-user ${personClass(p.name)}`, title: p.sharers.length > 1 ? `Shared account: ${p.sharers.join(", ")}` : null },
            el("span", { class: "dot" }), p.name),
          el("span", { class: "cmd" }, p.cmd),
          el("span", { class: "p-mem" }, `${gib(p.mem_mib)} GB`))))
      : g.util == null ? null : el("div", { class: "muted" }, "No jobs running");

    const next = el("div", { class: "next-line" }, g.next
      ? `Next: ${g.next.user === state.me.user ? "you" : g.next.user}, ${fmtRange(g.next.start, g.next.end)}`
      : "Nothing else booked");

    return el("article", { class: `gpu-card ${g.violation ? "alert" : ""}` },
      el("div", { class: "gpu-card-head" },
        el("span", { class: "gpu-id" }, `GPU ${g.index}`),
        el("span", { class: "gpu-model" }, g.name || "")),
      holder, alert, idle, env, meters, procs, next);
  }));
}

async function copy(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
    btn.textContent = "Copied";
    setTimeout(() => (btn.textContent = "Copy"), 1500);
  } catch {
    toast("Couldn't copy. Select the text instead.", "error");
  }
}

// ------------------------------------------------------------------ calendar (timeline: one row per GPU per day, hours across)

function renderCalendar() {
  const { cfg } = state;
  const days = [...Array(7)].map((_, i) => addDays(state.weekStart, i));
  const todayStr = new Date().toDateString();
  const end = addDays(state.weekStart, 6);
  $("#week-label").textContent =
    `${state.weekStart.toLocaleDateString(undefined, { day: "numeric", month: "short" })} – ${end.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" })}`;

  const tl = el("div", { class: "tl", style: `--track-w:${24 * HOUR_PX}px;--hour-w:${HOUR_PX}px;--lanes:${cfg.num_gpus}` });
  tl.append(el("div", { class: "tl-head" },
    el("div", { class: "tl-corner" }),
    el("div", { class: "tl-hours" },
      [...Array(24)].map((_, h) => el("span", { class: "tl-hour", style: `left:${h * HOUR_PX}px` }, `${pad(h)}:00`)))));

  const now = nowSecs();
  for (const d of days) {
    const ds = secs(d), de = secs(addDays(d, 1));
    const today = d.toDateString() === todayStr;
    const day = el("div", { class: `tl-day ${today ? "today" : ""}` },
      el("div", { class: "tl-day-label" },
        el("span", { class: "tl-dow" }, today ? "Today" : d.toLocaleDateString(undefined, { weekday: "short" })),
        el("span", { class: "tl-date" }, d.toLocaleDateString(undefined, { day: "numeric", month: "short" }))));
    for (let g = 0; g < cfg.num_gpus; g++) {
      day.append(el("div", { class: "tl-gpu" }, `GPU ${g}`));
      const lane = el("div", { class: "lane", "data-day": ds, "data-gpu": g });
      if (now > ds) {
        lane.append(el("div", { class: "past", style: `width:${((Math.min(now, de) - ds) / 60) * PX_PER_MIN}px` }));
      }
      if (now >= ds && now < de) lane.append(el("div", { class: "now-line", style: `left:${((now - ds) / 60) * PX_PER_MIN}px` }));

      // Blocks in this lane; overlapping pending requests share the row height.
      const segs = state.bookings
        .filter((b) => b.gpus.includes(g) && b.start < de && b.end > ds)
        .map((b) => ({ b, s: Math.max(b.start, ds), e: Math.min(b.end, de) }))
        .sort((x, y) => x.s - y.s || (x.b.status === "approved" ? -1 : 1));
      const rowEnds = [];
      for (const seg of segs) {
        let r = rowEnds.findIndex((endT) => endT <= seg.s);
        if (r === -1) { r = rowEnds.length; rowEnds.push(0); }
        rowEnds[r] = seg.e;
        seg.row = r;
      }
      const rows = Math.max(1, rowEnds.length);
      for (const { b, s, e, row } of segs) lane.append(blockEl(b, s, e, ds, row, rows));
      attachDrag(lane);
      day.append(lane);
    }
    tl.append(day);
  }
  $("#calendar").replaceChildren(tl);
}

function blockEl(b, s, e, ds, row, rows) {
  const mine = b.user === state.me.user;
  const left = ((s - ds) / 60) * PX_PER_MIN;
  const w = Math.max(((e - s) / 60) * PX_PER_MIN - 2, 16);
  const h = 100 / rows;
  const label = `${b.user}${b.status === "pending" ? " (waiting)" : ""}`;
  return el("button", {
    type: "button",
    class: `block ${b.status} ${mine ? "mine" : ""} ${personClass(b.user)}`,
    style: `left:${left + 1}px;width:${w}px;top:calc(${row * h}% + 3px);height:calc(${h}% - 6px)`,
    title: `${label} · ${fmtGpus(b.gpus)} · ${fmtRange(b.start, b.end)}${b.note ? ` · ${b.note}` : ""}`,
    onclick: (ev) => { ev.stopPropagation(); showDetail(b); },
  },
    el("span", { class: "b-user" }, mine ? "You" : b.user),
    w > 90 && rows === 1 && el("span", { class: "b-time" }, `${fmtClock(b.start)}–${fmtClock(b.end)}${b.status === "pending" ? " · waiting" : ""}`));
}

function attachDrag(lane) {
  const slotPx = state.cfg.slot_minutes * PX_PER_MIN;
  const slotsPerDay = Math.round(1440 / state.cfg.slot_minutes);
  const slotAt = (clientX) => Math.min(slotsPerDay - 1,
    Math.max(0, Math.floor((clientX - lane.getBoundingClientRect().left) / slotPx)));
  const twoHours = Math.round(120 / state.cfg.slot_minutes);
  let startSlot = null, sel = null, dragged = false;

  const finish = (a, b) => {
    const ds = Number(lane.dataset.day), slot = state.cfg.slot_minutes * 60;
    const lo = Math.min(a, b), hi = Math.min(Math.max(a, b) + 1, slotsPerDay);
    openBookingDialog({ gpus: [Number(lane.dataset.gpu)], start: ds + lo * slot, end: ds + hi * slot });
  };
  const update = (e) => {
    const cur = slotAt(e.clientX);
    if (cur !== startSlot) dragged = true;
    const lo = Math.min(startSlot, cur), hi = Math.max(startSlot, cur) + 1;
    sel.style.left = `${lo * slotPx}px`;
    sel.style.width = `${(hi - lo) * slotPx}px`;
  };

  lane.addEventListener("pointerdown", (e) => {
    if (e.target !== lane || e.pointerType !== "mouse" || e.button !== 0) return;
    e.preventDefault();
    startSlot = slotAt(e.clientX);
    dragged = false;
    sel = el("div", { class: "selection" });
    lane.append(sel);
    lane.setPointerCapture(e.pointerId);
    update(e);
  });
  lane.addEventListener("pointermove", (e) => { if (sel) update(e); });
  lane.addEventListener("pointerup", (e) => {
    if (!sel) return;
    const cur = slotAt(e.clientX);
    sel.remove(); sel = null;
    // A plain click books a 2-hour window from that slot.
    finish(startSlot, dragged ? cur : startSlot + twoHours - 1);
    startSlot = null;
  });
  // Touch and pen: tap opens a 2-hour request (dragging would fight scrolling).
  lane.addEventListener("click", (e) => {
    if (e.target !== lane || !e.pointerType || e.pointerType === "mouse") return;
    const s = slotAt(e.clientX);
    finish(s, s + twoHours - 1);
  });
}

function scrollCalendarTo(hour) {
  $("#calendar").scrollLeft = Math.max(0, (hour - 2) * HOUR_PX);
}
const scrollCalendarToNow = () => scrollCalendarTo(Math.min(new Date().getHours(), 16));

$("#prev-week").addEventListener("click", () => { state.weekStart = addDays(state.weekStart, -7); refreshAll(); });
$("#next-week").addEventListener("click", () => { state.weekStart = addDays(state.weekStart, 7); refreshAll(); });
$("#this-week").addEventListener("click", () => { state.weekStart = startOfWeek(new Date()); refreshAll().then(scrollCalendarToNow); });
$("#new-request").addEventListener("click", () => {
  const s = slotFloor(nowSecs()) + state.cfg.slot_minutes * 60;
  openBookingDialog({ gpus: [0], start: s, end: s + 2 * 3600 });
});
$("#admin-pill").addEventListener("click", () => $("#admin-panel").scrollIntoView({ behavior: "smooth", block: "start" }));

function showWeekOf(ts) {
  state.weekStart = startOfWeek(new Date(ts * 1000));
  refreshAll().then(() => {
    scrollCalendarTo(new Date(ts * 1000).getHours());
    $("#calendar").scrollIntoView({ behavior: "smooth", block: "nearest" });
  });
}

// ------------------------------------------------------------------ theme

const darkQuery = matchMedia("(prefers-color-scheme: dark)");
const isDark = () => {
  const t = document.documentElement.dataset.theme;
  return t ? t === "dark" : darkQuery.matches;
};
function syncThemeButton() {
  const btn = $("#theme-toggle"), dark = isDark();
  btn.classList.toggle("is-dark", dark);
  const label = dark ? "Switch to light mode" : "Switch to dark mode";
  btn.setAttribute("aria-label", label);
  btn.title = label;
}
$("#theme-toggle").addEventListener("click", () => {
  const next = isDark() ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("theme", next); } catch { /* not saved; still applies for this visit */ }
  syncThemeButton();
});
darkQuery.addEventListener("change", syncThemeButton);
syncThemeButton();

// ------------------------------------------------------------------ booking dialog

let dialogMode = null; // { kind: "new" } | { kind: "edit", booking }

function openBookingDialog({ gpus, start, end, booking = null }) {
  const { cfg, me } = state;
  dialogMode = booking ? { kind: "edit", booking } : { kind: "new" };
  $("#booking-title").textContent = booking ? `Edit booking #${booking.id}` : bookInstantly() ? "Book GPU time" : "Request GPU time";
  $("#booking-submit").textContent = booking ? "Save changes" : bookInstantly() ? "Book" : "Send request";

  $("#gpu-picks").replaceChildren(...[...Array(cfg.num_gpus)].map((_, g) =>
    el("label", { class: "gpu-pick" },
      el("input", { type: "checkbox", value: g, id: `f-gpu-${g}`, checked: gpus.includes(g), onchange: updateDuration }),
      el("span", {}, `GPU ${g}`))));

  const step = cfg.slot_minutes * 60;
  for (const id of ["#f-start", "#f-end"]) $(id).step = step;
  $("#f-start").value = toLocalInput(start);
  $("#f-end").value = toLocalInput(end);
  $("#f-note").value = booking ? booking.note : "";

  const userRow = $("#f-user-row");
  userRow.hidden = !me.admin || !!booking;
  if (me.admin) {
    $("#f-user").replaceChildren(...cfg.users.map((u) => el("option", { value: u.name, selected: u.name === me.user }, u.name)));
  }
  $("#booking-error").hidden = true;
  updateDuration();
  $("#booking-dialog").hidden = false;
  $("#f-start").focus();
}

function pickedGpus() {
  return [...document.querySelectorAll("#gpu-picks input:checked")].map((i) => Number(i.value));
}

function updateDuration() {
  const s = fromLocalInput($("#f-start").value), e = fromLocalInput($("#f-end").value);
  const gpus = pickedGpus();
  const all = gpus.length === state.cfg.num_gpus;
  const limit = all ? state.cfg.max_hours_all : state.cfg.max_hours_single;
  const parts = [];
  if (s && e && e > s) parts.push(fmtDur(e - s));
  if (!state.me.admin) parts.push(`limit ${limit} h for ${all ? "all GPUs" : "one GPU"}`);
  if (!bookInstantly() && !dialogMode?.booking) parts.push("an admin will approve it");
  $("#f-duration").textContent = parts.join(" · ");
}
$("#f-start").addEventListener("change", () => {
  // Keep the same length when the start moves.
  const s = fromLocalInput($("#f-start").value), e = fromLocalInput($("#f-end").value);
  if (s && e && e <= s) $("#f-end").value = toLocalInput(s + 2 * 3600);
  updateDuration();
});
$("#f-end").addEventListener("change", updateDuration);

$("#booking-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    gpus: pickedGpus(),
    start: fromLocalInput($("#f-start").value),
    end: fromLocalInput($("#f-end").value),
    note: $("#f-note").value,
  };
  const errBox = $("#booking-error");
  if (!body.gpus.length) { errBox.textContent = "Pick at least one GPU."; errBox.hidden = false; return; }
  try {
    if (dialogMode.kind === "edit") {
      await act(() => api("PATCH", `/api/bookings/${dialogMode.booking.id}`, body), "Booking updated");
    } else {
      if (state.me.admin) body.user = $("#f-user").value;
      await act(() => api("POST", "/api/bookings", body), (r) => {
        const n = r.auto_rejected.length;
        return r.booking.status === "approved"
          ? `Booked${n ? `; ${n} overlapping request${n > 1 ? "s" : ""} rejected` : ""}`
          : "Request sent. You'll see it here once an admin approves it.";
      });
    }
    closeDialogs();
  } catch (err) {
    errBox.textContent = err.message;
    errBox.hidden = false;
  }
});

// ------------------------------------------------------------------ detail dialog

function showDetail(b) {
  const box = $("#detail-body");
  const hist = el("div", {});
  const actions = bookingActions(b, { inDialog: true });
  box.replaceChildren(
    el("div", { class: "item-head" },
      el("h2", {}, `${b.user === state.me.user ? "Your booking" : b.user} · #${b.id}`),
      el("span", { class: `chip ${b.status}` }, statusLabel(b.status))),
    el("dl", { class: "kv" },
      el("dt", {}, "GPUs"), el("dd", { class: "mono" }, fmtGpus(b.gpus)),
      el("dt", {}, "When"), el("dd", { class: "mono" }, fmtRange(b.start, b.end)),
      el("dt", {}, "Length"), el("dd", { class: "mono" }, fmtDur(b.end - b.start)),
      b.status === "approved" && b.start < nowSecs() && [el("dt", {}, "Used"), el("dd", {}, usedText(b))],
      b.note && [el("dt", {}, "Note"), el("dd", {}, b.note)],
      b.extends_id && [el("dt", {}, "Extends"), el("dd", {}, `#${b.extends_id}`)],
      b.decided_by && [el("dt", {}, b.status === "approved" ? "Approved by" : "Decided by"), el("dd", {}, b.decided_by)],
      b.admin_note && [el("dt", {}, "Admin note"), el("dd", {}, b.admin_note)]),
    actions,
    el("details", { class: "subsection", ontoggle: (e) => e.target.open && loadHistory(b.id, hist) },
      el("summary", {}, "History"), hist),
    el("div", { class: "actions" }, el("button", { class: "btn ghost", type: "button", "data-close": true }, "Close")));
  $("#detail-dialog").hidden = false;
}

/** How much of a started booking the booker's jobs kept the GPUs busy. */
function usedText(b) {
  const sofar = (Math.min(b.end, nowSecs()) - b.start) * b.gpus.length;
  const pct = sofar > 0 ? Math.round((100 * b.active_secs) / sofar) : 0;
  return `${Math.min(pct, 100)}% of the booked time so far (${fmtDur(b.active_secs / b.gpus.length)}${b.gpus.length > 1 ? " per GPU" : ""})`;
}

async function loadHistory(id, box) {
  box.replaceChildren(el("p", { class: "muted" }, "Loading…"));
  try {
    const rows = await api("GET", `/api/bookings/${id}/history`);
    box.replaceChildren(...(rows.length ? rows.map(historyRow) : [el("p", { class: "muted" }, "No changes recorded.")]));
  } catch (e) {
    box.replaceChildren(el("p", { class: "muted" }, e.message));
  }
}

/** One history line: "Bob booked · GPU 0, Wed 05 Nov 14:00 → 16:00", with when it happened. */
function historyRow(h, { withBooking = false } = {}) {
  const who = h.actor ? (h.actor === state.me.user ? "You" : h.actor) : "Automatically";
  return el("div", { class: "row" },
    el("div", {},
      el("div", {}, el("b", {}, who), ` ${h.action} `,
        withBooking && el("button", { class: "link", type: "button", onclick: () => openBooking(h.booking_id) }, `#${h.booking_id}`),
        withBooking && h.user !== h.actor && ` (${h.user}'s)`),
      h.detail && el("div", { class: "sub" }, h.detail)),
    el("span", { class: "sub mono" }, fmtWhen(h.at)));
}

/** Open a booking's details by id, from the bookings already loaded. */
function openBooking(id) {
  const b = [...state.bookings, ...state.mine, ...state.pending].find((x) => x.id === id);
  if (b) showDetail(b);
  else toast(`Booking #${id} isn't in this week's view.`);
}

const statusLabel = (s) => ({ pending: "Waiting for approval", approved: "Approved", rejected: "Rejected",
  cancelled: "Cancelled", expired: "Expired" })[s] || s;

/** Buttons for a booking: cancel / end, extend, and (for admins) approve / reject / edit. */
function bookingActions(b, { inDialog = false } = {}) {
  const { me } = state;
  const now = nowSecs();
  const owner = b.user === me.user;
  const canManage = owner || me.admin;
  const wrap = el("div", { class: "item-actions" });

  if (me.admin && b.status === "pending") {
    wrap.append(el("button", { class: "btn ok small", type: "button", onclick: () => approve(b) }, "Approve"));
    wrap.append(rejectControl(b));
  }
  if (owner && b.status === "pending") {
    wrap.append(confirmButton("Withdraw",
      () => act(() => api("POST", `/api/bookings/${b.id}/cancel`), "Request withdrawn").then(() => inDialog && closeDialogs())));
  }
  if (canManage && b.status === "approved" && b.end > now) {
    const running = b.start <= now;
    wrap.append(confirmButton(running ? "End now" : "Cancel booking",
      () => act(() => api("POST", `/api/bookings/${b.id}/cancel`), running ? "Ended. The GPU is free." : "Booking cancelled")
        .then(() => inDialog && closeDialogs())));
    wrap.append(extendControl(b, inDialog));
  }
  if (me.admin && (b.status === "approved" || b.status === "pending") && b.end > now) {
    wrap.append(el("button", { class: "btn ghost small", type: "button", onclick: () => {
      closeDialogs();
      openBookingDialog({ gpus: b.gpus, start: b.start, end: b.end, booking: b });
    } }, "Edit"));
  }
  return wrap.childElementCount ? wrap : null;
}

function confirmButton(label, fn) {
  let armed = false;
  const btn = el("button", { class: "btn danger small", type: "button" }, label);
  btn.addEventListener("click", () => {
    if (!armed) {
      armed = true;
      btn.textContent = "Click again to confirm";
      setTimeout(() => { armed = false; btn.textContent = label; }, 3000);
      return;
    }
    fn().catch(() => {});
  });
  return btn;
}

function extendControl(b, inDialog) {
  const span = el("span", { class: "item-actions" });
  const open = el("button", { class: "btn ghost small", type: "button" }, bookInstantly() ? "Extend" : "Ask for more time");
  open.addEventListener("click", () => {
    const input = el("input", { type: "datetime-local", step: state.cfg.slot_minutes * 60, value: toLocalInput(b.end + 3600), "aria-label": "New end time" });
    const send = el("button", { class: "btn primary small", type: "button" }, "Send");
    send.addEventListener("click", () => act(
      () => api("POST", `/api/bookings/${b.id}/extend`, { end: fromLocalInput(input.value) }),
      (r) => (r.booking.status === "approved" ? "Extended" : "Extension requested"),
    ).then(() => inDialog && closeDialogs()).catch(() => {}));
    span.replaceChildren(input, send);
    input.focus();
  });
  span.append(open);
  return span;
}

function rejectControl(b) {
  const span = el("span", { class: "item-actions" });
  const open = el("button", { class: "btn danger small", type: "button" }, "Reject");
  open.addEventListener("click", () => {
    const input = el("input", { type: "text", placeholder: "Reason (the requester sees this)", "aria-label": "Reason for rejecting" });
    const send = el("button", { class: "btn danger small", type: "button" }, "Reject");
    const go = () => act(() => api("POST", `/api/admin/bookings/${b.id}/reject`, { reason: input.value }), "Request rejected")
      .then(closeDialogs).catch(() => {});
    send.addEventListener("click", go);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); go(); } });
    span.replaceChildren(input, send);
    input.focus();
  });
  span.append(open);
  return span;
}

async function approve(b) {
  await act(() => api("POST", `/api/admin/bookings/${b.id}/approve`, {}), (r) => {
    const n = r.auto_rejected.length;
    return `Approved #${b.id}${n ? `; ${n} overlapping request${n > 1 ? "s" : ""} rejected` : ""}`;
  }).then(closeDialogs).catch(() => {});
}

// ------------------------------------------------------------------ side lists

function bookingItem(b, extra = [], { showUser = false } = {}) {
  return el("article", { class: "item" },
    el("div", { class: "item-head" },
      el("span", { class: `item-title ${personClass(b.user)}` }, showUser && [el("span", { class: "dot" }), ` ${b.user} · `], fmtGpus(b.gpus)),
      el("span", { class: `chip ${b.status}` }, statusLabel(b.status))),
    el("div", { class: "item-when" }, fmtRange(b.start, b.end), el("span", { class: "dur" }, ` · ${fmtDur(b.end - b.start)} `),
      el("button", { class: "link", type: "button", onclick: () => showWeekOf(b.start) }, "Show")),
    b.note && el("div", { class: "item-note" }, b.note),
    b.admin_note && el("div", { class: "item-admin-note" }, b.decided_by && el("b", {}, `${b.decided_by}: `), b.admin_note),
    extra,
    bookingActions(b));
}

function renderMine() {
  const now = nowSecs();
  const order = { approved: 0, pending: 1, rejected: 2, expired: 3, cancelled: 4 };
  const list = [...state.mine].sort((a, b) =>
    (a.end <= now) - (b.end <= now) || order[a.status] - order[b.status] || a.start - b.start);
  $("#my-list").replaceChildren(...(list.length
    ? list.map((b) => bookingItem(b))
    : [el("div", { class: "empty" }, "No bookings yet. Drag along a GPU row on the timeline, or use New booking.")]));
}

function renderAdmin() {
  const n = state.pending.length;
  const asks = state.waiting.length + state.passwordRequests.length;
  $("#pending-count").textContent = n + asks;
  $("#admin-pill").classList.toggle("has", n + asks > 0);
  const reviewing = state.cfg.require_approval || n > 0;
  $("#admin-pill").hidden = !reviewing && !asks;
  $("#admin-title").textContent = reviewing ? "Requests to review" : "Admin";
  $("#pending-list").hidden = !reviewing;

  $("#pending-list").replaceChildren(...(n
    ? state.pending.map((p) => {
        const notes = [];
        if (p.conflicts.length) {
          notes.push(el("div", { class: "warn-box bad" },
            "Overlaps an approved booking, so it can't be approved as is:",
            el("ul", {}, p.conflicts.map((c) => el("li", {}, `${c.user}, ${fmtGpus(c.gpus)}, ${fmtRange(c.start, c.end)}`)))));
        }
        if (p.competitors.length) {
          notes.push(el("div", { class: "warn-box" },
            `Approving rejects ${p.competitors.length} overlapping request${p.competitors.length > 1 ? "s" : ""}:`,
            el("ul", {}, p.competitors.map((c) => el("li", {}, `${c.user}, ${fmtGpus(c.gpus)}, ${fmtRange(c.start, c.end)}`)))));
        }
        return bookingItem(p, notes, { showUser: true });
      })
    : [el("div", { class: "empty" }, "Nothing waiting. New requests show up here.")]));

  const thisWeek = secs(state.weekStart) === secs(startOfWeek(new Date()));
  $("#hours-title").textContent = thisWeek ? "Booked vs used this week" : `Booked vs used, week of ${fmtDay(secs(state.weekStart))}`;
  const { people, unbooked } = state.usage;
  const top = Math.max(1, ...people.map((h) => h.booked));
  $("#hours-list").replaceChildren(...people.map((h) => el("div", { class: "hours-row" },
    el("span", { class: personClass(h.user) }, el("span", { class: "dot" }), " ", h.user),
    el("div", { class: `meter two ${personClass(h.user)}`, title: `${h.booked}h booked, ${h.used}h used` },
      el("i", { class: "booked", style: `width:${(100 * h.booked) / top}%` }),
      el("i", { class: "used", style: `width:${(100 * Math.min(h.used, h.booked)) / top}%` })),
    el("span", { class: "mono" }, `${h.booked}h · used ${h.used}h`))));
  $("#unbooked-list").replaceChildren(...(unbooked.length
    ? unbooked.map((u) => el("div", { class: "row" },
        el("div", {}, el("b", {}, u.account),
          el("div", { class: "sub" }, u.names.length ? `Account of ${u.names.join(", ")}` : "Not in the booking list")),
        el("span", { class: "mono" }, `${u.hours}h`)))
    : [el("p", { class: "muted" }, "None this week.")]));

  $("#activity-list").replaceChildren(...(state.activity.length
    ? state.activity.map((h) => historyRow(h, { withBooking: true }))
    : [el("p", { class: "muted" }, "Nothing yet.")]));

  const userApi = (u, what, body) => api("POST", `/api/admin/users/${encodeURIComponent(u.name)}/${what}`, body);
  $("#waiting-wrap").hidden = !state.waiting.length;
  $("#waiting-list").replaceChildren(...state.waiting.map((u) => {
    const approve = el("button", { class: "btn primary small", type: "button" }, "Approve");
    approve.addEventListener("click", () => act(() => userApi(u, "approve"), `${u.name} can log in now`).catch(() => {}));
    return el("div", { class: "row" },
      el("div", {}, el("b", {}, u.name), el("div", { class: "sub" }, `Server account ${u.unix} · signed up ${fmtWhen(u.at)}`)),
      el("span", { class: "row-actions" }, approve,
        confirmButton("Delete", () => act(() => userApi(u, "delete"), `${u.name}'s sign-up deleted`))));
  }));

  $("#pwreq-wrap").hidden = !state.passwordRequests.length;
  $("#pwreq-list").replaceChildren(...state.passwordRequests.map((u) => {
    const approve = el("button", { class: "btn primary small", type: "button" }, "Approve");
    approve.addEventListener("click", () => act(() => userApi(u, "password-request/approve"),
      `${u.name}'s new password works now`).catch(() => {}));
    return el("div", { class: "row" },
      el("div", {}, el("b", {}, u.name), el("div", { class: "sub" }, `asked ${fmtWhen(u.at)}`)),
      el("span", { class: "row-actions" }, approve,
        confirmButton("Delete", () => act(() => userApi(u, "password-request/delete"), "Request deleted"))));
  }));

  $("#people-list").replaceChildren(...state.cfg.users.map((u) => {
    const me = u.name === state.me.user;
    let adminBtn = null;
    if (!me) {
      adminBtn = el("button", { class: "btn ghost small", type: "button" }, u.admin ? "Remove admin" : "Make admin");
      adminBtn.addEventListener("click", () => act(() => userApi(u, "admin", { admin: !u.admin }),
        u.admin ? `${u.name} is no longer an admin` : `${u.name} is now an admin`).catch(() => {}));
    }
    return el("div", { class: "row person" },
      el("div", {},
        el("div", { class: personClass(u.name) }, el("span", { class: "dot" }), " ", el("b", {}, u.name), u.admin && " (admin)"),
        el("div", { class: "sub" }, `${u.unix} · ${u.has_password ? "password set" : "hasn't logged in yet"}`)),
      !me && el("span", { class: "row-actions" },
        adminBtn,
        confirmButton("Delete", () => act(() => userApi(u, "delete"), `${u.name} deleted`))));
  }));
}

// ------------------------------------------------------------------ dialogs

function closeDialogs() {
  for (const id of ["#booking-dialog", "#detail-dialog", "#password-dialog"]) $(id).hidden = true;
}
document.addEventListener("click", (e) => {
  if (e.target.closest("[data-close]") || e.target.classList.contains("overlay") && e.target.id !== "login") closeDialogs();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDialogs(); });

// ------------------------------------------------------------------ boot

(async function boot() {
  state.cfg = await api("GET", "/api/config");
  state.me = await api("GET", "/api/me");
  if (state.me.user) enterApp(); else showLogin();
  // Refresh only while the tab is visible, and right away when someone comes back to it.
  setInterval(() => state.me.user && !document.hidden && refreshAll(), REFRESH_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && state.me.user) refreshAll(); });
  setInterval(() => state.status && renderNow(), 60_000);
})();
