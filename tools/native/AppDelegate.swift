import Cocoa
import WebKit

/// 原生外壳：启动内嵌的 Python 控制台，并用自己的窗口显示界面，
/// 不再调用系统浏览器。
final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, NSWindowDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var backend: Process?
    private var startedBackend = false
    private var port: Int = 8788
    private var statusItem: NSStatusItem?
    private var tunMenuItem: NSMenuItem?
    private var modeItems: [NSMenuItem] = []
    private var statusTimer: Timer?

    private var baseURL: URL { URL(string: "http://127.0.0.1:\(port)/")! }

    private var apiToken: String {
        let home = FileManager.default.homeDirectoryForCurrentUser
        let path = home.appendingPathComponent("Library/Application Support/ClashLink/api_token")
        return (try? String(contentsOf: path, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        buildWindow()
        buildStatusBar()
        showLoading("正在启动本地控制台…")
        bootstrap()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { window?.makeKeyAndOrderFront(nil) }
        NSApp.activate(ignoringOtherApps: true)
        return true
    }

    func windowWillClose(_ notification: Notification) {
        window?.orderOut(nil)
    }

    func applicationWillTerminate(_ notification: Notification) {
        if startedBackend {
            // 先让服务端收尾（关闭系统代理、停内核），再杀进程
            quitBackend()
        }
        if startedBackend, let proc = backend, proc.isRunning {
            proc.terminate()
        }
    }

    private func quitBackend(timeout: TimeInterval = 25) {
        guard let url = URL(string: "http://127.0.0.1:\(port)/api/quit") else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = timeout
        request.httpBody = Data("{}".utf8)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if !apiToken.isEmpty { request.setValue(apiToken, forHTTPHeaderField: "X-ClashLink-Token") }
        let semaphore = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: request) { _, _, _ in semaphore.signal() }.resume()
        _ = semaphore.wait(timeout: .now() + timeout)
    }

    func applicationDockMenu(_ sender: NSApplication) -> NSMenu? {
        let menu = NSMenu()
        menu.addItem(withTitle: "打开控制台", action: #selector(showConsole), keyEquivalent: "")
        menu.addItem(.separator())
        let tun = NSMenuItem(title: "切换 TUN", action: #selector(toggleTun), keyEquivalent: "")
        menu.addItem(tun)
        let repair = NSMenuItem(title: "修复网络", action: #selector(repairNetwork), keyEquivalent: "")
        menu.addItem(repair)
        menu.addItem(.separator())
        for (title, mode) in [("规则模式", "rule"), ("全局模式", "global"), ("直连模式", "direct")] {
            let item = NSMenuItem(title: title, action: #selector(pickMode(_:)), keyEquivalent: "")
            item.representedObject = mode
            menu.addItem(item)
        }
        for item in menu.items where item.action != nil {
            item.target = self
        }
        return menu
    }

    // MARK: - 窗口

    private func buildWindow() {
        let rect = NSRect(x: 0, y: 0, width: 1180, height: 860)
        window = NSWindow(
            contentRect: rect,
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Clash Link"
        window.minSize = NSSize(width: 900, height: 640)
        window.center()
        window.delegate = self
        window.titlebarAppearsTransparent = true

        let config = WKWebViewConfiguration()
        config.preferences.setValue(true, forKey: "developerExtrasEnabled")
        webView = WKWebView(frame: rect, configuration: config)
        webView.navigationDelegate = self
        webView.setValue(false, forKey: "drawsBackground")
        window.contentView = webView
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func buildMenu() {
        let mainMenu = NSMenu()
        let appItem = NSMenuItem()
        mainMenu.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于 Clash Link",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
                        keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "重新载入页面", action: #selector(reloadPage), keyEquivalent: "r")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出 Clash Link",
                        action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        NSApp.mainMenu = mainMenu
    }

    @objc private func reloadPage() {
        webView.load(URLRequest(url: baseURL))
    }

    private func showLoading(_ message: String) {
        let html = """
        <!doctype html><meta charset="utf-8">
        <style>
          :root { color-scheme: dark; }
          body { margin:0; height:100vh; display:flex; align-items:center; justify-content:center;
                 background:#08080b; color:#9b9ba4; font:15px -apple-system, "PingFang SC", sans-serif; }
          .box { text-align:center; }
          .dot { width:8px; height:8px; border-radius:50%; background:#d97757; display:inline-block;
                 margin-right:10px; animation:pulse 1.2s ease-in-out infinite; }
          @keyframes pulse { 0%,100% { opacity:.25 } 50% { opacity:1 } }
        </style>
        <div class="box"><span class="dot"></span>\(message)</div>
        """
        webView.loadHTMLString(html, baseURL: baseURL)
    }

    // MARK: - 菜单栏

    private func buildStatusBar() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let button = item.button {
            let image = NSImage(systemSymbolName: "shield.lefthalf.filled", accessibilityDescription: "Clash Link")
                ?? NSImage(named: NSImage.applicationIconName)
            if let image = image {
                image.isTemplate = true
                image.size = NSSize(width: 17, height: 17)
                button.image = image
            } else {
                button.title = "CL"
            }
            button.toolTip = "Clash Link"
            NSLog("ClashLink status item ready: \(button.image != nil ? "image" : "text")")
        }
        let menu = NSMenu()
        menu.addItem(withTitle: "打开控制台", action: #selector(showConsole), keyEquivalent: "").target = self
        menu.addItem(.separator())
        let tun = menu.addItem(withTitle: "TUN：读取中…", action: #selector(toggleTun), keyEquivalent: "")
        tun.target = self
        tunMenuItem = tun
        let modeRoot = NSMenuItem(title: "模式", action: nil, keyEquivalent: "")
        let modeMenu = NSMenu()
        for (title, value) in [("规则", "rule"), ("全局", "global"), ("直连", "direct")] {
            let mi = NSMenuItem(title: title, action: #selector(pickMode(_:)), keyEquivalent: "")
            mi.target = self
            mi.representedObject = value
            modeMenu.addItem(mi)
            modeItems.append(mi)
        }
        modeRoot.submenu = modeMenu
        menu.addItem(modeRoot)
        menu.addItem(.separator())
        menu.addItem(withTitle: "修复网络", action: #selector(repairNetwork), keyEquivalent: "").target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "退出 Clash Link", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        item.menu = menu
        statusItem = item
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) {
            let frame = item.button?.window?.frame ?? .zero
            try? "created=1 image=\(item.button?.image != nil) visible=\(item.isVisible) frame=\(frame)".write(
                toFile: "/tmp/clashlink-status.txt", atomically: true, encoding: .utf8)
        }
        statusTimer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            self?.refreshStatus()
        }
        refreshStatus()
    }

    @objc private func showConsole() {
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func requestJSON(_ path: String, method: String = "GET", body: [String: Any]? = nil) -> [String: Any]? {
        guard let url = URL(string: "http://127.0.0.1:\(port)\(path)") else { return nil }
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.timeoutInterval = 30
        let token = apiToken
        if !token.isEmpty { req.setValue(token, forHTTPHeaderField: "X-ClashLink-Token") }
        if let body = body {
            req.httpBody = try? JSONSerialization.data(withJSONObject: body)
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        var result: [String: Any]?
        let sem = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: req) { data, _, _ in
            if let data = data, let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                result = obj
            }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 31)
        return result
    }

    private func refreshStatus() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            let state = self.requestJSON("/api/state")
            let core = self.requestJSON("/api/subscriptions")?["core"] as? [String: Any]
            let settings = state?["settings"] as? [String: Any]
            let running = core?["running"] as? Bool ?? false
            let tun = core?["tun"] as? Bool ?? false
            let mode = self.requestJSON("/api/clash/configs")?["mode"] as? String ?? "rule"
            let wantTun = settings?["tun"] as? Bool ?? false
            let menuBar = settings?["menu_bar"] as? Bool ?? true
            DispatchQueue.main.async {
                self.tunMenuItem?.title = tun ? "TUN：已开启（点击关闭）"
                    : (running ? "TUN：已关闭（点击开启）" : "TUN：内核未运行")
                for item in self.modeItems {
                    item.state = (item.representedObject as? String) == mode ? .on : .off
                }
                if !tun && wantTun && !running {
                    self.tunMenuItem?.title = "TUN：内核未运行"
                }
                self.statusItem?.isVisible = menuBar
            }
        }
    }

    @objc private func toggleTun() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let state = self.requestJSON("/api/subscriptions")
            let tun = (state?["core"] as? [String: Any])?["tun"] as? Bool ?? false
            _ = self.requestJSON("/api/core/tun", method: "POST", body: ["on": !tun])
            self.refreshStatus()
        }
    }

    @objc private func pickMode(_ sender: NSMenuItem) {
        guard let mode = sender.representedObject as? String else { return }
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            _ = self?.requestJSON("/api/clash/mode", method: "POST", body: ["mode": mode])
            self?.refreshStatus()
        }
    }

    @objc private func repairNetwork() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let res = self.requestJSON("/api/repair", method: "POST", body: [:])
            let ok = res?["ok"] as? Bool ?? false
            let steps = (res?["steps"] as? [String])?.joined(separator: "、") ?? ""
            DispatchQueue.main.async {
                let alert = NSAlert()
                alert.messageText = ok ? "已修复" : "修复失败"
                alert.informativeText = ok ? (steps.isEmpty ? "网络状态正常，无需处理" : steps)
                    : (res?["message"] as? String ?? "请查看日志")
                alert.addButton(withTitle: "好")
                alert.runModal()
            }
            self.refreshStatus()
        }
    }

    // MARK: - 后端


    private func bootstrap() {
        if ping() {
            loadConsole()
            return
        }
        guard let python = findPython() else {
            alert("缺少 Python 3", "应用包内没有找到内置 Python，也没有系统 python3。请重新安装本应用。")
            return
        }
        guard let runPy = Bundle.main.path(forResource: "run", ofType: "py") else {
            alert("安装不完整", "应用包内缺少 run.py，请重新解压或重新安装。")
            return
        }
        startBackend(python: python, runPy: runPy)
        waitForServer(attemptsLeft: 40)
    }

    private func findPython() -> String? {
        var candidates: [String] = []
        if let env = ProcessInfo.processInfo.environment["CLASHLINK_PYTHON"], !env.isEmpty {
            candidates.append(env)
        }
        if let bundled = Bundle.main.resourcePath {
            candidates.append(bundled + "/python/bin/python3")
        }
        candidates += [
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            "/usr/bin/python3",
        ]
        for path in candidates where FileManager.default.isExecutableFile(atPath: path) {
            if supportsModernPython(path) { return path }
        }
        return nil
    }

    private func supportsModernPython(_ path: String) -> Bool {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: path)
        proc.arguments = ["-B", "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)"]
        proc.environment = ["PYTHONDONTWRITEBYTECODE": "1", "HOME": NSHomeDirectory()]
        proc.standardOutput = FileHandle.nullDevice
        proc.standardError = FileHandle.nullDevice
        do {
            try proc.run()
            proc.waitUntilExit()
            return proc.terminationStatus == 0
        } catch {
            return false
        }
    }

    private func startBackend(python: String, runPy: String) {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: python)
        proc.arguments = ["-u", "-B", runPy, "--no-browser", "--port", String(port)]
        proc.environment = [
            "HOME": NSHomeDirectory(),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        ]
        if let logPath = logFileURL() {
            if !FileManager.default.fileExists(atPath: logPath.path) {
                FileManager.default.createFile(atPath: logPath.path, contents: nil)
            }
            if let handle = try? FileHandle(forWritingTo: logPath) {
                handle.seekToEndOfFile()
                proc.standardOutput = handle
                proc.standardError = handle
            }
        }
        do {
            try proc.run()
            backend = proc
            startedBackend = true
        } catch {
            alert("无法启动控制台", "启动 Python 失败：\(error.localizedDescription)")
        }
    }

    private func logFileURL() -> URL? {
        let dir = URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Library/Logs")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("ClashLink.log")
    }

    private func ping() -> Bool {
        var request = URLRequest(url: baseURL.appendingPathComponent("api/ping"))
        request.timeoutInterval = 1.5
        request.cachePolicy = .reloadIgnoringLocalCacheData
        let sem = DispatchSemaphore(value: 0)
        var ok = false
        URLSession.shared.dataTask(with: request) { data, response, _ in
            if let http = response as? HTTPURLResponse, http.statusCode == 200, data != nil { ok = true }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 2.0)
        return ok
    }

    private func waitForServer(attemptsLeft: Int) {
        DispatchQueue.global().asyncAfter(deadline: .now() + 0.4) { [weak self] in
            guard let self else { return }
            if self.ping() {
                DispatchQueue.main.async { self.loadConsole() }
            } else if attemptsLeft > 0 {
                self.waitForServer(attemptsLeft: attemptsLeft - 1)
            } else {
                DispatchQueue.main.async {
                    self.alert("控制台没有响应",
                               "日志在 ~/Library/Logs/ClashLink.log。请把日志发给开发者。")
                }
            }
        }
    }

    private func loadConsole() {
        webView.load(URLRequest(url: baseURL))
    }

    private func alert(_ title: String, _ message: String) {
        let a = NSAlert()
        a.messageText = title
        a.informativeText = message
        a.alertStyle = .critical
        a.addButton(withTitle: "好")
        a.runModal()
        NSApp.terminate(nil)
    }

    // MARK: - WKNavigationDelegate

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        showLoading("控制台还没准备好，正在重试…")
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
            guard let self else { return }
            if self.ping() { self.loadConsole() }
        }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
