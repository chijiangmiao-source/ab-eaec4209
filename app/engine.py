"""固定滞后平滑器（Fixed-Lag Smoother）核心引擎。

业务规则（导航审查）：
* 状态为二维位置 + 二维速度 [px, py, vx, vy]，匀速模型；观测只含二维位置。
* 迟到观测若落在当前滞后窗口内：从窗口起点检查点重算后缀（重放 / 重算）。
* 同稳定标识 (stable_id) 且观测内容（时间戳、位置）相同：回放原结论（幂等）。
* 同标识但内容不同：拒绝。
* 窗口外（时间戳早于窗口左边界）的观测：拒绝，不改写已封存位置。
* 同刻非递增顺序（时间戳晚于已处理最大时间）：拒绝。
* 非法噪声矩阵 / 不可逆创新矩阵：保留最近一次有效轨迹并返回失败原因。
* 全部观测进入不可变日志；每次成功修订产生单调递增修订号。
"""

from __future__ import annotations

import json
import math
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

MATRIX = List[List[float]]

# 窗口内最多保存的观测条数上限（页面最多录入 32 条，窗口再大也无意义）
MAX_OBSERVATIONS = 32


# --------------------------------------------------------------------------- #
# 线性代数（4x4 / 2x2 / 4x2，标准库实现，避免第三方依赖）
# --------------------------------------------------------------------------- #

def mat_zeros(r: int, c: int) -> MATRIX:
    return [[0.0 for _ in range(c)] for _ in range(r)]


def mat_identity(n: int) -> MATRIX:
    m = mat_zeros(n, n)
    for i in range(n):
        m[i][i] = 1.0
    return m


def mat_copy(m: MATRIX) -> MATRIX:
    return [row[:] for row in m]


def mat_mul(a: MATRIX, b: MATRIX) -> MATRIX:
    ra, ca, cb = len(a), len(a[0]), len(b[0])
    out = mat_zeros(ra, cb)
    for i in range(ra):
        for k in range(ca):
            aik = a[i][k]
            if aik == 0.0:
                continue
            brow = b[k]
            orow = out[i]
            for j in range(cb):
                orow[j] += aik * brow[j]
    return out


def mat_add(a: MATRIX, b: MATRIX) -> MATRIX:
    return [[a[i][j] + b[i][j] for j in range(len(a[0]))] for i in range(len(a))]


def mat_transpose(m: MATRIX) -> MATRIX:
    return [list(col) for col in zip(*m)]


def mat_inverse(m: MATRIX) -> MATRIX:
    """高斯-约旦消元求逆；奇异时抛出 ArithmeticError。"""
    n = len(m)
    a = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(m)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-15:
            raise ArithmeticError("矩阵奇异，无法求逆")
        if pivot != col:
            a[col], a[pivot] = a[pivot], a[col]
        pv = a[col][col]
        a[col] = [v / pv for v in a[col]]
        for r in range(n):
            if r == col:
                continue
            factor = a[r][col]
            if factor != 0.0:
                a[r] = [rv - factor * cv for rv, cv in zip(a[r], a[col])]
    return [row[n:] for row in a]


class ConfigError(ValueError):
    """配置（噪声矩阵 / 初值 / 滞后长度）非法。"""


class InnovationError(ArithmeticError):
    """创新协方差矩阵不可逆。"""


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

@dataclass
class Observation:
    stable_id: str
    timestamp: float
    x: float
    y: float
    seq: int  # 接收序号（日志不可变，按接收顺序追加）

    def content_key(self) -> Tuple[Any, ...]:
        # 同标识判定“内容相同”的依据：时间戳与二维位置
        return (self.stable_id, round(self.timestamp, 12), round(self.x, 12), round(self.y, 12))


@dataclass
class Checkpoint:
    """窗口左边界检查点：该时刻预测/滤波状态与协方差。"""
    timestamp: float
    state: List[float]
    covariance: MATRIX
    sealed: bool = False  # True=该点本身是已封存观测，同刻或更早的新观测不得再并入


