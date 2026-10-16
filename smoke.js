const { JSDOM } = require("jsdom");
const BASE = "http://127.0.0.1:18090", PW = "Correct-Horse-Battery-9";
const errors = []; process.on("unhandledRejection", e => errors.push("unhandledRejection: " + (e && e.stack || e)));
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function session(email) {
  const html = await (await fetch(BASE + "/")).text(), js = await (await fetch(BASE + "/static/app.js")).text(); let cookie = "";
  const dom = new JSDOM(html, { url: BASE + "/", runScripts: "outside-only", pretendToBeVisual: true }); const w = dom.window;
  w.fetch = async (u, o = {}) => { const r = await fetch(new URL(u, BASE), { ...o, headers: { ...(o.headers || {}), Cookie: cookie } }); for (const c of (r.headers.getSetCookie ? r.headers.getSetCookie() : [])) cookie = c.includes("Max-Age=0") ? "" : c.split(";")[0]; return r; };
  w.ResizeObserver = class { observe() {} disconnect() {} }; w.HTMLCanvasElement.prototype.getContext = () => new Proxy({}, { get: (t, k) => k === "canvas" ? {} : (() => {}), set: () => true });
  w.HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); }; w.HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); this.dispatchEvent(new w.Event("close")); };
  w.matchMedia = () => ({ matches: false, addEventListener() {} }); w.confirm = () => true; w.scrollTo = () => {};
  w.addEventListener("error", e => errors.push(email + " window error: " + e.message)); w.console.error = (...a) => errors.push(email + " console.error: " + a.join(" "));
  w.eval(js + "\n;window.__t = {ring, timeline, timingCard, oppCard, renderRadar, evRow, checklistCard, compareTable, dur, fmtDT};");
  const d = w.document, q = s => d.querySelector(s), qa = s => [...d.querySelectorAll(s)], nav = t => qa(".side .nav").find(b => b.textContent.trim() === t);
  const wait = async (fn, ms = 25000) => { const t = Date.now(); while (Date.now() - t < ms) { try { const v = fn(); if (v) return v; } catch (e) {} await sleep(60); } throw new Error("timeout waiting: " + fn); };
  const click = el => { if (!el) throw new Error("click target is missing"); el.dispatchEvent(new w.MouseEvent("click", { bubbles: true })); }, type = (el, v) => { el.value = v; el.dispatchEvent(new w.Event("input", { bubbles: true })); };
  const btn = t => qa("button").find(b => b.textContent.trim() === t);
  await wait(() => q(".hero h1")); type(q("input[type=email]"), email); type(q("input[type=password]"), PW); click(q(".authcard .btn.pri")); await wait(() => q(".shell"));
  return { w, d, q, qa, nav, wait, click, type, btn, sleep };
}
(async () => {
  const r = {};
  // ---------- free ----------
  let s = await session("free@example.com"); r.free = { ribbon: !!s.q(".ribbon-test") && /TEST DATA/.test(s.q(".ribbon-test").textContent), title: s.d.title, brandInSidebar: s.q(".brand").textContent.trim(), supportLink: !!s.qa("a").find(a => /^Support:/.test(a.textContent)),  landing: s.q("h2").textContent, navSections: s.qa(".sideh").map(x => x.textContent).join("/"), navItems: s.qa(".side .nav").map(b => b.textContent.trim()).join(",") };
  await s.wait(() => s.qa(".ev").length >= 1); r.free.homeEvents = s.qa(".ev").length; r.free.radarLocked = s.qa(".empty b").some(b => /radar is a Pro feature/.test(b.textContent)); r.free.liveClock = /UTC \d\d:\d\d:\d\d/.test(s.q(".clock").textContent);
  s.click(s.nav("Scanner")); await s.wait(() => s.q(".btn.pri")); s.click(s.q(".btn.pri")); await s.wait(() => s.q(".pill") && s.q(".tl"));
  r.free.timingPill = s.q(".statusbar .pill").textContent; r.free.timeline = !!s.q(".tl .nowm"); r.free.bands = s.qa(".tl .band").length; r.free.hourBars = s.qa(".hours i").length; r.free.reasons = s.qa(".reasons li").length; r.free.newsRows = s.qa(".nw").length; r.free.upcoming = s.qa(".ev").length; r.free.countdowns = s.qa(".num.m.cd, .ev .cd").length;
  s.click(s.nav("Calendar & news")); await s.wait(() => s.qa(".ev").length >= 2); r.free.calEvents = s.qa(".ev").length; r.free.soonHighlight = s.qa(".ev.soon").length; r.free.daySeps = s.qa(".daysep").length; r.free.sentBars = s.qa(".sentbar").length; r.free.headlines = s.qa(".nw").length;
  s.click(s.qa(".cc").find(b => b.textContent === "EUR")); await s.sleep(900); r.free.eurOnly = s.qa(".ev .cur").every(c => c.textContent === "EUR") && s.qa(".ev .cur").length >= 1;
  s.click(s.nav("Journal")); await s.wait(() => s.q(".tile")); const sel = s.qa("select"); s.type(s.q("input[aria-label=Entry]"), "1.1000"); s.type(s.q("input[aria-label=Stop]"), "1.0950"); s.type(s.q("input[aria-label=Exit]"), "1.1100"); s.click(s.btn("Add trade")); await s.wait(() => s.qa("table tr").length >= 2);
  r.free.journalRows = s.qa("table tr").length - 1; r.free.journalR = s.qa("td.num.L").map(x => x.textContent).join(","); await s.wait(() => s.qa(".tile .stat").length === 6); r.free.tiles = s.qa(".tile .stat").length; r.free.csvLink = !!s.q("a[download]");
  s.click(s.nav("Account")); await s.wait(() => s.qa("h3").some(h => h.textContent === "Active sessions") && s.qa("table").length >= 1); r.free.sessions = s.qa("tr").length - 1; r.free.exportLink = !!s.q("a[href='/api/account/export'][download]"); r.free.yourData = s.qa("h3").some(h => h.textContent === "Your data");
  s.click(s.nav("Alerts")); await s.wait(() => s.qa("h3").some(h => h.textContent.includes("Pro feature"))); s.click(s.nav("Plans")); await s.wait(() => s.qa(".pcard").length === 3); r.free.radarFeature = s.qa(".pcard li").some(li => /Opportunity radar/.test(li.textContent));
  // ---------- pro ----------
  s = await session("pro@example.com"); r.pro = {}; await s.wait(() => s.btn("Run radar")); r.pro.controls = !!s.btn("Edit watchlist"); s.click(s.btn("Run radar")); await s.wait(() => s.qa(".opp").length >= 1, 40000);
  r.pro.cards = s.qa(".opp").length; r.pro.rings = s.qa(".ring").length; r.pro.statusPills = s.qa(".opp .pill.sm").length; r.pro.bestCard = !!s.q(".opp.best"); r.pro.noSetupCards = s.qa(".opp.dim").length;
  s.click(s.btn("Edit watchlist")); await s.wait(() => s.q("dialog[open] h3")); r.pro.watchBoxes = s.qa("dialog input[type=checkbox]").length; s.click(s.qa("dialog input[type=checkbox]")[0]); s.click(s.btn("Save")); await s.wait(() => !s.q("dialog[open]") && s.btn("Run radar")); r.pro.radarClearedAfterWatchlist = s.qa(".opp").length === 0; s.click(s.btn("Run radar")); await s.wait(() => s.qa(".opp").length >= 1, 40000);
  const card = s.qa(".opp")[0]; s.click(card); await s.wait(() => s.q(".pill") && s.q(".tl"), 40000); r.pro.openedScanner = s.q("h2").textContent; r.pro.autoRan = true; r.pro.grade = s.q(".grade") && s.q(".grade").textContent;
  s.d.dispatchEvent(new s.w.KeyboardEvent("keydown", { key: "k", ctrlKey: true, bubbles: true })); await s.wait(() => s.q("dialog[open] .cmd")); r.pro.paletteItems = s.qa(".cmdlist li").length; const inp = s.q(".cmd input"); s.type(inp, "calendar"); r.pro.paletteFiltered = s.qa(".cmdlist li").length;
  inp.dispatchEvent(new s.w.KeyboardEvent("keydown", { key: "Enter", bubbles: true })); await s.wait(() => s.qa(".ev").length >= 1); r.pro.paletteNavigated = s.q("h2").textContent;
  s.click(s.nav("Journal")); await s.wait(() => s.q(".tile")); r.pro.journal = true; s.click(s.nav("Opportunities")); await s.wait(() => s.q(".opp")); r.pro.radarRemembered = s.qa(".opp").length >= 1;
  // ---------- elite ----------
  s = await session("elite@example.com"); s.click(s.nav("Scanner")); await s.wait(() => s.q(".btn.pri")); s.click(s.q(".btn.pri")); await s.wait(() => s.qa(".chip").length > 40 && s.q(".tl")); r.elite = { compareBtn: !!s.btn("Compare selectivity profiles"), timing: !!s.q(".tl"), chips: s.qa(".chip").length, locked: s.qa(".chip.k").length };
  // ---------- admin ----------
  s = await session("boss@example.com"); r.admin = {}; s.click(s.nav("Admin")); await s.wait(() => s.qa("tbody tr").length >= 3); r.admin.readiness = { rows: s.qa(".rd").length, badge: (s.qa(".badge").find(b => /READY/.test(b.textContent)) || {}).textContent, fails: s.qa(".rd.fail").length, warns: s.qa(".rd.warn").length, oks: s.qa(".rd.ok").length, recheck: !!s.btn("Re-check now"), testDataRow: s.qa(".rd").some(x => /TEST DATA/.test(x.textContent)) }; r.admin.feedsCard = s.qa("h3").some(h => h.textContent.startsWith("News & calendar feeds")); r.admin.feedRows = s.qa(".feedrow").length; r.admin.feedHealthy = s.qa(".feedrow .badge").map(b => b.textContent).join(",");
  s.click(s.btn("Sync now")); await s.wait(() => s.qa(".toast").length >= 1); r.admin.syncToast = s.q(".toast").textContent; r.admin.eventsCard = s.qa("h3").some(h => h.textContent.startsWith("Economic calendar"));
  // ---------- pure renderers with synthetic data ----------
  const T = s.w.__t; const rg = T.ring(0.8, 64, "80%", "#22c55e"); r.units = { ring: rg.querySelectorAll("circle").length, dur: [T.dur(3723000), T.dur(125000), T.dur(9000)].join("|"), tlBands: 0 };
  const tl = T.timeline({ now_utc: "2026-10-07T12:00:00Z", sessions: [{ name: "london", start: "2026-10-07T07:00:00Z", end: "2026-10-07T16:00:00Z" }, { name: "newyork", start: "2026-10-07T12:00:00Z", end: "2026-10-07T21:00:00Z" }], blackouts: [{ name: "CPI", currency: "USD", time: "2026-10-07T12:30:00Z", start: "2026-10-07T12:00:00Z", end: "2026-10-07T12:45:00Z" }], next_window: { start: "2026-10-07T12:45:00Z", end: "2026-10-07T16:45:00Z" } });
  r.units.tlBands = tl.querySelectorAll(".band").length; r.units.tlBlackout = tl.querySelectorAll(".black").length; r.units.tlWindow = tl.querySelectorAll(".win").length; r.units.tlNow = tl.querySelectorAll(".nowm").length; r.units.axis = tl.querySelectorAll(".axis span").length; r.units.nowAt = tl.querySelector(".nowm").style.getPropertyValue("--l");
  console.log(JSON.stringify(r, null, 1)); console.log(errors.length ? "RUNTIME ERRORS:\n" + errors.join("\n") : "NO RUNTIME ERRORS"); process.exit(errors.length ? 1 : 0);
})().catch(e => { console.log("SMOKE FAILED:", e.stack); console.log(errors.join("\n")); process.exit(2); });
