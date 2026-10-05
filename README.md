# 导航审查 · 固定滞后平滑器（Fixed-Lag Smoother）

巡检机返航后，导航审查员把**延迟到达的定位观测**并入既有惯导轨迹：
迟到观测只能**重算固定滞后窗口内的历史后缀**，已封存位置绝不改写。

- 状态：二维位置 + 二维速度 `[px, py, vx, vy]`（匀速模型）；观测：二维位置。
- 可在页面建立：位置/速度初值、初始协方差、过程噪声强度 q、2×2 观测噪声 R、滞后长度 L。
- 按接收顺序录入最多 **32** 条观测（稳定标识、采样时刻、二维位置）。
- 每条观测给出结论：**接受/重算**、**回放**、**拒绝**，并展示当前位置、协方差对角线、残差。
- 不可变观测日志（只追加）+ **单调修订号**维护已发布轨迹投影。

## 业务规则

| 情形 | 结论 | 是否产生新修订 |
|---|---|---|
| 窗口内顺序/迟到观测 | 接受，从检查点重算窗口后缀 | 是（+1） |
| 同 `stable_id` 且（时刻、位置）完全相同 | 回放原结论（幂等） | 否 |
| 同 `stable_id` 但内容不同 | 拒绝 | 否 |
| 时刻早于已封存边界 | 拒绝（窗口外，不得改写已封存位置） | 否 |
| 时刻等于已封存槽位（同刻非递增顺序） | 拒绝 | 否 |
| 非法噪声矩阵 / 不可逆创新矩阵 | 拒绝并说明原因，**保留最近有效轨迹** | 否 |

- 可修订后缀为半开区间 `(latest − L, latest]`；边界及更早的点已封存，永不重算。
- 计算期间继续录入时，前端**单调修订号守卫**会抑制旧修订响应，旧结果不得覆盖最新页面。
- 每次成功修订都原子写入持久化存储；重开后恢复完全相同的日志与轨迹。

## 运行（Docker Compose）

```bash
docker compose up app                     # http://localhost:8080
PORT=9090 docker compose up app           # 可配置主机端口
```

## 验收服务 verify

一次性执行：**代码测试 → 构建检查 → 健康/业务 API 冒烟 → 重开恢复核对**，随后退出，
退出码报告验收结果（0=通过）。

```bash
# 容器内（推荐，含前端守卫测试所需的 node）
docker compose --profile verify run --rm verify

# 或在主机直接运行（需要 python3.11；前端守卫测试需要 node）
./scripts/verify
./scripts/verify --base-url http://localhost:8080   # 追加冒烟已运行实例
```

## 本地直接运行（仅 Python 标准库）

```bash
python3 -m app.server --host 0.0.0.0 --port 8080
NAV_PORT=8080 NAV_STORE=./data/state.json python3 -m app.server
```

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康响应 `{"status":"ok"}` |
| GET | `/` | 审查页面 |
| GET | `/api/state` | 配置、日志、轨迹、当前估计、修订号 |
| GET | `/api/trajectory` | 已发布轨迹投影 |
| POST | `/api/config` | 建立初值/噪声/滞后长度（已有日志后拒绝重建） |
| POST | `/api/observations` | 录入一条观测 `{stable_id,timestamp,x,y}` |
| POST | `/api/reset` | 清空（重新建档） |

## 目录

```
app/engine.py        固定滞后平滑器 + 卡尔曼滤波 + JSON 持久化（标准库）
app/server.py        ThreadingHTTPServer 与 REST API
app/static/          审查页面（index.html / app.js，含单调修订号守卫）
tests/               Python 测试（test_*.py）与 Node 守卫测试
scripts/verify       可执行验收服务（测试 → 构建检查 → 冒烟，退出码报告结果）
compose.yaml         app 服务 + verify 验收 profile
```