@dataclass
class LogEntry:
    """不可变观测日志中的一条记录及其审查结论。"""
    seq: int
    stable_id: str
    timestamp: float
    x: float
    y: float
    decision: str          # accepted（接受/重算） / replayed（回放原结论） / rejected（拒绝）
    reason: str = ""
    revision: int = 0      # 本条录入所产生的修订号（拒绝/回放时为 0）
    position: List[float] = field(default_factory=lambda: [0.0, 0.0])
    cov_diag: List[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])
    residual: List[float] = field(default_factory=lambda: [0.0, 0.0])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seq": self.seq,
            "stable_id": self.stable_id,
            "timestamp": self.timestamp,
            "x": self.x,
            "y": self.y,
            "decision": self.decision,
            "reason": self.reason,
            "revision": self.revision,
            "position": self.position,
            "cov_diag": self.cov_diag,
            "residual": self.residual,
        }


# --------------------------------------------------------------------------- #
# 卡尔曼滤波步骤
# --------------------------------------------------------------------------- #

H: MATRIX = [
    [1.0, 0.0, 0.0, 0.0],
    [0.0, 1.0, 0.0, 0.0],
]


def transition(dt: float) -> MATRIX:
    return [
        [1.0, 0.0, dt, 0.0],
        [0.0, 1.0, 0.0, dt],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]


def process_noise(q: float, dt: float) -> MATRIX:
    """匀速模型的连续白噪声加速度离散化（4x4 PSD）。"""
    dt2 = dt * dt
    dt3 = dt2 * dt / 2.0
    dt4 = dt2 * dt2 / 4.0
    qx = q
    qy = q
    return [
        [qx * dt4,    0.0,       qx * dt3,  0.0],
        [0.0,         qy * dt4,  0.0,       qy * dt3],
        [qx * dt3,    0.0,       qx * dt2,  0.0],
        [0.0,         qy * dt3,  0.0,       qy * dt2],
    ]


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def _symmetric_2x2(v: Any, name: str) -> MATRIX:
    """解析 2x2 对称矩阵；接受 [[a,b],[b,c]]、扁平 [a,b,c] 或对角简写 [a,b]。"""
    if not isinstance(v, (list, tuple)):
        raise ConfigError(f"{name} 必须是数组")
    if len(v) == 2 and all(isinstance(row, (list, tuple)) for row in v):
        try:
            m = [[float(v[0][0]), float(v[0][1])], [float(v[1][0]), float(v[1][1])]]
        except (TypeError, ValueError, IndexError):
            raise ConfigError(f"{name} 必须是 2x2 数值矩阵")
    else:
        try:
            flat = [float(x) for x in v]
        except (TypeError, ValueError):
            raise ConfigError(f"{name} 必须全部为数值")
        if len(flat) == 4:
            m = [[flat[0], flat[1]], [flat[2], flat[3]]]
        elif len(flat) == 3:
            m = [[flat[0], flat[1]], [flat[1], flat[2]]]
        elif len(flat) == 2:
            m = [[flat[0], 0.0], [0.0, flat[1]]]
        else:
            raise ConfigError(f"{name} 维度非法，需要 2x2")
    if any(not math.isfinite(x) for row in m for x in row):
        raise ConfigError(f"{name} 含非有限值")
    if abs(m[0][1] - m[1][0]) > 1e-9:
        raise ConfigError(f"{name} 必须对称")
    return m


def _positive_definite_2x2(m: MATRIX, name: str) -> MATRIX:
    if m[0][0] <= 0.0 or m[1][1] <= 0.0:
        raise ConfigError(f"{name} 必须正定（对角元素必须为正）")
    det = m[0][0] * m[1][1] - m[0][1] * m[1][0]
    if det <= 0.0:
        raise ConfigError(f"{name} 必须正定（行列式 {det:g} <= 0）")
    return m


def _covariance_4x4(v: Any, name: str) -> MATRIX:
    if not isinstance(v, (list, tuple)):
        raise ConfigError(f"{name} 必须是 4x4 数组")
    try:
        m = [[float(v[i][j]) for j in range(4)] for i in range(4)]
    except (TypeError, ValueError, IndexError):
        raise ConfigError(f"{name} 必须是 4x4 数值矩阵")
    if any(not math.isfinite(x) for row in m for x in row):
        raise ConfigError(f"{name} 含非有限值")
    for i in range(4):
        if m[i][i] <= 0.0:
            raise ConfigError(f"{name} 对角元素必须为正")
        for j in range(i + 1, 4):
            if abs(m[i][j] - m[j][i]) > 1e-9:
                raise ConfigError(f"{name} 必须对称")
    # Sylvester 判据
    def det2(a, b, c, d):
        return a * d - b * c
    if m[0][0] <= 0:
        raise ConfigError(f"{name} 非正定")
    if det2(m[0][0], m[0][1], m[1][0], m[1][1]) <= 0:
        raise ConfigError(f"{name} 非正定")
    # 特征值下界的快速保护：Cholesky 尝试
    try:
        _cholesky4(m)
    except ArithmeticError:
        raise ConfigError(f"{name} 必须正定")
    return m


