"""claudenet 的单元测试（全部使用标准库，不访问外网）。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import os
import tempfile
import unittest

import urllib.request
from pathlib import Path
from unittest import mock

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from claudenet import checks, clash, core, geo, store, subscription
from claudenet.checks import Check, _verdict
from claudenet.server import create_server
from claudenet.timezone import compare_timezones, offset_label, tz_clock, valid_timezone


class TimezoneTests(unittest.TestCase):
    def test_known_drift(self) -> None:
        cmp = compare_timezones("Asia/Singapore", "America/Los_Angeles")
        self.assertTrue(cmp["comparable"])
        self.assertFalse(cmp["same_name"])
        self.assertFalse(cmp["same_offset"])
        self.assertEqual(cmp["diff_hours"], -16.0)
        self.assertEqual(cmp["system_offset_label"], "UTC+08:00")
        self.assertEqual(cmp["exit_offset_label"], "UTC-08:00")

    def test_same_offset_different_name(self) -> None:
        cmp = compare_timezones("Asia/Singapore", "Asia/Shanghai")
        self.assertTrue(cmp["comparable"])
        self.assertTrue(cmp["same_offset"])
        self.assertFalse(cmp["same_name"])

    def test_same_name(self) -> None:
        cmp = compare_timezones("Asia/Tokyo", "Asia/Tokyo")
        self.assertTrue(cmp["same_name"])
        self.assertEqual(cmp["diff_hours"], 0.0)

    def test_unknown_timezone(self) -> None:
        cmp = compare_timezones("Nope/Nowhere", "Asia/Tokyo")
        self.assertFalse(cmp["comparable"])
        self.assertIsNone(cmp["diff_hours"])

    def test_valid_timezone(self) -> None:
        self.assertTrue(valid_timezone("America/Los_Angeles"))
        self.assertTrue(valid_timezone("UTC"))
        self.assertFalse(valid_timezone("Mars/Olympus"))
        self.assertFalse(valid_timezone(""))
        self.assertFalse(valid_timezone("../../etc/passwd"))

    def test_offset_label(self) -> None:
        self.assertEqual(offset_label(28800), "UTC+08:00")
        self.assertEqual(offset_label(-28800), "UTC-08:00")
        self.assertEqual(offset_label(-12600), "UTC-03:30")
        self.assertIsNone(offset_label(None))

    def test_tz_clock(self) -> None:
        self.assertRegex(tz_clock("Asia/Tokyo"), r"^\d{2}:\d{2}$")
        self.assertEqual(tz_clock("bad/zone"), "--:--")


class GeoTests(unittest.TestCase):
    def test_flag(self) -> None:
        self.assertEqual(geo.flag_emoji("US"), "🇺🇸")
        self.assertEqual(geo.flag_emoji("jp"), "🇯🇵")
        self.assertEqual(geo.flag_emoji(""), "🌐")
        self.assertEqual(geo.flag_emoji("USA"), "🌐")

    def test_country_name(self) -> None:
        self.assertEqual(geo.country_name("US"), "美国")
        self.assertEqual(geo.country_name("us", "United States"), "美国")
        self.assertEqual(geo.country_name("ZZ", "Atlantis"), "Atlantis")
        self.assertEqual(geo.country_name(""), "未知")

    def test_blocked_regions(self) -> None:
        for code in ("CN", "HK", "RU", "IR"):
            self.assertIn(code, geo.BLOCKED_REGIONS)
        self.assertNotIn("US", geo.BLOCKED_REGIONS)


def make_check(cid: str, status: str, critical: bool = False) -> Check:
    return Check(
        id=cid,
        group="测试",
        title=cid,
        status=status,
        summary="",
        critical=critical,
    )


class VerdictTests(unittest.TestCase):
    def test_all_pass(self) -> None:
        result = _verdict([make_check("a", "pass"), make_check("b", "pass")])
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(result["score"], 100)

    def test_critical_fail(self) -> None:
        result = _verdict([make_check("a", "pass"), make_check("api", "fail", critical=True)])
        self.assertEqual(result["verdict"], "fail")
        self.assertLess(result["score"], 80)

    def test_noncritical_fail(self) -> None:
        result = _verdict([make_check("a", "fail")])
        self.assertEqual(result["verdict"], "warn")
        self.assertEqual(result["score"], 86)

    def test_warns_reduce_score(self) -> None:
        result = _verdict([make_check("a", "warn"), make_check("b", "warn")])
        self.assertEqual(result["verdict"], "warn")
        self.assertEqual(result["score"], 84)

    def test_score_floor(self) -> None:
        many = [make_check(f"x{i}", "fail", critical=True) for i in range(8)]
        result = _verdict(many)
        self.assertEqual(result["score"], 0)


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HOME": self.tmp.name, "CLASHLINK_DATA_DIR": self.tmp.name})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_defaults(self) -> None:
        settings = store.load_settings()
        self.assertFalse(settings["auto_follow"])
        self.assertEqual(settings["watch_interval"], 120)

    def test_roundtrip(self) -> None:
        saved = store.save_settings({"auto_follow": True, "watch_interval": 9999})
        self.assertEqual(saved["watch_interval"], 3600)
        self.assertTrue(saved["auto_follow"])
        again = store.load_settings()
        self.assertTrue(again["auto_follow"])
        self.assertEqual(again["watch_interval"], 3600)

    def test_interval_clamped(self) -> None:
        self.assertEqual(store.save_settings({"watch_interval": 1})["watch_interval"], 30)

    def test_report_roundtrip(self) -> None:
        report = {"score": 88, "verdict": "warn", "generated": "2026-01-01T00:00:00+08:00"}
        store.save_report(report)
        self.assertEqual(store.load_report()["score"], 88)


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.env = mock.patch.dict(os.environ, {"HOME": cls.tmp.name, "CLASHLINK_DATA_DIR": cls.tmp.name})
        cls.env.start()
        cls.server = create_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.env.stop()
        cls.tmp.cleanup()

    def get(self, path: str, headers: dict | None = None) -> tuple[int, bytes]:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers or {})
        try:
            with opener.open(req, timeout=3) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_ping(self) -> None:
        status, body = self.get("/api/ping")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["app"], "clashlink")

    def test_state(self) -> None:
        status, body = self.get("/api/state")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIn("settings", payload)
        self.assertIn("system_tz", payload)

    def test_static_index(self) -> None:
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Claude", body)

    def test_static_assets(self) -> None:
        for path in ("/styles.css", "/app.js"):
            status, body = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertGreater(len(body), 100)

    def test_traversal_blocked(self) -> None:
        status, _ = self.get("/../claudenet/server.py")
        self.assertIn(status, (403, 404))

    def test_host_header_required(self) -> None:
        status, _ = self.get("/api/ping", headers={"Host": "evil.example.com"})
        self.assertEqual(status, 403)

    def test_settings_update(self) -> None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        body = json.dumps({"auto_follow": True, "watch_interval": 300}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/settings",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with opener.open(req, timeout=3) as resp:
            saved = json.loads(resp.read())["settings"]
        self.assertTrue(saved["auto_follow"])
        self.assertEqual(saved["watch_interval"], 300)

    def test_invalid_timezone_rejected(self) -> None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        body = json.dumps({"tz": "Mars/Olympus"}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/timezone",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            opener.open(req, timeout=3)
            self.fail("应当返回 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)


class RegistryTests(unittest.TestCase):
    def test_specs_unique(self) -> None:
        ids = [spec.id for spec in checks.SPECS]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), len(checks.SPECS))
        self.assertGreaterEqual(len(ids), 13)

    def test_groups_cover_specs(self) -> None:
        used = {spec.group for spec in checks.SPECS}
        self.assertTrue(used.issubset(set(checks.GROUP_ORDER)))

    def test_every_spec_has_check_fn(self) -> None:
        for spec in checks.SPECS:
            self.assertIn(spec.id, checks.CHECK_FUNCS)


PROXIES = {
    "PROXY": {"type": "Selector", "now": "节点A", "all": ["节点A", "节点B", "DIRECT"]},
    "节点A": {"type": "Shadowsocks", "history": [{"delay": 88}]},
    "节点B": {"type": "Trojan", "history": []},
    "DIRECT": {"type": "Direct", "history": []},
    "COMPATIBLE": {"type": "Compatible", "history": []},
    "GLOBAL": {"type": "Selector", "now": "DIRECT", "all": ["DIRECT", "节点A", "PROXY"]},
}


class _FakeController(BaseHTTPRequestHandler):
    state: dict = {}

    def log_message(self, *args) -> None:  # noqa: D102
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self) -> bool:
        return self.headers.get("Authorization") == self.state["auth"]

    def do_GET(self) -> None:  # noqa: N802
        if not self._auth_ok():
            return self._send(401, {"message": "unauthorized"})
        if self.path == "/version":
            return self._send(200, {"version": "v1.19.33"})
        if self.path == "/proxies":
            return self._send(200, {"proxies": self.state["proxies"]})
        if "/delay" in self.path:
            if self.state.get("fail_delay"):
                return self._send(503, {"message": "An error occurred in the delay test"})
            return self._send(200, {"delay": self.state["delay"]})
        return self._send(404, {"message": "not found"})

    def do_PUT(self) -> None:  # noqa: N802
        if not self._auth_ok():
            return self._send(401, {"message": "unauthorized"})
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        self.state["proxies"]["PROXY"]["now"] = payload.get("name")
        self.send_response(204)
        self.end_headers()


class ClashTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeController)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address[:2]
        cls.host = f"{host}:{port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        _FakeController.state = {
            "proxies": json.loads(json.dumps(PROXIES)),
            "auth": "Bearer test-secret",
            "delay": 120,
            "fail_delay": False,
        }
        self.client = clash.ClashClient(clash.ClashEndpoint("http", self.host, "test-secret"))

    def test_version(self) -> None:
        self.assertEqual(self.client.version()["version"], "v1.19.33")

    def test_groups_parse(self) -> None:
        groups = {g.name: g for g in self.client.groups()}
        self.assertIn("PROXY", groups)
        proxy = groups["PROXY"]
        names = [n.name for n in proxy.nodes]
        self.assertEqual(names, ["节点A", "节点B", "DIRECT"])
        self.assertEqual(proxy.now, "节点A")
        self.assertEqual(proxy.now_delay, 88)

    def test_groups_sorted_proxy_first(self) -> None:
        self.assertEqual(self.client.groups()[0].name, "PROXY")

    def test_select_updates_now(self) -> None:
        self.client.select("PROXY", "节点B")
        proxy = next(g for g in self.client.groups() if g.name == "PROXY")
        self.assertEqual(proxy.now, "节点B")

    def test_select_rejects_bad_auth(self) -> None:
        bad = clash.ClashClient(clash.ClashEndpoint("http", self.host, "wrong"))
        with self.assertRaises(clash.ClashError):
            bad.select("PROXY", "节点B")

    def test_delay_ok(self) -> None:
        self.assertEqual(self.client.delay("节点A"), 120)

    def test_delay_failure_returns_none(self) -> None:
        _FakeController.state["fail_delay"] = True
        self.assertIsNone(self.client.delay("节点A"))

    def test_test_group(self) -> None:
        proxy = next(g for g in self.client.groups() if g.name == "PROXY")
        delays = self.client.test_group(proxy)
        self.assertEqual(set(delays), {"节点A", "节点B"})

    def test_snapshot_via_env(self) -> None:
        env = {"CLAUDENET_CLASH_API": f"http://{self.host}", "CLAUDENET_CLASH_SECRET": "test-secret"}
        with mock.patch.dict(os.environ, env, clear=False):
            snap = clash.snapshot()
        self.assertTrue(snap["available"])
        self.assertEqual(snap["version"], "v1.19.33")
        self.assertEqual(snap["groups"][0]["name"], "PROXY")

    def test_snapshot_unreachable(self) -> None:
        env = {"CLAUDENET_CLASH_API": "http://127.0.0.1:1"}
        with mock.patch.dict(os.environ, env, clear=False):
            snap = clash.snapshot()
        self.assertFalse(snap["available"])
        self.assertEqual(snap["groups"], [])

    def test_read_scalar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text('mixed-port: 7897\nexternal-controller: 127.0.0.1:9097\nsecret: "abc"\n', encoding="utf-8")
            self.assertEqual(clash._read_scalar(path, "external-controller"), "127.0.0.1:9097")
            self.assertEqual(clash._read_scalar(path, "secret"), "abc")
            self.assertEqual(clash._read_scalar(path, "missing"), "")


class SubscriptionTests(unittest.TestCase):
    CLASH_YAML = """proxies:
