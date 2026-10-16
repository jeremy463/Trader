"use strict";
const RM = matchMedia("(prefers-reduced-motion: reduce)").matches, NS = "http://www.w3.org/2000/svg";
const $ = id => document.getElementById(id), root = $("app");
let ME = null, CSRF = "", PAGE = "home", SYM = null, TF = 15, STOP_HERO = null, LAST = null, PUB = {mail: false}; const brand = () => PUB.brand || "APEX Trader";

function h(tag, attrs, ...kids) {
  const svg = tag.startsWith("svg:"), e = svg ? document.createElementNS(NS, tag.slice(4)) : document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === "i") e.style.setProperty("--i", v);
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "class" && !svg) e.className = v;
    else e.setAttribute(k, v);
  }
  for (const c of kids.flat(Infinity)) if (c != null && c !== false) e.append(c.nodeType ? c : document.createTextNode(String(c)));
  return e;
}
const ICON = {
  scan: "M12 3a9 9 0 1 0 9 9M12 7a5 5 0 1 0 5 5M12 12l7-7", plans: "M3 7h18v10H3zM3 11h18", account: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21a8 8 0 0 1 16 0",
  home: "M3 11l9-8 9 8M5 10v10h5v-6h4v6h5V10", calendar: "M4 6h16v14H4zM4 10h16M8 3v4M16 3v4", book: "M5 4h10a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3zM5 17a3 3 0 0 1 3-3h10", alerts: "M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9M13.7 21a2 2 0 0 1-3.4 0", admin: "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z", trade: "M13 2L4 14h7l-1 8 9-12h-7z", sun: "M12 4V2M12 22v-2M4 12H2M22 12h-2M6 6L4.6 4.6M19.4 19.4L18 18M6 18l-1.4 1.4M19.4 4.6L18 6M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z", moon: "M21 13A9 9 0 1 1 11 3a7 7 0 0 0 10 10z"
};
const icon = n => h("svg:svg", {viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "1.9", "stroke-linecap": "round", "stroke-linejoin": "round"}, h("svg:path", {d: ICON[n]}));
const money = (x, c) => (c || "") + " " + Number(x).toLocaleString(undefined, {maximumFractionDigits: 2});
const date = ts => ts ? new Date(ts * 1000).toLocaleDateString(undefined, {day: "numeric", month: "short", year: "numeric"}) : "-";
const fmt = (v, d = 5) => v == null ? "-" : Number(v).toFixed(d).replace(/0+$/, "").replace(/\.$/, "");

