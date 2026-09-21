"""初始余额规划：反推使全部请求放行所需的最低桶初始余额。

请求体沿用 /simulate（buckets + requests），处理顺序与连续补充、
原子扣减语义也与模拟引擎一致，但规划时所有链上消耗都按目标全量计入：
每个桶独立承担其参与的每一次请求的 cost，不允许因别的桶暂时不足而跳过。

单桶可行性等价于：满桶起步、且参与的每次放行扣减都实际发生时，
能否承担自身全部事件。是否存在可行初始余额只需满桶回放即可判定
（满桶是初始余额的上界）；按处理顺序找到首个被拒的请求，
其 rejected_by（链上第一个不足的桶）即定位桶。

可行时对每个桶的事件序列（时间 t_k、成本 c_k）逆序归纳，
得到末事件扣减后恰好为 0 的最低初始余额：

    req_n = c_n
    req_k = c_k + max(0, req_{k+1} - refill_rate * (t_{k+1} - t_k))

封顶 capacity 只会让余额更少；满桶回放可行意味着归纳每一步
req_k ≤ capacity。首事件之前的补充同样封顶，所需初始余额为：

    initial = clamp(0, req_1 - refill_rate * (t_1 - t_start), capacity)

未被任何请求引用的桶 required_initial = 0。
"""
from __future__ import annotations

from app.models import (
    PlanFailure,
    PlannedBucket,
    PlanResponse,
    SimulateRequest,
)
from app.simulation import _round, simulate


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _full_start_first_failure(payload: SimulateRequest) -> PlanFailure | None:
    """满桶起步、按现有模拟语义回放，找出按处理顺序首个被拒的请求。

    满桶是初始余额的上限，因此满桶回放中被拒的请求不存在任何可行的初始
    余额组合；规划视角下各桶的链上消耗均须全量计入，而满桶模拟本就对
    每次放行执行扣减，首个被拒请求的 rejected_by 即定位桶。
    """
    # 强制所有桶以满桶起步（忽略请求中给定的 initial）
    full_payload = payload.model_copy(
        update={
            "buckets": [
                bucket.model_copy(update={"initial": float(bucket.capacity)})
                for bucket in payload.buckets
            ]
        }
    )
    full = simulate(full_payload)
    first_rejected = min(
        (result for result in full.results if not result.allowed),
        key=lambda result: (result.timestamp, result.index),
        default=None,
    )
    if first_rejected is None:
        return None
    return PlanFailure(
        request_id=first_rejected.request_id,
        index=first_rejected.index,
        timestamp=first_rejected.timestamp,
        bucket=first_rejected.rejected_by,
    )


def plan_initial_balances(payload: SimulateRequest) -> PlanResponse:
    if not payload.buckets:
        raise ValueError("buckets 不能为空")
    if not payload.requests:
        raise ValueError("requests 不能为空")

    t_start = min(request.timestamp for request in payload.requests)

    capacity = {b.id: float(b.capacity) for b in payload.buckets}
    refill_rate = {b.id: float(b.refill_rate) for b in payload.buckets}
    requested_initial = {
        b.id: float(b.capacity if b.initial is None else b.initial) for b in payload.buckets
    }

    # 收集每个桶参与的事件：(时间, 成本)，按处理顺序追加（请求已可乱序输入）
    events: dict[str, list[tuple[float, float]]] = {b.id: [] for b in payload.buckets}
    order = sorted(
        range(len(payload.requests)),
        key=lambda i: (payload.requests[i].timestamp, i),
    )
    for i in order:
        request = payload.requests[i]
        for bucket_id in request.chain:
            events[bucket_id].append((request.timestamp, request.cost))

    failure = _full_start_first_failure(payload)
    if failure is not None:
        return PlanResponse(
            reachable=False,
            failure=failure,
            buckets=None,
            final_balances=None,
        )

    required: dict[str, float] = {}
    for bucket_id, bucket_events in events.items():
        if not bucket_events:
            # 未参与任何请求：无需任何初始令牌
            required[bucket_id] = 0.0
            continue

        # 逆序归纳：末事件扣到 0，逐事件叠加“补充无法覆盖的缺口”
        needed = bucket_events[-1][1]
        for k in range(len(bucket_events) - 2, -1, -1):
            t_k, cost_k = bucket_events[k]
            t_next = bucket_events[k + 1][0]
            recoverable = refill_rate[bucket_id] * (t_next - t_k)
            needed = cost_k + max(0.0, needed - recoverable)

        # 首事件之前的连续补充可覆盖一部分（封顶 capacity）
        first_time = bucket_events[0][0]
        initial = needed - refill_rate[bucket_id] * (first_time - t_start)
        required[bucket_id] = _clamp(initial, 0.0, capacity[bucket_id])

    # 输出舍入（9 位），误差远小于 1e-6；再夹回 [0, capacity] 防止尾差越界
    planned = {
        bucket_id: _clamp(_round(required[bucket_id]), 0.0, capacity[bucket_id])
        for bucket_id in required
    }

    # 以规划值替换请求 initial，完整回放得到终态余额
    replay_payload = payload.model_copy(
        update={
            "buckets": [
                bucket.model_copy(update={"initial": planned[bucket.id]})
                for bucket in payload.buckets
            ]
        }
    )
    replay = simulate(replay_payload)
    # 规划可行时回放必须全部放行（否则即为规划器缺陷）
    if replay.rejected_count != 0:
        raise RuntimeError("初始余额规划回放出现被拒绝的请求")

    bucket_results = [
        PlannedBucket(
            id=bucket.id,
            requested_initial=_round(requested_initial[bucket.id]),
            required_initial=planned[bucket.id],
            delta=_round(planned[bucket.id] - requested_initial[bucket.id]),
        )
        for bucket in payload.buckets
    ]

    return PlanResponse(
        reachable=True,
        failure=None,
        buckets=bucket_results,
        final_balances=replay.final_balances,
    )