def _cholesky4(m: MATRIX) -> MATRIX:
    l = mat_zeros(4, 4)
    for i in range(4):
        for j in range(i + 1):
            s = sum(l[i][k] * l[j][k] for k in range(j))
            if i == j:
                val = m[i][i] - s
                if val <= 1e-15:
                    raise ArithmeticError("非正定")
                l[i][j] = math.sqrt(val)
            else:
                l[i][j] = (m[i][j] - s) / l[j][j]
    return l


def kf_predict(state: List[float], cov: MATRIX, dt: float, q: float) -> Tuple[List[float], MATRIX]:
    F = transition(dt)
    new_state = [sum(F[i][j] * state[j] for j in range(4)) for i in range(4)]
    new_cov = mat_add(mat_mul(mat_mul(F, cov), mat_transpose(F)), process_noise(q, dt))
    return new_state, new_cov


def kf_update(state: List[float], cov: MATRIX, z: List[float], R: MATRIX
              ) -> Tuple[List[float], MATRIX, List[float]]:
    """返回 (更新后状态, 更新后协方差, 残差 z-Hx)。创新矩阵奇异时抛 InnovationError。"""
    Hx = [
        H[0][0] * state[0] + H[0][1] * state[1],
        H[1][0] * state[0] + H[1][1] * state[1],
    ]
    residual = [z[0] - Hx[0], z[1] - Hx[1]]
    PHT = mat_mul(cov, mat_transpose(H))          # 4x2
    S = mat_add(mat_mul(H, PHT), R)               # 2x2
    try:
        S_inv = mat_inverse(S)
    except ArithmeticError as exc:
        raise InnovationError(f"创新协方差矩阵不可逆: {exc}") from exc
    K = mat_mul(PHT, S_inv)                        # 4x2
    new_state = [state[i] + K[i][0] * residual[0] + K[i][1] * residual[1] for i in range(4)]
    # P = (I - K H) P
    KH = mat_mul(K, H)
    I4 = mat_identity(4)
    factor = [[I4[i][j] - KH[i][j] for j in range(4)] for i in range(4)]
    new_cov = mat_mul(factor, cov)
    # 对称化，抑制数值漂移
    new_cov = [[(new_cov[i][j] + new_cov[j][i]) / 2.0 for j in range(4)] for i in range(4)]
    return new_state, new_cov, residual


# --------------------------------------------------------------------------- #
# 固定滞后平滑器
# --------------------------------------------------------------------------- #