- name: "US 1"
  type: trojan
  server: a.example.com
  port: 443
  password: pw
- name: "US 2"
  type: ss
  server: b.example.com
  port: 8388
  cipher: aes-128-gcm
  password: pw2
proxy-groups:
  - name: PROXY
    type: select
    proxies: ["US 1", "US 2"]
rules:
  - MATCH,PROXY
"""

    def test_normalize_clash(self) -> None:
        data = subscription.normalize(self.CLASH_YAML)
        self.assertEqual(data["kind"], "clash")
        self.assertEqual(data["nodes"], 2)

    def test_count_ignores_nested_lists(self) -> None:
        yaml = """proxies:
- name: a
  type: trojan
  alpn:
    - h2
    - http/1.1
- name: b
  type: ss
proxy-groups: []
"""
        self.assertEqual(subscription.count_proxies(yaml), 2)

    def test_links_from_base64(self) -> None:
        import base64

        links = "\n".join([
            "ss://YWVzLTEyOC1nY206cHc@1.2.3.4:8388#SS%20One",
            "trojan://pw@5.6.7.8:443?sni=x.com#Trojan",
            "vmess://" + base64.b64encode(json.dumps({
                "add": "9.9.9.9", "port": 443, "id": "uuid-1", "aid": 0,
                "net": "ws", "host": "h.com", "path": "/v", "tls": "tls", "ps": "VMess",
            }).encode()).decode(),
        ])
        payload = base64.b64encode(links.encode()).decode()
        data = subscription.normalize(payload)
        self.assertEqual(data["kind"], "links")
        self.assertEqual(data["nodes"], 3)
        self.assertIn("proxy-groups:", data["yaml"])

    def test_ss_parse(self) -> None:
        proxy = subscription._parse_ss("ss://YWVzLTEyOC1nY206cHc@1.2.3.4:8388#SS%20One")
        self.assertEqual(proxy["server"], "1.2.3.4")
        self.assertEqual(proxy["port"], 8388)
        self.assertEqual(proxy["name"], "SS One")

    def test_unknown_format(self) -> None:
        with self.assertRaises(RuntimeError):
            subscription.normalize("just some text")

    def test_public_masks_token(self) -> None:
        item = {"id": "x", "name": "n", "url": "https://fast.example.com:2096/secret-token/la-main",
                "kind": "clash", "nodes": 3, "added_at": 1, "updated_at": 2}
        masked = subscription.public(item)["url"]
        self.assertNotIn("secret-token", masked)
        self.assertIn("fast.example.com", masked)


class CoreConfigTests(unittest.TestCase):
    RAW = """mixed-port: 7890
