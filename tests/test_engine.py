"""固定滞后平滑器核心规则的代码测试。

覆盖：迟到观测窗口内重放、同标识回放/拒绝、窗口外与非递增顺序拒绝、
不可变日志、单调修订号、非法噪声、创新矩阵不可逆保护、32 条上限、
重开恢复以及并发录入一致性。
"""

import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import engine  # noqa: E402
from app.engine import (  # noqa: E402
    ConfigError, FixedLagSmoother, InnovationError, JsonStore, MAX_OBSERVATIONS,
)

BASE_CFG = {
    "initial_state": [0.0, 0.0, 1.0, 0.0],
    "initial_cov": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 0.1, 0], [0, 0, 0, 0.1]],
    "process_noise": 0.05,
    "measurement_noise": [[1.0, 0.0], [0.0, 1.0]],
    "lag": 3,
    "initial_timestamp": 0.0,
}


def make(cfg=None):
    return FixedLagSmoother(cfg or BASE_CFG)


def frozen_snapshot(s):
    return [(p["timestamp"], [round(v, 9) for v in p["position"]])
            for p in s.trajectory if p["frozen"]]


class TestOrderingAndReplay(unittest.TestCase):
    def test_ordered_acceptance_and_monotonic_revision(self):
        s = make()
        for i, t in enumerate([1, 2, 3, 4], start=1):
            r = s.submit(f"P{i}", t, float(t), 0.1 * i)
            self.assertEqual(r["decision"], "accepted")
            self.assertEqual(r["revision"], i)
            self.assertEqual(r["current_revision"], i)
        # 拒绝/回放都不得推进修订号
        rev_before = s.revision
        r = s.submit("P1", 1.0, 1.0, 0.1)  # 回放
        self.assertEqual(r["decision"], "replayed")
        self.assertEqual(s.revision, rev_before)
        r = s.submit("ZZZ", -5, 0, 0)  # 窗口外
        self.assertEqual(r["decision"], "rejected")
        self.assertEqual(s.revision, rev_before)

    def test_late_observation_recomputes_only_window_suffix(self):
        s = make()
        for i, t in enumerate([1, 2, 3, 4], start=1):
            s.submit(f"A{i}", t, float(t), 0.0)
        # t=4 后：t=1 已封存（cutoff = 4-3 = 1），窗口为 (1,4]
        self.assertEqual(s.checkpoint.timestamp, 1.0)
        self.assertTrue(s.checkpoint.sealed)
        self.assertEqual([o.timestamp for o in s.window], [2.0, 3.0, 4.0])

        frozen_before = frozen_snapshot(s)
        win_before = {p["timestamp"]: p["position"] for p in s.trajectory if not p["frozen"]}

        r = s.submit("LATE", 2.5, 2.5, 0.5)
        self.assertEqual(r["decision"], "accepted")
        self.assertEqual(r["revision"], 5)
        self.assertIn("重算窗口后缀", r["reason"])

        # 已封存位置逐位不变
        self.assertEqual(frozen_snapshot(s), frozen_before)
        # 窗口后缀包含新点且按时序插入；后续点位置确被重算
        win_ts = [p["timestamp"] for p in s.trajectory if not p["frozen"]]
        self.assertEqual(win_ts, [2.0, 2.5, 3.0, 4.0])
        win_after = {p["timestamp"]: p["position"] for p in s.trajectory if not p["frozen"]}
        # 迟到点之前的窗口点（t=2）确定性重放，值不变；之后的历史后缀（t=3,4）被重算
        self.assertEqual(win_after[2.0], win_before[2.0], "迟到点之前的窗口后缀不应改变")
        for t in (3.0, 4.0):
            self.assertNotEqual(win_after[t], win_before[t], f"t={t} 的历史后缀应被重算")
        # 残差与协方差对角齐备且有限
        self.assertEqual(len(r["residual"]), 2)
        self.assertEqual(len(r["cov_diag"]), 4)
        self.assertTrue(all(abs(x) < 1e9 for x in r["cov_diag"]))

    def test_idempotent_replay_returns_original_conclusion(self):
        s = make()
        r1 = s.submit("FIX-1", 2, 2.0, 0.3)
        r2 = s.submit("FIX-1", 2, 2.0, 0.3)
        self.assertEqual(r2["decision"], "replayed")
        self.assertEqual(r2["revision"], 0)  # 回放不产生新修订
        self.assertEqual(r2["position"], r1["position"])
        self.assertEqual(r2["cov_diag"], r1["cov_diag"])
        self.assertEqual(r2["residual"], r1["residual"])
        self.assertIn(str(r1["seq"]), r2["reason"])
        # 轨迹不因回放改变
        traj = json.loads(json.dumps(s.trajectory))
        s.submit("FIX-1", 2, 2.0, 0.3)
        self.assertEqual(s.trajectory, traj)

    def test_same_id_different_content_rejected(self):
        s = make()
        s.submit("FIX-1", 2, 2.0, 0.3)
        rev = s.revision
        for payload in [(2, 2.0, 0.31), (2.1, 2.0, 0.3), (2, 2.01, 0.3)]:
            r = s.submit("FIX-1", *payload)
            self.assertEqual(r["decision"], "rejected")
            self.assertIn("内容不同", r["reason"])
            self.assertEqual(s.revision, rev)

    def test_out_of_window_observation_rejected(self):
        s = make()
        for i, t in enumerate([1, 2, 3, 4], start=1):
            s.submit(f"B{i}", t, float(t), 0.0)
        before = json.loads(json.dumps(s.trajectory))
        rev = s.revision
        r = s.submit("OLD", 0.5, 9, 9)
        self.assertEqual(r["decision"], "rejected")
        self.assertIn("窗口外", r["reason"])
        self.assertEqual(s.trajectory, before)  # 已发布轨迹不变
        self.assertEqual(s.revision, rev)

    def test_nonincreasing_at_sealed_slot_rejected(self):
        s = make()
        for i, t in enumerate([1, 2, 3, 4], start=1):
            s.submit(f"C{i}", t, float(t), 0.0)
        before = json.loads(json.dumps(s.trajectory))
        # 与已封存点 t=1 同刻：不得并入（同刻非递增顺序）
        r = s.submit("DUP1", 1, 5, 5)
        self.assertEqual(r["decision"], "rejected")
        self.assertIn("同刻非递增", r["reason"])
        self.assertEqual(s.trajectory, before)

    def test_immutable_log_only_appends(self):
        s = make()
        s.submit("K1", 1, 1, 0)
        first = json.loads(json.dumps(s.log[0].to_dict()))
        s.submit("K2", 2, 2, 0)
        s.submit("LATE", 1.5, 1.5, 0)          # 迟到重算
        s.submit("K1", 1, 1, 0)                # 回放
        s.submit("K2", 2, 9, 9)                # 同标识不同内容
        s.submit("OLD", -1, 0, 0)              # 窗口外
        self.assertEqual(s.log[0].to_dict(), first)  # 首条结论永不改写
        self.assertEqual([e.seq for e in s.log], [1, 2, 3, 4, 5, 6])
        self.assertEqual([e.decision for e in s.log],
                         ["accepted", "accepted", "accepted", "replayed", "rejected", "rejected"])

    def test_capacity_32(self):
        s = make()
        for i in range(MAX_OBSERVATIONS):
            r = s.submit(f"OBS{i:02d}", 1 + i * 0.01, i * 0.01, 0)
            self.assertEqual(r["decision"], "accepted")
        r = s.submit("OVER", 1.32, 0, 0)
        self.assertEqual(r["decision"], "rejected")
        self.assertIn("32", r["reason"])
        self.assertEqual(len(s.log), MAX_OBSERVATIONS)