function toast(text, kind) {
  const t = h("div", {class: "toast " + (kind || "")}, text); $("toasts").append(t);
  setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 350); }, 4200);
}
function modal(build) {
  return new Promise(res => {
    const d = $("dlg"); d.replaceChildren(); const close = v => { res(v); if (d.open) d.close(); };
    d.append(...build(close)); d.addEventListener("close", () => res(null), {once: true}); d.showModal();
  });
}
const confirmBox = (title, text, ok = "Confirm", cls = "pri") => modal(close => [h("h3", {}, title), h("p", {class: "m"}, text), h("div", {class: "row"}, h("button", {class: "btn " + cls, onclick: () => close(true)}, ok), h("button", {class: "btn", onclick: () => close(false)}, "Cancel"))]);
function countUp(el, to, dec = 0, dur = 900) {
  if (RM || !isFinite(to)) { el.textContent = Number(to).toFixed(dec); return; }
  const t0 = performance.now(); const step = t => { const p = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - p, 3); el.textContent = (to * e).toFixed(dec); if (p < 1) requestAnimationFrame(step); }; requestAnimationFrame(step);
}
async function api(path, body) {
  const opt = body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": CSRF}, body: JSON.stringify(body)};
  const r = await fetch(path, opt); let d = {}; try { d = await r.json(); } catch (e) { /* non-json */ }
  if (!r.ok) { const err = new Error(d.error || "Request failed (" + r.status + ")"); err.status = r.status; err.data = d; throw err; }
  return d;
}
function setTheme(t) { document.documentElement.dataset.theme = t; localStorage.setItem("apex-theme", t); }
setTheme(localStorage.getItem("apex-theme") || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"));

/* ---------------- auth ---------------- */
function heroTicker(cv) {
  const ctx = cv.getContext("2d"); let stop = false, data = [], p = 100;
  for (let i = 0; i < 70; i++) { const o = p, c = o + (Math.random() - .5) * 3 + .15; data.push([o, Math.max(o, c) + Math.random(), Math.min(o, c) - Math.random(), c]); p = c; }
  function draw() {
    if (stop) return; const dpr = devicePixelRatio || 1, W = cv.clientWidth, H = cv.clientHeight; if (cv.width !== W * dpr) { cv.width = W * dpr; cv.height = H * dpr; }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, W, H);
    const lo = Math.min(...data.map(d => d[2])), hi = Math.max(...data.map(d => d[1])), cw = W / data.length;
    data.forEach((d, i) => { const y = v => H - 12 - (v - lo) / (hi - lo || 1) * (H - 24), up = d[3] >= d[0]; ctx.strokeStyle = ctx.fillStyle = up ? "#22c55e" : "#ef4444";
      ctx.beginPath(); ctx.moveTo(i * cw + cw / 2, y(d[1])); ctx.lineTo(i * cw + cw / 2, y(d[2])); ctx.stroke(); ctx.fillRect(i * cw + cw * .2, Math.min(y(d[0]), y(d[3])), cw * .6, Math.max(2, Math.abs(y(d[0]) - y(d[3])))); });
  }
  const tick = () => { if (stop) return; const o = p, c = o + (Math.random() - .48) * 3; data.shift(); data.push([o, Math.max(o, c) + Math.random(), Math.min(o, c) - Math.random(), c]); p = c; draw(); };
  draw(); const iv = RM ? null : setInterval(tick, 700); return () => { stop = true; if (iv) clearInterval(iv); };
}
function testRibbon() { return PUB.data_mode === "test" ? h("div", {class: "ribbon-test", role: "alert"}, "TEST DATA: results are NOT based on real market data. This server is not configured with a live data provider.") : null; }
function renderAuth(note) {
  if (STOP_HERO) STOP_HERO();
  const rtoken = new URLSearchParams(location.search).get("reset"); if (rtoken) history.replaceState(null, "", "/");
  let mode = rtoken ? "token" : "login"; const out = h("div", {class: "m"}), form = h("div", {class: "col"});
  const em = h("input", {type: "email", placeholder: "you@example.com", autocomplete: "username", "aria-label": "Email"}), pw = h("input", {type: "password", placeholder: "Password (12+ characters)", autocomplete: "current-password", "aria-label": "Password"});
  const totp = h("input", {placeholder: "6-digit authenticator code", inputmode: "numeric", maxlength: 6, class: "hide", "aria-label": "Authenticator code"}), tk = h("input", {placeholder: "Reset token", class: "hide"});
  if (rtoken) tk.value = rtoken;
  const agree = h("input", {type: "checkbox", id: "agree"}), terms = h("label", {class: "checkrow hide", for: "agree"}, agree, h("span", {}, "I understand trading is risky, this is analysis not advice, and I accept the ", h("a", {href: "/legal", target: "_blank", rel: "noopener"}, "terms and risk disclosure"), "."));
  const go = h("button", {class: "btn pri"}, "Log in"); const say = (t, ok) => { out.textContent = t; out.className = ok ? "L" : "S"; };
  const LABEL = {login: "Log in", signup: "Create account", forgot: "Email me a reset link", token: "Set new password"};
  const modes = [["login", "Log in"], ["signup", "Create account"]].concat(PUB.mail ? [["forgot", "Forgot"]] : []).concat([["token", "Have a token"]]);
  const apply = () => { em.classList.toggle("hide", mode === "token"); pw.classList.toggle("hide", mode === "forgot"); tk.classList.toggle("hide", mode !== "token"); terms.classList.toggle("hide", mode !== "signup"); if (mode !== "login") totp.classList.add("hide");
    pw.placeholder = mode === "token" ? "New password (12+)" : "Password (12+ characters)"; pw.autocomplete = mode === "login" ? "current-password" : "new-password"; go.textContent = LABEL[mode]; seg.querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.m === mode)); say(""); };
  const seg = h("div", {class: "seg", role: "tablist"}, modes.map(([k, l]) => h("button", {"data-m": k, class: k === mode ? "on" : "", onclick: () => { mode = k; apply(); }}, l)));
  go.addEventListener("click", async () => {
    go.disabled = true;
    try {
      if (mode === "signup") { await api("/api/register", {email: em.value, password: pw.value, accept_terms: agree.checked}); say(PUB.mail ? "Account created. Check your email to confirm it, then log in." : "Account created - you can log in now.", true); }
      else if (mode === "forgot") { await api("/api/forgot", {email: em.value}); say("If that account exists, a reset link is on its way.", true); }
      else if (mode === "token") { await api("/api/reset-password", {token: tk.value, password: pw.value}); say("Password updated. Log in.", true); mode = "login"; setTimeout(apply, 1200); }
      else { const r = await api("/api/login", {email: em.value, password: pw.value, totp: totp.value || undefined});
        if (r.need_totp) { totp.classList.remove("hide"); totp.focus(); say("Enter the code from your authenticator app.", true); } else { pw.value = ""; await boot(); } }
    } catch (e) { say(e.message + (e.data && e.data.retry_after ? " Try again in " + e.data.retry_after + "s." : "")); }
    go.disabled = false;
  });
  [em, pw, totp, tk].forEach(i => i.addEventListener("keydown", e => { if (e.key === "Enter") go.click(); }));
  const cv = h("canvas", {height: "150", "aria-hidden": "true"}); form.append(seg, em, tk, pw, totp, terms, go, out);
  root.replaceChildren(testRibbon(), h("div", {class: "auth"},
    h("section", {class: "hero"}, h("div", {class: "orb a"}), h("div", {class: "orb b"}),
      h("div", {class: "row enter", i: 0}, h("img", {src: "/static/favicon.svg", width: 34, height: 34, alt: ""}), h("b", {}, brand())),
      h("h1", {class: "enter", i: 1}, "Every angle on the chart. ", h("em", {}, "One clear verdict.")),
      h("p", {class: "enter", i: 2}, (PUB.methods || 51) + " independent analysis methods in " + (PUB.families || 16) + " families, an A/B/C setup checklist you can audit, and out-of-sample validation, so you see evidence instead of hype."),
      h("div", {class: "kpis enter", i: 3}, h("div", {}, h("b", {}, String(PUB.methods || 51)), h("span", {class: "m"}, "methods scanned")), h("div", {}, h("b", {}, String(PUB.families || 16)), h("span", {class: "m"}, "independent families")), h("div", {}, h("b", {}, "0"), h("span", {class: "m"}, "trades placed for you"))),
      h("div", {class: "ticker enter", i: 4}, cv), h("p", {class: "m"}, "Analysis tool, not financial advice. Trading carries a real risk of loss. ", h("a", {href: "/legal"}, "Terms and risk disclosure"))),
    h("section", {class: "formside"}, h("div", {class: "card authcard enter", i: 2}, h("h2", {}, "Welcome back"), note ? h("div", {class: "banner warn"}, note) : null, h("div", {class: "col"}, form)))));
  apply(); if (rtoken) say("Choose a new password for your account.", true); STOP_HERO = heroTicker(cv);
}
/* ---------------- shell ---------------- */
async function boot() {
  const params = new URLSearchParams(location.search); let note = null;
  try { PUB = await (await fetch("/api/public")).json(); } catch (e) { PUB = {mail: false}; }
  document.title = brand() + " · market analysis"; const vt = params.get("verify");
  if (vt) { try { await api("/api/verify-email", {token: vt}); note = "Email confirmed - thank you."; } catch (e) { note = e.message; } history.replaceState(null, "", "/"); }
  try { ME = await api("/api/me"); CSRF = ME.csrf; if (!SYM || !ME.symbols.includes(SYM)) SYM = ME.symbols[0] || null; renderShell(); if (note) toast(note, "ok"); } catch (e) { renderAuth(note); }
}
function renderShell() {
  if (STOP_HERO) { STOP_HERO(); STOP_HERO = null; } clearTickers();
  const sections = [["Trade", [["home", "Opportunities"], ["scan", "Scanner"], ["calendar", "Calendar & news"]]], ["Track", [["journal", "Journal"], ["alerts", "Alerts"]]], ["Account", [["plans", "Plans"], ["account", "Account"]]]];
  if (ME.role === "admin") sections.push(["Admin", [["admin", "Admin"]].concat(ME.trading ? [["trade", "Trading"]] : [])]);
  const pages = sections.flatMap(s => s[1]), IC = {home: "home", scan: "scan", calendar: "calendar", journal: "book", alerts: "alerts", plans: "plans", account: "account", admin: "admin", trade: "trade"};
  const navBtn = ([k, l]) => h("button", {class: "nav" + (k === PAGE ? " on" : ""), "aria-label": l, "aria-current": k === PAGE ? "page" : null, onclick: () => nav(k)}, icon(IC[k]), h("span", {}, l));
  const th = h("button", {class: "btn sm", "aria-label": "Toggle theme", onclick: () => { setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"); renderShell(); }}, icon(document.documentElement.dataset.theme === "dark" ? "sun" : "moon"));
  const main = h("main", {id: "main", class: "page"});
  const tier = h("span", {class: "badge " + ME.tier}, ME.tier_label + (ME.tier !== "free" && ME.role !== "admin" && ME.paid_until ? " · until " + date(ME.paid_until) : ""));
  root.replaceChildren(testRibbon(), h("div", {class: "shell"},
    h("aside", {class: "side"}, h("div", {class: "brand"}, h("img", {src: "/static/favicon.svg", alt: ""}), brand()), sections.map(([t, ps]) => [h("div", {class: "sideh"}, t), ps.map(navBtn)]),
      h("div", {class: "sidefoot"}, h("div", {class: "user"}, h("div", {}, ME.email), h("div", {}, tier), h("a", {class: "m", href: "/legal", target: "_blank", rel: "noopener"}, "Terms & risk"), PUB.support_email ? h("a", {class: "m", href: "mailto:" + PUB.support_email}, "Support: " + PUB.support_email) : null),
        h("div", {class: "row"}, th, h("button", {class: "btn sm", onclick: toggleTZ, title: "Show times in UTC or local time"}, TZ === "utc" ? "UTC" : "Local"), h("button", {class: "btn sm", onclick: openPalette, title: "Command palette"}, "Ctrl K")), h("button", {class: "btn sm", onclick: logout}, "Log out"))),
    main, h("nav", {class: "tabbar", "aria-label": "Primary"}, pages.map(navBtn))));
  const q = new URLSearchParams(location.search).get("payment");
  if (q) { const t = {credited: ["Payment received - your plan is active.", "ok"], already: ["Payment already applied.", "ok"], pending: ["Payment pending - it activates automatically once confirmed.", ""], failed: ["Payment failed - you were not charged.", "err"], mismatch: ["Payment could not be matched - contact support.", "err"], unverified: ["Could not confirm yet - it will activate automatically if it succeeded.", ""]}[q]; if (t) toast(t[0], t[1]); history.replaceState(null, "", "/"); }
  if (!ME.email_verified && ME.mail) main.append(h("div", {class: "banner warn"}, "Please confirm your email address (check your inbox). ", h("button", {class: "btn sm", onclick: async () => { try { await api("/api/resend-verification", {}); toast("Confirmation email sent", "ok"); } catch (e) { toast(e.message, "err"); } }}, "Resend")));
  ({home: pageHome, scan: pageScan, calendar: pageCalendar, journal: pageJournal, alerts: pageAlerts, plans: pagePlans, account: pageAccount, admin: pageAdmin, trade: pageTrade})[PAGE](main);
}
async function logout() { await api("/api/logout", {}); ME = null; LAST = null; renderAuth(); }
const top = (title, sub, ...right) => h("div", {class: "top enter", i: 0}, h("div", {class: "grow"}, h("h2", {}, title), sub ? h("div", {class: "m"}, sub) : null), right);

/* ---------------- chart ---------------- */
function cssv(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }
function chartView(ch, plan) {
  const wrap = h("div", {class: "chartwrap"}), cv = h("canvas", {height: "360", role: "img", "aria-label": "Candlestick chart with moving averages and trade plan"}), tip = h("div", {class: "tip"}); wrap.append(cv, tip);
  let hover = -1, prog = RM ? 1 : 0, raf = 0;
  function draw() {
    const dpr = devicePixelRatio || 1, W = cv.clientWidth, H = 360; if (!W) return; if (cv.width !== Math.round(W * dpr)) { cv.width = Math.round(W * dpr); cv.height = H * dpr; }
    const x = cv.getContext("2d"); x.setTransform(dpr, 0, 0, dpr, 0, 0); x.clearRect(0, 0, W, H);
    const n = ch.t.length, pl = 10, pr = 70, pt = 12, pb = 24, pw = W - pl - pr, ph = H - pt - pb;
    const lv = plan ? [plan.entry, plan.stop, plan.tp1, plan.tp2, plan.tp3] : []; let lo = Math.min(...ch.l, ...lv), hi = Math.max(...ch.h, ...lv); const pad = (hi - lo) * .05; lo -= pad; hi += pad;
    const Y = v => pt + (hi - v) / (hi - lo) * ph, cw = pw / n, X = i => pl + i * cw + cw / 2;
    const g = cssv("--g"), r = cssv("--r"), mut = cssv("--mut"), bd = cssv("--bd");
    x.font = "11px " + cssv("--mono"); x.lineWidth = 1;
    for (let k = 0; k <= 5; k++) { const v = lo + (hi - lo) * k / 5, y = Y(v); x.strokeStyle = bd; x.globalAlpha = .6; x.beginPath(); x.moveTo(pl, y); x.lineTo(W - pr, y); x.stroke(); x.globalAlpha = 1; x.fillStyle = mut; x.fillText(fmt(v, v > 50 ? 3 : 5), W - pr + 6, y + 4); }
    const upto = Math.ceil(prog * n);
    const line = (arr, col) => { x.strokeStyle = col; x.lineWidth = 1.6; x.beginPath(); let s = false; for (let i = 0; i < upto; i++) { if (arr[i] == null) continue; s ? x.lineTo(X(i), Y(arr[i])) : (x.moveTo(X(i), Y(arr[i])), s = true); } x.stroke(); };
    for (let i = 0; i < upto; i++) { const up = ch.c[i] >= ch.o[i], col = up ? g : r; x.strokeStyle = x.fillStyle = col; x.lineWidth = 1; x.beginPath(); x.moveTo(X(i), Y(ch.h[i])); x.lineTo(X(i), Y(ch.l[i])); x.stroke(); x.fillRect(X(i) - cw * .32, Math.min(Y(ch.o[i]), Y(ch.c[i])), cw * .64, Math.max(1.5, Math.abs(Y(ch.o[i]) - Y(ch.c[i])))); }
    line(ch.e21, cssv("--acc2")); line(ch.e50, "#f59e0b");
    if (plan && prog >= 1) {
      x.globalAlpha = .1; x.fillStyle = g; const long = plan.side === "BUY"; x.fillRect(pl, Math.min(Y(plan.entry), Y(plan.tp2)), pw, Math.abs(Y(plan.entry) - Y(plan.tp2))); x.fillStyle = r; x.fillRect(pl, Math.min(Y(plan.entry), Y(plan.stop)), pw, Math.abs(Y(plan.entry) - Y(plan.stop))); x.globalAlpha = 1;
      [["Entry", plan.entry, "#818cf8", [4, 4]], ["Stop", plan.stop, r, []], ["TP1", plan.tp1, g, [2, 3]], ["TP2", plan.tp2, g, []], ["TP3", plan.tp3, g, [2, 3]]].forEach(([nm, v, col, dash]) => {
        x.setLineDash(dash); x.strokeStyle = col; x.lineWidth = 1.4; x.beginPath(); x.moveTo(pl, Y(v)); x.lineTo(W - pr, Y(v)); x.stroke(); x.setLineDash([]); x.fillStyle = col; x.fillRect(W - pr + 2, Y(v) - 8, 64, 16); x.fillStyle = "#fff"; x.fillText(nm + " " + fmt(v, v > 50 ? 2 : 4), W - pr + 5, Y(v) + 4); });
    }
    if (hover >= 0 && hover < n && prog >= 1) { x.strokeStyle = mut; x.setLineDash([3, 3]); x.beginPath(); x.moveTo(X(hover), pt); x.lineTo(X(hover), H - pb); x.stroke(); x.setLineDash([]); }
    x.fillStyle = mut; [0, Math.floor(n / 2), n - 1].forEach(i => x.fillText(new Date(ch.t[i] * 1000).toLocaleString(undefined, {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"}), Math.min(W - pr - 90, X(i) - 40), H - 6));
  }
  function anim() { const t0 = performance.now(); const step = t => { prog = Math.min(1, (t - t0) / 1100); draw(); if (prog < 1) raf = requestAnimationFrame(step); }; raf = requestAnimationFrame(step); }
  cv.addEventListener("mousemove", e => { const rc = cv.getBoundingClientRect(), n = ch.t.length, i = Math.floor((e.clientX - rc.left - 10) / ((rc.width - 80) / n)); if (i < 0 || i >= n) { tip.style.opacity = 0; return; } hover = i; draw();
    tip.textContent = new Date(ch.t[i] * 1000).toLocaleString() + "  O " + fmt(ch.o[i]) + "  H " + fmt(ch.h[i]) + "  L " + fmt(ch.l[i]) + "  C " + fmt(ch.c[i]); tip.style.opacity = 1; tip.style.left = Math.min(rc.width - 330, Math.max(4, e.clientX - rc.left - 120)) + "px"; tip.style.top = "6px"; });
  cv.addEventListener("mouseleave", () => { hover = -1; tip.style.opacity = 0; draw(); });
  new ResizeObserver(draw).observe(wrap); requestAnimationFrame(() => RM ? draw() : anim());
  return wrap;
}

/* ---------------- scanner ---------------- */
const TFS = [[5, "5m"], [15, "15m"], [30, "30m"], [60, "1h"], [240, "4h"]];
function pageScan(main) {
  const res = h("div", {id: "res", class: "col"});
  const usage = h("div", {class: "meter", role: "progressbar", "aria-label": "Daily scans used"}, h("i", {})); const ul = h("span", {class: "m num"});
  const setUsage = () => { const lim = ME.scans_limit; ul.textContent = lim ? ME.scans_used + " / " + lim + " scans today" : "unlimited scans"; usage.firstChild.style.width = lim ? Math.min(100, ME.scans_used / lim * 100) + "%" : "6%"; };
  const sym = h("select", {"aria-label": "Symbol", onchange: e => { SYM = e.target.value; }}, ME.symbols.map(s => h("option", {value: s, selected: s === SYM}, s)));
  const seg = h("div", {class: "seg", role: "group", "aria-label": "Timeframe"}, TFS.map(([v, l]) => h("button", {class: v === TF ? "on" : "", onclick: e => { TF = v; seg.querySelectorAll("button").forEach(b => b.classList.toggle("on", b === e.currentTarget)); }}, l)));
  const btn = h("button", {class: "btn pri"}, "Scan chart");
  btn.addEventListener("click", async () => {
    if (!SYM) { toast("No market data configured yet.", "err"); return; }
    btn.disabled = true; btn.classList.add("busy"); skeleton(res, ME.methods_total);
    try { const d = await api("/api/scan", {symbol: SYM, tf: TF}); ME.scans_used++; setUsage(); LAST = d; renderScan(res, d); }
    catch (e) { res.replaceChildren(h("div", {class: "banner err"}, e.message, e.data && e.data.upgrade ? h("button", {class: "btn sm", onclick: () => { PAGE = "plans"; renderShell(); }}, "See plans") : null)); }
    btn.disabled = false; btn.classList.remove("busy");
  });
  setUsage();
  main.append(top("Scanner", "Run every method on the chart and see how many pass.", ME.features.edge ? h("span", {class: "badge elite"}, "Validation unlocked") : null),
    h("div", {class: "card enter col", i: 1}, h("div", {class: "row"}, sym, seg, btn, h("div", {class: "grow"}), h("div", {class: "col"}, ul, usage)), profileBar()), res);
  if (AUTORUN) { AUTORUN = false; setTimeout(() => btn.click(), 60); } else if (LAST && LAST.symbol === SYM) renderScan(res, LAST);
  else res.append(h("div", {class: "card enter", i: 2}, h("h3", {}, "Ready when you are"), h("p", {class: "m"}, "Pick a symbol and timeframe, then press Scan. You'll get a verdict, a chart with the trade plan, and a per-method breakdown with the reasoning behind each signal.")));
}
function skeleton(res, n) {
  res.replaceChildren(h("div", {class: "grid g2"}, h("div", {class: "card"}, h("div", {class: "skel"}), h("div", {class: "skel"}), h("div", {class: "skel"})), h("div", {class: "card col"}, h("div", {class: "skel"}), h("div", {class: "skel"}), h("div", {class: "skel"}))),
    h("div", {class: "card"}, h("div", {class: "chips"}, Array.from({length: n}, (_, i) => h("span", {class: "chip n", i: i % 30}, "· · ·")))));
}
function renderScan(res, d) {
  const sig = d.decision === "BUY" || d.decision === "SELL" ? d.decision : (d.consensus ? d.consensus : "NO SIGNAL"), cnt = d.counts, tot = d.families_long + d.families_short || 1;
  const big = h("span", {class: "num"}, "0"); countUp(big, d.passed);
  const ev = d.evidence ? h("div", {class: "banner"}, h("b", {}, "Historical evidence on your data"), h("div", {class: "m"}, `${d.evidence.n} out-of-sample signals · mean ${d.evidence.mean_R >= 0 ? "+" : ""}${d.evidence.mean_R}R · hit rate ${Math.round(d.evidence.hit_rate * 100)}% (95% CI ${Math.round(d.evidence.hit_ci[0] * 100)}-${Math.round(d.evidence.hit_ci[1] * 100)}%)`), h("span", {class: "badge"}, d.evidence.verdict))
    : h("div", {class: "banner m"}, d.plan !== undefined && ME.features.edge ? "No validation run yet - run walk-forward below to see how this signal type actually performed." : "No validation evidence available yet. A signal is a hypothesis, not a promise.");
  const rd = d.regime_detail || {};
  const verdict = h("div", {class: "card col enter", i: 1}, h("div", {class: "row"}, h("span", {class: "pill " + sig.split(" ")[0]}, sig), h("span", {class: "m num"}, d.symbol), h("span", {class: "m"}, d.classification || "")),
    h("div", {}, h("div", {class: "big"}, big, h("span", {class: "m"}, " / " + d.scanned)), h("div", {class: "m"}, "methods passed")),
    h("div", {class: "num"}, h("span", {class: "L"}, "LONG " + cnt.long), "  ·  ", h("span", {class: "S"}, "SHORT " + cnt.short), "  ·  ", h("span", {class: "m"}, "neutral " + cnt.neutral + " · n/a " + cnt.na)),
    h("div", {}, h("div", {class: "bal", role: "img", "aria-label": "Independent families long versus short"}, h("i", {class: "l"}), h("i", {class: "s"})), h("div", {class: "row m"}, h("span", {}, "Independent families: " + d.families_long + " long"), h("span", {class: "grow"}), h("span", {}, d.families_short + " short"))),
    h("div", {class: "chips"}, rd.trending ? h("span", {class: "badge"}, "Trending") : null, rd.ranging ? h("span", {class: "badge"}, "Ranging") : null, rd.adx != null ? h("span", {class: "badge num"}, "ADX " + rd.adx) : null, rd.chop != null ? h("span", {class: "badge num"}, "CHOP " + rd.chop) : null, (d.regime || []).map(r => h("span", {class: "badge"}, r))),
    d.consensus ? h("div", {class: "L"}, "Consensus reached: " + d.consensus) : h("div", {class: "m"}, h("b", {}, "No consensus. "), d.why_not.join(" · ")), ev, alertControl(d),
    d.upgrade_hint ? h("div", {class: "banner warn"}, d.upgrade_hint, " ", h("button", {class: "btn sm", onclick: () => { PAGE = "plans"; renderShell(); }}, "Upgrade")) : null,
    h("div", {class: "m"}, d.source + " · " + d.data_ts.replace("T", " ").slice(0, 16) + " UTC · informational, not financial advice"));
  setTimeout(() => { const l = verdict.querySelector(".bal .l"), s = verdict.querySelector(".bal .s"); l.style.width = d.families_long / tot * 100 + "%"; s.style.width = d.families_short / tot * 100 + "%"; }, 80);
  const chart = h("div", {class: "card enter", i: 0}, h("div", {class: "row"}, h("h3", {class: "grow"}, d.symbol + " · " + (TFS.find(t => t[0] === d.tf) || [0, d.tf + "m"])[1]), h("span", {class: "badge"}, "● EMA21"), h("span", {class: "badge Y"}, "● EMA50")), chartView(d.chart, d.plan));
  const plan = d.plan ? h("div", {class: "card enter", i: 2}, h("h3", {}, d.plan.side + " plan"), h("div", {class: "ladder"}, ladder(d.plan)),
    h("div", {class: "kv"}, h("span", {class: "m"}, "Net reward:risk (TP2)"), h("b", {class: "num"}, d.plan.net_rr)), h("div", {class: "kv"}, h("span", {class: "m"}, "Size for your account"), h("b", {class: "num"}, d.plan.size != null ? d.plan.size + " lots · risk " + d.plan.risk_amount : (d.plan.sizing_note || "-"))),
    h("div", {class: "m"}, "Management idea: take ~50% at TP1, move the stop to breakeven, let the rest run to TP2. Targets are never assumed to be reached."))
    : (ME.features.plan ? h("div", {class: "card enter", i: 2}, h("h3", {}, "No trade plan"), h("p", {class: "m"}, "A plan is only produced when consensus, risk:reward and every filter agree. Waiting is a valid decision.")) : h("div", {class: "card enter", i: 2}, h("h3", {}, "Trade plan"), h("p", {class: "m"}, "Entry, stop, three targets and position sizing unlock on Pro."), h("button", {class: "btn pri", onclick: () => { PAGE = "plans"; renderShell(); }}, "See plans")));
  const detail = h("div", {class: "banner m"}, "Tap any method to see why it passed or failed."); const fams = {}; d.methods.forEach(m => (fams[m.family] = fams[m.family] || []).push(m));
  let k = 0; const heat = h("div", {class: "card enter", i: 3}, h("h3", {}, "Method heatmap · grouped by independent family"), detail, Object.entries(fams).map(([f, ms]) => h("div", {class: "fam"}, h("div", {class: "nm"}, f.replace("_", " "), h("span", {class: "badge"}, ms.filter(m => m.passed).length + "/" + ms.length)),
    h("div", {class: "chips"}, ms.map(m => h("button", {class: "chip " + (m.locked ? "k" : !m.applicable ? "n" : m.passed ? (m.dir > 0 ? "l" : "s") : "n"), i: k++, title: m.name, onclick: () => { detail.className = "banner"; detail.replaceChildren(h("b", {}, m.name), h("span", {class: "m"}, " · " + m.family), h("div", {}, m.locked ? "Locked on your plan." : (m.passed ? (m.dir > 0 ? "LONG" : "SHORT") + " · strength " + m.strength.toFixed(2) + " · " : "") + m.detail)); }}, m.locked ? "🔒 " : m.passed ? (m.dir > 0 ? "▲ " : "▼ ") : "", m.name))))));
  const kids = [h("div", {class: "grid g2"}, chart, verdict), timingCard(d.timing, d.upcoming), checklistCard(d.grade, 4), plan, newsCard(d.news, d.news_bias), heat];
  if (d.grade && d.grade.grade === "A") burst();
  if (d.report) kids.push(h("details", {class: "enter", i: 4}, h("summary", {}, "Full analysis report"), h("pre", {}, d.report)));
  if (ME.features.edge) kids.push(validationCard());
  res.replaceChildren(...kids);
}
function ladder(p) {
  const long = p.side === "BUY", rows = [["TP3", p.tp3, "tp"], ["TP2", p.tp2, "tp"], ["TP1", p.tp1, "tp"], ["Entry", p.entry, "en"], ["Stop", p.stop, "sl"]], risk = Math.abs(p.entry - p.stop) || 1;
  return rows.map(([n, v, c], i) => h("div", {class: "rung " + c, i}, h("b", {}, n), h("span", {class: "num"}, fmt(v, v > 50 ? 3 : 5)), h("span", {class: "r num"}, n === "Entry" ? "" : (n === "Stop" ? "-1R" : "+" + (Math.abs(v - p.entry) / risk).toFixed(2) + "R"))));
}
function validationCard() {
  const out = h("div", {}), bar = h("div", {class: "meter hide"}, h("i", {}));
  const run = (path, label) => async ev => {
    const b = ev.currentTarget; b.disabled = true; bar.classList.remove("hide"); bar.firstChild.style.width = "8%"; const iv = setInterval(() => { const w = parseFloat(bar.firstChild.style.width); bar.firstChild.style.width = Math.min(92, w + 6) + "%"; }, 900);
    try { const d = await api(path, {symbol: SYM, tf: TF}); bar.firstChild.style.width = "100%";
      if (d.verdict) out.replaceChildren(h("div", {class: "banner " + (d.verdict === "ROBUST EDGE" ? "ok" : "warn")}, h("b", {}, d.verdict), h("div", {class: "num"}, `${d.oos_trades} unseen trades · mean ${d.mean_R}R (95% CI ${d.mean_R_ci95[0]} to ${d.mean_R_ci95[1]}) · hit ${Math.round(d.hit_rate * 100)}% · positive folds ${d.positive_folds}`), h("div", {class: "m"}, d.note)));
      else if (d.rows[0] && d.rows[0].profile) out.replaceChildren(compareTable(d));
      else out.replaceChildren(h("div", {class: "m"}, d.note), h("table", {}, h("tr", {}, ["Method", "OOS n", "Mean R", "t", "Verdict"].map(x => h("th", {}, x))), d.rows.map(r => h("tr", {class: "rowh"}, h("td", {}, r.method), h("td", {class: "num"}, r.oos_n), h("td", {class: "num"}, r.oos_mean_r), h("td", {class: "num"}, r.t), h("td", {}, r.verdict)))));
    } catch (e) { toast(e.message, "err"); } clearInterval(iv); setTimeout(() => bar.classList.add("hide"), 600); b.disabled = false;
  };
  return h("div", {class: "card enter", i: 5}, h("h3", {}, "Validation (Elite)"), h("p", {class: "m"}, "Walk-forward picks methods from each training window only, then judges the consensus on unseen data. Confidence intervals show how much luck could explain."),
    h("div", {class: "row"}, h("button", {class: "btn pri", onclick: run("/api/validate")}, "Run walk-forward validation"), h("button", {class: "btn", onclick: run("/api/edge")}, "Per-method edge table"), h("button", {class: "btn", onclick: run("/api/profiles-compare")}, "Compare selectivity profiles")), bar, out);
}

function profileBar() {
  if (!ME.features.plan) return null;
  const desc = h("div", {class: "desc"}, (ME.profiles.find(p => p.name === ME.profile) || {}).desc || "");
  const seg = h("div", {class: "seg", role: "group", "aria-label": "Selectivity profile"}, ME.profiles.map(p => h("button", {class: p.name === ME.profile ? "on" : "", title: p.desc, onclick: async ev => { const b = ev.currentTarget;
    try { await api("/api/risk", {equity: ME.equity, risk_pct: ME.risk_pct, profile: p.name}); ME.profile = p.name; seg.querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b)); desc.textContent = p.desc; toast("Profile set to " + p.label, "ok"); } catch (e) { toast(e.message, "err"); } }}, p.label)));
  return h("div", {class: "col"}, h("div", {class: "row"}, h("span", {class: "m"}, "Selectivity"), seg), desc);
}
function alertControl(d) {
  if (!ME.alerts_limit) return null;
  const sel = h("select", {"aria-label": "Minimum grade"}, [["A", "Grade A only"], ["B", "Grade B or better"]].map(([v, l]) => h("option", {value: v}, l)));
  return h("div", {class: "row"}, sel, h("button", {class: "btn sm", onclick: async () => { try { await api("/api/alerts", {symbol: d.symbol, tf: d.tf, min_grade: sel.value}); toast("Alert created - we will email you when a setup qualifies.", "ok"); } catch (e) { toast(e.message, "err"); } }}, "Alert me"));
}
function checklistCard(g, i) {
  if (!g || !g.grade) return null;
  return h("div", {class: "card enter", i}, h("div", {class: "row"}, h("div", {class: "grade g" + g.grade, "aria-label": "Setup grade " + g.grade}, g.grade),
    h("div", {class: "grow"}, h("h3", {}, "Setup checklist"), h("div", {class: "m"}, g.passed + " of " + g.total + " criteria met" + (g.grade === "A" ? " - every applicable criterion passed" : " - see what failed below")))),
    h("ul", {class: "checks"}, g.items.map((it, k) => h("li", {class: it.ok === true ? "ok" : it.ok === false ? "bad" : "unk", i: k}, h("span", {class: "ic"}, it.ok === true ? "✓" : it.ok === false ? "✕" : "–"),
      h("div", {}, h("b", {}, it.label + (it.critical && it.ok === false ? " (critical)" : "")), h("div", {class: "m"}, it.detail))))),
    h("p", {class: "m"}, "The grade counts explicit, auditable criteria. It is not a probability of profit and does not predict the next candle."));
}
function compareTable(d) {
  return h("div", {}, h("p", {class: "m"}, d.note), h("table", {}, h("tr", {}, ["Profile", "Signals", "OOS n", "OOS mean R", "OOS hit rate (95% CI)", "Verdict"].map(x => h("th", {}, x))),
    d.rows.map(r => h("tr", {class: "rowh"}, h("td", {}, r.profile), h("td", {class: "num"}, r.signals), h("td", {class: "num"}, r.oos_n), h("td", {class: "num"}, r.oos_mean_R), h("td", {class: "num"}, Math.round(r.oos_hit * 100) + "% (" + Math.round(r.oos_hit_ci[0] * 100) + "-" + Math.round(r.oos_hit_ci[1] * 100) + ")"), h("td", {}, r.verdict)))));
}
async function pageAlerts(main) {
  main.append(top("Email alerts", "Get an email only when a setup meets your grade bar. Nothing is traded for you."));
  if (!ME.alerts_limit) { main.append(h("div", {class: "card enter", i: 1}, h("h3", {}, "Alerts are a Pro feature"), h("p", {class: "m"}, "Pro includes 3 alerts and Elite 10. Alerts fire at most once per new candle and are capped per day."), h("button", {class: "btn pri", onclick: () => { PAGE = "plans"; renderShell(); }}, "See plans"))); return; }
  const d = await api("/api/alerts"); const sym = h("select", {"aria-label": "Symbol"}, ME.symbols.map(s => h("option", {value: s}, s))), tf = h("select", {"aria-label": "Timeframe"}, TFS.map(([v, l]) => h("option", {value: v, selected: v === 15}, l))), gr = h("select", {"aria-label": "Minimum grade"}, [["A", "Grade A only"], ["B", "Grade B or better"]].map(([v, l]) => h("option", {value: v}, l)));
  const rows = d.alerts.map(a => h("tr", {class: "rowh"}, h("td", {}, a.symbol), h("td", {}, (TFS.find(t => t[0] === a.tf) || [0, a.tf + "m"])[1]), h("td", {}, "Grade " + a.min_grade + (a.min_grade === "B" ? "+" : "")), h("td", {class: a.active ? "L" : "m"}, a.active ? "active" : "paused"), h("td", {class: "m"}, a.last_sent ? date(a.last_sent) : "never"),
    h("td", {}, h("div", {class: "row"}, h("button", {class: "btn sm", onclick: async () => { await api("/api/alerts/toggle", {id: a.id, active: !a.active}); renderShell(); }}, a.active ? "Pause" : "Resume"), h("button", {class: "btn sm no", onclick: async () => { await api("/api/alerts/delete", {id: a.id}); renderShell(); }}, "Delete")))));
  main.append(!d.mail ? h("div", {class: "banner warn"}, "Email delivery is not configured on this server yet, so alerts cannot be sent.") : null, !d.verified ? h("div", {class: "banner warn"}, "Confirm your email address to create alerts.") : null,
    h("div", {class: "card enter", i: 1}, h("h3", {}, "New alert (" + d.alerts.length + " of " + d.limit + " used)"), h("div", {class: "row"}, sym, tf, gr, h("button", {class: "btn pri", onclick: async () => { try { await api("/api/alerts", {symbol: sym.value, tf: +tf.value, min_grade: gr.value}); toast("Alert created", "ok"); renderShell(); } catch (e) { toast(e.message, "err"); } }}, "Create alert")),
      h("p", {class: "m"}, "We email you on a new candle when the consensus, risk:reward and checklist meet your grade. Alerts are analysis, not advice.")),
    h("div", {class: "card enter", i: 2}, h("h3", {}, "Your alerts"), rows.length ? h("table", {}, h("tr", {}, ["Symbol", "Timeframe", "Minimum grade", "Status", "Last sent", ""].map(x => h("th", {}, x))), rows) : h("div", {class: "m"}, "No alerts yet.")));
}
function eventsCard(ev) {
  const ta = h("textarea", {placeholder: "time_utc,currency,impact,name\n2026-10-09T12:30:00Z,USD,high,Non-Farm Payrolls", "aria-label": "Calendar CSV"});
  const info = h("div", {class: "m"}, ev.events.length ? ev.events.length + " upcoming events loaded. Next: " + ev.events[0].time_utc + " " + ev.events[0].currency + " " + ev.events[0].name : "No calendar loaded - news-blackout checks are inactive.");
  return h("div", {class: "card enter col", i: 8}, h("h3", {}, "Economic calendar (news blackout)"), info, ta, h("div", {class: "row"}, h("button", {class: "btn pri", onclick: async () => { try { const r = await api("/api/admin/events", {csv: ta.value}); toast("Saved " + r.saved + " events" + (r.errors.length ? " (" + r.errors.length + " rows skipped)" : ""), "ok"); renderShell(); } catch (e) { toast(e.message, "err"); } }}, "Replace calendar"),
    h("span", {class: "m"}, "High-impact events block entries 30 min before and 15 min after, for the currencies involved.")));
}

/* ---------------- plans ---------------- */
function pagePlans(main) {
  const feats = t => [[`${t.scans.toLocaleString()} scans per day`, 1], [t.all_methods ? `All ${ME.methods_total} analysis methods` : `${ME.methods_free} core methods`, 1], [t.plan ? "Entry, stop, 3 targets and position sizing" : "Signal direction only", 1], [t.plan ? "A/B/C setup checklist and 3 selectivity profiles" : "Chart and method breakdown", 1], [t.plan ? "Opportunity radar, timing verdicts and trade journal" : "Session and news timing, basic journal", 1], [t.alerts ? `${t.alerts} email alerts for A/B setups` : "Email alerts", t.alerts > 0], [t.edge ? "Walk-forward validation and profile comparison" : "Walk-forward validation", t.edge]];
  const cards = ME.tiers.map((t, i) => { const cur = ME.stored_tier === t.name && ME.tier !== "free" || (t.name === "free" && ME.tier === "free");
    return h("div", {class: "card pcard hov enter" + (t.name === "pro" ? " pop" : ""), i: i + 1}, t.name === "pro" ? h("div", {class: "ribbon"}, "POPULAR") : null, h("h3", {}, t.label), h("div", {class: "price"}, t.price ? money(t.price, t.currency) : "Free", h("small", {}, t.price ? " / 30 days" : "")),
      h("ul", {class: "feat"}, feats(t).map(([f, ok]) => h("li", {class: ok ? "" : "no"}, f))),
      t.name === "free" ? h("span", {class: "badge"}, cur ? "Current plan" : "Included") : h("button", {class: "btn " + (t.name === "pro" ? "pri" : ""), disabled: !ME.billing || ME.role === "admin", onclick: async ev => { ev.currentTarget.disabled = true; try { const d = await api("/api/checkout", {tier: t.name}); location.href = d.url; } catch (e) { toast(e.message, "err"); ev.currentTarget.disabled = false; } }}, cur ? "Renew / extend" : "Upgrade to " + t.label)); });
  const pay = h("div", {class: "card enter", i: 5}, h("h3", {}, "Payment history"), h("div", {class: "skel"}));
  main.append(top("Plans & billing", "30-day passes. Upgrades credit your unused time pro-rata."), ME.billing ? null : h("div", {class: "banner warn"}, "Online payments are not enabled on this server yet."), h("div", {class: "grid g3"}, cards),
    h("p", {class: "m"}, "Payments are processed securely by Paystack. We never see or store your card details. Past performance of any analysis method does not guarantee future results."), pay);
  api("/api/payments").then(d => pay.replaceChildren(h("h3", {}, "Payment history"), d.payments.length ? h("table", {}, h("tr", {}, ["Date", "Plan", "Amount", "Status", "Reference"].map(x => h("th", {}, x))), d.payments.map(p => h("tr", {class: "rowh"}, h("td", {}, date(p.created)), h("td", {}, p.tier), h("td", {class: "num"}, money(p.amount, p.currency)), h("td", {class: p.status === "success" ? "L" : p.status === "pending" ? "m" : "S"}, p.status), h("td", {class: "m mono"}, p.reference)))) : h("div", {class: "m"}, "No payments yet."))).catch(() => {});
}

/* ---------------- account ---------------- */
function pageAccount(main) {
  const eq = h("input", {type: "number", value: ME.equity, min: 100, "aria-label": "Account equity"}), rk = h("input", {type: "number", value: ME.risk_pct, step: "0.05", min: 0.05, max: 2, "aria-label": "Risk percent"});
  const op = h("input", {type: "password", placeholder: "Current password", autocomplete: "current-password"}), np = h("input", {type: "password", placeholder: "New password (12+)", autocomplete: "new-password"}), dp = h("input", {type: "password", placeholder: "Password to confirm", autocomplete: "current-password"});
  main.append(top("Account", ME.email),
    h("div", {class: "grid g3"},
      h("div", {class: "card enter", i: 1}, h("h3", {}, "Position sizing"), h("p", {class: "m"}, "Used to size your trade plans (Pro and above). Risk is capped at 2% per trade."), h("div", {class: "col"}, h("label", {class: "m"}, "Account equity"), eq, h("label", {class: "m"}, "Risk per trade (%)"), rk,
        h("button", {class: "btn pri", onclick: async () => { try { await api("/api/risk", {equity: +eq.value, risk_pct: +rk.value}); ME.equity = +eq.value; ME.risk_pct = +rk.value; toast("Saved", "ok"); } catch (e) { toast(e.message, "err"); } }}, "Save"))),
      h("div", {class: "card enter", i: 2}, h("h3", {}, "Change password"), h("div", {class: "col"}, op, np, h("button", {class: "btn pri", onclick: async () => { try { await api("/api/password", {old: op.value, new: np.value}); op.value = np.value = ""; toast("Password updated; other devices logged out.", "ok"); } catch (e) { toast(e.message, "err"); } }}, "Update password"))),
      sessionsCard(),
      h("div", {class: "card enter col", i: 3}, h("h3", {}, "Your data"), h("p", {class: "m"}, "Download everything we hold about you (account, watchlist, alerts, journal, payments, usage)."), h("a", {class: "btn", href: "/api/account/export", download: "my-data.json"}, "Download my data")),
      h("div", {class: "card enter", i: 3}, h("h3", {}, "Delete account"), h("p", {class: "m"}, "Removes your identity. Payment records are retained for accounting."), h("div", {class: "col"}, dp, h("button", {class: "btn no", onclick: async () => { if (!await confirmBox("Delete account?", "This cannot be undone.", "Delete", "no")) return; try { await api("/api/delete-account", {password: dp.value}); ME = null; renderAuth("Account deleted."); } catch (e) { toast(e.message, "err"); } }}, "Delete my account")))));
}

/* ---------------- admin ---------------- */
async function pageAdmin(main) {
  main.append(top("Admin console", "Users, billing and audit"), h("div", {class: "skel"}));
  const [st, u, p, a, ev, fd, rd] = await Promise.all([api("/api/admin/stats"), api("/api/admin/users"), api("/api/admin/payments"), api("/api/admin/audit"), api("/api/admin/events"), api("/api/admin/feeds"), api("/api/admin/readiness")]);
  const tile = (label, val, i, dec = 0) => { const n = h("div", {class: "stat num"}, "0"); countUp(n, val, dec); return h("div", {class: "card enter", i}, h("div", {class: "m"}, label), n); };
  const revTxt = Object.entries(st.revenue_30d).map(([c, v]) => money(v, c)).join(" · ") || "0";
  const q = h("input", {placeholder: "Search users…", "aria-label": "Search users"}); const tb = h("tbody", {});
  const rows = u.users.map(x => ({x, tr: h("tr", {class: "rowh"}, h("td", {class: "num"}, x.id), h("td", {}, x.email, x.role === "admin" ? h("span", {class: "badge admin"}, "admin") : null), h("td", {}, h("span", {class: "badge " + x.effective}, x.effective + (x.tier !== "free" && x.paid_until ? " · " + date(x.paid_until) : ""))), h("td", {class: x.status === "active" ? "" : "S"}, x.status), h("td", {class: "num"}, x.scans_today),
    h("td", {}, x.role === "admin" ? "" : h("button", {class: "btn sm", onclick: () => manage(x)}, "Manage")))}));
  rows.forEach(r => tb.append(r.tr)); q.addEventListener("input", () => rows.forEach(r => r.tr.classList.toggle("hide", !r.x.email.toLowerCase().includes(q.value.toLowerCase()))));
  async function manage(x) {
    await modal(close => { const tier = h("select", {}, ["free", "pro", "elite"].map(t => h("option", {value: t, selected: t === x.tier}, t))), days = h("input", {type: "number", value: 30, min: 0});
      const act = (path, body, msg, extra) => async () => { try { const d = await api(path, body); toast(msg, "ok"); if (extra) extra(d); else { close(true); renderShell(); } } catch (e) { toast(e.message, "err"); } };
      return [h("h3", {}, x.email), h("div", {class: "col"}, h("label", {class: "m"}, "Grant plan"), h("div", {class: "row"}, tier, days), h("button", {class: "btn pri", onclick: act("/api/admin/grant", {id: x.id, tier: tier.value, days: +days.value}, "Plan updated")}, "Apply plan"),
        h("button", {class: "btn", onclick: act("/api/admin/status", {id: x.id, status: x.status === "active" ? "suspended" : "active"}, "Status changed")}, x.status === "active" ? "Suspend account" : "Unsuspend"),
        h("button", {class: "btn", onclick: act("/api/admin/revoke", {id: x.id}, "Sessions revoked")}, "Log out everywhere"),
        h("button", {class: "btn", onclick: act("/api/admin/reset-token", {id: x.id}, "Token issued", d => { const t = h("pre", {}, d.token); close(true); modal(c2 => [h("h3", {}, "One-time reset token"), h("p", {class: "m"}, "Valid for 1 hour. Give it to the user privately."), t, h("button", {class: "btn", onclick: () => { navigator.clipboard && navigator.clipboard.writeText(d.token); toast("Copied", "ok"); }}, "Copy"), h("button", {class: "btn", onclick: () => c2(true)}, "Done")]); })}, "Issue password-reset token"), h("button", {class: "btn", onclick: () => close(false)}, "Close"))]; });
  }
  main.replaceChildren(top("Admin console", "Go-live readiness, users, billing and audit"), readinessCard(rd),
    h("div", {class: "grid g4"}, tile("Users", st.users, 1), tile("New (7d)", st.new_7d, 2), tile("Pro", st.tiers.pro || 0, 3), tile("Elite", st.tiers.elite || 0, 4), tile("Scans today", st.scans_today, 5), tile("Pending payments", st.pending_payments, 6),
      h("div", {class: "card enter", i: 7}, h("div", {class: "m"}, "Revenue (30d)"), h("div", {class: "stat num"}, revTxt))),
    h("div", {class: "card enter", i: 8}, h("div", {class: "row"}, h("h3", {class: "grow"}, "Users (" + u.users.length + ")"), q), h("table", {}, h("thead", {}, h("tr", {}, ["ID", "Email", "Plan", "Status", "Scans", ""].map(x => h("th", {}, x)))), tb)),
    feedsCard(fd), eventsCard(ev),
    h("div", {class: "card enter", i: 9}, h("h3", {}, "Payments"), h("table", {}, h("tr", {}, ["Date", "User", "Plan", "Amount", "Status", "Reference"].map(x => h("th", {}, x))), p.payments.map(x => h("tr", {class: "rowh"}, h("td", {}, date(x.created)), h("td", {}, x.email), h("td", {}, x.tier), h("td", {class: "num"}, money(x.amount, x.currency)), h("td", {class: x.status === "success" ? "L" : "m"}, x.status), h("td", {class: "m mono"}, x.reference))))),
    h("details", {class: "enter", i: 10}, h("summary", {}, "Audit log ", h("span", {class: a.chain_ok ? "L" : "S"}, a.chain_ok ? "· hash chain intact" : "· CHAIN BROKEN - investigate")), h("table", {}, a.rows.map(r => h("tr", {}, h("td", {class: "m"}, new Date(r.ts * 1000).toLocaleString()), h("td", {}, r.actor), h("td", {}, r.action), h("td", {}, r.target), h("td", {class: "m mono"}, r.detail))))));
}

/* ---------------- trading (admin) ---------------- */
async function pageTrade(main) {
  const st = h("div", {class: "card enter", i: 1}, h("div", {class: "skel"})), out = h("div", {class: "col"}), prop = h("div", {});
  const status = async () => { const s = await api("/api/admin/trade/status"); st.replaceChildren(h("div", {class: "row"}, h("span", {class: "badge"}, s.mode), h("span", {class: "badge"}, "broker: " + s.broker + (s.real_account ? " · REAL" : "")), h("span", {class: "num"}, "equity " + s.equity.toFixed(2)), h("span", {class: "num"}, "open " + s.positions.length), s.kill ? h("span", {class: "badge S"}, "KILL SWITCH ACTIVE") : null, s.calendar ? null : h("span", {class: "badge Y"}, "no news calendar"))); return s; };
  const scan = async ev => { const b = ev.currentTarget; b.disabled = true; b.classList.add("busy"); skeleton(out, 29);
    try { const d = await api("/api/admin/trade/scan", {}); show(d); } catch (e) { out.replaceChildren(h("div", {class: "banner err"}, e.message)); } b.disabled = false; b.classList.remove("busy"); status(); };
  function show(d) {
    prop.replaceChildren(); if (d.error) { out.replaceChildren(h("div", {class: "banner err"}, d.error)); return; }
    const sc = d.scan, big = h("span", {class: "num"}, "0"); countUp(big, sc.long + sc.short);
    out.replaceChildren(h("div", {class: "card enter", i: 0}, h("div", {class: "row"}, h("span", {class: "pill " + (d.decision === "BUY" || d.decision === "SELL" ? d.decision : "WAIT")}, d.decision), h("span", {class: "m"}, d.classification + " · score " + d.score + "/100 (not a probability)")), h("div", {class: "big"}, big, h("span", {class: "m"}, " / " + sc.n_total)), h("div", {class: "m"}, sc.summary), sc.consensus ? null : h("div", {class: "m"}, "No consensus: " + sc.why_not.join(" · ")), d.issues.length ? h("div", {class: "S"}, "Reconcile: " + d.issues.join("; ")) : null), h("details", {class: "enter", i: 1}, h("summary", {}, "Full report"), h("pre", {}, d.report)));
    if (d.proposal) { const p = d.proposal; prop.replaceChildren(h("div", {class: "card enter pcard pop"}, h("div", {class: "row"}, h("span", {class: "pill " + p.side}, p.side + " proposal")), h("div", {class: "ladder"}, ladder({side: p.side, entry: p.entry, stop: p.sl, tp1: p.tp1, tp2: p.tp2, tp3: p.tp3})),
      h("div", {class: "kv"}, h("span", {class: "m"}, "Size / risk"), h("b", {class: "num"}, p.size + " lots · " + p.risk + " (" + p.risk_pct + "%)")), h("div", {class: "kv"}, h("span", {class: "m"}, "Net R:R / margin"), h("b", {class: "num"}, p.rr_net + " / " + p.margin)), h("p", {class: "m"}, "Expires " + p.expires + ". ALLOW re-checks price, spread, risk limits and R:R on a fresh quote before anything is sent."),
      h("div", {class: "row"}, h("button", {class: "btn go", onclick: async () => { if (!await confirmBox("Send this order?", p.side + " " + p.size + " lots with stop " + p.sl + " and target " + p.tp2 + ".", "ALLOW trade", "go")) return; try { const r = await api("/api/admin/trade/allow", {id: p.id}); r.ok ? toast("FILLED " + r.size + " @ " + r.fill, "ok") : toast("Not sent: " + (r.error || r.detail), "err"); } catch (e) { toast(e.message, "err"); } prop.replaceChildren(); status(); }}, "ALLOW trade"), h("button", {class: "btn no", onclick: async () => { await api("/api/admin/trade/reject", {id: p.id}); prop.replaceChildren(); }}, "Reject")))); }
  }
  main.append(top("Trading panel", "Your own account. Nothing is sent until you press Allow."), st, h("div", {class: "row enter", i: 2}, h("button", {class: "btn pri", onclick: scan}, "Scan chart"),
    h("button", {class: "btn no", onclick: async () => { if (await confirmBox("Kill switch", "Close all positions and block new entries?", "Activate", "no")) { await api("/api/admin/trade/kill", {}); toast("Kill switch active", "err"); status(); } }}, "KILL SWITCH")), prop, out);
  const s0 = await status().catch(() => null);
  if (s0 && s0.feed === "csv") main.querySelector(".row.enter").append(h("button", {class: "btn", onclick: async ev => { await api("/api/admin/trade/advance", {n: 4}); scan(ev); }}, "Advance 4 bars (replay)"));
}

/* ---------------- v6: time, motion, widgets ---------------- */
let TZ = localStorage.getItem("apex-tz") || "local", LASTRADAR = null, JPRE = null, AUTORUN = false; const TICKERS = [];
const clearTickers = () => { TICKERS.splice(0).forEach(clearInterval); };
const pad2 = n => String(n).padStart(2, "0"), tzOpt = () => (TZ === "utc" ? {timeZone: "UTC"} : {});
function fmtDT(iso, withDate) { const o = Object.assign({hour: "2-digit", minute: "2-digit", hour12: false}, tzOpt()); if (withDate) Object.assign(o, {weekday: "short", month: "short", day: "numeric"}); return new Date(iso).toLocaleString(undefined, o) + (TZ === "utc" ? " UTC" : ""); }
const dayLabel = iso => new Date(iso).toLocaleDateString(undefined, Object.assign({weekday: "long", month: "long", day: "numeric"}, tzOpt()));
function dur(ms) { const s = Math.max(0, Math.round(ms / 1000)), hh = Math.floor(s / 3600), mm = Math.floor(s % 3600 / 60), ss = s % 60; return hh ? hh + "h " + pad2(mm) + "m" : mm ? mm + "m " + pad2(ss) + "s" : ss + "s"; }
function countdown(el, iso, pre = "in ", nowTxt = "now") { const tick = () => { const ms = new Date(iso) - Date.now(); el.textContent = ms > 1000 ? pre + dur(ms) : ms > -60000 ? nowTxt : dur(-ms) + " ago"; }; tick(); const id = setInterval(() => { if (!el.isConnected) { clearInterval(id); return; } tick(); }, 1000); TICKERS.push(id); }
function ring(v, size, label, color) {
  const r = size / 2 - 6, C = 2 * Math.PI * r, c = size / 2;
  const fg = h("svg:circle", {class: "fg", cx: c, cy: c, r, fill: "none", "stroke-width": 6, "stroke-linecap": "round", stroke: color}); fg.style.strokeDasharray = C; fg.style.strokeDashoffset = C;
  const v1 = Math.max(0, Math.min(1, v)); requestAnimationFrame(() => requestAnimationFrame(() => { fg.style.strokeDashoffset = C * (1 - v1); }));
  return h("div", {class: "ring"}, h("svg:svg", {width: size, height: size, viewBox: "0 0 " + size + " " + size}, h("svg:circle", {class: "bg", cx: c, cy: c, r, fill: "none", "stroke-width": 6}), fg), h("span", {class: "v"}, label));
}
function burst() {
  if (RM) return; let cv = $("confetti"); if (!cv) { cv = h("canvas", {id: "confetti", "aria-hidden": "true"}); document.body.append(cv); }
  cv.width = innerWidth; cv.height = innerHeight; const x = cv.getContext("2d"), cols = ["#22c55e", "#22d3ee", "#6366f1", "#f59e0b", "#ec4899"];
  const ps = Array.from({length: 90}, () => ({x: innerWidth / 2 + (Math.random() - .5) * 220, y: innerHeight * .35, vx: (Math.random() - .5) * 11, vy: -Math.random() * 11 - 3, s: Math.random() * 6 + 3, c: cols[Math.random() * 5 | 0], r: Math.random() * 6, vr: (Math.random() - .5) * .4}));
  let f = 0; const step = () => { x.clearRect(0, 0, cv.width, cv.height); ps.forEach(p => { p.vy += .32; p.x += p.vx; p.y += p.vy; p.r += p.vr; x.save(); x.translate(p.x, p.y); x.rotate(p.r); x.fillStyle = p.c; x.globalAlpha = Math.max(0, 1 - f / 110); x.fillRect(-p.s / 2, -p.s / 2, p.s, p.s * .6); x.restore(); }); if (++f < 110) requestAnimationFrame(step); else cv.remove(); }; requestAnimationFrame(step);
}
function tilt(el) {
  if (RM || matchMedia("(hover: none)").matches) return el;
  el.addEventListener("mousemove", e => { const r = el.getBoundingClientRect(), px = (e.clientX - r.left) / r.width - .5, py = (e.clientY - r.top) / r.height - .5; el.style.setProperty("--ry", px * 6 + "deg"); el.style.setProperty("--rx", -py * 6 + "deg"); });
  el.addEventListener("mouseleave", () => { el.style.setProperty("--ry", "0deg"); el.style.setProperty("--rx", "0deg"); }); return el;
}
function liveChip() { const t = h("span", {class: "num"}); const tick = () => { const d = new Date(); t.textContent = "UTC " + pad2(d.getUTCHours()) + ":" + pad2(d.getUTCMinutes()) + ":" + pad2(d.getUTCSeconds()); }; tick(); const id = setInterval(() => { if (!t.isConnected) { clearInterval(id); return; } tick(); }, 1000); TICKERS.push(id); return h("span", {class: "clock", title: "Live clock"}, h("span", {class: "live"}), t); }
const nav = p => { PAGE = p; renderShell(); };
function toggleTZ() { TZ = TZ === "utc" ? "local" : "utc"; localStorage.setItem("apex-tz", TZ); renderShell(); }
function openScanner(sym, tf, auto) { SYM = sym; TF = tf; LAST = null; AUTORUN = !!auto; nav("scan"); }
const emptyState = (title, text, ...kids) => h("div", {class: "empty"}, h("svg:svg", {viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "1.4", "stroke-linecap": "round"}, h("svg:circle", {cx: 12, cy: 12, r: 9}), h("svg:path", {d: "M8 12h8M12 8v8"})), h("b", {}, title), h("div", {}, text), kids);
function openPalette() {
  if (!ME) return; const cmds = [["Opportunities", "go", () => nav("home")], ["Scanner", "go", () => nav("scan")], ["Calendar & news", "go", () => nav("calendar")], ["Journal", "go", () => nav("journal")], ["Alerts", "go", () => nav("alerts")], ["Plans & billing", "go", () => nav("plans")], ["Account", "go", () => nav("account")]];
  ME.symbols.forEach(s => cmds.push(["Scan " + s, "symbol", () => openScanner(s, TF, true)]));
  cmds.push(["Toggle theme", "ui", () => { setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"); renderShell(); }], ["Show times in " + (TZ === "utc" ? "local time" : "UTC"), "ui", toggleTZ], ["Log out", "account", logout]);
  modal(close => { const input = h("input", {placeholder: "Type a command or symbol…", "aria-label": "Command palette"}), list = h("ul", {class: "cmdlist", role: "listbox"}); let sel = 0, vis = cmds;
    const draw = () => list.replaceChildren(...vis.map((c, i) => h("li", {class: i === sel ? "sel" : "", role: "option", onclick: () => { close(true); c[2](); }}, h("span", {}, c[0]), h("span", {class: "k"}, c[1]))));
    input.addEventListener("input", () => { const q = input.value.toLowerCase(); vis = cmds.filter(c => c[0].toLowerCase().includes(q)); sel = 0; draw(); });
    input.addEventListener("keydown", e => { if (e.key === "ArrowDown") { sel = Math.min(vis.length - 1, sel + 1); draw(); e.preventDefault(); } else if (e.key === "ArrowUp") { sel = Math.max(0, sel - 1); draw(); e.preventDefault(); } else if (e.key === "Enter" && vis[sel]) { close(true); vis[sel][2](); } });
    draw(); setTimeout(() => input.focus(), 30); return [h("div", {class: "cmd"}, input, list, h("div", {class: "m"}, h("kbd", {}, "↑↓"), " navigate ", h("kbd", {}, "Enter"), " select ", h("kbd", {}, "Esc"), " close"))]; });
}
document.addEventListener("keydown", e => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k" && ME) { e.preventDefault(); openPalette(); } });

/* ---------------- events, news, timing widgets ---------------- */
function evRow(e, i, compact) {
  const soon = e.impact === "high" && e.minutes > 0 && e.minutes <= 60, past = e.minutes < -15, cd = h("span", {class: "num m cd"});
  if (e.minutes > -60 && e.minutes < 48 * 60) countdown(cd, e.time_utc);
  const meta = []; if (e.forecast) meta.push("Forecast " + e.forecast); if (e.previous) meta.push("Previous " + e.previous); if (e.actual) meta.push("Actual " + e.actual);
  return h("div", {class: "ev" + (soon ? " soon" : "") + (past ? " past" : ""), i: Math.min(i, 14)}, h("span", {class: "num"}, fmtDT(e.time_utc)), h("span", {class: "cur"}, e.currency),
    h("div", {}, h("div", {class: "nm"}, h("span", {class: "dot " + e.impact, title: e.impact + " impact"}), e.name, e.surprise ? h("span", {class: e.surprise > 0 ? "L" : "S", title: "Versus forecast, for the currency"}, e.surprise > 0 ? " ▲ beat" : " ▼ miss") : null), compact ? null : h("div", {class: "meta"}, meta.join(" · "))), cd);
}
function newsRow(n, i) {
  const tone = n.sentiment > 0.2 ? ["p", "▲"] : n.sentiment < -0.2 ? ["n", "▼"] : ["z", "•"], ago = Math.max(1, Math.round((Date.now() / 1000 - n.published) / 60));
  const title = n.link ? h("a", {href: n.link, target: "_blank", rel: "noopener noreferrer"}, n.title) : h("span", {class: "nm"}, n.title);
  return h("div", {class: "nw", i: Math.min(i, 14)}, h("div", {class: "tone " + tone[0], title: "Keyword-based headline tone (low reliability)"}, tone[1]), h("div", {}, title, h("div", {class: "meta m"}, n.source + " · " + (ago < 90 ? ago + " min ago" : Math.round(ago / 60) + " h ago") + " · " + n.currencies.split(",").filter(Boolean).map(c => c).join(" "))));
}
function newsCard(items, bias) {
  return h("div", {class: "card enter", i: 5}, h("div", {class: "row"}, h("h3", {class: "grow"}, "Recent headlines for this pair"), bias != null ? h("span", {class: "badge " + (bias > 0.35 ? "L" : bias < -0.35 ? "S" : "")}, "tone " + (bias > 0 ? "+" : "") + bias) : null),
    items.length ? items.map(newsRow) : h("div", {class: "m"}, "No tagged headlines in the last 48 hours (or news feeds are not enabled on this server)."), h("p", {class: "m"}, "Headline tone is a keyword heuristic: context only, never a signal on its own."));
}
function timeline(t) {
  const now = Date.parse(t.now_utc), start = now - 6 * 3600e3, span = 36 * 3600e3, pct = ms => (ms - start) / span * 100, box = h("div", {class: "tl", role: "img", "aria-label": "Session and news timeline"});
  const add = (cls, a, b, label, text) => { const l = Math.max(0, pct(a)), r = Math.min(100, pct(b)); if (r <= l) return; const el = h("div", {class: cls, title: label}, text || null); el.style.setProperty("--l", l); el.style.setProperty("--w", r - l); box.append(el); };
  t.sessions.forEach(s => add("band " + s.name, Date.parse(s.start), Date.parse(s.end), s.name + " session", s.name === "newyork" ? "NEW YORK" : s.name.toUpperCase()));
  t.blackouts.forEach(b => add("black", Date.parse(b.start), Date.parse(b.end), "News blackout: " + b.currency + " " + b.name));
  if (t.next_window) add("win", Date.parse(t.next_window.start), Date.parse(t.next_window.end), "Next good window");
  const nm = h("div", {class: "nowm"}); nm.style.setProperty("--l", pct(now)); box.append(nm);
  const ticks = Array.from({length: 7}, (_, k) => h("span", {}, fmtDT(start + k * 6 * 3600e3 - 0).replace(" UTC", "")));
  return h("div", {}, box, h("div", {class: "axis"}, ticks), h("div", {class: "legend"}, [["sydney", "Sydney"], ["tokyo", "Tokyo"], ["london", "London"], ["newyork", "New York"], ["black", "News blackout"], ["win", "Next good window"]].map(([k, l]) => h("span", {}, h("i", {class: "lg " + k}), l))));
}
function hourStrip(hp, nowHour) {
  if (!hp) return null; const bars = hp.hours.map((v, k) => { const el = h("i", {class: k === nowHour ? "cur" : "", title: pad2(k) + ":00 UTC · " + (v == null ? "n/a" : v + "x average range"), i: k}); el.style.setProperty("--o", v == null ? .08 : Math.max(.12, Math.min(1, (v - .4) / 2.2 + .15))); return el; });
  return h("div", {}, h("div", {class: "m", }, "Typical activity by hour (UTC) for this symbol: brighter = wider average range. Best hours: " + hp.best.map(x => pad2(x) + ":00").join(", ")), h("div", {class: "hours"}, bars), h("div", {class: "hlabels"}, hp.hours.map((_, k) => h("span", {}, k % 3 === 0 ? pad2(k) : ""))));
}
function timingCard(t, up) {
  const cdn = h("span", {class: "m num"}), lines = []; countdown(cdn, t.next_candle_close, "next candle in ");
  if (t.signal_valid_until) { const sp = h("b", {class: "num"}); countdown(sp, t.signal_valid_until, "", "expired"); lines.push(h("div", {}, "Signal valid until ", h("b", {class: "num"}, fmtDT(t.signal_valid_until, true)), " (", sp, " left)")); }
  if (t.next_window) { const sp = h("b", {class: "num"}); countdown(sp, t.next_window.start, "starts in "); lines.push(h("div", {}, "Next good window: ", h("b", {class: "num"}, fmtDT(t.next_window.start, true) + " to " + fmtDT(t.next_window.end)), " · " + t.next_window.why + " · ", sp)); }
  return h("div", {class: "card enter col", i: 3}, h("div", {class: "statusbar"}, h("span", {class: "pill " + t.status}, t.status), h("h3", {}, t.headline), h("span", {class: "grow"}), cdn), lines,
    h("ul", {class: "reasons"}, t.reasons.filter(r => r.text).map(r => h("li", {class: r.ok ? "y" : "n"}, h("span", {class: "ic"}, r.ok ? "✓" : "✕"), r.text))),
    timeline(t), hourStrip(t.hour_profile, new Date(t.now_utc).getUTCHours()), up && up.length ? h("div", {}, h("div", {class: "daysep"}, "Upcoming events for this pair"), up.slice(0, 5).map((e, i) => evRow(e, i, true))) : null,
    h("p", {class: "m"}, "Timing filters WHEN conditions are sensible (liquidity, scheduled news). It says nothing about direction. Times shown in " + (TZ === "utc" ? "UTC" : "your local time") + (t.tz_exact ? "." : " (server lacks timezone data: sessions use fixed winter hours).")));
}

function readinessCard(rd) {
  const rows = rd.rows.map((r, i) => h("div", {class: "rd " + r.status, i: Math.min(i, 14)}, h("span", {class: "rdot", title: r.status}, r.status === "ok" ? "✓" : r.status === "warn" ? "!" : "✕"), h("div", {}, h("b", {}, r.label), h("div", {class: "m"}, r.detail))));
  const btn = h("button", {class: "btn sm", onclick: async () => { btn.disabled = true; try { await api("/api/admin/readiness?force=1"); renderShell(); } catch (e) { toast(e.message, "err"); btn.disabled = false; } }}, "Re-check now");
  return h("div", {class: "card enter " + (rd.ready ? "" : "glow"), i: 1}, h("div", {class: "row"}, h("h3", {class: "grow"}, "Go-live checklist"), h("span", {class: "badge " + (rd.ready ? "L" : "S")}, rd.ready ? "READY FOR CUSTOMERS" : "NOT READY"),
    h("span", {class: "m num"}, rd.counts.ok + " ok · " + rd.counts.warn + " warnings · " + rd.counts.fail + " failures"), btn), h("div", {class: "rdgrid"}, rows), h("p", {class: "m"}, "Checked " + fmtDT(rd.checked, true) + ". Warnings are things to finish (for example adding your Paystack key); failures block a safe launch."));
}
function sessionsCard() {
  const box = h("div", {class: "card enter col", i: 2}, h("h3", {}, "Active sessions"), h("div", {class: "skel"}));
  api("/api/sessions").then(d => box.replaceChildren(h("h3", {}, "Active sessions"), h("table", {}, h("tr", {}, ["Signed in", "Last active", "Address", ""].map(x => h("th", {}, x))), d.sessions.map(s => h("tr", {}, h("td", {}, date(s.created)), h("td", {}, fmtDT(new Date(s.last_seen * 1000).toISOString(), true)), h("td", {class: "mono"}, s.ip), h("td", {}, s.current ? h("span", {class: "badge L"}, "this device") : null)))),
    d.sessions.length > 1 ? h("button", {class: "btn", onclick: async () => { const r = await api("/api/sessions/revoke-others", {}); toast(r.revoked + " other session(s) signed out", "ok"); renderShell(); }}, "Sign out all other devices") : null)).catch(e => console.error("sessions failed:", e));
  return box;
}

/* ---------------- Opportunities (what to trade, when) ---------------- */
function oppCard(it, i, best) {
  const dir = it.direction, rv = it.criteria[1] ? it.criteria[0] / it.criteria[1] : 0, col = it.grade === "A" ? cssv("--g") : it.grade === "B" ? cssv("--y") : dir ? cssv("--r") : cssv("--mut"), tm = it.timing, p = it.plan;
  const cd = h("span", {class: "num"}); if (tm.signal_valid_until) countdown(cd, tm.signal_valid_until, "valid for ", "expired"); else if (tm.next_window) countdown(cd, tm.next_window.start, "window in ");
  const fmtp = v => fmt(v, v > 50 ? 3 : 5);
  const levels = p ? (best ? h("div", {class: "levels"}, [["Entry", p.entry], ["Stop", p.stop], ["TP1", p.tp1], ["TP2", p.tp2], ["TP3", p.tp3]].map(([k, v]) => h("div", {}, h("span", {}, k), h("b", {}, fmtp(v)))).concat([h("div", {}, h("span", {}, "Net R:R"), h("b", {}, p.net_rr)), h("div", {}, h("span", {}, "Size"), h("b", {}, p.size != null ? p.size + " lots" : "n/a"))]))
    : h("div", {class: "m num"}, "R:R " + p.net_rr + " · entry " + fmtp(p.entry) + " · stop " + fmtp(p.stop))) : null;
  const card = h("div", {class: "card opp enter " + (best ? "best glow" : "") + (dir ? "" : " dim"), i: Math.min(i, 12), tabindex: 0, role: "button", "aria-label": "Open " + it.symbol + " in the scanner", onclick: () => openScanner(it.symbol, it.tf, true), onkeydown: e => { if (e.key === "Enter") openScanner(it.symbol, it.tf, true); }},
    h("div", {class: "row"}, h("div", {class: "grow"}, h("div", {class: "sym"}, it.symbol, h("span", {class: "m"}, " " + (TFS.find(t => t[0] === it.tf) || [0, it.tf + "m"])[1])), h("div", {class: "row"}, dir ? h("span", {class: "pill sm " + (dir === "BUY" ? "BUY" : "SELL")}, dir) : h("span", {class: "pill sm NONE"}, "NO SETUP"),
      it.grade ? h("div", {class: "grade g" + it.grade, title: "Setup grade " + it.grade}, it.grade) : null)), ring(rv, best ? 92 : 64, it.criteria[1] ? Math.round(rv * 100) + "%" : "–", col)),
    h("div", {class: "statusbar"}, h("span", {class: "pill sm " + tm.status}, tm.status), h("span", {class: "m"}, tm.headline)), levels,
    h("div", {class: "row m"}, h("span", {}, it.passed + "/" + it.scanned + " methods · " + it.families[0] + "L/" + it.families[1] + "S families"), it.news_bias != null && Math.abs(it.news_bias) >= .35 ? h("span", {class: it.news_bias * (dir === "SELL" ? -1 : 1) > 0 ? "L" : "S"}, "headline tone " + (it.news_bias > 0 ? "+" : "") + it.news_bias) : null, h("span", {class: "grow"}), cd),
    best ? h("div", {class: "row"}, h("button", {class: "btn pri", onclick: ev => { ev.stopPropagation(); openScanner(it.symbol, it.tf, true); }}, "Open in scanner"), dir && p ? h("button", {class: "btn", onclick: ev => { ev.stopPropagation(); JPRE = {symbol: it.symbol, side: dir, entry: p.entry, stop: p.stop, grade: it.grade || ""}; nav("journal"); }}, "Log to journal") : null) : null);
  return best ? card : tilt(card);
}
function renderRadar(out, d) {
  const items = d.items, best = items.find(i => i.direction && i.grade);
  const kids = [h("div", {class: "row m"}, h("span", {class: "live"}), "Updated " + fmtDT(d.generated) + (d.cached ? " (cached)" : "") + " · " + d.scanned + " symbols ranked")];
  if (best) { kids.push(h("h3", {class: "enter", i: 0}, "Best setup right now"), oppCard(best, 0, true)); if (best.grade === "A") burst(); }
  else kids.push(h("div", {class: "card enter", i: 0}, emptyState("No qualifying setup right now", "Nothing on your list has both a consensus and a graded plan. Waiting is a valid decision; check the timing windows below.")));
  const rest = items.filter(i => i !== best); if (rest.length) kids.push(h("h3", {class: "enter", i: 1}, best ? "Everything else on your list" : "Your list"), h("div", {class: "grid g3o"}, rest.map((it, k) => oppCard(it, k + 1, false))));
  kids.push(h("p", {class: "m"}, d.note)); out.replaceChildren(...kids);
}
async function pageHome(main) {
  const strip = h("div", {class: "card enter", i: 1}, h("div", {class: "row"}, h("h3", {class: "grow"}, "Next high-impact news"), h("button", {class: "btn sm", onclick: () => nav("calendar")}, "Full calendar")), h("div", {class: "skel"})), out = h("div", {class: "col"});
  const seg = h("div", {class: "seg", role: "group", "aria-label": "Timeframe"}, TFS.map(([v, l]) => h("button", {class: v === TF ? "on" : "", onclick: e => { TF = v; seg.querySelectorAll("button").forEach(b => b.classList.toggle("on", b === e.currentTarget)); }}, l)));
  const btn = h("button", {class: "btn pri"}, "Run radar"), wl = h("button", {class: "btn", onclick: watchlistDialog}, "Edit watchlist");
  const run = async () => { btn.disabled = true; btn.classList.add("busy"); out.replaceChildren(h("div", {class: "grid g3o"}, Array.from({length: 6}, () => h("div", {class: "card col"}, h("div", {class: "skel"}), h("div", {class: "skel"}), h("div", {class: "skel"})))));
    try { const d = await api("/api/radar", {tf: TF}); LASTRADAR = d; if (!d.cached) ME.scans_used += 3; renderRadar(out, d); } catch (e) { out.replaceChildren(h("div", {class: "banner err"}, e.message, e.data && e.data.upgrade ? h("button", {class: "btn sm", onclick: () => nav("plans")}, "See plans") : null)); }
    btn.disabled = false; btn.classList.remove("busy"); }; btn.addEventListener("click", run);
  main.append(top("Opportunities", "What to trade, ranked by setup quality, and when it makes sense to act.", liveChip()), strip);
  api("/api/calendar?days=2").then(c => { const up = c.events.filter(e => e.impact === "high" && e.minutes > -30).slice(0, 4); strip.replaceChildren(h("div", {class: "row"}, h("h3", {class: "grow"}, "Next high-impact news"), h("button", {class: "btn sm", onclick: () => nav("calendar")}, "Full calendar")),
    up.length ? h("div", {}, up.map((e, i) => evRow(e, i, true))) : h("div", {class: "m"}, c.enabled ? "No high-impact events in the next 48 hours." : "No economic calendar loaded yet. Ask the admin to enable news feeds or upload a calendar.")); }).catch(e => console.error("schedule strip failed:", e));
  if (!ME.radar) { main.append(h("div", {class: "card enter glow", i: 2}, emptyState("The opportunity radar is a Pro feature", "It scans your whole watchlist, grades every setup, checks timing and news, and ranks what to look at first.", h("div", {class: "row"}, h("button", {class: "btn pri", onclick: () => nav("plans")}, "See plans"), h("button", {class: "btn", onclick: () => openScanner(SYM || "EURUSD", TF, false)}, "Use the scanner instead")))));
    return; }
  main.append(h("div", {class: "card enter row", i: 2}, seg, btn, wl, h("span", {class: "grow"}), h("span", {class: "m"}, "A run uses 3 of your daily scans and is cached for 45s.")), out);
  if (LASTRADAR && LASTRADAR.tf === TF) renderRadar(out, LASTRADAR); else out.append(h("div", {class: "card enter", i: 3}, emptyState("Ready to rank your watchlist", "Press Run radar. You'll get the best setup, its levels, a timing verdict, and when the next good window opens.")));
}
async function watchlistDialog() {
  const w = await api("/api/watchlist"); const sel = new Set(w.symbols);
  await modal(close => { const boxes = w.available.map(s => { const cb = h("input", {type: "checkbox", id: "wl-" + s}); cb.checked = sel.has(s); cb.addEventListener("change", () => { cb.checked ? sel.add(s) : sel.delete(s); cnt.textContent = sel.size + " / " + w.limit; }); return h("label", {class: "checkrow", for: "wl-" + s}, cb, h("b", {}, s)); }), cnt = h("span", {class: "m num"}, sel.size + " / " + w.limit);
    return [h("h3", {}, "Watchlist"), h("p", {class: "m"}, "The radar ranks these symbols. Leave empty to use the first " + w.limit + " available."), h("div", {class: "col"}, boxes), cnt,
      h("div", {class: "row"}, h("button", {class: "btn pri", onclick: async () => { try { await api("/api/watchlist", {symbols: [...sel]}); toast("Watchlist saved", "ok"); LASTRADAR = null; close(true); renderShell(); } catch (e) { toast(e.message, "err"); } }}, "Save"), h("button", {class: "btn", onclick: () => close(false)}, "Cancel"))]; });
}

/* ---------------- Calendar & news ---------------- */
async function pageCalendar(main) {
  const ALL = ["USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD"], sel = new Set(), maxDays = ME.features.plan ? 14 : 3; let days = Math.min(3, maxDays);
  const evBox = h("div", {}, h("div", {class: "skel"})), side = h("div", {class: "col"});
  const chips = h("div", {class: "cchips"}, ALL.map(c => h("button", {class: "cc", onclick: e => { sel.has(c) ? sel.delete(c) : sel.add(c); e.currentTarget.classList.toggle("on"); load(); }}, c)));
  const dseg = h("div", {class: "seg", role: "group", "aria-label": "Days ahead"}, [1, 3, 7, 14].filter(d => d <= maxDays).map(d => h("button", {class: d === days ? "on" : "", onclick: e => { days = d; dseg.querySelectorAll("button").forEach(b => b.classList.toggle("on", b === e.currentTarget)); load(); }}, d + "d")));
  main.append(top("Calendar & news", "Scheduled events and central-bank headlines that move currencies.", liveChip(), h("button", {class: "btn sm", onclick: toggleTZ}, "Times: " + (TZ === "utc" ? "UTC" : "local"))),
    h("div", {class: "card enter row", i: 1}, chips, h("span", {class: "grow"}), dseg), h("div", {class: "grid g2"}, h("div", {class: "card enter", i: 2}, evBox), side));
  async function load() {
    const q = [...sel].join(","); let c, n; try { [c, n] = await Promise.all([api("/api/calendar?days=" + days + (q ? "&c=" + q : "")), api("/api/news" + (q ? "?c=" + q : ""))]); } catch (e) { evBox.replaceChildren(h("div", {class: "banner err"}, e.message)); return; }
    const ev = c.events; let day = "", rows = [];
    ev.forEach((e, i) => { const dl = dayLabel(e.time_utc); if (dl !== day) { day = dl; rows.push(h("div", {class: "daysep"}, dl)); } rows.push(evRow(e, i, false)); });
    evBox.replaceChildren(h("div", {class: "row"}, h("h3", {class: "grow"}, "Economic calendar"), h("span", {class: "m"}, c.last_sync ? "feed synced " + fmtDT(new Date(c.last_sync * 1000).toISOString(), true) : "")), ev.length ? h("div", {}, rows) : emptyState("No events in this window", c.enabled ? "Try a longer range or different currencies." : "No calendar is loaded. Enable news feeds (admin) or upload a calendar CSV."));
    const sb = Object.entries(c.sentiment).sort((a, b) => b[1].score - a[1].score).map(([cur, v]) => { const fill = h("i", {}); fill.style.left = v.score >= 0 ? "50%" : "50%"; fill.style.width = "0%"; fill.style.background = v.score >= 0 ? cssv("--g") : cssv("--r"); requestAnimationFrame(() => requestAnimationFrame(() => { const w = Math.abs(v.score) * 50; fill.style.width = w + "%"; if (v.score < 0) fill.style.left = (50 - w) + "%"; }));
      return h("div", {class: "sentbar", title: v.n + " headlines"}, h("b", {}, cur), h("div", {class: "tr"}, fill), h("span", {class: v.score >= 0 ? "L" : "S"}, (v.score > 0 ? "+" : "") + v.score)); });
    side.replaceChildren(h("div", {class: "card enter", i: 3}, h("h3", {}, "Headline tone (24h)"), sb.length ? sb : h("div", {class: "m"}, "No tagged headlines yet."), h("p", {class: "m"}, "Keyword heuristic on headlines: crude context, not a trading signal.")),
      h("div", {class: "card enter", i: 4}, h("h3", {}, "Latest headlines"), n.items.length ? n.items.slice(0, 14).map(newsRow) : h("div", {class: "m"}, "No headlines yet."), h("p", {class: "m"}, n.note)));
  }
  load();
}

/* ---------------- Journal ---------------- */
function curveView(curve) {
  const cv = h("canvas", {class: "spark", height: "120", role: "img", "aria-label": "Cumulative R curve"}); let prog = RM ? 1 : 0;
  const draw = () => { const dpr = devicePixelRatio || 1, W = cv.clientWidth, H = 120; if (!W) return; cv.width = W * dpr; cv.height = H * dpr; const x = cv.getContext("2d"); x.setTransform(dpr, 0, 0, dpr, 0, 0); x.clearRect(0, 0, W, H);
    const pts = [0].concat(curve), lo = Math.min(0, ...pts), hi = Math.max(0, ...pts), Y = v => 10 + (hi - v) / ((hi - lo) || 1) * (H - 20), X = i => 8 + i / Math.max(1, pts.length - 1) * (W - 16), n = Math.max(2, Math.ceil(prog * pts.length));
    x.strokeStyle = cssv("--bd"); x.beginPath(); x.moveTo(0, Y(0)); x.lineTo(W, Y(0)); x.stroke(); const up = pts[pts.length - 1] >= 0; x.strokeStyle = up ? cssv("--g") : cssv("--r"); x.lineWidth = 2.2; x.lineJoin = "round"; x.beginPath(); pts.slice(0, n).forEach((v, i) => i ? x.lineTo(X(i), Y(v)) : x.moveTo(X(i), Y(v))); x.stroke();
    x.lineTo(X(n - 1), Y(0)); x.lineTo(X(0), Y(0)); x.globalAlpha = .12; x.fillStyle = x.strokeStyle; x.fill(); x.globalAlpha = 1; };
  requestAnimationFrame(() => { if (RM) { draw(); return; } const t0 = performance.now(), st = t => { prog = Math.min(1, (t - t0) / 900); draw(); if (prog < 1) requestAnimationFrame(st); }; requestAnimationFrame(st); }); new ResizeObserver(draw).observe(cv); return cv;
}
async function pageJournal(main) {
  const d = await api("/api/journal"), st = d.stats, pre = JPRE; JPRE = null;
  const tile = (k, val, dec, suffix, cls) => { const n = h("b", {class: "stat num " + (cls || "")}, "0"); if (val == null) n.textContent = "–"; else { countUp(n, val, dec); } return h("div", {class: "card tile enter", i: 1}, h("span", {class: "k"}, k), h("div", {}, n, suffix ? h("span", {class: "m"}, suffix) : null)); };
  const f = {symbol: h("select", {"aria-label": "Symbol"}, ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD", "EURJPY", "GBPJPY"].map(s => h("option", {value: s}, s))), side: h("select", {"aria-label": "Side"}, ["BUY", "SELL"].map(s => h("option", {value: s}, s))),
    entry: h("input", {type: "number", step: "any", placeholder: "Entry", "aria-label": "Entry"}), stop: h("input", {type: "number", step: "any", placeholder: "Stop", "aria-label": "Stop"}), exit: h("input", {type: "number", step: "any", placeholder: "Exit (optional)", "aria-label": "Exit"}),
    grade: h("select", {"aria-label": "Grade"}, [["", "Grade (optional)"], ["A", "A"], ["B", "B"], ["C", "C"]].map(([v, l]) => h("option", {value: v}, l))), notes: h("input", {placeholder: "Notes (optional)", maxlength: 300, "aria-label": "Notes"})};
  if (pre) { f.symbol.value = pre.symbol; f.side.value = pre.side; f.entry.value = pre.entry; f.stop.value = pre.stop; f.grade.value = pre.grade; }
  const rows = d.rows.map(r => h("tr", {class: "rowh"}, h("td", {}, date(r.ts)), h("td", {}, r.symbol), h("td", {class: r.side === "BUY" ? "L" : "S"}, r.side), h("td", {class: "num"}, r.entry), h("td", {class: "num"}, r.stop), h("td", {class: "num"}, r.exit == null ? "open" : r.exit),
    h("td", {class: "num " + (r.r == null ? "m" : r.r > 0 ? "L" : "S")}, r.r == null ? "–" : (r.r > 0 ? "+" : "") + r.r + "R"), h("td", {}, r.grade || "-"), h("td", {class: "m"}, r.notes), h("td", {}, h("div", {class: "row"}, r.exit == null ? h("button", {class: "btn sm", onclick: () => closeTrade(r)}, "Close") : null, h("button", {class: "btn sm no", onclick: async () => { await api("/api/journal/delete", {id: r.id}); renderShell(); }}, "Delete")))));
  async function closeTrade(r) { const inp = h("input", {type: "number", step: "any", placeholder: "Exit price"}); const ok = await modal(close => [h("h3", {}, "Close " + r.symbol + " " + r.side), inp, h("div", {class: "row"}, h("button", {class: "btn pri", onclick: () => close(true)}, "Save"), h("button", {class: "btn", onclick: () => close(false)}, "Cancel"))]); if (!ok) return; try { await api("/api/journal/close", {id: r.id, exit: +inp.value}); renderShell(); } catch (e) { toast(e.message, "err"); } }
  main.append(top("Trade journal", "Record your own trades and see your real results. This is how you find out whether anything works.", h("a", {class: "btn sm", href: "/api/journal/export", download: "apex-journal.csv"}, "Export CSV")),
    h("div", {class: "grid g4"}, tile("Closed trades", st.closed, 0), tile("Win rate", st.win_rate == null ? null : st.win_rate * 100, 0, st.win_ci ? "%  (95% CI " + Math.round(st.win_ci[0] * 100) + "-" + Math.round(st.win_ci[1] * 100) + ")" : ""), tile("Average R", st.avg_r, 2, "R", st.avg_r > 0 ? "L" : "S"), tile("Total R", st.total_r, 1, "R", st.total_r >= 0 ? "L" : "S"), tile("Profit factor", st.profit_factor, 2), tile("Max drawdown", st.max_dd_r, 1, "R")),
    h("div", {class: "banner warn enter", i: 2}, st.note), st.curve.length ? h("div", {class: "card enter", i: 3}, h("h3", {}, "Cumulative R"), curveView(st.curve)) : null,
    h("div", {class: "card enter col", i: 4}, h("h3", {}, "Add a trade (" + d.rows.length + " of " + d.limit + ")"), h("div", {class: "row"}, f.symbol, f.side, f.entry, f.stop, f.exit, f.grade), h("div", {class: "row"}, f.notes, h("button", {class: "btn pri", onclick: async () => { try { await api("/api/journal", {symbol: f.symbol.value, side: f.side.value, entry: +f.entry.value, stop: +f.stop.value, exit: f.exit.value === "" ? null : +f.exit.value, grade: f.grade.value, notes: f.notes.value}); toast("Trade logged", "ok"); renderShell(); } catch (e) { toast(e.message, "err"); } }}, "Add trade"))),
    h("div", {class: "card enter", i: 5}, h("h3", {}, "Your trades"), rows.length ? h("table", {}, h("tr", {}, ["Date", "Symbol", "Side", "Entry", "Stop", "Exit", "Result", "Grade", "Notes", ""].map(x => h("th", {}, x))), rows) : emptyState("No trades logged yet", "Log the trades you actually take, including losers. Honest records beat any back-test.")));
}
function feedsCard(fd) {
  const rows = fd.feeds.map(f => h("div", {class: "feedrow"}, h("div", {}, h("b", {}, f.name), h("div", {class: "m"}, f.type + " · " + f.url)), h("span", {class: "m num"}, f.last_ok ? "ok " + fmtDT(new Date(f.last_ok * 1000).toISOString(), true) + " · " + f.items + " items" : "never synced"), h("span", {class: "badge " + (f.last_error ? "S" : f.last_ok ? "L" : "")}, f.last_error ? f.last_error : f.last_ok ? "healthy" : "pending")));
  const btn = h("button", {class: "btn sm", onclick: async () => { btn.disabled = true; try { const r = (await api("/api/admin/feeds/sync", {})).result, bad = Object.entries(r).filter(([, v]) => !v.ok); toast(bad.length ? bad.length + " of " + Object.keys(r).length + " feeds failed: " + bad.map(([k, v]) => k + " (" + v.error + ")").join("; ") : Object.keys(r).length + " feeds synced", bad.length ? "err" : "ok"); renderShell(); } catch (e) { toast(e.message, "err"); btn.disabled = false; } }}, "Sync now");
  return h("div", {class: "card enter", i: 7}, h("div", {class: "row"}, h("h3", {class: "grow"}, "News & calendar feeds"), btn), fd.enabled ? null : h("div", {class: "banner warn"}, "Automatic sync is off (news_enabled is false). You can still sync manually."), rows,
    h("p", {class: "m"}, "Run `news-check` on the server to verify each source is reachable. Unofficial feeds can change or be rate limited; respect each provider's terms."));
}

boot();
