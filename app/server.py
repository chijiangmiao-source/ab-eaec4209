"""导航审查 · 固定滞后平滑器 —— HTTP 服务（仅依赖 Python 标准库）。

路由：
  GET  /                       页面
  GET  /healthz                健康响应
  GET  /api/state              当前日志 / 轨迹 / 修订号 / 配置
  POST /api/config             建立初值、过程/观测噪声、滞后长度（仅在空日志时）
  POST /api/observations       按接收顺序录入一条观测 {stable_id,timestamp,x,y}
  GET  /api/trajectory         已发布轨迹
  POST /api/reset              清空（用于测试 / 重新建档）
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import ConfigError, FixedLagSmoother, JsonStore  # noqa: E402

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
DEFAULT_CONFIG = {
    "initial_state": [0.0, 0.0, 1.0, 0.0],
    "initial_cov": [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 0.1, 0.0],
        [0.0, 0.0, 0.0, 0.1],
    ],
    "process_noise": 0.05,
    "measurement_noise": [[1.0, 0.0], [0.0, 1.0]],
    "lag": 5,
    "initial_timestamp": 0.0,
}


class AppState:
    """持有平滑器与持久化存储；所有修改串行化，重开后自动恢复。"""

    def __init__(self, store_path: str):
        self.store = JsonStore(store_path)
        self.lock = threading.RLock()
        self.smoother: Optional[FixedLagSmoother] = None
        self.config: Dict[str, Any] = dict(DEFAULT_CONFIG)
        self.configured: bool = False
        data = self.store.load()
        if data:
            try:
                self.smoother = FixedLagSmoother.restore(data)
                self.config = self.smoother.config
                self.configured = True
            except Exception as exc:  # 持久化文件损坏：保留文件，从默认配置起步
                self._load_error = f"持久化数据无法恢复：{exc}"
                self.smoother = None
        if self.smoother is None:
            self._load_error = getattr(self, "_load_error", "")

    def _persist(self) -> None:
        if self.smoother is not None:
            self.store.save(self.smoother.snapshot())

    def configure(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            # 配置校验交给引擎；非法噪声矩阵会抛 ConfigError，不改变现状
            new_smoother = FixedLagSmoother(cfg)
            # 已有非空日志时禁止重建（防止改写已封存轨迹）；空档可重复配置
            if self.smoother is not None and len(self.smoother.log) > 0:
                raise ConfigError("已存在观测日志，不能重新建档；如需新建请先重置")
            self.smoother = new_smoother
            self.config = new_smoother.config
            self.configured = True
            self._persist()
            return self.smoother.state_dict()

    def submit(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            if self.smoother is None:
                # 未显式建档时用默认配置自动建档，页面首录即可用
                self.smoother = FixedLagSmoother(self.config)
                self.configured = True
            result = self.smoother.submit(
                payload.get("stable_id"),
                payload.get("timestamp"),
                payload.get("x"),
                payload.get("y"),
            )
            self._persist()
            return result

    def reset(self) -> Dict[str, Any]:
        with self.lock:
            if os.path.exists(self.store.path):
                os.remove(self.store.path)
            self.smoother = None
            self.config = dict(DEFAULT_CONFIG)
            self.configured = False
            return {"ok": True}

    def state(self) -> Dict[str, Any]:
        with self.lock:
            if self.smoother is None:
                return {
                    "configured": False,
                    "config": self.config,
                    "revision": 0,
                    "log": [],
                    "trajectory": [],
                    "load_error": getattr(self, "_load_error", ""),
                }
            out = self.smoother.state_dict()
            out["configured"] = True
            out["load_error"] = getattr(self, "_load_error", "")
            return out


def make_handler(app: AppState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "NavReview/1.0"

        def log_message(self, fmt, *args):  # 收敛测试期噪音
            sys.stderr.write("[http] " + fmt % args + "\n")

        def _json(self, code: int, obj: Any):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> Optional[Dict[str, Any]]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._json(400, {"error": "请求体不是合法 JSON"})
                return None
            if not isinstance(data, dict):
                self._json(400, {"error": "请求体必须是 JSON 对象"})
                return None
            return data

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/healthz":
                self._json(200, {"status": "ok", "service": "nav-review"})
            elif path == "/":
                self._serve_file("index.html", "text/html; charset=utf-8")
            elif path == "/api/state":
                self._json(200, app.state())
            elif path == "/api/trajectory":
                self._json(200, {"trajectory": app.state().get("trajectory", [])})
            elif path.startswith("/static/"):
                name = path.split("/static/", 1)[1]
                self._serve_file(name, self._guess_mime(name))
            else:
                self._json(404, {"error": "not found", "path": path})

        def do_POST(self):
            path = urlparse(self.path).path
            if path == "/api/config":
                payload = self._read_json()
                if payload is None:
                    return
                try:
                    self._json(200, app.configure(payload))
                except ConfigError as exc:
                    self._json(422, {"error": str(exc), "state": app.state()})
                except Exception as exc:  # noqa: BLE001
                    self._json(500, {"error": f"建档失败：{exc}"})
            elif path == "/api/observations":
                payload = self._read_json()
                if payload is None:
                    return
                try:
                    result = app.submit(payload)
                    self._json(200 if result["decision"] != "rejected" else 409, result)
                except Exception as exc:  # noqa: BLE001
                    self._json(500, {"error": f"录入失败：{exc}", "state": app.state()})
            elif path == "/api/reset":
                self._read_json()
                self._json(200, app.reset())
            else:
                self._json(404, {"error": "not found", "path": path})

        def _serve_file(self, name: str, mime: str):
            # 防目录穿越
            safe = os.path.normpath(os.path.join(STATIC_DIR, name))
            if not safe.startswith(STATIC_DIR + os.sep) or not os.path.isfile(safe):
                self._json(404, {"error": "not found"})
                return
            with open(safe, "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        @staticmethod
        def _guess_mime(name: str) -> str:
            if name.endswith(".js"):
                return "application/javascript; charset=utf-8"
            if name.endswith(".css"):
                return "text/css; charset=utf-8"
            return "application/octet-stream"

    return Handler


def serve(host: str = "0.0.0.0", port: int = 8080, store_path: str = "") -> ThreadingHTTPServer:
    store_path = store_path or os.environ.get(
        "NAV_STORE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "state.json")
    )
    os.makedirs(os.path.dirname(store_path), exist_ok=True)
    app = AppState(store_path)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.app = app  # type: ignore[attr-defined]
    return httpd


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="导航审查固定滞后平滑器")
    parser.add_argument("--host", default=os.environ.get("NAV_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("NAV_PORT", "8080")))
    parser.add_argument("--store", default=os.environ.get("NAV_STORE", ""))
    args = parser.parse_args(argv)
    httpd = serve(args.host, args.port, args.store)
    print(f"nav-review listening on http://{args.host}:{args.port} (store={httpd.app.store.path})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