class TestFailureModes(unittest.TestCase):
    def test_invalid_noise_and_config(self):
        bad_configs = [
            {"measurement_noise": [[1, 0], [0, -1]]},        # 非正定 R
            {"measurement_noise": [[1, 2], [2, 1]]},         # 行列式为负
            {"measurement_noise": [[1, 0.5], [0, 1]]},       # 非对称
            {"initial_cov": [[1, 0, 0, 0], [0, 1, 0, 0],
                              [0, 0, -1, 0], [0, 0, 0, 1]]},  # 负对角
            {"initial_cov": [[1, 0, 0, 0], [0, 1, 0, 0],
                              [0, 0, 0, 0], [0, 0, 0, 1]]},   # 奇异
            {"process_noise": -0.1},
            {"lag": 0},
            {"lag": 2.5},
            {"initial_state": [0, 0, 0]},
        ]
        for override in bad_configs:
            cfg = json.loads(json.dumps(BASE_CFG))
            cfg.update(override)
            with self.assertRaises(ConfigError):
                make(cfg)

    def test_singular_innovation_preserves_last_good_trajectory(self):
        s = make()
        s.submit("G1", 1, 1, 0)
        s.submit("G2", 2, 2, 0)
        before = json.loads(json.dumps(s.trajectory))
        rev = s.revision

        orig = engine.kf_update

        def boom(*a, **k):
            raise InnovationError("矩阵奇异，无法求逆")

        engine.kf_update = boom
        try:
            r = s.submit("BAD", 1.5, 1.5, 0)
        finally:
            engine.kf_update = orig
        self.assertEqual(r["decision"], "rejected")
        self.assertIn("创新", r["reason"])
        self.assertIn("保留最近有效轨迹", r["reason"])
        self.assertEqual(s.trajectory, before)
        self.assertEqual(s.revision, rev)
        self.assertTrue(s.last_error)
        # 恢复后可继续正常录入
        r = s.submit("G3", 1.6, 1.6, 0)
        self.assertEqual(r["decision"], "accepted")

    def test_malformed_observation_rejected(self):
        s = make()
        for args in [("", 1, 0, 0), ("x", "nope", 0, 0), ("x", 1, float("nan"), 0)]:
            r = s.submit(*args)
            self.assertEqual(r["decision"], "rejected")
        self.assertEqual(s.revision, 0)


