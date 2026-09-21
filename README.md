# 多层配额原子试算 API

基于 **Python + FastAPI + Pydantic v2** 的令牌桶配额试算服务。给定若干令牌桶与一条带时间戳的请求序列，服务按时间顺序模拟连续补充，并对每次请求执行 **桶链原子扣减**：

- 每个桶以 `refill_rate`（令牌/秒）按时间差**连续补充**，余额上限为 `capacity`；
- 请求携带一条桶链 `chain`，链上**每个桶**都需满足 `余额 >= cost`；
- 全部充足 → 链上每个桶原子扣减 `cost`；任一不足 → **全部桶保持不变**，并返回链上第一个不足的桶（`rejected_by`）；
- 逐项返回放行结果、限制桶以及链上各桶扣减前后余额。

另提供 **等待时间预测** 接口 `POST /forecast`：给定当前时刻各桶快照（容量/速率/当前余额）、请求成本与桶链，在**不扣减任何状态**的前提下，计算链上全部桶首次同时满足成本所需的等待秒数，返回预计可通过时刻、等待最长的限制桶与各桶预计余额；零速率或容量不足导致永远不可达时返回明确的不可达标记。

还提供 **初始余额规划** 接口 `POST /plan-initial-balances`：请求体与 `/simulate` 完全一致，目标是让**所有请求均获放行**。服务按相同的时间排序与连续补充语义，为每个桶在 `[0, capacity]` 内反推满足其**全部链上消耗**的最低初始余额，返回各桶 `required_initial`、相对请求 `initial` 的 `delta`，以及用规划值完整回放得到的 `final_balances`；即使在 `/simulate` 中某次请求会因链上其他桶不足而被整体拒绝，规划时该桶自身的消耗仍**全额计入**。若满桶起步仍不可行，返回 `reachable=false` 并指出按处理顺序首个无法满足的请求与桶。

## 一条命令启动

需要已安装 Docker（含 Compose 插件）。在本目录执行：

```bash
docker compose up --build
```

启动后访问地址与端口：

| 用途 | 地址 |
| --- | --- |
| 健康检查 | http://localhost:8000/health |
| 试算接口 | http://localhost:8000/simulate （POST） |
| 预测接口 | http://localhost:8000/forecast （POST） |
| 初始余额规划 | http://localhost:8000/plan-initial-balances （POST） |
| Swagger 交互文档 | http://localhost:8000/docs |
| ReDoc 文档 | http://localhost:8000/redoc |

依赖安装与启动初始化均已封装进镜像（见 `Dockerfile` 与 `entrypoint.sh`），宿主机无需 Python 环境。
后台运行可用 `docker compose up --build -d`，停止用 `docker compose down`。

## 请求格式 `POST /simulate`

```json
{
  "buckets": [
    {"id": "tenant_quota", "capacity": 100, "refill_rate": 10, "initial": 100},
    {"id": "api_qps", "capacity": 5, "refill_rate": 5}
  ],
  "requests": [
    {"id": "req-1", "timestamp": 0, "cost": 4, "chain": ["tenant_quota", "api_qps"]},
    {"id": "req-2", "timestamp": 0, "cost": 4, "chain": ["tenant_quota", "api_qps"]},
    {"id": "req-3", "timestamp": 1, "cost": 4, "chain": ["tenant_quota", "api_qps"]}
  ]
}
```

### 字段说明

**桶 `buckets[]`**

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 桶唯一标识，非空字符串 |
| `capacity` | 是 | 桶容量（令牌上限），必须 > 0 |
| `refill_rate` | 是 | 连续补充速率（令牌/秒），必须 ≥ 0 |
| `initial` | 否 | 初始余额，默认等于 `capacity`；要求 0 ≤ initial ≤ capacity |

**请求 `requests[]`**

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 请求唯一标识 |
| `timestamp` | 是 | 请求时间（秒，任意一致的时间原点，如相对秒或 Unix 时间） |
| `cost` | 是 | 消耗量，必须 > 0；从链上**每个**桶各扣此数量 |
| `chain` | 是 | 桶 ID 列表（至少 1 个），同一链内不可重复 |

