/* 静态文案翻译：按中文原文精确匹配，切换语言时原地替换。 */
(function () {
  const ZH_TO_EN = {"语言与浏览器用于「浏览器核对」打开的窗口，以及 CLI 模式启动时的 LANG；界面语言可在「设置 → 维护」里单独切换。": "Language and browser apply to the browser check window and the CLI LANG; the interface language is switched under Settings → Maintenance.","界面语言可在「设置 → 维护」里单独切换。": "The interface language can be switched separately under Settings → Maintenance.","删除": "Delete", "跟随规则": "Follow rules", "TUN 已接管": "TUN active", "会话中": "Session on", "内核已就绪": "Kernel ready", "未启用订阅": "No profile", "名称，如 ChatGPT 桌面": "Name, e.g. ChatGPT desktop", "应用名 / 命令 / 网址，如 Claude, claude, https://chatgpt.com/": "App name / command / URL", "搜索域名 / 规则 / 链路": "Search host / rule / chain", "搜索日志内容": "Search logs", "搜索节点或地区": "Search nodes or region", "输入域名或 IP，测试会命中哪条规则": "Enter a domain or IP to see which rule matches", "点击复制目标地址": "Click to copy the target", "取消收藏": "Unstar", "收藏这个节点": "Star this node", "名称 行为 URL": "name behavior url", "跟随规则生效": "Follow rules", "Clash Link · 海外 AI 网络体检": "Clash Link · AI network checkup", "控制台连接已断开，请确认应用仍在运行。": "Console disconnected — make sure the app is still running.", "主页": "Home", "测试": "Test", "连接": "Proxy", "订阅": "Profile", "规则": "Rules", "日志": "Logs", "设置": "Settings", "内核未运行": "Kernel not running", "首页": "Home", "选择节点，准备网络并打开 Claude。": "Pick a node, prepare the network, then open Claude.", "待检测": "Not tested", "上次操作没有成功": "Last action failed", "一键修复": "Repair", "准备一次工作会话": "Prepare a work session", "官方桌面客户端使用 TUN；也可以只走内核的混合端口代理。": "The desktop app uses TUN; you can also use the kernel's mixed port only.", "未连接": "Disconnected", "使用节点": "Node", "测速": "Test speed", "添加节点": "Add node", "AI 客户端": "AI client", "管理客户端": "Manage clients", "项目文件夹": "Project folder", "浏览…": "Browse…", "TUN 虚拟网卡": "TUN virtual adapter", "桌面客户端需要 TUN；已启用免密接管时不会弹授权。": "The desktop app needs TUN; no password prompt once passwordless takeover is enabled.", "准备并打开 Claude 桌面客户端": "Prepare and open Claude desktop", "连接并检测": "Connect and test", "断开": "Disconnect", "连接后自动检测当前出口，约 5–10 秒": "Detects the current exit automatically after connecting — about 5–10 s.", "上次体检": "Last checkup", "尚未检测": "Not tested yet", "准备好网络后，点「测试」跑一次完整体检。": "Once the network is ready, open Test to run a full checkup.", "查看规则": "View rules", "去测试": "Go to Test", "桌面应用": "Desktop app", "命令行": "CLI", "网页": "Web", "仅准备网络": "Network only", "添加": "Add", "主页会列出这些客户端，点「准备并打开」时启动选中的那个。": "These clients show up on Home; “Prepare and open” launches the selected one.", "出口节点": "Exit node", "等待检测": "Waiting for test", "出口 IP": "Exit IP", "代理": "Proxy", "边缘节点": "Edge", "时区一致性": "Timezone alignment", "正在比对系统时区与出口时区…": "Comparing system timezone with exit timezone…", "系统": "System", "出口": "Exit", "一键同步时区": "Sync timezone", "复制命令": "Copy command", "本地代理": "Local proxy", "连接模式": "Connection mode", "API 连通性": "API reachability", "启动前检查": "Pre-flight check", "官方网络要求": "Official requirements", "浏览器核对": "Browser check", "重新检测": "Scan again", "展开 13 项": "Show 13 items", "分": "pts", "体检结果": "Checkup result", "正在检测网络环境…": "Checking the network…", "首次检测约需 5–10 秒": "First run takes 5–10 s.", "运行模式": "Run mode", "按规则分流，日常用这个": "Route by rules — the usual choice", "全局": "Global", "所有流量都走代理": "All traffic through the proxy", "直连": "Direct", "全部不走代理": "No traffic through the proxy", "代理组": "Proxy groups", "实时流量": "Live traffic", "↓ 下行": "↓ Down", "↑ 上行": "↑ Up", "断开全部": "Close all", "最近使用": "Most recent", "按下载": "By download", "按上传": "By upload", "0 条连接": "0 connections", "全部级别": "All levels", "导出日志": "Export logs", "重新载入配置": "Reload config", "测试命中": "Test match", "按当前规则从上到下匹配，支持 DOMAIN / SUFFIX / KEYWORD / IP-CIDR / RULE-SET / MATCH。": "Matches the active rules top-down; DOMAIN / SUFFIX / KEYWORD / IP-CIDR / RULE-SET / MATCH.", "规则集": "Rule sets", "远程规则集，更新会重新下载内容": "Remote rule sets — update re-downloads them", "全部更新": "Update all", "行为可选 domain / ipcidr / classical；保存后热重载": "Behavior: domain / ipcidr / classical; hot-reloads on save", "保存规则集": "Save rule sets", "自定义规则": "Custom rules", "写在 rules 顶部，优先于订阅规则": "Written at the top of rules; wins over subscription rules", "保存后热重载，不重启内核；格式：类型,内容[,策略]": "Hot-reloads on save, no kernel restart; format: TYPE,CONTENT[,POLICY]", "保存并应用": "Save and apply", "添加订阅": "Add profile", "更新选中": "Update selected", "编辑": "Edit", "删除订阅": "Delete profile", "不自动更新": "No auto-update", "每 6 小时": "Every 6 h", "每 12 小时": "Every 12 h", "每天": "Daily", "停止内核": "Stop kernel", "流量由服务商提供，未提供时保留未知。地址与凭据在本机保存。": "Usage comes from the provider; shown as unknown when missing. Address and credentials stay on this Mac.", "TUN 模式：创建虚拟网卡接管整机流量（开启后系统代理可以关掉）": "TUN mode: a virtual adapter takes over all traffic (system proxy can then stay off)", "接管系统代理": "Take over system proxy", "节点管理": "Nodes", "切到最快": "Fastest", "★ 收藏": "★ Starred", "默认顺序": "Default order", "按延迟": "By latency", "按名称": "By name", "按地区": "By region", "当前": "Current", "点击节点即切换；刚测过速时直接复用延迟，不再重复测": "Click a node to switch; a recent speed test is reused instead of re-testing.", "延迟已统一校正（unified-delay，扣除各协议握手差异），可直接和 Clash Verge 的数字对比；实际 claude.ai 首字节约 0.4–0.5 s，连接复用后更快。": "Latency is normalized (unified-delay) and comparable with Clash Verge; claude.ai TTFB is about 0.4–0.5 s and gets faster once connections are reused.", "重新连接": "Reconnect", "连接与客户端": "Connection and client", "本地混合端口": "Local mixed port", "仅监听 127.0.0.1，不修改 macOS 系统代理。": "Listens on 127.0.0.1 only; does not change the macOS system proxy.", "启动方式": "Launch mode", "官方 Claude 桌面客户端": "Claude desktop app", "Claude Code CLI（终端）": "Claude Code CLI (terminal)", "Claude 桌面应用": "Claude desktop app", "CLI 可执行文件": "CLI executable", "进程与核对浏览器语言": "Process / browser language", "核对浏览器": "Browser for checks", "自动（优先 Chrome）": "Auto (prefer Chrome)", "语言与浏览器用于「浏览器核对」打开的窗口，以及 CLI 模式启动时的 LANG；应用自身界面目前只有中文。": "Language and browser apply to the browser check window and the CLI LANG; the app UI is fully localized now.", "临时系统环境": "Temporary system environment", "本次临时关闭活动物理网络的 IPv6": "Disable IPv6 on the active physical network for this session", "出口验证后临时匹配 macOS 系统时区": "Match the macOS timezone to the verified exit for this session", "需要 TUN 时才有意义。修改前记录原状态，断开或退出时恢复；不支持自动恢复的 IPv6 配置会在修改前停止。": "Only meaningful with TUN. Original state is recorded and restored on disconnect or quit; IPv6 setups that cannot be restored are left untouched.", "自定义严格预检": "Custom strict pre-check", "兼容预检，不是官方封号要求：三个 IPv4 出口一致、参考地区为 US、TUN 启用、IPv6 关闭、系统时区匹配以及网页可达。地区结果仅供参考。": "A compatibility pre-check, not an official ban rule: three matching IPv4 exits, US region, TUN on, IPv6 off, timezone matched and web reachable. Region is advisory.", "保存设置": "Save settings", "启用 DNS 覆写": "Enable DNS override", "TUN 模式下强制启用（防 DNS 泄漏）；关闭仅影响非 TUN 场景": "Always on under TUN (prevents DNS leaks); off only affects non-TUN use", "启用": "Enable", "解析模式": "Resolution mode", "fake-ip 适合 TUN；redir-host 兼容性更好": "fake-ip suits TUN; redir-host is more compatible", "主 DNS（每行一个）": "Primary DNS (one per line)", "Fallback（可选）": "Fallback (optional)", "fake-ip-filter（可选）": "fake-ip-filter (optional)", "保存后热重载配置，不重启内核": "Hot-reloads the config on save; no kernel restart", "保存 DNS": "Save DNS", "内核": "Kernel", "允许局域网连接": "Allow LAN", "手机或其它设备可通过本机端口上网": "Phones or other devices can use this machine's port", "允许": "Allow", "开启后内核会解析并使用 IPv6": "The kernel resolves and uses IPv6", "开启": "On", "日志级别": "Log level", "影响「代理 → 日志」的详细程度": "Controls detail in Proxy → Logs", "内核版本": "Kernel version", "从 GitHub 拉取 mihomo 最新版；只换用户目录里的二进制，不动 App 包": "Fetch the latest mihomo from GitHub into the user directory; the app bundle stays untouched", "检查更新": "Check for updates", "更新": "Update", "TUN 栈": "TUN stack", "gvisor 兼容性好，system 性能更好；改动会重启内核": "gvisor is more compatible, system is faster; changing it restarts the kernel", "保存": "Save", "权限": "Permissions", "免密接管网络": "Passwordless network takeover", "启用一次后，改时区、切换系统代理、开关 TUN 都不再弹密码": "Authorize once; timezone, system proxy and TUN changes stop asking for a password", "本地时区": "Local timezone", "断开节点或退出时自动恢复到这个时区；每次改时区前会自动记录当前值": "Restores this timezone on disconnect or quit; the current value is recorded before each change", "维护": "Maintenance", "巡检间隔": "Watch interval", "守护模式下的自动检测周期（秒）": "Automatic checkup interval in watch mode (seconds)", "界面语言": "Interface language", "中文 / English（导航与页面标题）": "Chinese / English", "中文": "Chinese", "界面主题": "Theme", "深色 / 浅色，或跟随 macOS 外观": "Dark / light, or follow macOS", "深色": "Dark", "始终使用深色": "Always dark", "浅色": "Light", "始终使用浅色": "Always light", "自动": "Auto", "跟随 macOS 外观": "Follow macOS", "网络自检": "Network self-check", "还原 DNS/代理、按当前设置重启内核": "Restore DNS/proxy and restart the kernel with current settings", "数据目录": "Data directory", "订阅、内核配置、历史记录都放在这里": "Profiles, kernel config and history live here", "本次会话": "This session", "恢复临时设置": "Restore temporary settings", "接管代理、改时区、启停内核都会被记在这里。": "Proxy takeover, timezone changes and kernel start/stop are recorded here.", "用量统计": "Usage", "今日下行": "Today down", "今日上行": "Today up", "本月下行": "Month down", "本月上行": "Month up", "运行记录": "Check history", "刷新": "Refresh", "就绪 · 添加节点或订阅即可开始": "Ready · add a node or profile to start", "导出报告": "Export report", "退出": "Quit", "取消": "Cancel", "确认": "Confirm", "建议处理：": "Suggested: ", "检查项": "checks", "项": "items", "代理链路": "Proxy chain", "DNS 解析": "DNS resolution", "Anthropic API": "Anthropic API", "Claude 网页版": "Claude web", "Console 控制台": "Console", "Statsig 遥测": "Statsig telemetry", "TLS 与证书": "TLS and certificate", "链路延迟": "Link latency", "地区可用性": "Region availability", "系统时钟": "System clock", "本地客户端": "Local clients", "OpenAI / ChatGPT": "OpenAI / ChatGPT", "Google Gemini": "Google Gemini", "xAI Grok": "xAI Grok", "Perplexity": "Perplexity", "Mistral": "Mistral", "GitHub Copilot": "GitHub Copilot", "可以连接，但存在需要处理的风险项": "Reachable, with a few things to fix", "存在需要处理的项": "has items to fix", "桌面": "Desktop", "准备并打开": "Prepare and open", "准备并打开 Claude 桌面": "Prepare and open Claude desktop", "内核运行中": "Kernel running", "可以连接，但有需要处理的风险项": "Reachable, with a few things to fix", "当前网络无法正常使用 Claude": "Claude is not usable on this network", "上次检测没有发现问题。": "No problems found in the last checkup.", "内核在跑，但还没接管系统代理；点「开始会话」让浏览器和 Claude 也走这里。": "The kernel is running but the system proxy is not taken over yet — click Start session to route browsers and Claude through it.", "整机流量已走虚拟网卡，浏览器、Claude 桌面端都自动走这条线路，不用再开系统代理。": "All traffic uses the virtual adapter; browsers and Claude go through it automatically, no system proxy needed.", "系统代理已指向": "System proxy points to", "还没有启用订阅；先到「机场订阅」粘贴链接并启用，再回来开始会话。": "No profile yet — add and enable one under Profile, then start a session.", "引擎未运行": "Kernel stopped", "Claude 桌面": "Claude desktop", "ChatGPT 桌面": "ChatGPT desktop", "ChatGPT 网页": "ChatGPT web", "留空自动查找 Claude.app": "Auto-detect Claude.app", "留空自动查找 claude": "Auto-detect claude", "可用": "Available", "有风险": "At risk", "检测中": "Checking", "可以正常使用": "Working normally", "有需要处理的风险项": "Needs attention", "当前网络无法正常使用": "Network unavailable", "会话已就绪 · TUN 接管整机流量": "Session ready · TUN handles all traffic", "内核未运行 · 到「订阅管理」启用一条订阅": "Kernel stopped · enable a profile under Profile", "全部通过": "All passed", "项正常": "passed", "项注意": "warnings", "项异常": "failures"};
  const EN_TO_ZH = {};
  for (const [zh, en] of Object.entries(ZH_TO_EN)) if (!(en in EN_TO_ZH)) EN_TO_ZH[en] = zh;
  const ORIG = new WeakMap();
  let lang = "zh";

  function keepSpace(oldValue, newValue) {
    const lead = oldValue.match(/^\s*/)[0];
    const tail = oldValue.match(/\s*$/)[0];
    return lead + newValue + tail;
  }

  function walkText(root) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        const parent = node.parentElement;
        if (!parent) return NodeFilter.FILTER_REJECT;
        if (parent.closest("script,style,textarea,pre,code,[data-i18n-skip]")) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const node of nodes) {
      const value = node.nodeValue || "";
      const trimmed = value.trim();
      if (!trimmed) continue;
      if (lang === "en") {
        if (!ORIG.has(node) && ZH_TO_EN[trimmed]) {
          ORIG.set(node, value);
          node.nodeValue = keepSpace(value, ZH_TO_EN[trimmed]);
        }
      } else if (ORIG.has(node)) {
        node.nodeValue = ORIG.get(node);
        ORIG.delete(node);
      } else if (EN_TO_ZH[trimmed] && !ZH_TO_EN[trimmed]) {
        node.nodeValue = keepSpace(value, EN_TO_ZH[trimmed]);
      }
    }
  }

  function walkAttrs(root) {
    const scope = root.querySelectorAll ? root : document;
    const list = scope.querySelectorAll("[placeholder],[title],[aria-label]");
    for (const el of list) {
      for (const attr of ["placeholder", "title", "aria-label"]) {
        const value = el.getAttribute(attr);
        if (!value) continue;
        const key = "data-i18n-" + attr;
        if (lang === "en") {
          if (!el.hasAttribute(key) && ZH_TO_EN[value.trim()]) {
            el.setAttribute(key, value);
            el.setAttribute(attr, ZH_TO_EN[value.trim()]);
          }
        } else if (el.hasAttribute(key)) {
          el.setAttribute(attr, el.getAttribute(key));
          el.removeAttribute(key);
        }
      }
    }
  }

  function apply(next) {
    lang = next === "en" ? "en" : "zh";
    walkText(document.body);
    walkAttrs(document);
    document.documentElement.lang = lang === "en" ? "en" : "zh-Hans";
  }

  window.applyI18n = apply;
  window.currentI18nLang = function () { return lang; };
  window.t = function (text) {
    if (lang !== "en") return text;
    return ZH_TO_EN[text] || text;
  };

  const observer = new MutationObserver((records) => {
    if (lang !== "en") return;
    for (const record of records) {
      for (const node of record.addedNodes) {
        if (node.nodeType === 1) { walkText(node); walkAttrs(node); }
        else if (node.nodeType === 3 && node.parentElement) walkText(node.parentElement);
      }
    }
  });
  document.addEventListener("DOMContentLoaded", () => {
    observer.observe(document.body, { childList: true, subtree: true });
  });
})();