class TestPersistenceAndConcurrency(unittest.TestCase):
    def test_restore_same_log_and_trajectory(self):
        s = make()
        for i, t in enumerate([1, 2, 3, 4], start=1):
            s.submit(f"R{i}", t, float(t), 0.1 * i)
        s.submit("RLATE", 2.5, 2.5, -0.2)
        s.submit("R1", 1, 1.0, 0.1)   # 回放也在日志里
        expected = json.loads(json.dumps(s.state_dict()))

        with tempfile.TemporaryDirectory() as d:
            store = JsonStore(os.path.join(d, "state.json"))
            store.save(s.snapshot())
            s2 = FixedLagSmoother.restore(store.load())
        got = json.loads(json.dumps(s2.state_dict()))
        self.assertEqual(got["revision"], expected["revision"])
        self.assertEqual(got["log"], expected["log"])
        self.assertEqual(got["trajectory"], expected["trajectory"])
        self.assertEqual(got["window_left"], expected["window_left"])
        # 恢复后继续工作：规则仍生效
        r = s2.submit("RLATE", 2.5, 2.5, -0.2)
        self.assertEqual(r["decision"], "replayed")
        r = s2.submit("ANEW", 3.5, 3.5, 0.0)
        self.assertEqual(r["decision"], "accepted")

    def test_concurrent_submissions_consistent(self):
        s = make()
        s.submit("BASE1", 0.5, 0.5, 0)
        s.submit("BASE2", 1.0, 1.0, 0)
        errors = []

        def worker(i):
            try:
                # 全部落在窗口 (0, 3.5] 内的迟到/顺序观测，唯一标识
                s.submit(f"T{i:02d}", 1 + i * 0.05, 1 + i * 0.05, 0.01 * (i % 3))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(errors)
        # 2 条基础 + 20 条新录入，修订恰好 +20（全部接受）
        self.assertEqual(s.revision, 22)
        self.assertEqual(len(s.log), 22)
        # 轨迹窗口点时间序严格非递减；日志序号连续
        ts = [p["timestamp"] for p in s.trajectory if not p["frozen"]]
        self.assertEqual(ts, sorted(ts))
        self.assertEqual([e.seq for e in s.log], list(range(1, 23)))

        # 回放幂等在并发下仍稳定：同标识同内容永远回放原结论
        outcomes = []

        def replay_worker():
            outcomes.append(s.submit("T00", 1.0, 1.0, 0.0)["decision"])

        ws = [threading.Thread(target=replay_worker) for _ in range(6)]
        for t in ws:
            t.start()
        for t in ws:
            t.join()
        self.assertTrue(all(d == "replayed" for d in outcomes))
        self.assertEqual(s.revision, 22)


if __name__ == "__main__":
    unittest.main(verbosity=2)