### 模拟语义

1. **时间顺序**：所有请求按 `timestamp` 升序处理；同一时间戳按输入数组顺序处理。`results` 仍按原始数组顺序返回（含 `index`）。
2. **连续补充**：桶在模拟起始时刻（最早请求时间戳）持有 `initial` 余额，之后每次被请求触及时按 `(当前时间 - 上次更新时间) × refill_rate` 惰性补充，封顶 `capacity`。因此时间戳可以是任意大小（Unix 秒等），只取决于时间差。
3. **原子扣减**：判定时先对链上所有桶补充到当前时刻，再按链顺序找第一个余额不足的桶。不足则全部不扣，`balances` 中各桶 `after == before`；充足则每个桶都扣 `cost`。
4. **终态**：`final_balances` 给出所有桶推进到最后一个时间戳后的余额。

## 响应示例

```json
{
  "allowed_count": 2,
  "rejected_count": 1,
  "results": [
    {
      "request_id": "req-1",
      "index": 0,
      "timestamp": 0.0,
      "allowed": true,
      "rejected_by": null,
      "balances": {
        "tenant_quota": {"before": 100.0, "after": 96.0},
        "api_qps": {"before": 5.0, "after": 1.0}
      }
    },
    {
      "request_id": "req-2",
      "index": 1,
      "timestamp": 0.0,
      "allowed": false,
      "rejected_by": "api_qps",
      "balances": {
        "tenant_quota": {"before": 96.0, "after": 96.0},
        "api_qps": {"before": 1.0, "after": 1.0}
      }
    }
  ],
  "final_balances": {"tenant_quota": 96.0, "api_qps": 1.0}
}
```

- `before`：该请求时刻补充之后、扣减之前的余额；
- `after`：扣减之后的余额；被拒绝时与 `before` 相同（原子性）。

## 请求格式 `POST /forecast`

```json
{
  "now": 1000,
  "cost": 4,
  "chain": ["tenant_quota", "api_qps", "user_quota"],
  "buckets": [
    {"id": "tenant_quota", "capacity": 100, "refill_rate": 10, "balance": 96},
    {"id": "api_qps", "capacity": 5, "refill_rate": 5, "balance": 1},
    {"id": "user_quota", "capacity": 20, "refill_rate": 1, "balance": 2}
  ]
}
```

### 字段说明

**顶层**

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `now` | 是 | 当前时刻（秒，任意一致的时间原点，如相对秒或 Unix 时间） |
| `cost` | 是 | 请求成本，必须 > 0；链上**每个**桶都需持有此数量 |
| `chain` | 是 | 桶 ID 列表（至少 1 个），同一链内不可重复 |
| `buckets` | 是 | 桶状态快照集合（至少 1 个） |

**桶快照 `buckets[]`**

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 桶唯一标识，非空字符串 |
| `capacity` | 是 | 桶容量（令牌上限），必须 > 0 |
| `refill_rate` | 是 | 连续补充速率（令牌/秒），必须 ≥ 0 |
| `balance` | 是 | 当前余额（`now` 时刻持有），要求 0 ≤ balance ≤ capacity |

### 预测语义

1. **纯计算**：不扣减、不修改任何状态；`now` 可以是任意一致的时间原点。
2. **单桶等待**：余额已满足 `cost` → 等待 0；否则若 `refill_rate > 0`，等待 `(cost - balance) / refill_rate` 秒。
3. **链等待**：链上各桶等待时间的**最大值**，即全部桶首次同时满足成本的时刻；`pass_at = now + wait_seconds`。
4. **限制桶**：等待时间最长的桶（`limiting_bucket`），并列时取链上首个；立即可通过（等待为 0）时为 `null`。
5. **预计余额**：`projected_balance` 为预计可通过时刻连续补充后的余额（封顶 `capacity`），**不扣减** `cost`。
6. **不可达标记**：`cost > capacity`（`cost_exceeds_capacity`）或余额不足且 `refill_rate == 0`（`zero_refill_rate`）的桶永远不可达；链上任一桶不可达则整体 `reachable = false`，`wait_seconds` / `pass_at` / `limiting_bucket` / `projected_balance` 均为 `null`，各桶 `reason` 标明原因。

