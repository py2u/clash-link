"use strict";

/* AIClash 控制台前端 */

const $ = (id) => document.getElementById(id);

const STATUS_TEXT = { pass: "正常", warn: "注意", fail: "异常", info: "信息", pending: "等待" };
const VERDICT_PILL = { pass: ["pass", "可用"], warn: ["warn", "有风险"], fail: ["fail", "不可用"] };
const VERDICT_COLOR = { pass: "var(--pass)", warn: "var(--warn)", fail: "var(--fail)" };
const RING_CIRCUMFERENCE = 339.29;
const FALLBACK_GROUPS = ["出口与代理", "Claude 连通性", "环境一致性"];

const state = {
  version: "1.0.0",
  port: 0,
  settings: {},
  report: null,
  clients: [],
  job: null,
  watcher: null,
  rows: new Map(),
  builtGroups: "",
  timer: 0,
  running: false,
  tz: { system: "", exit: "" },
  offline: false,
  nodes: { data: null, group: "", busy: false, tried: {}, query: "", sort: "default" },
  subs: { items: [], core: null, sysproxy: null, busy: false },
  session: { events: [], backup: false },
};

/* ------------------------------------------------------------------ */
/* 请求                                                                */

const API_TOKEN = (document.querySelector('meta[name="clashlink-token"]') || {}).content || "";

async function api(path, options = {}) {
  const init = { method: options.method || "GET", cache: "no-store", headers: {} };
  if (API_TOKEN) init.headers["X-ClashLink-Token"] = API_TOKEN;
  if (options.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch (err) {
    setOffline(true);
    throw new Error("无法连接本地控制台");
  }
  setOffline(false);
  const text = await res.text();
  let data = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch (err) {
    data = { raw: text };
  }
  if (!res.ok) {
    throw new Error(data.error || data.message || `HTTP ${res.status}`);
  }
  return data;
}

function setOffline(off) {
  if (state.offline === off) return;
  state.offline = off;
  $("offline").hidden = !off;
}

/* ------------------------------------------------------------------ */
/* 启动                                                                */


const THEME_ORDER = ["dark", "light", "auto"];
const THEME_LABEL = { dark: "深色", light: "浅色", auto: "自动" };

function resolveTheme(mode) {
  if (mode === "auto") {
    return matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }
  return mode === "light" ? "light" : "dark";
}

function applyTheme(mode) {
  const next = THEME_ORDER.includes(mode) ? mode : "dark";
  document.documentElement.dataset.theme = resolveTheme(next);
  document.documentElement.dataset.themeMode = next;
  try { localStorage.setItem("claudenet.theme", next); } catch (e) {}
  if (document.getElementById("theme-seg")) updateThemeSeg();
}

async function chooseTheme(mode) {
  applyTheme(mode);
  state.settings.theme = mode;
  updateThemeSeg();
  try {
    await api("/api/settings", { method: "POST", body: { theme: mode } });
  } catch (err) {
    toast("主题已切换，但没能保存到设置", "warn");
  }
}

function currentTheme() {
  return document.documentElement.dataset.themeMode || "dark";
}

matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => {
  if (currentTheme() === "auto") applyTheme("auto");
});

async function boot() {
  const params = new URLSearchParams(location.search);
  bindEvents();
  setupEnvAccordions();
  applyTheme(currentTheme());
  updateClocks();
  setInterval(updateClocks, 1000);
  try {
    const info = await api("/api/state");
    state.version = info.version || state.version;
    state.port = info.port || 0;
    state.settings = info.settings || {};
    applyLang(state.settings.lang === "en" ? "en" : "zh");
    renderRepair(info);
    loadClients();
    applyTheme(params.get("theme") || state.settings.theme || currentTheme());
    state.watcher = info.watcher || null;
    state.dataDir = info.data_dir || "";
    if (info.system_tz) state.tz.system = info.system_tz;

    if (info.report) {
      renderReport(info.report, { cached: true });
    } else {
      $("verdict-title").textContent = "正在检测网络环境…";
    }
    renderProject();
    renderFooter();
    const initial = (location.hash || "").replace("#", "") || "desk";
    switchView(initial, { silent: true });
    if (initial === "history") loadHistory();
    renderTiles();
    loadNodes();
    loadSubs();
    setInterval(async () => {
      await loadSubs(true);
      if (state.subs.core && state.subs.core.running && !state.nodes.busy) await loadNodes();
      renderFooter();
    }, 8000);

    const job = info.last_job;
    if (job && (job.state === "running" || job.state === "queued")) {
      setRunning(true);
      poll(job.id);
    } else if (params.has("noauto")) {
      // 仅供截图/调试：只展示上次结果
      setRunning(false);
    } else {
      // 内核（尤其 TUN）可能还在后台拉起，等它就绪再扫，避免把「代理未就绪」误报成异常
      if (state.settings.tun || state.settings.active_sub) {
        const core = await waitForCore();
        if (core) renderTiles();
      }
      startScan();
    }
  } catch (err) {
    failBoot(err);
  }
}

async function waitForCore(timeoutMs = 25000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try {
      const info = await api("/api/subscriptions");
      if (info.core && info.core.running) {
        state.subs.core = info.core;
        return info.core;
      }
    } catch (err) {
      // 忽略，继续等
    }
    await new Promise((resolve) => setTimeout(resolve, 800));
  }
  return null;
}

function failBoot(err) {
  $("verdict-title").textContent = "无法连接本地控制台";
  $("verdict-sub").textContent = String(err.message || err);
  $("pill-text").textContent = "离线";
  $("live-pill").className = "pill fail";
  $("btn-scan").disabled = true;
  setOffline(true);
}

/* ------------------------------------------------------------------ */
/* 扫描                                                                */

async function startScan() {
  if (state.running) return;
  setRunning(true);
  clearTimeout(state.timer);
  try {
    const res = await api("/api/scan", { method: "POST", body: {} });
    poll(res.job_id);
  } catch (err) {
    setRunning(false);
    toast("检测启动失败：" + err.message, "fail");
  }
}

async function poll(id) {
  let job;
  try {
    job = await api("/api/scan/" + encodeURIComponent(id));
  } catch (err) {
    setRunning(false);
    clearProgress();
    toast("读取检测进度失败：" + err.message, "fail");
    return;
  }
  state.job = job;

  if (Array.isArray(job.checks) && job.checks.length) {
    ensureGroups(job.checks);
    renderChecks(job.checks);
  }
  setProgress(job.progress || 0, job.completed || 0, job.total || 13);

  if (job.state === "running" || job.state === "queued") {
    $("pill-text").textContent = `检测中 ${job.completed || 0}/${job.total || 13}`;
    state.timer = setTimeout(() => poll(id), 380);
    return;
  }

  setRunning(false);
  clearProgress();

  if (job.state === "done" && job.report) {
    renderReport(job.report, { fresh: true });
    loadNodes();
  } else {
    toast(`检测失败：${job.error || job.state || "未知错误"}`, "fail");
    $("pill-text").textContent = "检测失败";
    $("live-pill").className = "pill fail";
  }
}

const VIEWS = ["home", "desk", "proxy", "subs", "rules", "history", "env"];
const I18N = {
  zh: {
    nav: { home: "主页", desk: "测试", proxy: "连接", subs: "订阅", rules: "规则", history: "日志", env: "设置" },
    meta: {
      home: ["主页", "选节点、项目目录和 AI 客户端，一键准备并打开。"],
      desk: ["测试", "时区、状态与逐项体检结果。"],
      proxy: ["连接", "运行模式、代理组、实时流量与连接。"],
      subs: ["订阅", "订阅链接、节点列表与内核。"],
      rules: ["规则", "生效规则、规则集与自定义规则。"],
      history: ["日志", "最近的检测记录与内核日志。"],
      env: ["设置", "客户端、内核、DNS 与权限。"],
    },
    scan: "重新检测",
    repair: "一键修复",
  },
  en: {
    nav: { home: "Home", desk: "Test", proxy: "Proxy", subs: "Profile", rules: "Rules", history: "Logs", env: "Settings" },
    meta: {
      home: ["Home", "Pick a node, project folder and AI client, then launch."],
      desk: ["Test", "Clock, status and the item-by-item check."],
      proxy: ["Proxy", "Mode, groups, live traffic and connections."],
      subs: ["Profile", "Subscription link, nodes and kernel."],
      rules: ["Rules", "Active rules, rule sets and custom rules."],
      history: ["Logs", "Recent checks and kernel logs."],
      env: ["Settings", "Client, kernel, DNS and permissions."],
    },
    scan: "Scan again",
    repair: "Repair",
  },
};

function currentLang() {
  const lang = (state.settings && state.settings.lang) || "zh";
  return I18N[lang] ? lang : "zh";
}

function applyLang() {
  const lang = currentLang();
  const dict = I18N[lang];
  for (const item of document.querySelectorAll(".nav-item")) {
    const label = item.querySelector("span");
    if (label && dict.nav[item.dataset.view]) label.textContent = dict.nav[item.dataset.view];
  }
  const meta = dict.meta[state.view] || dict.meta.home;
  const t = $("page-title");
  const sub = $("page-sub");
  if (t) t.textContent = meta[0];
  if (sub) sub.textContent = meta[1];
  const scan = $("btn-scan");
  if (scan) {
    const span = scan.querySelector("span");
    if (span) span.textContent = dict.scan;
  }
  const repair = $("btn-repair");
  if (repair) {
    const span = repair.querySelector("span");
    if (span) span.textContent = dict.repair;
  }
  if (window.applyI18n) window.applyI18n(lang);
}

const VIEW_META = {
  home: ["主页", "选节点、项目目录和 AI 客户端，一键准备并打开。"],
  desk: ["测试", "时区、状态与逐项体检结果。"],
  proxy: ["连接", "运行模式、代理组、实时流量与连接。"],
  subs: ["订阅", "订阅链接、节点列表与内核。"],
  rules: ["规则", "生效规则、规则集与自定义规则。"],
  history: ["日志", "最近的检测记录与内核日志。"],
  env: ["设置", "客户端、内核、DNS 与权限。"],
};

function switchView(name, opts = {}) {
  const view = VIEWS.includes(name) ? name : "desk";
  for (const item of document.querySelectorAll(".nav-item")) {
    item.classList.toggle("active", item.dataset.view === view);
  }
  for (const section of document.querySelectorAll(".view")) {
    section.hidden = section.dataset.view !== view;
  }
  state.view = view;
  const meta = (I18N[currentLang()].meta[view]) || VIEW_META[view] || VIEW_META.desk;
  const t = $("page-title");
  const sub = $("page-sub");
  if (t) t.textContent = meta[0];
  if (sub) sub.textContent = meta[1];
  if (!opts.silent) {
    try { history.replaceState(null, "", "#" + view); } catch (e) {}
  }
  if (view === "history") { loadHistory(); loadUsage(); }
  applyLang();
  if (view === "env") { setupEnvAccordions(); checkKernelUpdate(true); }
  if (view === "env") loadSession();
  if (view === "env") renderEnv();
  if (view === "proxy") startProxyPolling();
  else stopProxyPolling();
  if (view === "rules") { loadRules(); loadProviders(); }
}


// ---- 代理页：模式 / 代理组 / 流量 / 连接 / 日志 ---------------------------
const proxyPoll = { traffic: null, conn: null, log: null, logSeq: 0, tab: "conn" };

function stopProxyPolling() {
  for (const key of ["traffic", "conn", "log", "groups"]) {
    if (proxyPoll[key]) { clearInterval(proxyPoll[key]); proxyPoll[key] = null; }
  }
}

async function startProxyPolling() {
  stopProxyPolling();
  await Promise.all([loadProxyConfig(), loadProxyGroups(), loadTraffic(), loadConnections(), loadLogs(true)]);
  proxyPoll.traffic = setInterval(loadTraffic, 1000);
  proxyPoll.conn = setInterval(loadConnections, 1500);
  proxyPoll.log = setInterval(() => loadLogs(false), 2000);
  proxyPoll.groups = setInterval(() => { loadProxyGroups(); loadProxyConfig(); }, 5000);
}

async function loadProxyConfig() {
  try {
    const data = await api("/api/clash/configs");
    renderMode(data.mode);
    $("proxy-groups-meta").textContent = `端口 ${data.port || "—"}`;
  } catch (err) {
    renderMode("");
    $("proxy-groups-meta").textContent = "内核未运行";
  }
}

function renderMode(mode) {
  for (const btn of document.querySelectorAll(".mode-btn")) {
    btn.classList.toggle("active", btn.dataset.mode === mode);
  }
}

function renderRepair(info) {
  const banner = $("repair-banner");
  if (!banner) return;
  const message = (info && info.last_error) || "";
  banner.hidden = !message;
  if (message) $("repair-text").textContent = message;
}