allow-lan: true
tun:
  enable: true
  stack: gvisor
dns:
  enable: true
  listen: 0.0.0.0:53
proxies:
- name: a
  type: trojan
  server: a.example.com
  port: 443
  password: pw
proxy-groups:
  - name: PROXY
    type: select
    proxies: ["a", DIRECT]
rules:
  - MATCH,PROXY
"""

    def test_strips_local_only_blocks(self) -> None:
        cfg = core.build_config(self.RAW, 7899, 9096, "s3cret")
        self.assertNotIn("tun:", cfg)
        self.assertNotIn("0.0.0.0:53", cfg)
        self.assertNotIn("mixed-port: 7890", cfg)
        self.assertIn("mixed-port: 7899", cfg)
        self.assertIn("external-controller: 127.0.0.1:9096", cfg)
        self.assertIn("secret: s3cret", cfg)
        self.assertIn("MATCH,PROXY", cfg)

    def test_injects_group_when_missing(self) -> None:
        raw = "proxies:\n- name: only\n  type: trojan\n  server: x\n  port: 443\n  password: p\n"
        cfg = core.build_config(raw, 7899, 9096, "s")
        self.assertIn("proxy-groups:", cfg)
        self.assertIn("only", cfg)
        self.assertIn("rules:", cfg)

    def test_proxy_names(self) -> None:
        self.assertEqual(core._proxy_names(self.RAW), ["a"])


if __name__ == "__main__":
    unittest.main()


class SessionTests(unittest.TestCase):
    def test_parse_userinfo(self):
        from claudenet import subscription
        info = subscription.parse_userinfo("upload=10; download=20; total=100; expire=1700000000; foo=1")
        self.assertEqual(info["upload"], 10)
        self.assertEqual(info["total"], 100)
        self.assertEqual(info["expire"], 1700000000)
        self.assertNotIn("foo", info)
        self.assertEqual(subscription.parse_userinfo(""), {})

    def test_restore_script(self):
        from claudenet import core
        state = {
            "service": "Wi-Fi",
            "web": {"enabled": True, "server": "127.0.0.1", "port": 7897},
            "secure": {"enabled": False, "server": "", "port": 0},
            "socks": {"enabled": False, "server": "", "port": 0},
            "bypass": ["127.0.0.1"],
        }
        script = core._restore_script(state)
        self.assertIn('-setwebproxy "Wi-Fi" 127.0.0.1 7897', script)
        self.assertIn('-setsecurewebproxystate "Wi-Fi" off', script)
        self.assertIn("-setproxybypassdomains", script)

    def test_tun_config(self):
        from claudenet import core
        raw = "proxies:\n  - name: a\n    type: ss\n    server: 1.2.3.4\n    port: 1\n    cipher: aes-128-gcm\n    password: x\n"
        text = core.build_config(raw, 7899, 9096, "s", tun=True)
        self.assertIn("tun:", text)
        self.assertIn("enable: true", text)
        self.assertIn("dns-hijack:", text)
        plain = core.build_config(raw, 7899, 9096, "s")
        self.assertNotIn("tun:", plain)

    def test_event_log(self):
        from claudenet import store
        store.clear_events()
        store.append_event("proxy", "接管系统代理")
        events = store.load_events()
        self.assertTrue(events and events[-1]["text"] == "接管系统代理")
        store.clear_events()