### 响应示例（可达）

```json
{
  "reachable": true,
  "wait_seconds": 2.0,
  "pass_at": 1002.0,
  "limiting_bucket": "user_quota",
  "buckets": [
    {"id": "tenant_quota", "balance_now": 96.0, "reachable": true, "reason": null, "wait_seconds": 0.0, "projected_balance": 100.0},
    {"id": "api_qps", "balance_now": 1.0, "reachable": true, "reason": null, "wait_seconds": 0.6, "projected_balance": 5.0},
    {"id": "user_quota", "balance_now": 2.0, "reachable": true, "reason": null, "wait_seconds": 2.0, "projected_balance": 4.0}
  ]
}
```

### 响应示例（不可达，`user_quota` 零速率且余额不足）

```json
{
  "reachable": false,
  "wait_seconds": null,
  "pass_at": null,
  "limiting_bucket": null,
  "buckets": [
    {"id": "tenant_quota", "balance_now": 96.0, "reachable": true, "reason": null, "wait_seconds": 0.0, "projected_balance": null},
    {"id": "api_qps", "balance_now": 1.0, "reachable": true, "reason": null, "wait_seconds": 0.6, "projected_balance": null},
    {"id": "user_quota", "balance_now": 2.0, "reachable": false, "reason": "zero_refill_rate", "wait_seconds": null, "projected_balance": null}
  ]
}
```

## 请求格式 `POST /plan-initial-balances`

请求体与字段**完全沿用** `POST /simulate`（`buckets` + `requests`，校验规则也一致，见下文）：

```json
{
  "buckets": [
    {"id": "tenant_quota", "capacity": 100, "refill_rate": 10, "initial": 100},
    {"id": "api_qps", "capacity": 10, "refill_rate": 5},
    {"id": "user_quota", "capacity": 20, "refill_rate": 1, "initial": 3}
  ],
  "requests": [
    {"id": "req-1", "timestamp": 0, "cost": 4, "chain": ["tenant_quota", "api_qps", "user_quota"]},
    {"id": "req-2", "timestamp": 1, "cost": 4, "chain": ["tenant_quota", "api_qps"]},
    {"id": "req-3", "timestamp": 2, "cost": 4, "chain": ["tenant_quota", "api_qps", "user_quota"]}
  ]
}
```

### 规划语义

1. **目标全量放行**：处理顺序与 `/simulate` 一致（按 `timestamp` 升序、同刻按输入顺序），连续补充与封顶规则也完全相同；规划目标是让每个请求链上的所有桶在判定时刻都 `余额 >= cost`。
2. **链上消耗全量计入**：每个桶独立承担自己参与的**每一次**请求的 `cost`。即使某次请求在 `/simulate` 中会因链上其他桶不足而被原子拒绝（该桶不扣），规划时该桶自身的消耗仍照常计入，不会因其他桶暂时不足而跳过。
3. **最低初始余额（逆序归纳）**：对桶的事件序列 `(t_k, c_k)`，令末事件后余额恰为 0，逐事件叠加补充无法覆盖的缺口：

   ```
   req_n = c_n
   req_k = c_k + max(0, req_{k+1} - refill_rate × (t_{k+1} - t_k))
   initial = clamp(0, req_1 - refill_rate × (t_1 - t_start), capacity)
   ```

   其中 `t_start` 为最早请求时间戳（首事件之前的连续补充同样封顶 `capacity`）。结果满足 `0 ≤ required_initial ≤ capacity`，与真实最低值误差不超过 1e-6；未被任何请求引用的桶 `required_initial = 0`。