async function repairNetwork() {
  const btn = $("btn-repair");
  if (btn) btn.disabled = true;
  try {
    const res = await api("/api/repair", { method: "POST", body: {} });
    if (!res.ok) throw new Error(res.message || "修复失败");
    toast(res.steps && res.steps.length ? "已修复：" + res.steps.join("、") : "网络状态正常，无需修复");
    const info = await api("/api/state");
    state.settings = info.settings || state.settings;
    renderRepair(info);
    await loadSubs(true);
    await loadNodes();
  } catch (err) {
    toast("修复失败：" + (err.message || err), "fail");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function toggleFavorite(name, on) {
  try {
    const res = await api("/api/nodes/favorite", { method: "POST", body: { name, on } });
    state.nodes.data.favorites = res.favorites || [];
    renderNodes();
  } catch (err) {
    toast("收藏失败：" + (err.message || err), "fail");
  }
}

async function setMode(mode) {
  try {
    await api("/api/clash/mode", { method: "POST", body: { mode } });
    renderMode(mode);
    toast("已切换到" + ({ rule: "规则模式", global: "全局模式", direct: "直连模式" }[mode] || mode));
  } catch (err) {
    toast("切换失败：" + (err.message || err), "fail");
  }
}

async function loadProxyGroups() {
  const box = $("proxy-groups");
  if (!box) return;
  try {
    const data = state.nodes.data && state.nodes.data.available ? state.nodes.data : await api("/api/nodes");
    state.nodes.data = data;
    if (data.available && data.version) {
      const ep = data.endpoint && data.endpoint.address ? String(data.endpoint.address) : "";
      const port = (state.subs.core && state.subs.core.mixed_port) || ep.split(":").pop() || "—";
      $("proxy-groups-meta").textContent = `内核 ${data.version} · 端口 ${port}`;
    }
    if (!data.available) {
      box.innerHTML = `<p class="empty">${nodesHelpText(data.reason)}</p>`;
      return;
    }
    const groups = (data.groups || []).filter((g) => g.name !== "GLOBAL");
    box.innerHTML = "";
    for (const group of groups) {
      const card = document.createElement("div");
      card.className = "group-card";
      const head = document.createElement("div");
      head.className = "group-head";
      head.innerHTML = `<strong>${group.name}</strong><span class="group-type">${group.type}</span><span class="group-now">当前 ${shortNodeName(group.now)}${group.now_delay ? " · " + group.now_delay + " ms" : ""}</span>`;
      card.appendChild(head);
      const chips = document.createElement("div");
      chips.className = "group-chips";
      for (const node of group.nodes || []) {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "group-chip" + (node.name === group.now ? " active" : "");
        chip.textContent = shortNodeName(node.name);
        if (node.delay) chip.title = node.delay + " ms";
        chip.addEventListener("click", () => selectNodeFromProxy(group.name, node.name));
        chips.appendChild(chip);
      }
      card.appendChild(chips);
      box.appendChild(card);
    }
  } catch (err) {
    box.innerHTML = `<p class="empty">读取代理组失败：${err.message}</p>`;
  }
}

async function selectNodeFromProxy(group, name) {
  try {
    await api("/api/nodes/select", { method: "POST", body: { group, name } });
    toast("已切换到 " + shortNodeName(name));
    loadProxyGroups();
    setTimeout(() => startScan(), 1000);
  } catch (err) {
    toast("切换失败：" + (err.message || err), "fail");
  }
}

function fmtRate(bytes) {
  if (!bytes) return "0 B/s";
  if (bytes < 1024) return bytes + " B/s";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB/s";
  return (bytes / 1024 / 1024).toFixed(2) + " MB/s";
}

async function loadTraffic() {
  try {
    const data = await api("/api/traffic");
    $("traffic-up").textContent = fmtRate(data.up);
    $("traffic-down").textContent = fmtRate(data.down);
    $("traffic-total").textContent = `累计 ↓ ${fmtBytes(data.down_total)} · ↑ ${fmtBytes(data.up_total)}`;
    drawTraffic(data.samples || []);
  } catch (err) {
    // 内核没跑时静默
  }
}

function drawTraffic(samples) {
  const down = $("t-down");
  const up = $("t-up");
  if (!down || !up) return;
  const max = Math.max(1024, ...samples.map((s) => Math.max(s.down, s.up)));
  const w = 600, h = 120;
  const pts = (key) => samples.map((s, i) => {
    const x = samples.length <= 1 ? 0 : (i / (samples.length - 1)) * w;
    const y = h - (s[key] / max) * (h - 10) - 4;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  down.setAttribute("points", pts("down"));
  up.setAttribute("points", pts("up"));
}

async function copyText(text) {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(String(text));
    toast("已复制：" + text);
  } catch (err) {
    toast("复制失败：" + (err.message || err), "fail");
  }
}

async function loadConnections() {
  const box = $("conn-list");
  if (!box) return;
  try {
    const data = await api("/api/clash/connections");
    state.conn = Object.assign({ query: "", sort: "recent" }, state.conn, { data });
    renderConnections();
  } catch (err) {
    box.innerHTML = `<p class="empty">读取连接失败：${err.message || err}</p>`;
  }
}

function renderConnections() {
  const box = $("conn-list");
  if (!box) return;
  const data = (state.conn && state.conn.data) || { items: [], count: 0 };
  const q = ((state.conn && state.conn.query) || "").trim().toLowerCase();
  const sort = (state.conn && state.conn.sort) || "recent";
  let items = (data.items || []).slice();
  if (q) {
    items = items.filter((i) => [i.host, i.dest, i.rule, i.rule_payload, (i.chains || []).join(" ")]
      .filter(Boolean).some((v) => String(v).toLowerCase().includes(q)));
  }
  if (sort === "down") items.sort((a, b) => (b.download || 0) - (a.download || 0));
  if (sort === "up") items.sort((a, b) => (b.upload || 0) - (a.upload || 0));
  $("conn-count").textContent = `${items.length} / ${data.count || 0} 条连接`;
  $("conn-traffic").textContent = `↓ ${fmtBytes(data.down_total)} · ↑ ${fmtBytes(data.up_total)}`;
  box.innerHTML = "";
  if (!items.length) {
    box.innerHTML = '<p class="empty">没有匹配的连接</p>';
    return;
  }
  for (const item of items) {
    const row = document.createElement("div");
    row.className = "conn-row";
    const chain = (item.chains || []).filter((c) => c !== "DIRECT").join(" → ") || "直连";
    row.innerHTML = `
      <div class="conn-main">
        <strong>${item.host || item.dest || "未知目标"}</strong>
        <em>${chain}${item.rule ? " · " + item.rule + (item.rule_payload ? "(" + item.rule_payload + ")" : "") : ""}</em>
      </div>
      <div class="conn-nums"><span>↓ ${fmtBytes(item.download)}</span><span>↑ ${fmtBytes(item.upload)}</span></div>
      <button class="conn-close" type="button" title="断开">✕</button>`;
    row.querySelector(".conn-close").addEventListener("click", async (ev) => {
      ev.stopPropagation();
      try {
        await api("/api/clash/connections/close", { method: "POST", body: { id: item.id } });
        row.remove();
      } catch (err) {
        toast("断开失败：" + (err.message || err), "fail");
      }
    });
    row.title = "点击复制目标地址";
    row.addEventListener("click", () => copyText(item.host || item.dest || ""));
    box.appendChild(row);
  }
}

async function loadLogs(reset) {
  try {
    const data = await api(`/api/logs?since=${reset ? 0 : proxyPoll.logSeq}&limit=200`);
    if (reset) state.logItems = [];
    for (const line of data.items || []) state.logItems.push(line);
    if (state.logItems.length > 400) state.logItems = state.logItems.slice(-400);
    proxyPoll.logSeq = data.seq || proxyPoll.logSeq;
    renderLogs();
  } catch (err) {
    if (reset) {
      state.logItems = [];
      const box = $("log-list");
      if (box) box.innerHTML = '<p class="empty">内核未运行</p>';
    }
  }
}

function renderLogs() {
  const box = $("log-list");
  if (!box) return;
  const q = (state.logQuery || "").trim().toLowerCase();
  const level = state.logLevel || "all";
  const items = (state.logItems || []).filter((line) => {
    if (level !== "all" && (line.type || "info") !== level) return false;
    if (q && !String(line.payload || "").toLowerCase().includes(q)) return false;
    return true;
  });
  box.innerHTML = "";
  if (!items.length) {
    box.innerHTML = '<p class="empty">没有匹配的日志</p>';
    return;
  }
  for (const line of items) {
    const el = document.createElement("div");
    el.className = "log-row " + (line.type || "info");
    const t = new Date((line.at || 0) * 1000);
    el.innerHTML = `<time>${t.toLocaleTimeString("zh-CN", { hour12: false })}</time><span>${line.payload || ""}</span>`;
    box.appendChild(el);
  }
  box.scrollTop = box.scrollHeight;
}

async function runRuleTest() {
  const input = $("rule-test-input");
  const out = $("rule-test-result");
  if (!input || !out) return;
  const target = (input.value || "").trim();
  if (!target) return;
  out.textContent = "匹配中…";
  try {
    const r = await api("/api/clash/test-rule", { method: "POST", body: { target } });
    if (!r.ok) {
      out.textContent = r.message || "测试失败";
      return;
    }
    if (!r.rule) {
      out.textContent = `没有命中任何规则，会走最后的 MATCH 出站（共 ${r.total} 条规则）`;
      return;
    }
    out.textContent = `命中第 ${(r.index ?? 0) + 1} 条：${r.rule.type} ${r.rule.payload || ""} ⇒ ${r.rule.proxy}（共 ${r.total} 条）`;
  } catch (err) {
    out.textContent = "测试失败：" + (err.message || err);
  }
}

function exportLogs() {
  const lines = (state.logItems || []).map((line) => {
    const t = new Date((line.at || 0) * 1000).toISOString();
    return `${t} [${line.type || "info"}] ${line.payload || ""}`;
  });
  if (!lines.length) {
    toast("还没有日志可以导出", "fail");
    return;
  }
  const blob = new Blob([lines.join("\n")], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `clashlink-log-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.txt`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
  toast("日志已导出");
}


function switchProxyTab(tab) {
  proxyPoll.tab = tab;
  for (const btn of document.querySelectorAll("#proxy-tabs .tab")) {
    btn.classList.toggle("active", btn.dataset.tab === tab);
  }
  $("tab-conn").hidden = tab !== "conn";
  $("tab-log").hidden = tab !== "log";
  $("btn-close-all").hidden = tab !== "conn";
}

async function closeAllConnections() {
  try {
    await api("/api/clash/connections/close", { method: "POST", body: { all: true } });
    toast("已断开全部连接");
    loadConnections();
  } catch (err) {
    toast("操作失败：" + err.message, "error");
  }
}


// ---- 规则页 ---------------------------------------------------------------
let rulesCache = [];

async function loadRules() {
  const box = $("rules-list");
  if (!box) return;
  try {
    const data = await api("/api/clash/rules");
    rulesCache = data.items || [];
    $("rules-meta").textContent = `共 ${data.count} 条，命中按从上到下顺序`;
    renderRules();
  } catch (err) {
    $("rules-meta").textContent = "内核未运行";
    box.innerHTML = '<p class="empty">启用订阅后才能读取规则。</p>';
  }
  try {
    const settings = state.settings || {};
    if (Array.isArray(settings.custom_rules) && !$("rules-editor").value.trim()) {
      $("rules-editor").value = settings.custom_rules.join("\n");
    }
  } catch (err) { /* 忽略 */ }
}

function renderRules() {
  const box = $("rules-list");
  if (!box) return;
  const query = ($("rules-search").value || "").trim().toLowerCase();
  const items = !query
    ? rulesCache
    : rulesCache.filter((r) =>
        (r.type + " " + r.payload + " " + r.proxy).toLowerCase().includes(query));
  box.innerHTML = "";
  if (!items.length) {
    box.innerHTML = '<p class="empty">没有匹配的规则</p>';
    return;
  }
  for (const rule of items.slice(0, 300)) {
    const row = document.createElement("div");
    row.className = "rule-row";
    row.innerHTML = `<span class="rule-type">${rule.type}</span><span class="rule-payload">${rule.payload || "—"}</span><span class="rule-proxy">${rule.proxy}</span>`;
    box.appendChild(row);
  }
  if (items.length > 300) {
    const more = document.createElement("p");
    more.className = "empty";
    more.textContent = `还有 ${items.length - 300} 条，输入关键字缩小范围`;
    box.appendChild(more);
  }
}

async function loadProviders() {
  const box = $("providers-list");
  if (!box) return;
  try {
    const data = await api("/api/clash/providers");
    $("providers-meta").textContent = `共 ${data.count} 个`;
    box.innerHTML = "";
    if (!data.count) {
      box.innerHTML = '<p class="empty">订阅里没有 rule-providers，可以在下面自己加。</p>';
    }
    for (const item of data.items) {
      const row = document.createElement("div");
      row.className = "rule-row provider-row";
      row.innerHTML = `<span class="rule-type">${item.behavior || "—"}</span><span class="rule-payload">${item.name} · ${item.count} 条</span><span class="rule-proxy">${item.type || ""}</span>`;
      const btn = document.createElement("button");
      btn.className = "btn subtle small";
      btn.textContent = "更新";
      btn.addEventListener("click", () => refreshProvider(item.name, btn));
      row.appendChild(btn);
      row.title = "点击复制目标地址";
      row.addEventListener("click", () => copyText(item.host || item.dest || ""));
      box.appendChild(row);
    }
  } catch (err) {
    $("providers-meta").textContent = "内核未运行";
    box.innerHTML = '<p class="empty">启用订阅后才能读取规则集。</p>';
  }
  const providers = (state.settings && state.settings.custom_providers) || [];
  if (providers.length && !$("providers-editor").value.trim()) {
    $("providers-editor").value = providers
      .map((item) => `${item.name} ${item.behavior} ${item.url}`)
      .join("\n");
  }
}

async function refreshProvider(name, btn) {
  if (btn) btn.disabled = true;
  try {
    await api("/api/clash/providers/update", { method: "POST", body: { name } });
    toast(`已更新 ${name}`);
    loadProviders();
  } catch (err) {
    toast("更新失败：" + err.message, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function updateAllProviders() {
  try {
    const data = await api("/api/clash/providers");
    for (const item of data.items) {
      try {
        await api("/api/clash/providers/update", { method: "POST", body: { name: item.name } });
      } catch (err) { /* 单个失败不阻塞 */ }
    }
    toast(`已更新 ${data.count} 个规则集`);
    loadProviders();
  } catch (err) {
    toast("更新失败：" + err.message, "error");
  }
}

async function saveCustomProviders() {
  try {
    const data = await api("/api/clash/providers/custom", {
      method: "POST",
      body: { text: $("providers-editor").value || "" },
    });
    toast(data.count ? `已保存 ${data.count} 个规则集` : "已清空自定义规则集");
    loadProviders();
  } catch (err) {
    toast("保存失败：" + err.message, "error");
  }
}

async function saveCustomRules() {
  try {
    const data = await api("/api/clash/rules/custom", {
      method: "POST",
      body: { text: $("rules-editor").value || "" },
    });
    toast(data.count ? `已保存 ${data.count} 条自定义规则` : "已清空自定义规则");
    loadRules();
  } catch (err) {
    toast("保存失败：" + err.message, "error");
  }
}

async function reloadKernelConfig() {
  try {
    await api("/api/clash/reload", { method: "POST", body: {} });
    toast("已重新载入配置");
    loadRules();
  } catch (err) {
    toast("重载失败：" + err.message, "error");
  }
}

function linesOf(id) {
  return ($(id).value || "").split("\n").map((s) => s.trim()).filter(Boolean);
}

async function saveDnsSettings() {
  const payload = {
    dns_enabled: $("env-dns-enabled").checked,
    dns_mode: $("env-dns-mode").value,
    dns_nameservers: linesOf("env-dns-nameserver"),
    dns_fallback: linesOf("env-dns-fallback"),
    dns_fakeip_filter: linesOf("env-dns-filter"),
  };
  try {
    const data = await api("/api/clash/options", { method: "POST", body: payload });
    state.settings = data.settings || state.settings;
    toast(payload.dns_enabled ? "DNS 已保存并热重载" : "DNS 覆写已关闭");
  } catch (err) {
    toast("保存失败：" + err.message, "error");
  }
}

function renderDnsSettings(st) {
  if (!$("env-dns-enabled")) return;
  $("env-dns-enabled").checked = !!st.dns_enabled;
  $("env-dns-mode").value = st.dns_mode || "fake-ip";
  const join = (v) => (Array.isArray(v) ? v.join("\n") : "");
  $("env-dns-nameserver").value = join(st.dns_nameservers);
  $("env-dns-fallback").value = join(st.dns_fallback);
  $("env-dns-filter").value = join(st.dns_fakeip_filter);
}

async function saveKernelOptions() {
  try {
    const data = await api("/api/clash/options", {
      method: "POST",
      body: {
        allow_lan: $("env-allow-lan").checked,
        ipv6: $("env-kernel-ipv6").checked,
        log_level: $("env-log-level").value,
        tun_stack: $("env-tun-stack").value,
      },
    });
    state.settings = data.settings || state.settings;
    toast(data.restarted ? "已保存，正在用新参数重启内核" : "已保存");
  } catch (err) {
    toast("保存失败：" + err.message, "error");
  }
}


function renderTiles() {
  const core = state.subs.core || {};
  const proxy = state.subs.sysproxy || {};
  const report = state.report;

  const proxyTile = $("tile-proxy");
  const proxySub = $("tile-proxy-sub");
  if (proxy.enabled) {
    proxyTile.textContent = "已接管";
    proxyTile.parentElement.dataset.level = "pass";
    proxySub.textContent = `${proxy.host || "127.0.0.1"}:${proxy.port || "—"} · ${proxy.service || ""}`.trim();
  } else if (core.running && core.tun) {
    proxyTile.textContent = "TUN 接管";
    proxyTile.parentElement.dataset.level = "pass";
    proxySub.textContent = "整机流量走虚拟网卡，不需要系统代理";
  } else {
    proxyTile.textContent = "未接管";
    proxyTile.parentElement.dataset.level = "warn";
    proxySub.textContent = "浏览器仍走系统原来的网络设置";
  }

  const modeTile = $("tile-mode");
  const modeSub = $("tile-mode-sub");
  if (core.running) {
    modeTile.textContent = core.tun ? "TUN 模式" : "独立内核";
    modeTile.parentElement.dataset.level = "pass";
    modeSub.textContent = core.tun
      ? "虚拟网卡 198.18.0.1 · 无需系统代理"
      : `mihomo ${core.version || ""} · 混合端口 ${core.mixed_port}`.replace("  ", " ");
  } else {
    modeTile.textContent = "待连接";
    modeTile.parentElement.dataset.level = "warn";
    modeSub.textContent = "到「订阅管理」启用一条订阅";
  }

  const apiCheck = report && (report.checks || []).find((c) => c.id === "api");
  const apiTile = $("tile-api");
  const apiSub = $("tile-api-sub");
  if (apiCheck && apiCheck.status !== "pending") {
    apiTile.textContent = apiCheck.status === "pass" ? "正常" : apiCheck.status === "warn" ? "偏慢" : "异常";
    apiTile.parentElement.dataset.level = apiCheck.status === "pass" ? "pass" : apiCheck.status === "warn" ? "warn" : "fail";
    apiSub.textContent = apiCheck.summary || "";
  } else {
    apiTile.textContent = "待检测";
    apiTile.parentElement.dataset.level = "";
    apiSub.textContent = "点「开始会话并检测」";
  }

  const tzTile = $("tile-tz");
  const tzSub = $("tile-tz-sub");
  if (tzTile) {
    const tz = (report && report.timezone) || {};
    const cmp = tz.comparison || {};
    if (!tz.exit) {
      tzTile.textContent = "未知";
      tzTile.parentElement.dataset.level = "";
      tzSub.textContent = "还没有取到出口时区";
    } else if (cmp.same_name || cmp.same_offset) {
      tzTile.textContent = "已对齐";
      tzTile.parentElement.dataset.level = "pass";
      tzSub.textContent = tz.system || cmp.system_name || "";
    } else {
      tzTile.textContent = "不一致";
      tzTile.parentElement.dataset.level = "warn";
      tzSub.textContent = `${tz.system || "系统"} → ${tz.exit}`;
    }
  }
  renderSession();
}

function renderProject() {
  const box = $("desk-project");
  if (box) box.value = (state.settings && state.settings.project_dir) || "";
  const req = $("link-requirements");
  if (req) req.onclick = (event) => {
    event.preventDefault();
    api("/api/open-url", { method: "POST", body: { url: req.href } }).catch(() => {});
  };
}

async function browseProject() {
  try {
    const res = await api("/api/project/browse", { method: "POST", body: {} });
    if (res.ok) {
      state.settings.project_dir = res.project_dir;
      renderProject();
      toast("项目文件夹已设置", "ok");
    }
  } catch (err) {
    if (String(err.message || "").includes("取消")) return;
    toast(err.message || "选择目录失败", "fail");
  }
}

function renderSession() {
  const core = state.subs.core || {};
  const proxy = state.subs.sysproxy || {};
  const chip = $("session-state");
  const sub = $("session-sub");
  const box = $("desk-proxy");
  if (!chip) return;
  if (box) box.checked = !!(core.tun || proxy.enabled);
  if (proxy.enabled && core.running) {
    chip.textContent = "会话中";
    chip.dataset.state = "on";
    sub.textContent = window.t ? `System proxy points to 127.0.0.1:${proxy.port || core.mixed_port || "—"} — browsers and Claude use it.` : `系统代理已指向 127.0.0.1:${proxy.port || core.mixed_port || "—"}，浏览器与 Claude 桌面端都走这条线路。`;
  } else if (core.running && core.tun) {
    chip.textContent = "TUN 已接管";
    chip.dataset.state = "live";
    sub.textContent = window.t ? window.t("整机流量已走虚拟网卡，浏览器、Claude 桌面端都自动走这条线路，不用再开系统代理。") : "整机流量已走虚拟网卡，浏览器、Claude 桌面端都自动走这条线路，不用再开系统代理。";
  } else if (core.running) {
    chip.textContent = "内核已就绪";
    chip.dataset.state = "ready";
    sub.textContent = window.t ? window.t("内核在跑，但还没接管系统代理；点「开始会话」让浏览器和 Claude 也走这里。") : "内核在跑，但还没接管系统代理；点「开始会话」让浏览器和 Claude 也走这里。";
  } else {
    chip.textContent = "未连接";
    chip.dataset.state = "idle";
    sub.textContent = window.t ? window.t("还没有启用订阅；先到「机场订阅」粘贴链接并启用，再回来开始会话。") : "还没有启用订阅；先到「订阅管理」粘贴链接并启用，再回来开始会话。";
  }
  syncDeskNode();
}

function syncDeskNode() {
  const select = $("desk-node");
  const group = currentNodeGroup();
  if (!select) return;
  if (!group) {
    select.innerHTML = '<option value="">未启用订阅</option>';
    select.disabled = true;
    return;
  }
  select.disabled = false;
  const key = group.name + "|" + group.nodes.map((n) => `${n.name}:${n.delay ?? ""}`).join(",");
  if (select.dataset.key !== key) {
    select.dataset.key = key;
    select.innerHTML = group.nodes
      .map((n) => {
        const ms = typeof n.delay === "number" ? ` · ${n.delay} ms` : "";
        return `<option value="${n.name.replace(/"/g, "&quot;")}">${shortNodeName(n.name)}${ms}</option>`;
      })
      .join("");
  }
  if (group.now) select.value = group.now;
}

function setupEnvAccordions() {
  const cards = [...document.querySelectorAll('.view[data-view="env"] .env-card')];
  for (const card of cards) {
    if (card.dataset.accReady === "1") continue;
    card.dataset.accReady = "1";
    const title = card.querySelector(".env-title");
    if (!title) continue;
    const body = document.createElement("div");
    body.className = "env-body";
    [...card.children].forEach((el) => { if (el !== title) body.appendChild(el); });
    card.appendChild(body);
    const chev = document.createElement("span");
    chev.className = "env-chev";
    chev.textContent = "›";
    title.appendChild(chev);
    title.classList.add("env-toggle");
    title.addEventListener("click", () => card.classList.toggle("collapsed"));
  }
  cards.forEach((card, i) => { if (i > 0) card.classList.add("collapsed"); });
}

function renderEnv() {
  try {
    const st = state.settings || {};
    if ($("env-allow-lan")) $("env-allow-lan").checked = !!st.allow_lan;
    if ($("env-kernel-ipv6")) $("env-kernel-ipv6").checked = !!st.ipv6;
    if ($("env-log-level")) $("env-log-level").value = st.log_level || "warning";
    if ($("env-tun-stack")) $("env-tun-stack").value = st.tun_stack || "gvisor";
  renderDnsSettings(st);
  } catch (err) { /* 忽略 */ }
  refreshPrivilege();
  const cfg = state.settings || {};
  const set = (id, value) => { const el = $(id); if (el) el.value = value; };
  set("env-port", String(cfg.mixed_port || 7899));
  set("env-launch-mode", cfg.launch_mode || "desktop");
  applyLaunchMode(cfg.launch_mode || "desktop");
  set("env-app-path", cfg.claude_app_path || "");
  set("env-cli-path", cfg.claude_cli_path || "");
  set("env-locale", cfg.locale || "zh-CN");
  set("env-browser", cfg.browser || "auto");
  set("env-lang", cfg.lang === "en" ? "en" : "zh");
  if ($("env-menu-bar")) $("env-menu-bar").checked = cfg.menu_bar !== false;
  const chk = (id, on) => { const el = $(id); if (el) el.checked = !!on; };
  chk("env-ipv6", cfg.disable_ipv6);
  chk("env-follow-tz", cfg.follow_timezone !== false);
  chk("env-strict", cfg.strict_precheck !== false);
  const local = $("env-local-tz");
  if (local) local.textContent = tzFriendly((state.settings && state.settings.tz_backup) || "");
  $("watch-interval").value = String(state.settings.watch_interval || 120);
  const dir = $("env-data-dir");
  if (dir) dir.textContent = state.dataDir || "~/Library/Application Support/ClashLink";
  updateThemeSeg();
}

function updateThemeSeg() {
  const mode = currentTheme();
  for (const btn of document.querySelectorAll(".theme-card")) {
    btn.classList.toggle("active", btn.dataset.themeMode === mode);
  }
}

async function toggleTun(on) {
  const ok = await confirmDialog({
    title: on ? "开启 TUN 模式" : "关闭 TUN 模式",
    body: on
      ? "TUN 会创建虚拟网卡接管整机流量；开启期间不要再开 Clash Verge 的 TUN。若网络异常，关掉这个开关即可回退。"
      : "会重启内核并回到混合端口代理模式，系统代理保持原样。",
    okText: on ? "开启" : "关闭",
  });
  if (!ok) {
    $("tun-mode").checked = !on;
    return;
  }
  try {
    const res = await api("/api/core/tun", { method: "POST", body: { on } });
    state.subs.core = res.core;
    toast(on ? "TUN 已开启" : "TUN 已关闭", "ok");
    renderSubs();
    loadNodes();
    loadSession();
  } catch (err) {
    $("tun-mode").checked = !on;
    toast(err.message || "切换 TUN 失败", "fail");
  }
}

async function loadSession() {
  try {
    const data = await api("/api/session");
    state.session = data;
    renderSessionLog(data);
  } catch (err) {
    /* 忽略：环境页打开失败不影响主流程 */
  }
}

function renderSessionLog(data) {
  const list = $("session-log");
  if (!list) return;
  list.textContent = "";
  const events = (data && data.events) || [];
  if (!events.length) {
    const li = document.createElement("li");
    li.className = "session-log-empty";
    li.textContent = "还没有记录。";
    list.appendChild(li);
    return;
  }
  for (const ev of events.slice().reverse()) {
    const li = document.createElement("li");
    const time = document.createElement("span");
    time.className = "sl-time";
    time.textContent = new Date(ev.at * 1000).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
    const text = document.createElement("span");
    text.className = "sl-text";
    text.textContent = ev.text;
    li.append(time, text);
    list.appendChild(li);
  }
  const note = $("session-log-note");
  if (note && data) {
    note.textContent = data.backup
      ? `接管前的代理记录还在，断开或恢复时会还原到原来的设置。`
      : `当前没有待还原的代理记录；恢复会停掉内核并关闭系统代理。`;
  }
}

async function restoreSession() {
  const ok = await confirmDialog({
    title: "恢复临时设置",
    body: "会停掉内置内核，并把系统代理还原成接管前的样子。",
    okText: "恢复",
  });
  if (!ok) return;
  if (state.settings && state.settings.disable_ipv6) {
    try {
      await api("/api/env/ipv6", { method: "POST", body: { on: true } });
    } catch (err) {
      // 恢复 IPv6 失败不阻断后面的还原
    }
  }
  try {
    const res = await api("/api/session/restore", { method: "POST", body: {} });
    toast(res.message || "已恢复临时设置", "ok");
    await loadSubs();
    await loadNodes();
    await loadSession();
    renderTiles();
  } catch (err) {
    toast("恢复失败：" + err.message, "fail");
  }
}

async function refreshPrivilege() {
  try {
    const info = await api("/api/timezone/privilege");
    const on = !!info.passwordless;
    $("env-sudo-state").textContent = on ? "已启用 · 改时区、代理、TUN 不再弹密码" : "未启用";
    const btn = $("btn-sudo");
    btn.hidden = on;
  } catch (err) {
    // 忽略
  }
}

async function enablePasswordless() {
  const btn = $("btn-sudo");
  btn.disabled = true;
  btn.textContent = "授权中…";
  try {
    const res = await api("/api/timezone/privilege", { method: "POST", body: { on: true } });
    if (res.ok) {
      toast("已启用免密改时区，之后跟随节点不再弹密码框", "ok");
    } else {
      toast(res.message || "启用失败", "fail");
    }
  } catch (err) {
    toast(err.message || "启用失败", "fail");
  }
  refreshPrivilege();
}

async function saveWatchInterval() {
  const raw = Number($("watch-interval").value);
  if (!Number.isFinite(raw) || raw < 30 || raw > 3600) {
    toast("巡检间隔请填 30–3600 秒", "warn");
    return;
  }
  try {
    const res = await api("/api/settings", { method: "POST", body: { watch_interval: Math.round(raw) } });
    state.settings = res.settings || state.settings;
    toast("巡检间隔已保存", "pass");
    renderFooter();
  } catch (err) {
    toast("保存失败：" + (err.message || err), "fail");
  }
}

async function loadUsage() {
  try {
    const data = await api("/api/usage");
    const fmt = (v) => fmtBytes(v || 0);
    $("usage-today-down").textContent = fmt(data.today && data.today.down);
    $("usage-today-up").textContent = fmt(data.today && data.today.up);
    $("usage-month-down").textContent = fmt(data.month && data.month.down);
    $("usage-month-up").textContent = fmt(data.month && data.month.up);
    const days = (data.recent || []).length;
    $("usage-meta").textContent = days ? `最近 ${days} 天有记录` : "还没有记录";
  } catch (err) {
    // 忽略
  }
}

async function loadHistory() {
  try {
    const res = await api("/api/history");
    renderHistory(res.items || []);
  } catch (err) {
    renderHistory([], err.message || String(err));
  }
}

function renderHistory(items, error) {
  const list = $("history-list");
  list.textContent = "";
  $("history-meta").textContent = items.length ? `最近 ${items.length} 次` : "";
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "history-empty";
    empty.textContent = error ? `读取失败：${error}` : "还没有记录，去「首页」做一次检测。";
    list.append(empty);
    return;
  }
  for (const item of items) {
    const row = document.createElement("div");
    row.className = "history-row";

    const score = document.createElement("div");
    score.className = "history-score";
    score.dataset.level = item.verdict || "warn";
    score.textContent = item.score == null ? "—" : String(item.score);

    const main = document.createElement("div");
    main.className = "history-main";
    const title = document.createElement("strong");
    const place = [item.flag, item.city || item.country].filter(Boolean).join(" ");
    title.textContent = place || item.ip || "未知出口";
    const sub = document.createElement("em");
    const bits = [];
    if (item.warnings) bits.push(`${item.warnings} 项注意`);
    if (item.issues) bits.push(`${item.issues} 项异常`);
    sub.textContent = bits.join(" · ") || "全部通过";
    main.append(title, sub);

    const time = document.createElement("div");
    time.className = "history-time";
    time.textContent = item.at ? new Date(item.at).toLocaleString("zh-CN", { hour12: false }) : "";

    row.append(score, main, time);
    list.append(row);
  }
}

function applyLaunchMode(mode) {
  const appRow = $("env-app-row");
  const cliRow = $("env-cli-row");
  if (appRow) appRow.hidden = mode !== "desktop";
  if (cliRow) cliRow.hidden = mode !== "cli";
}

async function saveEnv() {
  const payload = {
    mixed_port: Number($("env-port").value || 7899),
    launch_mode: $("env-launch-mode").value,
    claude_app_path: $("env-app-path").value.trim(),
    claude_cli_path: $("env-cli-path").value.trim(),
    locale: $("env-locale").value,
    browser: $("env-browser").value,
    disable_ipv6: $("env-ipv6").checked,
    follow_timezone: $("env-follow-tz").checked,
    strict_precheck: $("env-strict").checked,
  };
  try {
    const res = await api("/api/settings", { method: "POST", body: payload });
    state.settings = res.settings || { ...state.settings, ...payload };
    toast("设置已保存；语言 / 浏览器在下次打开核对窗口、启动 CLI 时生效", "ok");
  } catch (err) {
    toast(err.message || "保存失败", "fail");
  }
}

async function pickFile(kind, targetId) {
  try {
    const res = await api("/api/pick-file", { method: "POST", body: { kind } });
    if (res.ok && res.path) {
      const el = $(targetId);
      if (el) el.value = res.path;
    }
  } catch (err) {
    if (!String(err.message || "").includes("取消")) toast(err.message || "选择失败", "fail");
  }
}

async function startSession(openClaudeAfter = false) {
  const core = state.subs.core || {};
  if (!core.running) {
    toast("先在「订阅管理」启用一条订阅，内核才会启动", "warn");
    return;
  }
  const proxy = state.subs.sysproxy || {};
  const wantTun = $("desk-proxy").checked;
  try {
    if (wantTun && !core.tun) {
      const res = await api("/api/core/tun", { method: "POST", body: { on: true } });
      if (!res.ok) throw new Error(res.message || "开启 TUN 失败");
      await loadSubs(true);
    } else if (!wantTun && !proxy.enabled) {
      if (!(await setSysProxy(true))) return;
    }
  } catch (err) {
    toast(err.message || "准备网络失败", "fail");
    return;
  }
  if (state.settings && state.settings.disable_ipv6) {
    try {
      await api("/api/env/ipv6", { method: "POST", body: { on: false } });
    } catch (err) {
      toast(err.message || "关闭 IPv6 失败", "warn");
    }
  }
  if (!state.settings || state.settings.follow_timezone !== false) {
    try {
      const follow = await api("/api/timezone/follow", { method: "POST" });
      if (follow && follow.changed) {
        toast(`已把系统时区跟随到 ${follow.timezone}`, "ok");
      }
    } catch (err) {
      // 没取到出口时区就交给后面的体检去提示
    }
  }
  renderTiles();
  startScan();
  if (openClaudeAfter) {
    setTimeout(() => openClient(), 800);
  }
}

async function endSession() {
  if (state.settings && state.settings.disable_ipv6) {
    try {
      await api("/api/env/ipv6", { method: "POST", body: { on: true } });
    } catch (err) {
      toast(err.message || "恢复 IPv6 失败", "warn");
    }
  }
  const core = state.subs.core || {};
  const proxy = state.subs.sysproxy || {};
  if (core.tun) {
    try {
      const res = await api("/api/core/tun", { method: "POST", body: { on: false } });
      if (!res.ok) throw new Error(res.message || "关闭 TUN 失败");
      await loadSubs(true);
      renderTiles();
      toast("已断开 TUN，回到普通代理模式", "ok");
    } catch (err) {
      toast(err.message || "关闭 TUN 失败", "fail");
    }
    return;
  }
  if (proxy.enabled) {
    try {
      if (!(await setSysProxy(false))) return;
    } catch (err) {
      toast(err.message || "恢复系统代理失败", "fail");
      return;
    }
  }
  toast("已断开：系统代理恢复为原来的设置，内核仍在后台", "ok");
}

async function openClient() {
  const sel = $("desk-client");
  const id = sel ? sel.value : "";
  if (!id) { toast("先在设置里添加一个 AI 客户端", "warn"); return; }
  try {
    const res = await api("/api/clients/open", { method: "POST", body: { id } });
    toast(res.message || "已启动", res.ok ? "pass" : "fail");
  } catch (err) {
    toast("打开失败：" + (err.message || err), "fail");
  }
}

function setRunning(on) {
  state.running = on;
  const btn = $("btn-scan");
  btn.disabled = on;
  btn.classList.toggle("busy", on);
  btn.querySelector("span").textContent = on ? "检测中" : "重新检测";
  const conn = $("btn-connect");
  if (conn) {
    conn.disabled = on;
    conn.querySelector("span").textContent = on ? "检测中…" : "连接并检测";
    conn.classList.toggle("busy", on);
  }
  if (on) {
    $("live-pill").className = "pill running";
    $("pill-text").textContent = "检测中";
    if (state.report) $("scan-meta").textContent = "正在重新检测…";
  }
}

function setProgress(ratio, done, total) {
  const bar = $("progress-bar");
  bar.classList.add("active");
  bar.style.setProperty("--p", `${Math.round(Math.max(0, Math.min(1, ratio)) * 100)}%`);
  if (done && total) $("pill-text").textContent = `检测中 ${done}/${total}`;
}

function clearProgress() {
  const bar = $("progress-bar");
  bar.classList.remove("active");
  bar.style.setProperty("--p", "0%");
}

/* ------------------------------------------------------------------ */
/* 报告渲染                                                            */

function renderHome(report) {
  const score = $("home-score");
  const line = $("home-line");
  if (!score || !line) return;
  if (!report) {
    score.textContent = (window.t || ((x) => x))("尚未检测");
    line.textContent = "准备好网络后，点「测试」跑一次完整体检。";
    return;
  }
  const zhVerdict = report.verdict === "pass" ? "可以正常使用" : report.verdict === "warn" ? "有需要处理的风险项" : "当前不可用";
  const enHome = (state.settings && state.settings.lang === "en") || (window.currentI18nLang && window.currentI18nLang() === "en");
  score.textContent = enHome
    ? `${report.score} pts · ${report.verdict === "pass" ? "working normally" : report.verdict === "warn" ? "needs attention" : "unavailable"}`
    : `${report.score} 分 · ${zhVerdict}`;
  const issues = (report.issues || []).length;
  const warns = (report.warnings || []).length;
  const tt = window.t || ((x) => x);
  line.textContent = issues || warns
    ? `${tt("建议处理：")}${(report.issues || []).concat(report.warnings || []).slice(0, 2).map(tt).join(" / ")}`
    : tt("上次检测没有发现问题。");
}

function renderReport(report, opts = {}) {
  state.report = report;
  renderHome(report);
  const verdict = report.verdict || "warn";
  const card = $("verdict-card");
  card.dataset.verdict = verdict;
  card.style.setProperty("--ring-color", VERDICT_COLOR[verdict] || "var(--accent)");

  animateScore(Number(report.score) || 0);

  const checks = Array.isArray(report.checks) ? report.checks : [];
  $("verdict-eyebrow").textContent = `体检结果 · ${checks.length || 13} 项`;
  $("verdict-title").textContent = (window.t || ((x) => x))(report.headline || "");
  $("verdict-sub").textContent = report.subline || "";

  const meta = VERDICT_PILL[verdict] || ["idle", "未知"];
  if (!state.running) {
    $("live-pill").className = "pill " + meta[0];
    $("pill-text").textContent = meta[1];
  } else {
    $("live-pill").className = "pill running";
  }

  renderMeta(report, checks);
  renderNode(report);
  renderTz(report.timezone || {});
  ensureGroups(checks);
  renderChecks(checks);
  renderFooter();

  if (opts.fresh) {
    const fails = checks.filter((c) => c.status === "fail").length;
    if (fails) toast(`检测完成：${fails} 项异常，请查看下方详情`, "fail");
  }
}

function renderMeta(report, checks) {
  const secs = report.duration_ms ? (report.duration_ms / 1000).toFixed(1) : "?";
  const finished = new Date(report.generated || Date.now());
  const time = Number.isNaN(finished.getTime())
    ? ""
    : finished.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
  const fails = checks.filter((c) => c.status === "fail").length;
  const warns = checks.filter((c) => c.status === "warn").length;
  const parts = [];
  if (time) parts.push(`完成于 ${time}`);
  parts.push(`用时 ${secs} 秒`);
  if (fails || warns) parts.push(`异常 ${fails} · 注意 ${warns}`);
  else parts.push("全部通过");
  const en = (state.settings && state.settings.lang === "en") || (window.currentI18nLang && window.currentI18nLang() === "en");
  $("checks-summary").textContent = report
    ? `${report.score} ${en ? "pts" : "分"} · ${checksSummary(report)}`
    : checksSummary(report);
  const btn = $("btn-checks");
  const total = report && report.checks ? report.checks.length : 19;
  const collapsed = document.querySelector(".checks-card").dataset.collapsed === "1";
  btn.textContent = collapsed ? `展开 ${total} 项` : "收起";
  $("scan-meta").textContent = parts.join(" · ");
}

function renderNode(report) {
  const exit = report.exit || {};
  const proxy = report.proxy || {};
  $("node-flag").textContent = exit.flag || "🌐";
  $("node-country").textContent = exit.country || "出口节点";
  $("node-city").textContent =
    [exit.city, exit.region].filter(Boolean).join(" · ") || (exit.error ? "未获取归属地" : "未知位置");

  const colo = $("node-colo");
  colo.hidden = !exit.country_code;
  colo.textContent = exit.country_code || "";

  const coloText = exit.colo ? `Cloudflare ${exit.colo}` : "—";
  const ipText = exit.ip || "—";
  const asnText = [exit.asn, exit.org].filter(Boolean).join(" ") || "—";
  const proxyText = (proxy.tun ? "TUN · " : "") + (proxy.label || "直连");

  const ip = $("node-ip");
  ip.textContent = ipText;
  ip.title = ipText;
  const asn = $("node-asn");
  asn.textContent = asnText;
  asn.title = asnText;
  $("node-proxy").textContent = proxyText;
  $("node-edge").textContent = coloText;
}

const TZ_FRIENDLY = {
  "Asia/Shanghai": "北京时间",
  "Asia/Hong_Kong": "香港时间",
  "Asia/Taipei": "台北时间",
  "Asia/Tokyo": "日本时间",
  "Asia/Singapore": "新加坡时间",
  "America/Los_Angeles": "洛杉矶时间",
  "America/New_York": "纽约时间",
  "Europe/London": "伦敦时间",
};

function tzFriendly(tz) {
  if (!tz) return "尚未记录";
  const name = TZ_FRIENDLY[tz];
  if (!name) return tz;
  const offset = tzOffsetLabel(tz);
  return offset ? `${tz}（${name} ${offset}）` : `${tz}（${name}）`;
}

function tzOffsetLabel(tz) {
  try {
    const parts = new Intl.DateTimeFormat("en-US", { timeZone: tz, timeZoneName: "shortOffset" }).formatToParts(new Date());
    const found = parts.find((p) => p.type === "timeZoneName");
    return found ? found.value.replace("GMT", "UTC") : "";
  } catch (err) {
    return "";
  }
}

function renderTz(tz) {
  if (!$("tz-card")) return;
  const system = tz.system || state.tz.system || "";
  const exit = tz.exit || "";
  state.tz = { system, exit };
  updateClocks();

  $("tz-system").textContent = system || "未知";
  $("tz-system").title = system || "";
  $("tz-exit").textContent = exit || "未知";
  $("tz-exit").title = exit || "";

  const cmp = tz.comparison || {};
  const card = $("tz-card");
  let mode = "unknown";
  let title = "正在比对系统时区与出口时区…";
  let note = "";
  let canFix = false;

  if (!exit) {
    title = "未能确定出口时区";
    note = "归属地接口没有返回时区信息。请确认代理已开启，稍后重新检测；也可以手动在「系统设置 › 通用 › 日期与时间」中调整。";
  } else if (cmp.comparable === false) {
    mode = "unknown";
    title = `系统 ${system} · 出口 ${exit}`;
    note = "无法计算偏移差（时区数据缺失），请手动核对。";
    canFix = true;
  } else if (cmp.same_name) {
    mode = "ok";
    title = "系统时区已与出口节点对齐";
    note = `系统与出口节点同为 ${system}（${cmp.exit_offset_label || ""}），无需修改。`;
  } else if (cmp.same_offset) {
    mode = "ok";
    title = "时区名称不同，但当前偏移一致";
    note = `${system} 与 ${exit} 现在都是 ${cmp.exit_offset_label || ""}，不会造成时间错位。`;
  } else {
    mode = "drift";
    canFix = true;
    const hours = Math.abs(Number(cmp.diff_hours) || 0);
    title = `系统时区与出口节点相差 ${fmtDuration(hours)}`;
    note = `系统 ${cmp.system_offset_label || ""} · 出口 ${cmp.exit_offset_label || ""}。时区不一致会让 Claude 的会话时间与限额重置提示错位，也更容易触发风控。`;
  }

  const backup = (state.settings && state.settings.tz_backup) || "";
  if (backup) {
    note += ` 断开节点时会自动恢复为 ${tzFriendly(backup)}。`;
  }
  card.dataset.state = mode;
  $("tz-title").textContent = title;
  $("tz-note").textContent = note;
  $("btn-sync").disabled = !canFix;
  $("btn-copy").disabled = !exit;
}

function fmtDuration(hours) {
  if (!hours) return "0 分钟";
  if (hours < 1) return `${Math.round(hours * 60)} 分钟`;
  const rounded = Math.round(hours * 10) / 10;
  return `${Number.isInteger(rounded) ? rounded : rounded.toFixed(1)} 小时`;
}

function updateClocks() {
  if (!$("tz-system-clock")) return;
  const sys = fmtClock(state.tz.system);
  const exit = fmtClock(state.tz.exit);
  $("tz-system-clock").textContent = sys.time;
  $("tz-system-date").textContent = sys.date;
  $("tz-exit-clock").textContent = exit.time;
  $("tz-exit-date").textContent = exit.date;
}

function fmtClock(tz) {
  if (!tz) return { time: "--:--:--", date: "" };
  try {
    const now = new Date();
    const time = new Intl.DateTimeFormat("zh-CN", {
      timeZone: tz,
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    }).format(now);
    const date = new Intl.DateTimeFormat("zh-CN", {
      timeZone: tz,
      month: "numeric",
      day: "numeric",
      weekday: "short",
    }).format(now);
    return { time, date };
  } catch (err) {
    return { time: "--:--:--", date: "无效时区" };
  }
}

function animateScore(target) {
  const value = $("score");
  const ring = $("ring-fg");
  const from = Number(value.dataset.value || 0);
  const clamped = Math.max(0, Math.min(100, target));
  ring.style.strokeDashoffset = String(RING_CIRCUMFERENCE * (1 - clamped / 100));
  value.dataset.value = String(clamped);
  const reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduced) {
    value.textContent = String(clamped);
    return;
  }
  const startedAt = performance.now();
  const duration = 900;
  const tick = (now) => {
    const p = Math.min(1, (now - startedAt) / duration);
    const eased = 1 - Math.pow(1 - p, 3);
    value.textContent = String(Math.round(from + (clamped - from) * eased));
    if (p < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

/* ------------------------------------------------------------------ */
/* 检查项                                                              */

function ensureGroups(checks) {
  const groups = [];
  for (const c of checks) {
    if (c.group && !groups.includes(c.group)) groups.push(c.group);
  }
  if (!groups.length) return;
  const signature = groups.join("|");
  if (signature === state.builtGroups) return;

  const root = $("checks");
  root.innerHTML = "";
  state.rows.clear();
  for (const name of groups) {
    const section = document.createElement("section");
    section.className = "group";
    section.dataset.group = name;

    const head = document.createElement("div");
    head.className = "group-head";
    const title = document.createElement("span");
    title.className = "group-title";
    title.textContent = name;
    const line = document.createElement("span");
    line.className = "group-line";
    const count = document.createElement("span");
    count.className = "group-count";
    head.append(title, line, count);

    const list = document.createElement("div");
    list.className = "check-list";
    section.append(head, list);
    root.appendChild(section);
  }
  state.builtGroups = signature;
}

function toggleChecks() {
  const card = document.querySelector(".checks-card");
  const collapsed = card.dataset.collapsed === "1";
  card.dataset.collapsed = collapsed ? "0" : "1";
  const box = $("checks");
  if (box) box.dataset.collapsed = card.dataset.collapsed;
  $("btn-checks").textContent = collapsed ? "收起" : `展开 ${state.report ? state.report.checks.length : 13} 项`;
}

function checksSummary(report) {
  const checks = (report && report.checks) || [];
  const pass = checks.filter((c) => c.status === "pass").length;
  const warn = checks.filter((c) => c.status === "warn").length;
  const fail = checks.filter((c) => c.status === "fail").length;
  if (!checks.length) return "等待检测";
  const en = (state.settings && state.settings.lang === "en") || (window.currentI18nLang && window.currentI18nLang() === "en");
  const parts = [en ? `${pass} passed` : `${pass} 项正常`];
  if (warn) parts.push(en ? `${warn} warnings` : `${warn} 项注意`);
  if (fail) parts.push(en ? `${fail} failures` : `${fail} 项异常`);
  return parts.join(" · ");
}

function renderChecks(checks) {
  if (document.getElementById("tile-api")) renderTiles();
  for (const c of checks) {
    let row = state.rows.get(c.id);
    if (!row) {
      row = buildRow(c);
      state.rows.set(c.id, row);
      const section = document.querySelector(`.group[data-group="${cssEscape(c.group || FALLBACK_GROUPS[0])}"]`);
      const list = section ? section.querySelector(".check-list") : null;
      if (list) list.appendChild(row);
    }
    updateRow(row, c);
  }
  updateGroupCounts();
}

function buildRow(check) {
  const row = document.createElement("article");
  row.className = "check";
  row.dataset.id = check.id;
  row.dataset.status = "pending";
  row.innerHTML = `
    <div class="check-icon" aria-hidden="true"></div>
    <div class="check-body">
      <div class="check-head"><h3></h3><span class="badge"></span></div>
      <p class="check-summary"></p>
      <p class="check-detail"></p>
    </div>
    <div class="check-meta"><span class="ms"></span></div>`;
  row.querySelector(".check-body").addEventListener("click", () => {
    row.classList.toggle("expanded");
  });
  return row;
}

function updateRow(row, check) {
  row.dataset.status = check.status || "pending";
  row.querySelector("h3").textContent = check.title || check.id;
  row.querySelector(".badge").textContent = check.status_label || STATUS_TEXT[check.status] || "";

  let summary = check.summary || "";
  if (!summary && check.status === "pending") summary = "排队中…";
  row.querySelector(".check-summary").textContent = summary;
  row.querySelector(".check-detail").textContent = check.detail || "";
  row.querySelector(".ms").textContent = check.elapsed_ms ? `${check.elapsed_ms} ms` : "";

  const meta = row.querySelector(".check-meta");
  let fixBtn = meta.querySelector(".mini");
  const fix = check.fix || null;
  if (fix && fix.action === "set_timezone") {
    if (!fixBtn) {
      fixBtn = document.createElement("button");
      fixBtn.type = "button";
      fixBtn.className = "mini";
      fixBtn.textContent = "同步时区";
      fixBtn.addEventListener("click", (event) => {
        event.stopPropagation();
        syncTimezone(fix.tz || state.tz.exit);
      });
      meta.appendChild(fixBtn);
    }
    fixBtn.textContent = "同步时区";
  } else if (fixBtn) {
    fixBtn.remove();
  }
}

function updateGroupCounts() {
  document.querySelectorAll(".group").forEach((section) => {
    const rows = section.querySelectorAll(".check");
    const total = rows.length;
    let done = 0;
    let bad = 0;
    rows.forEach((row) => {
      const st = row.dataset.status;
      if (st !== "pending") done += 1;
      if (st === "fail" || st === "warn") bad += 1;
    });
    const label = section.querySelector(".group-count");
    if (!label) return;
    label.textContent = bad ? `${done}/${total} · ${bad} 项待处理` : `${done}/${total}`;
  });
}

function cssEscape(value) {
  if (window.CSS && CSS.escape) return CSS.escape(value);
  return String(value).replace(/["\\]/g, "\\$&");
}

/* ------------------------------------------------------------------ */
/* 时区操作                                                            */

async function syncTimezone(tz) {
  if (!tz) return;
  const from = state.tz.system || "当前时区";
  const ok = await confirmDialog({
    title: "同步系统时区",
    body:
      `将把 macOS 系统时区从 <strong>${esc(from)}</strong> 修改为 <strong>${esc(tz)}</strong>。` +
      `<br><br>已启用免密接管时立即生效；否则系统会弹出一次授权窗口。`,
    okText: "继续",
  });
  if (!ok) return;

  const btn = $("btn-sync");
  const label = btn.querySelector("span");
  const old = label.textContent;
  btn.disabled = true;
  label.textContent = "正在同步…";
  try {
    const res = await api("/api/timezone", { method: "POST", body: { tz } });
    if (res.ok) {
      toast(`系统时区已切换为 ${res.timezone || tz}`, "pass");
      setTimeout(() => startScan(), 500);
    } else {
      toast(res.message || "同步失败", "fail");
      btn.disabled = false;
    }
  } catch (err) {
    toast("同步失败：" + err.message, "fail");
    btn.disabled = false;
  } finally {
    label.textContent = old;
  }
}

async function copyCommand() {
  const tz = state.tz.exit;
  if (!tz) return;
  const command = `sudo systemsetup -settimezone ${tz}`;
  try {
    await navigator.clipboard.writeText(command);
    toast("命令已复制到剪贴板", "pass");
  } catch (err) {
    toast(`复制失败，可手动执行：${command}`, "warn", 7000);
  }
}

/* ------------------------------------------------------------------ */
/* 设置与退出                                                          */


function renderFooter() {
  const sv = $("side-version");
  if (sv) sv.textContent = `v${state.version}`;
  const core = (state.subs && state.subs.core) || {};
  const subs = (state.subs && state.subs.items) || [];
  let status = "就绪 · 添加节点或订阅即可开始";
  if (core.running && core.tun) status = "会话已就绪 · TUN 接管整机流量";
  else if (core.running) status = "会话已就绪 · 内核已启动，可按需接管系统代理";
  else if (subs.length) status = "内核未运行 · 到「订阅管理」启用一条订阅";
  $("footer-meta").textContent = status;
}

async function quitApp() {
  const ok = await confirmDialog({
    title: "退出控制台",
    body: "退出后本地页面将无法访问。下次双击应用图标即可重新启动。",
    okText: "退出",
  });
  if (!ok) return;
  try {
    await api("/api/quit", { method: "POST", body: {} });
  } catch (err) {
    /* 服务端可能已关闭连接 */
  }
  document.querySelector(".app").innerHTML =
    `<div style="padding:80px 0;text-align:center;color:var(--muted)">` +
    `<div style="font-size:15px;color:var(--text);margin-bottom:6px">控制台已退出</div>` +
    `可以关闭此页面，或重新打开 Clash Link。</div>`;
  $("progress-bar").classList.remove("active");
}

/* ------------------------------------------------------------------ */
/* 组件                                                                */

function toast(message, kind = "info", ms = 4200) {
  const box = $("toasts");
  while (box.children.length >= 4) box.firstElementChild.remove();
  const node = document.createElement("div");
  node.className = `toast ${kind}`;
  node.textContent = message;
  box.appendChild(node);
  setTimeout(() => {
    node.classList.add("out");
    setTimeout(() => node.remove(), 240);
  }, ms);
}

function confirmDialog({ title, body, okText = "确认", cancelText = "取消", input = null }) {
  return new Promise((resolve) => {
    const modal = $("modal");
    if (!modal) {
      const plain = String(body).replace(/<[^>]+>/g, "");
      if (input !== null) {
        const value = window.prompt(plain, input);
        resolve(value === null ? null : value.trim());
        return;
      }
      resolve(window.confirm(title + "\n\n" + plain));
      return;
    }
    const ok = $("modal-ok");
    const cancel = $("modal-cancel");
    $("modal-title").textContent = title;
    $("modal-body").innerHTML =
      body +
      (input === null
        ? ""
        : `<input class="modal-input" id="modal-input" type="text" autocomplete="off" spellcheck="false" />`);
    if (input !== null) {
      const field = $("modal-input");
      if (field) {
        field.value = input;
        setTimeout(() => field.focus(), 30);
      }
    }
    ok.textContent = okText;
    cancel.textContent = cancelText;
    modal.hidden = false;

    const onKey = (event) => {
      if (event.key === "Escape") finish(false);
      if (event.key === "Enter") finish(true);
    };
    const finish = (confirmed) => {
      const field = $("modal-input");
      const value = input === null ? confirmed : confirmed && field ? field.value.trim() : null;
      modal.hidden = true;
      ok.onclick = null;
      cancel.onclick = null;
      document.removeEventListener("keydown", onKey);
      resolve(value);
    };
    ok.onclick = () => finish(true);
    cancel.onclick = () => finish(false);
    document.addEventListener("keydown", onKey);
    ok.focus();
  });
}

/* ------------------------------------------------------------------ */
/* 线路节点                                                            */

function nodeGroups() {
  const data = state.nodes.data;
  if (!data || !data.available || !Array.isArray(data.groups)) return [];
  const groups = data.groups.filter((g) => g.name !== "GLOBAL");
  return groups.length ? groups : data.groups;
}

function currentNodeGroup() {
  const groups = nodeGroups();
  if (!groups.length) return null;
  return groups.find((g) => g.name === state.nodes.group) || groups[0];
}

function delayLevel(ms) {
  if (ms == null || !Number.isFinite(ms) || ms <= 0) return "none";
  if (ms < 600) return "fast";
  if (ms < 1800) return "mid";
  return "slow";
}

function formatDelay(ms) {
  return ms == null || !Number.isFinite(ms) || ms <= 0 ? "—" : `${Math.round(ms)} ms`;
}

function fastestNode(group) {
  let best = null;
  for (const node of group.nodes || []) {
    if (!node.delay || node.delay <= 0) continue;
    if (!best || node.delay < best.delay) best = node;
  }
  return best;
}

function delayText(name, ms) {
  if (ms != null && Number.isFinite(ms) && ms > 0) return `${Math.round(ms)} ms`;
  return state.nodes.tried[name] ? "超时" : "—";
}

function delayColor(name, ms) {
  if (ms != null && Number.isFinite(ms) && ms > 0) return delayLevel(ms);
  return state.nodes.tried[name] ? "slow" : "none";
}

function shortNodeName(name) {
  return String(name || "").replace(/\s*\|\s*/g, " · ");
}

async function loadNodes() {
  try {
    state.nodes.data = await api("/api/nodes");
  } catch (err) {
    state.nodes.data = { available: false, reason: "unreachable", error: String(err.message || err) };
  }
  renderNodes();
  syncDeskNode();
  probeCurrentDelay();
}

function probeCurrentDelay() {
  const group = currentNodeGroup();
  if (!group || !group.now) return;
  if (group.now_delay != null) return;
  if (state.nodes.tried[group.now]) return;
  state.nodes.tried[group.now] = true;
  api("/api/nodes/delay", { method: "POST", body: { name: group.now } })
    .then((res) => {
      group.now_delay = res.delay;
      const node = (group.nodes || []).find((n) => n.name === group.now);
      if (node) node.delay = res.delay;
      if (currentNodeGroup() === group) renderNodes();
    })
    .catch(() => {
      delete state.nodes.tried[group.now];
    });
}

function renderNodes() {
  const data = state.nodes.data;
  if (!data) return;
  const card = $("nodes-card");
  const help = $("nodes-help");
  const groups = nodeGroups();

  if (!data.available || !groups.length) {
    card.hidden = true;
    help.hidden = false;
    $("nodes-help-text").innerHTML = nodesHelpText(data);
    return;
  }

  help.hidden = true;
  card.hidden = false;

  const preferred =
    groups.find((g) => g.name === state.nodes.group) ||
    groups.find((g) => g.name === "PROXY") ||
    groups.find((g) => g.selector) ||
    groups[0];
  state.nodes.group = preferred.name;

  renderGroupTabs(groups, preferred.name);
  renderNodeNow(preferred);
  renderNodeList(preferred);

  const meta = $("nodes-meta");
  const endpoint = data.endpoint ? data.endpoint.address : "";
  meta.textContent = [data.version, endpoint].filter(Boolean).join(" · ");
}

function renderGroupTabs(groups, active) {
  const box = $("node-groups");
  box.textContent = "";
  if (groups.length < 2) return;
  for (const group of groups) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "group-tab" + (group.name === active ? " active" : "");
    btn.textContent = group.name;
    btn.addEventListener("click", () => {
      state.nodes.group = group.name;
      renderNodes();
    });
    box.appendChild(btn);
  }
}

function renderNodeNow(group) {
  $("node-now").textContent = group.now ? shortNodeName(group.now) : "—";
  let value = group.now_delay;
  if (value == null && group.now) {
    const node = (group.nodes || []).find((n) => n.name === group.now);
    value = node ? node.delay : null;
  }
  const delay = $("node-now-delay");
  delay.textContent = delayText(group.now, value);
  delay.dataset.level = delayColor(group.now, value);

  const warn = $("nodes-warn");
  const dead = value == null && state.nodes.tried[group.now];
  warn.hidden = !dead;
  warn.textContent = dead
    ? "当前节点测速无响应，这个节点多半已经被墙或机场下线了。点一个绿色的节点切过去，再点「重新检测」。"
    : "";
}

function visibleNodes(group) {
  const q = (state.nodes.query || "").trim().toLowerCase();
  let nodes = (group.nodes || []).slice();
  if (q) nodes = nodes.filter((n) => String(n.name).toLowerCase().includes(q));
  if (state.nodes.onlyFav) {
    const favs = new Set((state.nodes.data && state.nodes.data.favorites) || []);
    nodes = nodes.filter((n) => favs.has(n.name) || n.name === group.now);
  }
  if (state.nodes.sort === "delay") {
    nodes.sort((a, b) => (a.delay == null ? 1e9 : a.delay) - (b.delay == null ? 1e9 : b.delay));
  } else if (state.nodes.sort === "name") {
    nodes.sort((a, b) => String(a.name).localeCompare(String(b.name), "zh-Hans"));
  } else if (state.nodes.sort === "region") {
    const region = (n) => String(n.name).split("|")[0].trim();
    nodes.sort((a, b) => region(a).localeCompare(region(b), "zh-Hans") || String(a.name).localeCompare(String(b.name), "zh-Hans"));
  }
  return nodes;
}

function renderNodeList(group) {
  const list = $("node-list");
  list.textContent = "";
  const fastest = fastestNode(group);
  const nodes = visibleNodes(group);
  if (!nodes.length) {
    const empty = document.createElement("p");
    empty.className = "node-empty";
    empty.textContent = "没有匹配的节点";
    list.appendChild(empty);
    return;
  }
  for (const node of nodes) {
    const parts = String(node.name).split("|").map((x) => x.trim());
    const card = document.createElement("button");
    card.type = "button";
    card.className = "node-card" +
      (node.name === group.now ? " active" : "") +
      (fastest && node.name === fastest.name ? " fastest" : "");
    card.dataset.level = delayLevel(node.delay);
    card.title = node.name;

    const top = document.createElement("div");
    top.className = "nc-top";
    const favs = new Set((state.nodes.data && state.nodes.data.favorites) || []);
    const star = document.createElement("span");
    star.className = "nc-star" + (favs.has(node.name) ? " on" : "");
    star.textContent = favs.has(node.name) ? "★" : "☆";
    star.title = favs.has(node.name) ? "取消收藏" : "收藏这个节点";
    star.addEventListener("click", (ev) => {
      ev.stopPropagation();
      toggleFavorite(node.name, !favs.has(node.name));
    });
    top.appendChild(star);
    const name = document.createElement("span");
    name.className = "nc-name";
    name.textContent = parts.length > 1 ? parts[0] : node.name;
    top.appendChild(name);
    if (fastest && node.name === fastest.name) {
      const badge = document.createElement("span");
      badge.className = "nc-badge";
      badge.textContent = "最快";
      top.appendChild(badge);
    }
    if (node.name === group.now) {
      const badge = document.createElement("span");
      badge.className = "nc-badge nc-badge-on";
      badge.textContent = "使用中";
      top.appendChild(badge);
    }

    const mid = document.createElement("div");
    mid.className = "nc-mid";
    mid.textContent = parts.slice(1).join(" · ") || "直连";

    const bottom = document.createElement("div");
    bottom.className = "nc-bottom";
    const dot = document.createElement("span");
    dot.className = "node-dot";
    dot.dataset.level = delayLevel(node.delay);
    const delay = document.createElement("span");
    delay.className = "node-delay";
    delay.dataset.level = delayColor(node.name, node.delay);
    delay.textContent = delayText(node.name, node.delay);
    bottom.append(dot, delay);

    const history = (state.nodes.data && state.nodes.data.history && state.nodes.data.history[node.name]) || [];
    const recent = history.filter((x) => x && x[1] != null).slice(-5).map((x) => x[1]);
    if (recent.length > 1) {
      const spark = document.createElement("span");
      spark.className = "nc-spark";
      spark.textContent = "最近 " + recent.join(" · ") + " ms";
      bottom.appendChild(spark);
    }

    card.append(top, mid, bottom);
    card.addEventListener("click", () => selectNode(group.name, node.name));
    list.appendChild(card);
  }
}

async function selectNode(group, name) {
  if (state.nodes.busy) return;
  const current = currentNodeGroup();
  if (current && current.now === name) return;
  state.nodes.busy = true;
  try {
    const res = await api("/api/nodes/select", { method: "POST", body: { group, name } });
    if (current) current.now = res.now || name;
    renderNodes();
    syncDeskNode();
    const node = current && (current.nodes || []).find((n) => n.name === name);
    const fresh = node && typeof node.delay === "number" && Date.now() - (state.nodes.testedAt || 0) < 180000;
    state.nodes.tried[name] = true;
    if (fresh) {
      if (current) current.now_delay = node.delay;
      toast("已切换到 " + shortNodeName(name) + " · " + node.delay + " ms", "ok");
      renderNodes();
      return;
    }
    toast("已切换到 " + shortNodeName(name) + "，正在测速并复测…", "ok");
    api("/api/nodes/delay", { method: "POST", body: { name } })
      .then((res) => {
        const node = current && (current.nodes || []).find((n) => n.name === name);
        if (node) node.delay = res.delay;
        if (current) current.now_delay = res.delay;
        renderNodes();
      })
      .catch(() => {
        delete state.nodes.tried[name];
        renderNodes();
      });
    setTimeout(() => startScan(), 1000);
  } catch (err) {
    toast("切换节点失败：" + err.message, "fail");
  } finally {
    state.nodes.busy = false;
  }
}

async function selectFastest() {
  let group = currentNodeGroup();
  if (!group) return;
  let best = fastestNode(group);
  if (!best) {
    await testNodes();
    group = currentNodeGroup();
    best = group ? fastestNode(group) : null;
  }
  if (best) selectNode(group.name, best.name);
}

async function testNodes() {
  const group = currentNodeGroup();
  if (!group) {
    toast("先在「订阅管理」启用一条订阅，再测速", "warn");
    return;
  }
  const btns = [$("btn-test-nodes"), $("btn-desk-test")].filter(Boolean);
  if (btns.some((b) => b.disabled)) return;
  const labels = btns.map((b) => [b, b.querySelector("span")]);
  for (const [b, label] of labels) {
    b.disabled = true;
    if (label) label.textContent = "测速中";
  }
  try {
    const res = await api("/api/nodes/test", { method: "POST", body: { group: group.name } });
    const target = currentNodeGroup();
    if (target && res.delays) {
      for (const node of target.nodes) {
        if (Object.prototype.hasOwnProperty.call(res.delays, node.name)) {
          node.delay = res.delays[node.name];
        }
      }
      if (target.now && Object.prototype.hasOwnProperty.call(res.delays, target.now)) {
        target.now_delay = res.delays[target.now];
      }
    }
    state.nodes.testedAt = Date.now();
    renderNodes();
    syncDeskNode();
    const fastest = target
      ? target.nodes.filter((n) => typeof n.delay === "number").sort((a, b) => a.delay - b.delay)[0]
      : null;
    toast(fastest ? `测速完成，最快 ${shortNodeName(fastest.name)} · ${fastest.delay} ms` : "测速完成", "ok");
  } catch (err) {
    toast("测速失败：" + err.message, "fail");
  } finally {
    for (const [b, label] of labels) {
      b.disabled = false;
      if (label) label.textContent = "测速";
    }
  }
}

function nodesHelpText(data) {
  const reason = data && data.reason;
  if (!state.subs.items.length) {
    return "还没有节点来源：在上面的「机场订阅」里粘贴订阅链接 → 点<strong>添加</strong> → 点<strong>启用</strong>，程序会自己启动内核并列出节点。";
  }
  if (reason === "disabled_in_settings") {
    return "检测到 Clash Verge 关闭了「外部控制器」。打开 Clash Verge → <strong>设置</strong> → 打开 <strong>外部控制器</strong>（External Controller），保存后回到这里点「重新连接」。";
  }
  if (reason === "not_running") {
    return "外部控制器没有在监听：Clash Verge 可能没启动，或内核还没加载配置。启动后点「重新连接」。";
  }
  if (reason === "no_config") {
    return "没有找到 Clash Verge 的配置文件。请确认已安装并启动 Clash Verge，或设置环境变量 <code>AIREADY_CLASH_API</code> 指向其他内核的控制器。";
  }
  const detail = data && data.error ? `（${esc(String(data.error))}）` : "";
  return "无法连接代理内核的外部控制器" + detail + "。确认 Clash Verge 正在运行、外部控制器已开启，然后点「重新连接」。";
}

const esc = (value) =>
  String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[ch]));


/* ------------------------------------------------------------------ */
/* 机场订阅与内置内核                                                  */

async function checkKernelUpdate(silent = true) {
  const label = $("kernel-version");
  const upd = $("btn-kernel-update");
  const chk = $("btn-kernel-check");
  if (!label) return;
  chk.disabled = true;
  label.textContent = silent ? label.textContent : "检查中…";
  try {
    const info = await api("/api/kernel");
    label.textContent = `${info.current || "v—"}${info.latest && info.latest !== info.current ? ` → ${info.latest}` : ""}`;
    if (upd) {
      upd.hidden = !info.update_available;
      if (info.update_available) upd.querySelector("span").textContent = `更新到 ${info.latest}`;
    }
    if (info.error && !silent) toast(`检查失败：${info.error}`, "warn");
    if (!info.error && !info.update_available && !silent) toast("已是最新内核");
  } catch (err) {
    if (!silent) toast(`检查失败：${err.message || err}`, "fail");
  } finally {
    chk.disabled = false;
  }
}

async function updateKernel() {
  const upd = $("btn-kernel-update");
  const chk = $("btn-kernel-check");
  upd.disabled = true;
  chk.disabled = true;
  upd.querySelector("span").textContent = "下载中…";
  try {
    const res = await api("/api/kernel/update", { method: "POST", body: {} });
    if (!res.ok) { toast(res.message || "更新失败", "fail"); return; }
    toast(`内核已更新到 ${res.version}`, "pass");
    await loadSubs(true);
    await checkKernelUpdate(true);
  } catch (err) {
    toast(`更新失败：${err.message || err}`, "fail");
  } finally {
    upd.disabled = false;
    chk.disabled = false;
    upd.hidden = true;
  }
}

async function loadClients() {
  try {
    const data = await api("/api/clients");
    state.clients = data.clients || [];
  } catch (err) {
    state.clients = [];
  }
  renderClients();
  syncDeskClient();
}

function clientById(id) { return state.clients.find((c) => c.id === id) || state.clients[0]; }

function syncDeskClient() {
  const sel = $("desk-client");
  if (!sel) return;
  const current = sel.value || (state.settings && state.settings.active_client) || "";
  sel.innerHTML = "";
  for (const c of state.clients) {
    const opt = document.createElement("option");
    opt.value = c.id;
    opt.textContent = c.name;
    sel.appendChild(opt);
  }
  if (current && state.clients.some((c) => c.id === current)) sel.value = current;
  const btn = $("btn-connect-open");
  const active = clientById(sel.value);
  if (btn && active) {
    const nm = window.t ? window.t(active.name) : active.name;
    btn.querySelector("span").textContent = `${window.t ? window.t("准备并打开") : "准备并打开"} ${nm}`;
  }
}

let editingClientId = "";

function nodeNameOptions() {
  const group = currentNodeGroup();
  const nodes = (group && group.nodes) || [];
  return nodes.filter((n) => n.name !== "DIRECT").slice(0, 60).map((n) => [n.name, shortNodeName(n.name)]);
}

function renderClients() {
  const box = $("client-list");
  if (!box) return;
  box.innerHTML = "";
  if (!state.clients.length) {
    box.innerHTML = '<p class="empty-line">还没有客户端，添加一个吧。</p>';
    return;
  }
  for (const c of state.clients) {
    const row = document.createElement("div");
    row.className = "client-row";
    row.innerHTML = `<strong></strong><span class="client-kind"></span><em></em>`;
    row.querySelector("strong").textContent = c.name;
    row.querySelector(".client-kind").textContent = (window.t || ((x) => x))({ desktop: "桌面", cli: "命令行", url: "网页", proxy: "仅准备网络" }[c.kind] || c.kind);
    row.querySelector("em").textContent = c.target || "—";
    const edit = document.createElement("button");
    edit.className = "btn subtle small";
    edit.type = "button";
    edit.textContent = "编辑";
    edit.addEventListener("click", () => {
      editingClientId = c.id;
      $("client-name").value = c.name;
      $("client-kind").value = c.kind;
      $("client-target").value = c.target || "";
      $("btn-client-add").querySelector("span").textContent = "保存";
    });
    const del = document.createElement("button");
    del.className = "btn subtle small";
    del.type = "button";
    del.textContent = "删除";
    del.addEventListener("click", async () => {
      const r = await api("/api/clients/save", { method: "POST", body: { action: "remove", item: { id: c.id } } });
      if (r.ok) { state.clients = r.clients; renderClients(); syncDeskClient(); toast("已删除客户端"); }
    });
    const route = document.createElement("select");
    route.className = "env-input client-route";
    route.title = "这个客户端走哪个节点（需要重新连接生效）";
    const routes = (state.settings && state.settings.ai_routes) || {};
    const options = [["", "跟随规则"]].concat(nodeNameOptions());
    for (const [value, label] of options) {
      const opt = document.createElement("option");
      opt.value = value;
      opt.textContent = label;
      if ((routes[c.id] || "") === value) opt.selected = true;
      route.appendChild(opt);
    }
    route.addEventListener("change", async () => {
      const next = Object.assign({}, routes);
      if (route.value) next[c.id] = route.value; else delete next[c.id];
      try {
        const r = await api("/api/clients/route", { method: "POST", body: { routes: next } });
        state.settings.ai_routes = r.routes || {};
        toast(route.value ? `已把 ${c.name} 固定到 ${shortNodeName(route.value)}` : `已恢复 ${c.name} 跟随规则`);
      } catch (err) {
        toast("保存失败：" + (err.message || err), "fail");
      }
    });

    const actions = document.createElement("div");
    actions.className = "client-actions";
    actions.appendChild(route);
    actions.appendChild(edit);
    actions.appendChild(del);
    row.appendChild(actions);
    box.appendChild(row);
  }
}

async function addClient() {
  const name = $("client-name").value.trim();
  const kind = $("client-kind").value;
  const target = $("client-target").value.trim();
  if (!name) { toast("先填名称", "warn"); return; }
  const body = { action: "add", item: { name, kind, target } };
  if (editingClientId) body.item.id = editingClientId;
  const r = await api("/api/clients/save", { method: "POST", body });
  if (!r.ok) { toast(r.message || "保存失败", "fail"); return; }
  state.clients = r.clients;
  editingClientId = "";
  $("client-name").value = ""; $("client-target").value = "";
  $("btn-client-add").querySelector("span").textContent = "添加";
  renderClients(); syncDeskClient(); toast("已保存客户端");
}

async function loadSubs(quiet) {
  if (quiet && state.subs.busy) return;
  try {
    const data = await api("/api/subscriptions");
    state.subs.items = data.items || [];
    state.subs.core = data.core || null;
    state.subs.sysproxy = data.system_proxy || null;
  } catch (err) {
    state.subs.items = state.subs.items || [];
  }
  renderSubs();
  if (state.nodes.data) renderNodes();
}

function fmtBytes(n) {
  if (typeof n !== "number" || !isFinite(n) || n <= 0) return "";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let value = n;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  const text = value >= 100 ? value.toFixed(0) : value.toFixed(1);
  return `${text} ${units[index]}`;
}

function fmtDate(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  const mi = String(d.getMinutes()).padStart(2, "0");
  return `${mm}-${dd} ${hh}:${mi}`;
}

function usageOf(item) {
  const u = (item && item.usage) || {};
  const used = (Number(u.upload) || 0) + (Number(u.download) || 0);
  const total = Number(u.total) || 0;
  const expire = Number(u.expire) || 0;
  return { used, total, expire };
}

function selectedSub() {
  const items = state.subs.items || [];
  return items.find((i) => i.id === state.subs.selected) || items[0] || null;
}

function renderSubs() {
  const core = state.subs.core || {};
  const chip = $("core-chip");
  chip.textContent = core.running
    ? `内核 ${core.version || ""} · :${core.mixed_port}`.replace("  ", " ")
    : "内核未运行";
  chip.dataset.state = core.running ? "on" : "off";
  $("btn-core-stop").hidden = !core.running;
  const side = $("side-kernel");
  if (side) {
    side.textContent = core.running ? `${window.t ? window.t("内核运行中") : "内核运行中"} · :${core.mixed_port}` : (window.t ? window.t("内核未运行") : "内核未运行");
    side.dataset.on = core.running ? "1" : "0";
  }
  renderTiles();
  const tun = $("tun-mode");
  if (tun) tun.checked = !!core.tun;

  const items = state.subs.items || [];
  if (!state.subs.selected || !items.some((i) => i.id === state.subs.selected)) {
    state.subs.selected = core.active || (items[0] && items[0].id) || "";
  }
  const active = items.find((i) => i.id === core.active) || null;

  const list = $("sub-list");
  list.textContent = "";
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "subs-empty";
    empty.innerHTML = "还没有订阅。点「添加订阅」粘贴机场链接，程序自己拉节点、起内核。";
    list.appendChild(empty);
  }

  for (const item of items) {
    const { used, total, expire } = usageOf(item);
    const card = document.createElement("article");
    card.className = "sub-item";
    card.dataset.id = item.id;
    card.dataset.selected = item.id === state.subs.selected ? "1" : "0";
    card.dataset.active = item.id === (active && active.id) ? "1" : "0";

    const head = document.createElement("header");
    head.className = "sub-item-head";
    const title = document.createElement("div");
    title.className = "sub-item-title";
    const h = document.createElement("h3");
    h.textContent = item.name || "订阅";
    const count = document.createElement("span");
    count.className = "sub-item-count";
    count.textContent = item.nodes ? `${item.nodes} 个节点` : "";
    title.append(h, count);

    const side = document.createElement("div");
    side.className = "sub-item-side";
    if (item.id === core.active) {
      const badge = document.createElement("span");
      badge.className = "sub-item-active";
      badge.textContent = "使用中";
      side.appendChild(badge);
    } else {
      const enable = document.createElement("button");
      enable.type = "button";
      enable.className = "btn primary small";
      enable.textContent = "启用";
      enable.addEventListener("click", (event) => {
        event.stopPropagation();
        activateSub(item.id);
      });
      side.appendChild(enable);
    }
    const check = document.createElement("span");
    check.className = "sub-item-check";
    check.textContent = item.id === state.subs.selected ? "✓" : "";
    side.appendChild(check);
    head.append(title, side);
    card.appendChild(head);

    const body = document.createElement("div");
    body.className = "sub-usage";
    if (total > 0) {
      const percent = Math.min(100, Math.max(0, (used / total) * 100));
      body.innerHTML = `
        <div class="sub-usage-line"><span>已用 ${fmtBytes(used)} / 共 ${fmtBytes(total)}</span><strong>${percent.toFixed(1)}%</strong></div>
        <div class="sub-bar"><i style="width:${percent.toFixed(2)}%"></i></div>
        <div class="sub-usage-foot"><span>剩余 ${fmtBytes(Math.max(0, total - used))}</span><span>${expire ? `到期 ${fmtDate(expire)}` : "到期时间未提供"}</span><span>更新 ${fmtDate(item.updated_at)}</span></div>`;
    } else {
      body.innerHTML = `
        <div class="sub-usage-line"><span>已用 ${used ? fmtBytes(used) : "—"}</span><strong>配额未知</strong></div>
        <div class="sub-usage-foot"><span>${expire ? `到期 ${fmtDate(expire)}` : "到期时间未提供"}</span><span>更新 ${fmtDate(item.updated_at)}</span></div>`;
    }
    card.appendChild(body);
    card.addEventListener("click", () => {
      state.subs.selected = item.id;
      renderSubs();
    });
    list.appendChild(card);
  }

  const foot = $("subs-foot");
  if (foot) {
    foot.textContent = items.length
      ? `${items.length} 个订阅 · 流量由服务商提供，未提供时保留未知。地址与凭据在本机保存。`
      : "1 个订阅都还没有 · 地址与凭据只存在本机。";
  }

  const update = $("btn-sub-update");
  const edit = $("btn-sub-edit");
  const del = $("btn-sub-delete");
  const hasSelection = Boolean(selectedSub());
  if (update) update.disabled = !hasSelection;
  if (edit) edit.disabled = !hasSelection;
  if (del) del.disabled = !hasSelection;

  const proxy = state.subs.sysproxy || {};
  const btn = $("btn-sysproxy");
  btn.hidden = !core.running;
  btn.textContent = proxy.enabled ? "取消接管" : "接管系统代理";
  $("sysproxy-note").textContent = proxy.enabled
    ? `系统代理已指向 127.0.0.1:${proxy.port}，浏览器和 Claude 桌面端都走这里`
    : core.running
      ? "要让浏览器、Claude 桌面端也走这个内核，点左侧按钮"
      : "";
}

async function updateSelectedSub() {
  const item = selectedSub();
  if (!item || state.subs.busy) return;
  state.subs.busy = true;
  const btn = $("btn-sub-update");
  const before = btn.textContent;
  btn.disabled = true;
  btn.textContent = "更新中…";
  try {
    const res = await api("/api/subscriptions/update", { method: "POST", body: { id: item.id } });
    toast(`已更新 ${res.item ? res.item.name : item.name}：${res.item ? res.item.nodes : item.nodes} 个节点`, "ok");
    await loadSubs();
  } catch (err) {
    subError(err.message);
    toast("更新失败：" + err.message, "fail");
  } finally {
    state.subs.busy = false;
    btn.disabled = false;
    btn.textContent = before;
  }
}

async function editSelectedSub() {
  const item = selectedSub();
  if (!item) return;
  const value = await confirmDialog({
    title: "重命名订阅",
    body: "只改本机显示的名字，不会动订阅地址。",
    okText: "保存",
    input: item.name || "",
  });
  if (value === null) return;
  if (!value.trim()) {
    toast("名称不能为空", "fail");
    return;
  }
  try {
    await api("/api/subscriptions/rename", { method: "POST", body: { id: item.id, name: value.trim() } });
    await loadSubs();
    toast("已重命名", "ok");
  } catch (err) {
    toast("重命名失败：" + err.message, "fail");
  }
}

async function deleteSelectedSub() {
  const item = selectedSub();
  if (!item) return;
  const ok = await confirmDialog({
    title: "删除订阅",
    body: `确定删除「${item.name || "订阅"}」？如果正在使用，会先停止内核并还原系统代理。`,
    okText: "删除",
  });
  if (!ok) return;
  await removeSub(item);
}

function subError(message) {
  const box = $("sub-error");
  if (!message) {
    box.hidden = true;
    box.textContent = "";
    return;
  }
  box.hidden = false;
  box.textContent = message;
}

async function addSub(event) {
  if (event) event.preventDefault();
  const input = $("sub-url");
  const url = input.value.trim();
  if (!url || state.subs.busy) return;
  state.subs.busy = true;
  subError("");
  const btn = $("btn-sub-add-confirm");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "拉取中…";
  }
  let autoStart = "";
  try {
    const res = await api("/api/subscriptions/add", { method: "POST", body: { url } });
    input.value = "";
    await loadSubs();
    const created = res && res.item;
    if (created && created.id && !(state.subs.core || {}).running) {
      autoStart = created.id;
    } else {
      toast("订阅已添加", "ok");
    }
  } catch (err) {
    subError(err.message);
    toast("添加失败：" + err.message, "fail");
  } finally {
    state.subs.busy = false;
    if (btn) {
      btn.disabled = false;
      btn.textContent = "添加";
    }
    const form = $("sub-form");
    if (form && !autoStart) form.hidden = true;
  }
  if (autoStart) {
    toast("订阅已添加，正在启用内核…", "ok");
    await activateSub(autoStart);
  }
}

async function activateSub(id) {
  if (state.subs.busy) return;
  state.subs.busy = true;
  toast("正在启动内核…", "ok");
  try {
    await api("/api/subscriptions/activate", { method: "POST", body: { id } });
    await loadSubs();
    await loadNodes();
    toast("内核已启动，正在按新节点复测…", "ok");
    setTimeout(() => startScan(), 600);
  } catch (err) {
    toast("启用失败：" + err.message, "fail");
    await loadSubs();
  } finally {
    state.subs.busy = false;
  }
}

async function removeSub(item) {
  const ok = await confirmDialog({ title: "删除订阅", body: `确定删除「${esc(item.name)}」？`, okText: "删除" });
  if (!ok) return;
  try {
    await api("/api/subscriptions/delete", { method: "POST", body: { id: item.id } });
    await loadSubs();
    await loadNodes();
    toast("已删除", "ok");
  } catch (err) {
    toast("删除失败：" + err.message, "fail");
  }
}

async function stopCore() {
  try {
    await api("/api/core/stop", { method: "POST", body: {} });
    await loadSubs();
    await loadNodes();
    toast("内核已停止", "ok");
  } catch (err) {
    toast("停止失败：" + err.message, "fail");
  }
}

async function setSysProxy(on) {
  const ok = await confirmDialog({
    title: on ? "接管系统代理" : "取消接管",
    body: on
      ? "会把 Wi-Fi 的 HTTP / HTTPS / SOCKS 代理指向 <code>127.0.0.1:" + (state.subs.core?.mixed_port || 7899) +
        "</code>。若 Clash Verge 的 TUN 还开着，请先关掉它，否则两个内核会打架。"
      : "会把系统代理恢复成接管前的样子（没记录时就是直连）。",
    okText: on ? "接管" : "取消接管",
  });
  if (!ok) return false;
  const res = await api("/api/system-proxy", { method: "POST", body: { on } });
  state.subs.sysproxy = res.system_proxy;
  renderSubs();
  renderTiles();
  return true;
}

async function toggleSysProxy() {
  const on = !(state.subs.sysproxy || {}).enabled;
  try {
    if (!(await setSysProxy(on))) return;
    toast(on ? "系统代理已接管，浏览器和 Claude 都会走内核" : "系统代理已还原，浏览器恢复直连", "ok");
    if (on) setTimeout(() => startScan(), 600);
  } catch (err) {
    toast("操作失败：" + err.message, "fail");
  }
}

function usageLine(usage) {
  if (!usage || (!usage.total && !usage.expire)) return null;
  const wrap = document.createElement("div");
  wrap.className = "sub-usage";
  const gb = (n) => `${(n / 1024 / 1024 / 1024).toFixed(1)} GB`;
  if (usage.total > 0) {
    const used = (usage.upload || 0) + (usage.download || 0);
    const pct = Math.min(100, Math.round((used / usage.total) * 100));
    const bar = document.createElement("div");
    bar.className = "sub-bar";
    const fill = document.createElement("i");
    fill.style.width = pct + "%";
    fill.dataset.level = pct >= 90 ? "fail" : pct >= 75 ? "warn" : "pass";
    bar.appendChild(fill);
    const text = document.createElement("span");
    text.textContent = `已用 ${gb(used)} / ${gb(usage.total)}（${pct}%）`;
    wrap.append(bar, text);
  }
  if (usage.expire) {
    const when = new Date(usage.expire * 1000);
    const days = Math.ceil((when.getTime() - Date.now()) / 86400000);
    const text = document.createElement("span");
    text.className = "sub-expire" + (days < 0 ? " expired" : days < 7 ? " soon" : "");
    text.textContent = days < 0 ? "已过期" : `${when.getFullYear()}-${String(when.getMonth() + 1).padStart(2, "0")}-${String(when.getDate()).padStart(2, "0")} 到期（${days} 天）`;
    wrap.appendChild(text);
  }
  return wrap;
}

function timeAgo(ts) {
  const diff = Math.max(0, Date.now() / 1000 - ts);
  if (diff < 90) return "刚刚";
  if (diff < 3600) return `${Math.round(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.round(diff / 3600)} 小时前`;
  return `${Math.round(diff / 86400)} 天前`;
}

/* ------------------------------------------------------------------ */

function bindEvents() {
  $("btn-scan").addEventListener("click", () => startScan());
  $("btn-browse").addEventListener("click", browseProject);
  $("btn-env-save").addEventListener("click", saveEnv);
  $("btn-pick-app").addEventListener("click", () => pickFile("app", "env-app-path"));
  $("btn-pick-cli").addEventListener("click", () => pickFile("file", "env-cli-path"));
  $("btn-restore-foot").addEventListener("click", restoreSession);
  const clientSel = $("desk-client");
  if (clientSel) clientSel.addEventListener("change", syncDeskClient);
  document.querySelectorAll(".mode-btn").forEach((btn) => {
    btn.addEventListener("click", () => setMode(btn.dataset.mode));
  });
  const repairBtn = $("btn-repair");
  if (repairBtn) repairBtn.addEventListener("click", repairNetwork);
  const repairBtn2 = $("btn-repair-2");
  if (repairBtn2) repairBtn2.addEventListener("click", repairNetwork);
  const kChk = $("btn-kernel-check");
  if (kChk) kChk.addEventListener("click", () => checkKernelUpdate(false));
  const kUpd = $("btn-kernel-update");
  if (kUpd) kUpd.addEventListener("click", updateKernel);
  const addBtn = $("btn-client-add");
  if (addBtn) addBtn.addEventListener("click", addClient);
  const manageBtn = $("btn-desk-client-new");
  if (manageBtn) manageBtn.addEventListener("click", () => switchView("env"));
  const homeTest = $("btn-home-test");
  if (homeTest) homeTest.addEventListener("click", () => switchView("desk"));
  const homeRules = $("btn-home-rules");
  if (homeRules) homeRules.addEventListener("click", () => switchView("rules"));
  $("btn-browser-check").addEventListener("click", () => {
    api("/api/open-url", { method: "POST", body: { url: "https://claude.ai/" } }).catch((err) => toast(err.message || "打开失败", "fail"));
  });
  $("btn-sudo").addEventListener("click", enablePasswordless);
  $("btn-checks").addEventListener("click", toggleChecks);
  $("btn-kernel-save").addEventListener("click", saveKernelOptions);
  $("btn-dns-save").addEventListener("click", saveDnsSettings);
  $("btn-rules-save").addEventListener("click", saveCustomRules);
  $("btn-providers-update").addEventListener("click", updateAllProviders);
  $("btn-providers-save").addEventListener("click", saveCustomProviders);
  $("btn-rules-reload").addEventListener("click", reloadKernelConfig);
  $("rules-search").addEventListener("input", renderRules);
  $("btn-desk-subs").addEventListener("click", () => switchView("subs"));
  $("btn-connect").addEventListener("click", () => startSession());
  $("btn-connect-open").addEventListener("click", () => startSession(true));
  $("btn-disconnect").addEventListener("click", endSession);
  $("btn-desk-test").addEventListener("click", testNodes);
  $("desk-node").addEventListener("change", (e) => {
    const group = currentNodeGroup();
    if (group && e.target.value) selectNode(group.name, e.target.value);
  });
  $("desk-proxy").addEventListener("change", (e) => {
    const on = e.target.checked;
    const core = state.subs.core || {};
    const action = core.tun || on
      ? api("/api/core/tun", { method: "POST", body: { on } }).then((res) => {
          if (res && res.ok === false) throw new Error(res.message || "切换 TUN 失败");
          return true;
        })
      : setSysProxy(on);
    action
      .then((ok) => {
        if (!ok) e.target.checked = !on;
        else toast(on ? "已开启 TUN，整机流量走内核" : "已关闭 TUN", "ok");
        return loadSubs(true);
      })
      .then(() => renderTiles())
      .catch((err) => {
        e.target.checked = !on;
        toast(err.message || "操作失败", "fail");
      });
  });
  const openBtn = $("btn-open-claude");
  if (openBtn) openBtn.addEventListener("click", openClaude);
  $("btn-history-refresh").addEventListener("click", loadHistory);
  $("btn-save-interval").addEventListener("click", saveWatchInterval);
  $("btn-restore").addEventListener("click", restoreSession);
  const tunBox = $("tun-mode");
  if (tunBox) tunBox.addEventListener("change", (e) => toggleTun(e.target.checked));
  for (const item of document.querySelectorAll(".nav-item")) {
    item.addEventListener("click", () => switchView(item.dataset.view));
  }
  for (const opt of document.querySelectorAll(".theme-card")) {
    opt.addEventListener("click", () => chooseTheme(opt.dataset.themeMode));
  }
  const syncBtn = $("btn-sync");
  if (syncBtn) syncBtn.addEventListener("click", () => syncTimezone(state.tz.exit));
  const copyBtn = $("btn-copy");
  if (copyBtn) copyBtn.addEventListener("click", copyCommand);
  $("sub-form").addEventListener("submit", addSub);
  $("btn-sub-add").addEventListener("click", () => {
    const form = $("sub-form");
    form.hidden = !form.hidden;
    if (!form.hidden) $("sub-url").focus();
  });
  $("btn-sub-update").addEventListener("click", updateSelectedSub);
  $("btn-sub-edit").addEventListener("click", editSelectedSub);
  $("btn-sub-delete").addEventListener("click", deleteSelectedSub);
  $("btn-core-stop").addEventListener("click", stopCore);
  $("btn-sysproxy").addEventListener("click", toggleSysProxy);
  $("btn-quit").addEventListener("click", quitApp);
  $("btn-test-nodes").addEventListener("click", testNodes);
  $("btn-fastest").addEventListener("click", selectFastest);
  $("node-search").addEventListener("input", (e) => {
    state.nodes.query = e.target.value;
    renderNodes();
  });
  const subAuto = $("sub-auto");
  if (subAuto) subAuto.addEventListener("change", async (e) => {
    try {
      await api("/api/settings", { method: "POST", body: { sub_auto_hours: Number(e.target.value) } });
      toast("自动更新已设置为 " + (e.target.value === "0" ? "关闭" : "每 " + e.target.value + " 小时"));
    } catch (err) {
      toast("保存失败：" + (err.message || err), "fail");
    }
  });
  const menuBar = $("env-menu-bar");
  if (menuBar) menuBar.addEventListener("change", async (e) => {
    try {
      await api("/api/settings", { method: "POST", body: { menu_bar: e.target.checked } });
      toast(e.target.checked ? "菜单栏图标已开启（重启应用生效）" : "菜单栏图标已关闭（重启应用生效）");
    } catch (err) {
      toast("保存失败：" + (err.message || err), "fail");
    }
  });
  const appUpdate = $("btn-app-update");
  if (appUpdate) appUpdate.addEventListener("click", async () => {
    const note = $("app-update-note");
    note.textContent = "检查中…";
    try {
      const r = await api("/api/app/update");
      if (!r.ok) { note.textContent = r.message || "检查失败"; return; }
      note.textContent = r.has_update
        ? `发现新版本 ${r.latest}，当前 ${r.current}${r.url ? "（" + r.url + "）" : ""}`
        : `已是最新版本（${r.current}）`;
    } catch (err) {
      note.textContent = "检查失败：" + (err.message || err);
    }
  });
  const langSel = $("env-lang");
  if (langSel) langSel.addEventListener("change", async (e) => {
    const lang = e.target.value === "en" ? "en" : "zh";
    state.settings = Object.assign({}, state.settings, { lang });
    applyLang();
    try {
      await api("/api/settings", { method: "POST", body: { lang } });
      setTimeout(() => location.reload(), 120);
    } catch (err) {
      toast("保存失败：" + (err.message || err), "fail");
    }
  });
  const connSearch = $("conn-search");
  if (connSearch) connSearch.addEventListener("input", (e) => {
    state.conn = Object.assign({}, state.conn, { query: e.target.value });
    renderConnections();
  });
  const connSort = $("conn-sort");
  if (connSort) connSort.addEventListener("change", (e) => {
    state.conn = Object.assign({}, state.conn, { sort: e.target.value });
    renderConnections();
  });
  const logSearch = $("log-search");
  if (logSearch) logSearch.addEventListener("input", (e) => {
    state.logQuery = e.target.value;
    renderLogs();
  });
  const logLevel = $("log-level");
  if (logLevel) logLevel.addEventListener("change", (e) => {
    state.logLevel = e.target.value;
    renderLogs();
  });
  const ruleTest = $("btn-rule-test");
  if (ruleTest) ruleTest.addEventListener("click", runRuleTest);
  const ruleInput = $("rule-test-input");
  if (ruleInput) ruleInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") runRuleTest();
  });
  const logExport = $("btn-log-export");
  if (logExport) logExport.addEventListener("click", exportLogs);
  $("btn-fav-only").addEventListener("click", () => {
    state.nodes.onlyFav = !state.nodes.onlyFav;
    $("btn-fav-only").classList.toggle("on", state.nodes.onlyFav);
    renderNodes();
  });
  $("node-sort").addEventListener("change", (e) => {
    state.nodes.sort = e.target.value;
    renderNodes();
  });
  $("btn-nodes-retry").addEventListener("click", () => {
    $("nodes-help-text").textContent = "正在连接…";
    loadNodes();
  });
}

boot();