class FixedLagSmoother:
    def __init__(self, config: Dict[str, Any]):
        cfg = self._validate_config(config)
        self.config = cfg
        self.lock = threading.RLock()
        self.log: List[LogEntry] = []
        self.window: List[Observation] = []        # 已接受、位于窗口内的观测（按时间序）
        self.checkpoint: Optional[Checkpoint] = None
        self.frozen_points: List[Dict[str, Any]] = []  # 已封存轨迹点（单调追加，永不重算）
        self.trajectory: List[Dict[str, Any]] = []  # 已发布轨迹（封存段 + 当前窗口后缀）
        self.current_state: List[float] = cfg["initial_state"][:]
        self.current_cov: MATRIX = mat_copy(cfg["initial_cov"])
        self.latest_time: Optional[float] = None
        self.revision = 0
        self.last_error: str = ""
        # 稳定标识 -> 首次出现的内容键与原结论快照（用于回放 / 拒绝）
        self._id_registry: Dict[str, Tuple[Tuple[Any, ...], Dict[str, Any]]] = {}
        self._init_checkpoint()
        self.trajectory = [dict(p) for p in self.frozen_points]

    # ---------------- 配置 ---------------- #

    @staticmethod
    def _validate_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(cfg, dict):
            raise ConfigError("配置必须是对象")
        state = cfg.get("initial_state")
        cov = cfg.get("initial_cov")
        qv = cfg.get("process_noise")
        rv = cfg.get("measurement_noise")
        lag = cfg.get("lag")

        if not isinstance(state, (list, tuple)) or len(state) != 4 or not all(_is_number(v) for v in state):
            raise ConfigError("initial_state 必须是 4 个有限数值 [px, py, vx, vy]")
        initial_state = [float(v) for v in state]
        initial_cov = _covariance_4x4(cov, "initial_cov")

        # 过程噪声：标量 q（加速度噪声强度，>=0）
        if not _is_number(qv) or float(qv) < 0.0:
            raise ConfigError("process_noise 必须是非负数值（加速度噪声强度 q）")
        q = float(qv)

        R = _positive_definite_2x2(_symmetric_2x2(rv, "measurement_noise"), "measurement_noise")

        if not isinstance(lag, int) or isinstance(lag, bool) or lag <= 0:
            raise ConfigError("lag 必须是正整数（滞后长度，单位与时间戳一致）")
        lag = int(lag)

        t0v = cfg.get("initial_timestamp", 0.0)
        if not _is_number(t0v):
            raise ConfigError("initial_timestamp 必须是数值")
        return {
            "initial_state": initial_state,
            "initial_cov": initial_cov,
            "process_noise": q,
            "measurement_noise": R,
            "lag": lag,
            "initial_timestamp": float(t0v),
        }

    def _init_checkpoint(self) -> None:
        self.checkpoint = Checkpoint(
            timestamp=self.config["initial_timestamp"],
            state=self.config["initial_state"][:],
            covariance=mat_copy(self.config["initial_cov"]),
        )
        # 轨迹锚点（先验检查点）：天然封存，永不重算
        self.frozen_points = [{
            "timestamp": self.checkpoint.timestamp,
            "position": self.checkpoint.state[:2],
            "velocity": self.checkpoint.state[2:],
            "cov_diag": [self.checkpoint.covariance[i][i] for i in range(4)],
            "frozen": True,
            "revision": 0,
            "stable_id": None,
            "seq": None,
        }]

    # ---------------- 持久化 ---------------- #

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "version": 1,
                "config": self.config,
                "revision": self.revision,
                "log": [e.to_dict() for e in self.log],
                "window": [
                    {"stable_id": o.stable_id, "timestamp": o.timestamp, "x": o.x, "y": o.y, "seq": o.seq}
                    for o in self.window
                ],
                "checkpoint": {
                    "timestamp": self.checkpoint.timestamp,
                    "state": self.checkpoint.state[:],
                    "covariance": self.checkpoint.covariance,
                    "sealed": self.checkpoint.sealed,
                },
                "frozen_points": self.frozen_points,
                "trajectory": self.trajectory,
                "current_state": self.current_state[:],
                "current_cov": self.current_cov,
                "latest_time": self.latest_time,
                "last_error": self.last_error,
                "registry": {k: [list(v[0]), v[1]] for k, v in self._id_registry.items()},
            }

    @classmethod
    def restore(cls, data: Dict[str, Any]) -> "FixedLagSmoother":
        obj = cls(data["config"])
        with obj.lock:
            obj.revision = int(data["revision"])
            obj.log = [
                LogEntry(
                    seq=d["seq"], stable_id=d["stable_id"], timestamp=d["timestamp"],
                    x=d["x"], y=d["y"], decision=d["decision"], reason=d.get("reason", ""),
                    revision=d.get("revision", 0), position=d.get("position", [0.0, 0.0]),
                    cov_diag=d.get("cov_diag", [0.0] * 4), residual=d.get("residual", [0.0, 0.0]),
                )
                for d in data.get("log", [])
            ]
            obj.window = [
                Observation(d["stable_id"], float(d["timestamp"]), float(d["x"]), float(d["y"]), d["seq"])
                for d in data.get("window", [])
            ]
            cp = data["checkpoint"]
            obj.checkpoint = Checkpoint(
                float(cp["timestamp"]), [float(v) for v in cp["state"]], cp["covariance"],
                sealed=bool(cp.get("sealed", False)),
            )
            obj.frozen_points = data.get("frozen_points") or [
                p for p in data.get("trajectory", []) if p.get("frozen")
            ]
            obj.trajectory = data.get("trajectory", [])
            obj.current_state = [float(v) for v in data["current_state"]]
            obj.current_cov = data["current_cov"]
            obj.latest_time = data.get("latest_time")
            obj.last_error = data.get("last_error", "")
            obj._id_registry = {}
            for k, v in data.get("registry", {}).items():
                obj._id_registry[k] = (tuple(v[0]), v[1])
        return obj

    # ---------------- 录入 ---------------- #

    @property
    def q(self) -> float:
        return self.config["process_noise"]

    @property
    def R(self) -> MATRIX:
        return self.config["measurement_noise"]

    @property
    def lag(self) -> int:
        return self.config["lag"]

    def _window_left(self) -> float:
        return self.checkpoint.timestamp

    def _replay_suffix(self, accepted_sorted: List[Observation]
                       ) -> Tuple[Checkpoint, List[float], MATRIX, List[Dict[str, Any]], float,
                                  List[Dict[str, Any]]]:
        """从检查点起按时间序重放后缀。

        返回 (新检查点, 末端状态, 末端协方差, 全部后缀点, 末端时间, 本次新封存点)。
        已封存点（在检查点之前）不在重放范围内，绝不重算。
        封存规则：timestamp <= latest - lag 的点封存（可修订后缀为半开区间
        (latest-lag, latest]，恰在边界上的槽位已封存）。
        同刻（相同时间戳）观测按接收序号 seq 决定先后，顺序确定可重复。
        """
        cp = Checkpoint(self.checkpoint.timestamp,
                        self.checkpoint.state[:], mat_copy(self.checkpoint.covariance),
                        sealed=self.checkpoint.sealed)
        state = cp.state[:]
        cov = mat_copy(cp.covariance)
        t = cp.timestamp
        suffix: List[Dict[str, Any]] = []

        for obs in accepted_sorted:
            dt = obs.timestamp - t
            if dt < -1e-9:
                # 理论上不会发生（按 (时间戳, 接收序号) 排序后），防御性处理
                raise InnovationError("重放遇到时间戳倒退")
            if dt > 0:
                state, cov = kf_predict(state, cov, dt, self.q)
            t = obs.timestamp
            state, cov, residual = kf_update(state, cov, [obs.x, obs.y], self.R)
            suffix.append({
                "timestamp": obs.timestamp,
                "stable_id": obs.stable_id,
                "seq": obs.seq,
                "state": state[:],
                "covariance": mat_copy(cov),
                "residual": residual[:],
            })

        latest = t
        cutoff = latest - self.lag
        new_cp = cp
        newly_frozen: List[Dict[str, Any]] = []
        for point in suffix:
            if point["timestamp"] <= cutoff + 1e-9:
                new_cp = Checkpoint(point["timestamp"], point["state"][:],
                                    mat_copy(point["covariance"]), sealed=True)
                newly_frozen.append(point)
        return new_cp, state, cov, suffix, latest, newly_frozen

    def _reject_entry(self, seq: int, obs: Optional[Observation], reason: str,
                      stable_id: Any = None, timestamp: Any = None,
                      x: Any = None, y: Any = None) -> Dict[str, Any]:
        """追加一条拒绝结论到不可变日志并返回结果（不改变轨迹）。"""
        if obs is not None:
            sid, ts, ox, oy = obs.stable_id, obs.timestamp, obs.x, obs.y
        else:
            sid = stable_id if isinstance(stable_id, str) and stable_id else ""
            ts = float(timestamp) if _is_number(timestamp) else 0.0
            ox = float(x) if _is_number(x) else 0.0
            oy = float(y) if _is_number(y) else 0.0
        entry = LogEntry(
            seq=seq, stable_id=sid, timestamp=ts, x=ox, y=oy,
            decision="rejected", reason=reason,
            position=self.current_state[:2],
            cov_diag=[self.current_cov[i][i] for i in range(4)],
        )
        self.log.append(entry)
        self.last_error = reason
        return self._entry_result(entry, changed=False)

    def submit(self, stable_id: Any, timestamp: Any, x: Any, y: Any) -> Dict[str, Any]:
        """按接收顺序录入一条观测，返回结论字典。线程安全。"""
        with self.lock:
            seq = len(self.log) + 1

            # 1) 输入合法性（非法输入记拒绝日志，不改变轨迹）
            bad = self._validate_observation(stable_id, timestamp, x, y)
            if bad is not None:
                return self._reject_entry(seq, None, bad, stable_id, timestamp, x, y)
            obs = Observation(str(stable_id), float(timestamp), float(x), float(y), seq)

            # 2) 容量硬闸门：最多 32 条，超出者无法录入（不追加日志）
            if len(self.log) >= MAX_OBSERVATIONS:
                reason = f"观测条数已达上限 {MAX_OBSERVATIONS} 条，拒绝录入"
                self.last_error = reason
                dummy = LogEntry(
                    seq=seq, stable_id=obs.stable_id, timestamp=obs.timestamp, x=obs.x, y=obs.y,
                    decision="rejected", reason=reason,
                    position=self.current_state[:2],
                    cov_diag=[self.current_cov[i][i] for i in range(4)],
                )
                return self._entry_result(dummy, changed=False)

            # 3) 稳定标识规则优先：同标识同内容回放原结论；同标识内容不同拒绝。
            #    重传观测即使落在封存边界上也只能回放，不能作为新观测处理。
            key = obs.content_key()
            if obs.stable_id in self._id_registry:
                prev_key, prev_result = self._id_registry[obs.stable_id]
                if prev_key == key:
                    replay = LogEntry(
                        seq=seq, stable_id=obs.stable_id, timestamp=obs.timestamp,
                        x=obs.x, y=obs.y, decision="replayed",
                        reason=(f"同标识同内容回放 seq={prev_result['seq']} 的原结论"
                                f"（幂等，不产生新修订）"),
                        revision=0,
                        position=prev_result["position"][:],
                        cov_diag=prev_result["cov_diag"][:],
                        residual=prev_result["residual"][:],
                    )
                    self.log.append(replay)
                    self.last_error = ""
                    return self._entry_result(replay, changed=False)
                reason = (f"稳定标识 {obs.stable_id} 已存在但内容不同"
                          f"（原 t={prev_key[1]}, pos=({prev_key[2]},{prev_key[3]})），拒绝")
                return self._reject_entry(seq, obs, reason)

            # 4) 顺序/窗口准入：窗口外观测、与已封存点同刻的非递增顺序一律拒绝
            left = self._window_left()
            if obs.timestamp < left - 1e-9:
                return self._reject_entry(
                    seq, obs,
                    f"窗口外观测：t={obs.timestamp:g} 早于已封存边界 t0={left:g}"
                    f"（滞后窗口长度 {self.lag:g}）；该段顺序已封存，不得改写已发布轨迹")
            if self.checkpoint.sealed and abs(obs.timestamp - left) <= 1e-9:
                return self._reject_entry(
                    seq, obs,
                    f"同刻非递增顺序：t={obs.timestamp:g} 的槽位已封存，"
                    f"迟到观测只能重算窗口内（t>{left:g}）的历史后缀")

            # 5) 尝试把新观测并入窗口并重算后缀；任何数值失败都保留最近有效轨迹
            candidate_window = self.window + [obs]
            candidate_sorted = sorted(candidate_window, key=lambda o: (o.timestamp, o.seq))
            try:
                (new_cp, end_state, end_cov, suffix_points,
                 latest, newly_frozen) = self._replay_suffix(candidate_sorted)
            except InnovationError as exc:
                return self._reject_entry(
                    seq, obs,
                    f"重算失败（创新协方差矩阵不可逆：{exc}），保留最近有效轨迹")

            # 成功提交：推进修订号并发布新轨迹
            self.revision += 1
            rev = self.revision
            was_late = self.latest_time is not None and obs.timestamp < self.latest_time - 1e-9
            self.window = [o for o in candidate_sorted
                           if o.timestamp > new_cp.timestamp
                           or (not new_cp.sealed and abs(o.timestamp - new_cp.timestamp) <= 1e-9)]
            self.checkpoint = new_cp
            self.current_state = end_state
            self.current_cov = end_cov
            self.latest_time = latest

            # 新封存点单调追加到不可变封存序列（它们在检查点之前，以后永不重算）
            for p in newly_frozen:
                self.frozen_points.append({
                    "timestamp": p["timestamp"],
                    "position": p["state"][:2],
                    "velocity": p["state"][2:],
                    "cov_diag": [p["covariance"][i][i] for i in range(4)],
                    "residual": p["residual"],
                    "stable_id": p["stable_id"],
                    "seq": p["seq"],
                    "frozen": True,
                    "revision": rev,
                })

            # 已发布轨迹 = 永不重算的封存段 + 当前修订下的窗口后缀
            published = [dict(p) for p in self.frozen_points]
            for p in suffix_points:
                in_window = (p["timestamp"] > new_cp.timestamp
                             or (not new_cp.sealed
                                 and abs(p["timestamp"] - new_cp.timestamp) <= 1e-9))
                if in_window:
                    published.append({
                        "timestamp": p["timestamp"],
                        "position": p["state"][:2],
                        "velocity": p["state"][2:],
                        "cov_diag": [p["covariance"][i][i] for i in range(4)],
                        "residual": p["residual"],
                        "stable_id": p["stable_id"],
                        "seq": p["seq"],
                        "frozen": False,
                        "revision": rev,
                    })
            self.trajectory = published

            # 本条观测对应的指标（它可能是迟到点，取其重放点）
            own_point = next((p for p in suffix_points if p["seq"] == seq), suffix_points[-1])
            result = {
                "seq": seq,
                "stable_id": obs.stable_id,
                "timestamp": obs.timestamp,
                "x": obs.x,
                "y": obs.y,
                "decision": "accepted",
                "reason": (f"迟到观测：已从检查点 t={new_cp.timestamp:g} 重算窗口后缀，"
                           f"仅修订窗口内历史后缀，已封存位置不变" if was_late
                           else "顺序观测：已并入窗口后缀"),
                "revision": rev,
                "position": own_point["state"][:2],
                "cov_diag": [own_point["covariance"][i][i] for i in range(4)],
                "residual": own_point["residual"],
            }
            self._id_registry[obs.stable_id] = (key, dict(result))
            entry = self._log_from_result(obs, result)
            self.log.append(entry)
            self.last_error = ""
            return self._entry_result(entry, changed=True)

    @staticmethod
    def _validate_observation(stable_id: Any, timestamp: Any, x: Any, y: Any) -> Optional[str]:
        if not isinstance(stable_id, str) or not stable_id.strip():
            return "stable_id 必须是非空字符串"
        if not _is_number(timestamp):
            return "timestamp 必须是有限数值"
        if not _is_number(x) or not _is_number(y):
            return "观测位置 x/y 必须是有限数值"
        return None

    def _log_from_result(self, obs: Observation, r: Dict[str, Any]) -> LogEntry:
        return LogEntry(
            seq=r["seq"], stable_id=obs.stable_id, timestamp=obs.timestamp, x=obs.x, y=obs.y,
            decision=r["decision"], reason=r.get("reason", ""), revision=r.get("revision", 0),
            position=r["position"][:], cov_diag=r["cov_diag"][:], residual=r.get("residual", [0.0, 0.0])[:],
        )

    def _entry_result(self, entry: LogEntry, changed: bool) -> Dict[str, Any]:
        out = entry.to_dict()
        out["changed"] = changed
        out["current_revision"] = self.revision
        out["window_left"] = self._window_left()
        out["latest_time"] = self.latest_time
        return out

    # ---------------- 查询 ---------------- #

    def state_dict(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "revision": self.revision,
                "config": self.config,
                "window_left": self._window_left(),
                "latest_time": self.latest_time,
                "current_position": self.current_state[:2],
                "current_velocity": self.current_state[2:],
                "current_state": self.current_state[:],
                "cov_diag": [self.current_cov[i][i] for i in range(4)],
                "last_error": self.last_error,
                "log": [e.to_dict() for e in self.log],
                "trajectory": self.trajectory,
                "capacity": MAX_OBSERVATIONS,
            }


# --------------------------------------------------------------------------- #
# 持久化存储（JSON 文件，fsync 保证重开恢复）
# --------------------------------------------------------------------------- #

class JsonStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> Optional[Dict[str, Any]]:
        if not os.path.exists(self.path):
            return None
        with open(self.path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def save(self, data: Dict[str, Any]) -> None:
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