4. **不可行判定**：满桶（`initial = capacity`）是初始余额的上界。以满桶起步完整回放，若仍有请求被拒，则不存在可行解，返回 `reachable = false`，并给出按处理顺序首个被拒请求的 `request_id` / `index` / `timestamp` 与其链上第一个不足的桶 `bucket`；此时 `buckets` 与 `final_balances` 为 `null`。
5. **完整回放**：可行时服务会用规划出的 `required_initial` 替换请求中的 `initial` 再跑一遍 `/simulate`，确认全部请求放行，并把终态写入 `final_balances`；`delta = required_initial - requested_initial`（正数需补足、负数可下调），省略 `initial` 时 `requested_initial` 取 `capacity`。

### 响应示例（可行，对应 `examples/plan.json`）

```json
{
  "reachable": true,
  "failure": null,
  "buckets": [
    {"id": "tenant_quota", "requested_initial": 100.0, "required_initial": 4.0, "delta": -96.0},
    {"id": "api_qps", "requested_initial": 10.0, "required_initial": 4.0, "delta": -6.0},
    {"id": "user_quota", "requested_initial": 3.0, "required_initial": 6.0, "delta": 3.0}
  ],
  "final_balances": {"tenant_quota": 12.0, "api_qps": 2.0, "user_quota": 0.0}
}
```

最低初始不保证终态为 0：事件间隔的补充若超过下一跳缺口会在回放中结余（如 `tenant_quota`）。

### 响应示例（不可行：零补充桶容量只够一次消耗）

请求：桶 `A`（`capacity = 3, refill_rate = 0`，默认满桶），`t=0` 扣 3、`t=100` 再扣 1。

```json
{
  "reachable": false,
  "failure": {
    "request_id": "r2",
    "index": 1,
    "timestamp": 100.0,
    "bucket": "A"
  },
  "buckets": null,
  "final_balances": null
}
```

## 校验规则（HTTP 422）

以下非法输入都会被拒绝，错误体统一为 `{"detail": [ {"type", "loc", "msg", ...}, ... ]}`：

- **重复桶**：`buckets[].id` 重复（`duplicate_bucket`）；
- **非法速率/容量**：`capacity <= 0`、`refill_rate < 0`、非有限数值、`initial`/`balance` 越界；
- **未知引用**：`chain` 引用了未在 `buckets` 中定义的 ID（`unknown_bucket_ref`）；
- 此外还校验：链为空、链内桶重复、`cost <= 0`、请求 ID 重复（`duplicate_request`，`/simulate` 与 `/plan-initial-balances`）、多余字段（`extra="forbid"`）。

以上规则对 `/simulate`、`/plan-initial-balances` 与 `/forecast` 同时生效（前两者共用同一请求体，按各自字段）。

## 本地调用示例

```bash
curl -X POST http://localhost:8000/simulate \
  -H 'Content-Type: application/json' \
  -d @examples/request.json

curl -X POST http://localhost:8000/forecast \
  -H 'Content-Type: application/json' \
  -d @examples/forecast.json

curl -X POST http://localhost:8000/plan-initial-balances \
  -H 'Content-Type: application/json' \
  -d @examples/plan.json
```

## 不使用 Docker 的本地开发（可选）

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试：

```bash
pip install -r requirements.txt pytest httpx
pytest -q
```

## 项目结构

```
.
├── app/
│   ├── main.py          # FastAPI 路由与异常处理
│   ├── models.py        # Pydantic 模型与输入校验
│   ├── simulation.py    # 连续补充 + 原子扣减模拟引擎
│   ├── forecast.py      # 等待时间预测引擎（不扣减状态）
│   ├── planning.py      # 初始余额规划引擎（逆序反推最低初始）
│   └── errors.py        # 跨字段校验错误类型
├── tests/               # pytest 测试（引擎语义 + HTTP）
├── examples/request.json
├── examples/forecast.json
├── examples/plan.json
├── requirements.txt
├── Dockerfile           # 依赖安装在镜像构建阶段完成
├── docker-compose.yml   # 单命令启动，映射 8000 端口
├── entrypoint.sh        # 容器启动初始化
└── README.md
```
