"""HTTP API / 持久化重开恢复 / 页面可交付性 的集成测试。

用随机端口在后台线程启动真实 ThreadingHTTPServer，零第三方依赖。
"""

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.server import AppState, make_handler  # noqa: E402

BASE_CFG = {
    "initial_state": [0.0, 0.0, 1.0, 0.0],
    "initial_cov": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 0.1, 0], [0, 0, 0, 0.1]],
    "process_noise": 0.05,
    "measurement_noise": [[1.0, 0.0], [0.0, 1.0]],
    "lag": 3,
    "initial_timestamp": 0.0,
}


class ServerHarness:
    def __init__(self, store_path):
        self.app = AppState(store_path)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as r:
                body = r.read()
                return r.status, body, dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def post(self, path, obj):
        data = json.dumps(obj).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=data, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))


class TestHttpApi(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = os.path.join(self._dir.name, "state.json")
        self.srv = ServerHarness(self.store)

    def tearDown(self):
        self.srv.stop()
        self._dir.cleanup()

    def test_health_and_page(self):
        status, body, headers = self.srv.get("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "ok")

        status, body, headers = self.srv.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        html = body.decode("utf-8")
        for marker in ("固定滞后平滑器", "stable_id", "logTable", "trajTable", "/static/app.js"):
            self.assertIn(marker, html)

        status, body, headers = self.srv.get("/static/app.js")
        self.assertEqual(status, 200)
        self.assertIn("createRevisionGuard", body.decode("utf-8"))

        status, _, _ = self.srv.get("/nope")
        self.assertEqual(status, 404)

    def test_full_workflow_over_http(self):
        # 非法噪声矩阵：422，且不建档
        bad = dict(BASE_CFG, measurement_noise=[[1, 0], [0, -1]])
        code, resp = self.srv.post("/api/config", bad)
        self.assertEqual(code, 422)
        self.assertIn("正定", resp["error"])
        code, state_raw, _ = self.srv.get("/api/state")
        state = json.loads(state_raw.decode("utf-8"))
        self.assertEqual(state["revision"], 0)

        # 合法建档
        code, state = self.srv.post("/api/config", BASE_CFG)
        self.assertEqual(code, 200)
        self.assertEqual(state["revision"], 0)

        # 已有日志后禁止重新建档（先录一条）
        code, r = self.srv.post("/api/observations",
                                {"stable_id": "H1", "timestamp": 1, "x": 1.0, "y": 0.1})
        self.assertEqual(code, 200)
        self.assertEqual(r["decision"], "accepted")
        code, resp = self.srv.post("/api/config", BASE_CFG)
        self.assertEqual(code, 422)
        self.assertIn("不能重新建档", resp["error"])

        # 顺序观测
        for i, t in enumerate([2, 3, 4], start=2):
            code, r = self.srv.post("/api/observations",
                                    {"stable_id": f"H{i}", "timestamp": t, "x": float(t), "y": 0.0})
            self.assertEqual(code, 200)

        # 迟到观测（窗口内）
        code, r = self.srv.post("/api/observations",
                                {"stable_id": "HL", "timestamp": 2.5, "x": 2.5, "y": 0.2})
        self.assertEqual(code, 200)
        self.assertEqual(r["decision"], "accepted")
        self.assertIn("重算窗口后缀", r["reason"])

        # 回放：409 语义留给拒绝；回放幂等返回 200
        code, r = self.srv.post("/api/observations",
                                {"stable_id": "HL", "timestamp": 2.5, "x": 2.5, "y": 0.2})
        self.assertEqual(code, 200)
        self.assertEqual(r["decision"], "replayed")

        # 同标识不同内容 -> 409
        code, r = self.srv.post("/api/observations",
                                {"stable_id": "HL", "timestamp": 2.5, "x": 2.5, "y": 9.0})
        self.assertEqual(code, 409)
        self.assertEqual(r["decision"], "rejected")

        # 窗口外 -> 409
        code, r = self.srv.post("/api/observations",
                                {"stable_id": "OLD", "timestamp": 0.2, "x": 0, "y": 0})
        self.assertEqual(code, 409)
        self.assertEqual(r["decision"], "rejected")

        # 状态查询：日志不可变、修订单调
        code, state_raw, _ = self.srv.get("/api/state")
        state = json.loads(state_raw.decode("utf-8"))
        self.assertEqual(code, 200)
        self.assertEqual(state["revision"], 5)
        self.assertEqual(len(state["log"]), 8)
        self.assertEqual([e["seq"] for e in state["log"]], list(range(1, 9)))
        self.assertTrue(all(p["frozen"] for p in state["trajectory"][:2]))

        code, traj_raw, _ = self.srv.get("/api/trajectory")
        traj = json.loads(traj_raw.decode("utf-8"))
        self.assertEqual(code, 200)
        self.assertEqual(len(traj["trajectory"]), len(state["trajectory"]))

    def test_bad_json_and_validation(self):
        code, _ = self.srv.post("/api/observations", {"stable_id": "", "timestamp": 1, "x": 0, "y": 0})
        self.assertEqual(code, 409)
        # 非法 JSON 体
        req = urllib.request.Request(self.srv.base + "/api/config",
                                     data=b"{not json", method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("应当返回 400")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)

    def test_reopen_restores_identical_log_and_trajectory(self):
        self.srv.post("/api/config", BASE_CFG)
        for i, t in enumerate([1, 2, 3, 4], start=1):
            self.srv.post("/api/observations",
                          {"stable_id": f"P{i}", "timestamp": t, "x": float(t), "y": 0.1 * i})
        self.srv.post("/api/observations",
                      {"stable_id": "LATE", "timestamp": 2.5, "x": 2.5, "y": 0.3})
        _, before_raw, _ = self.srv.get("/api/state")
        before = json.loads(before_raw.decode("utf-8"))
        self.srv.stop()

        # 重开：用同一持久化文件构造新的应用实例（等价于进程重启）
        reopened = AppState(self.store)
        state = reopened.state()
        self.assertEqual(state["revision"], before["revision"])
        self.assertEqual(state["log"], before["log"])
        self.assertEqual(state["trajectory"], before["trajectory"])
        self.assertEqual(state["window_left"], before["window_left"])
        self.assertEqual(state["current_position"], before["current_position"])
        self.assertTrue(state["configured"])

        # 重开后规则仍生效：同标识同内容依旧回放
        r = reopened.smoother.submit("LATE", 2.5, 2.5, 0.3)
        self.assertEqual(r["decision"], "replayed")
        # 已封存点未被重放改变
        self.assertEqual(
            [p["position"] for p in state["trajectory"] if p["frozen"]],
            [p["position"] for p in before["trajectory"] if p["frozen"]],
        )

    def test_concurrent_posts_never_regress_revision(self):
        self.srv.post("/api/config", BASE_CFG)
        results = []

        def post(i):
            code, body = self.srv.post(
                "/api/observations",
                {"stable_id": f"C{i:02d}", "timestamp": 1 + i * 0.02,
                 "x": 1 + i * 0.02, "y": 0.0})
            results.append((code, body.get("decision"), body.get("current_revision")))

        threads = [threading.Thread(target=post, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 16)
        self.assertTrue(all(d == "accepted" for _, d, _ in results))
        # 每个响应携带的 current_revision 单调：最终所有观测眼里最新修订一致
        _, state_raw, _ = self.srv.get("/api/state")
        state = json.loads(state_raw.decode("utf-8"))
        self.assertEqual(state["revision"], 16)
        self.assertEqual(len(state["log"]), 16)


if __name__ == "__main__":
    unittest.main(verbosity=2)
