"""初始余额规划引擎与 HTTP 层测试：pytest 运行。"""
from __future__ import annotations

import random

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import SimulateRequest
from app.planning import plan_initial_balances
from app.simulation import simulate

client = TestClient(app)


def _plan(buckets, requests):
    return plan_initial_balances(
        SimulateRequest.model_validate({"buckets": buckets, "requests": requests})
    )


def _replay_with_planned(buckets, requests, planned):
    """用规划出的 initial 完整回放，断言全部放行并返回模拟结果。"""
    new_buckets = [dict(b) for b in buckets]
    for bucket in new_buckets:
        bucket["initial"] = planned[bucket["id"]]
    resp = simulate(
        SimulateRequest.model_validate({"buckets": new_buckets, "requests": requests})
    )
    assert resp.allowed_count == len(requests)
    assert resp.rejected_count == 0
    return resp


# ---------- 可行场景 ----------

def test_single_bucket_no_refill_sums_costs():
    # 零补充：最低初始 = 全部成本之和
    resp = _plan(
        [{"id": "A", "capacity": 10, "refill_rate": 0, "initial": 2}],
        [
            {"id": "r1", "timestamp": 0, "cost": 3, "chain": ["A"]},
            {"id": "r2", "timestamp": 5, "cost": 4, "chain": ["A"]},
        ],
    )
    assert resp.reachable is True
    assert resp.failure is None
    planned = {b.id: b.required_initial for b in resp.buckets}
    assert planned == {"A": 7}
    bucket = resp.buckets[0]
    assert bucket.requested_initial == 2
    assert bucket.delta == 5
    replay = _replay_with_planned(
        [{"id": "A", "capacity": 10, "refill_rate": 0, "initial": 2}],
        [
            {"id": "r1", "timestamp": 0, "cost": 3, "chain": ["A"]},
            {"id": "r2", "timestamp": 5, "cost": 4, "chain": ["A"]},
        ],
        planned,
    )
    # 零补充、全量扣完 -> 终态 0
    assert replay.final_balances == {"A": 0}
    assert resp.final_balances == replay.final_balances


def test_refill_before_first_event_reduces_required_initial():
    # 模拟起始时刻取最早请求时间戳：A 首次出现在 t=10（由 X 的 t=0 请求锚定起点），
    # 速率 1/s 可在首事件前预补 10（封顶 10）-> 初始 0 即可
    resp = _plan(
        [
            {"id": "A", "capacity": 10, "refill_rate": 1, "initial": 10},
            {"id": "X", "capacity": 100, "refill_rate": 0},
        ],
        [
            {"id": "r0", "timestamp": 0, "cost": 1, "chain": ["X"]},
            {"id": "r1", "timestamp": 10, "cost": 10, "chain": ["A"]},
        ],
    )
    by_id = {b.id: b for b in resp.buckets}
    assert by_id["A"].required_initial == 0
    assert by_id["A"].delta == -10
    assert by_id["X"].required_initial == 1
    assert resp.final_balances == {"A": 0, "X": 0}


def test_backward_induction_with_inter_event_refill():
    # t=0 扣 8；t=10 以 1/s 补回 10（封顶 10），故首事件后余额须 >= 0，初始 8 即可
    resp = _plan(
        [{"id": "A", "capacity": 10, "refill_rate": 1}],
        [
            {"id": "r1", "timestamp": 0, "cost": 8, "chain": ["A"]},
            {"id": "r2", "timestamp": 10, "cost": 10, "chain": ["A"]},
        ],
    )
    assert resp.buckets[0].required_initial == 8
    _replay_with_planned(
        [{"id": "A", "capacity": 10, "refill_rate": 1}],
        [
            {"id": "r1", "timestamp": 0, "cost": 8, "chain": ["A"]},
            {"id": "r2", "timestamp": 10, "cost": 10, "chain": ["A"]},
        ],
        {"A": 8},
    )


def test_partial_inter_event_refill_only_partially_covers_gap():
    # t=0 扣 8；t=2 只补 2，需再扣 7 -> 初始 8 + (7 - 2) = 13
    resp = _plan(
        [{"id": "A", "capacity": 20, "refill_rate": 1}],
        [
            {"id": "r1", "timestamp": 0, "cost": 8, "chain": ["A"]},
            {"id": "r2", "timestamp": 2, "cost": 7, "chain": ["A"]},
        ],
    )
    assert resp.buckets[0].required_initial == 13
    replay = _replay_with_planned(
        [{"id": "A", "capacity": 20, "refill_rate": 1}],
        [
            {"id": "r1", "timestamp": 0, "cost": 8, "chain": ["A"]},
            {"id": "r2", "timestamp": 2, "cost": 7, "chain": ["A"]},
        ],
        {"A": 13},
    )
    assert replay.final_balances == {"A": 0}


def test_multi_bucket_chains_each_bucket_planned_independently():
    buckets = [
        {"id": "tenant_quota", "capacity": 100, "refill_rate": 10, "initial": 100},
        {"id": "api_qps", "capacity": 10, "refill_rate": 5},
        {"id": "user_quota", "capacity": 20, "refill_rate": 1, "initial": 3},
    ]
    requests = [
        {"id": "req-1", "timestamp": 0, "cost": 4,
         "chain": ["tenant_quota", "api_qps", "user_quota"]},
        {"id": "req-2", "timestamp": 1, "cost": 4, "chain": ["tenant_quota", "api_qps"]},
        {"id": "req-3", "timestamp": 2, "cost": 4,
         "chain": ["tenant_quota", "api_qps", "user_quota"]},
    ]
    resp = _plan(buckets, requests)
    assert resp.reachable is True
    by_id = {b.id: b for b in resp.buckets}
    # tenant 三次各 4、速率 10：每段补充都能完全覆盖缺口，初始仅需 4
    # 逆序：req3=4；req2 = 4+max(0,4-10)=4；req1 = 4+max(0,4-10)=4
    assert by_id["tenant_quota"].required_initial == 4
    assert by_id["tenant_quota"].delta == -96
    # api 速率 5，每段补 5 同样完全覆盖缺口，初始 4
    assert by_id["api_qps"].required_initial == 4
    assert by_id["api_qps"].requested_initial == 10  # initial 省略 -> capacity
    assert by_id["api_qps"].delta == -6
    # user 只在 t=0、t=2 各扣 4，间隔补 2 仅覆盖 2 个缺口 -> 初始 6
    assert by_id["user_quota"].required_initial == 6
    assert by_id["user_quota"].delta == 3
    planned = {bid: b.required_initial for bid, b in by_id.items()}
    replay = _replay_with_planned(buckets, requests, planned)
    # 最低初始不保证终态为 0：事件间隔的补充可能超过下一跳缺口。
    # tenant: 4-4 -> +10-4=6 -> +10-4=12；api: 4-4 -> +5-4=1 -> +5-4=2；
    # user: 6-4=2 -> +2-4=0
    assert replay.final_balances == {"tenant_quota": 12, "api_qps": 2, "user_quota": 0}
    assert resp.final_balances == replay.final_balances


def test_same_timestamp_requests_sum_costs():
    # 同一时刻两扣各 3、零补充 -> 初始 6
    resp = _plan(
        [{"id": "A", "capacity": 10, "refill_rate": 0}],
        [
            {"id": "r1", "timestamp": 0, "cost": 3, "chain": ["A"]},
            {"id": "r2", "timestamp": 0, "cost": 3, "chain": ["A"]},
        ],
    )
    assert resp.buckets[0].required_initial == 6


def test_out_of_order_input_planned_chronologically():
    # 乱序输入仍按时间排序规划：t=0 扣 1、t=5 扣 5，速率 1/s -> 初始 1
    resp = _plan(
        [{"id": "A", "capacity": 10, "refill_rate": 1, "initial": 0}],
        [
            {"id": "r_late", "timestamp": 5, "cost": 5, "chain": ["A"]},
            {"id": "r_early", "timestamp": 0, "cost": 1, "chain": ["A"]},
        ],
    )
    assert resp.buckets[0].required_initial == 1
    assert resp.buckets[0].delta == 1


def test_unreferenced_bucket_requires_zero():
    resp = _plan(
        [
            {"id": "A", "capacity": 10, "refill_rate": 0},
            {"id": "IDLE", "capacity": 10, "refill_rate": 0, "initial": 10},
        ],
        [{"id": "r1", "timestamp": 0, "cost": 4, "chain": ["A"]}],
    )
    by_id = {b.id: b for b in resp.buckets}
    assert by_id["A"].required_initial == 4
    assert by_id["IDLE"].required_initial == 0
    assert by_id["IDLE"].delta == -10
    # 未引用桶不补充/不扣减，终态为其规划初始 0
    assert resp.final_balances["IDLE"] == 0


def test_default_initial_is_capacity_for_delta():
    # initial 省略 -> requested_initial = capacity；若无需补充则 delta 为负或零
    resp = _plan(
        [{"id": "A", "capacity": 10, "refill_rate": 1}],
        [{"id": "r1", "timestamp": 0, "cost": 3, "chain": ["A"]}],
    )
    bucket = resp.buckets[0]
    assert bucket.requested_initial == 10
    assert bucket.required_initial == 3
    assert bucket.delta == -7


def test_required_initial_within_capacity_and_non_negative():
    # 三次各 5、速率 2/s：逆序 5 -> 8 -> 11；首事件在起点 -> 初始 11，超过容量 10 -> 不可行
    resp = _plan(
        [{"id": "A", "capacity": 10, "refill_rate": 2}],
        [
            {"id": "r1", "timestamp": 0, "cost": 5, "chain": ["A"]},
            {"id": "r2", "timestamp": 1, "cost": 5, "chain": ["A"]},
            {"id": "r3", "timestamp": 2, "cost": 5, "chain": ["A"]},
        ],
    )
    assert resp.reachable is False
    assert resp.failure.request_id == "r3"
    assert resp.failure.bucket == "A"

    # 速率改为 5/s 即可行：逆序 5 -> 5 -> 5；初始 5
    resp2 = _plan(
        [{"id": "A", "capacity": 10, "refill_rate": 5}],
        [
            {"id": "r1", "timestamp": 0, "cost": 5, "chain": ["A"]},
            {"id": "r2", "timestamp": 1, "cost": 5, "chain": ["A"]},
            {"id": "r3", "timestamp": 2, "cost": 5, "chain": ["A"]},
        ],
    )
    assert resp2.reachable is True
    required = resp2.buckets[0].required_initial
    assert required == 5
    _replay_with_planned(
        [{"id": "A", "capacity": 10, "refill_rate": 5}],
        [
            {"id": "r1", "timestamp": 0, "cost": 5, "chain": ["A"]},
            {"id": "r2", "timestamp": 1, "cost": 5, "chain": ["A"]},
            {"id": "r3", "timestamp": 2, "cost": 5, "chain": ["A"]},
        ],
        {"A": required},
    )


def test_fractional_required_initial_precision():
    # A 速率 3/s，事件 t=1 扣 5、t=2 扣 5（t_start=0 由 X 锚定）：
    # 逆序 req2=5；req1: 5 + max(0, 5-3*1)=7；首事件前补 3 -> 初始 4
    resp = _plan(
        [
            {"id": "A", "capacity": 10, "refill_rate": 3},
            {"id": "X", "capacity": 100, "refill_rate": 0},
        ],
        [
            {"id": "r0", "timestamp": 0, "cost": 1, "chain": ["X"]},
            {"id": "r1", "timestamp": 1, "cost": 5, "chain": ["A"]},
            {"id": "r2", "timestamp": 2, "cost": 5, "chain": ["A"]},
        ],
    )
    assert resp.buckets[0].required_initial == pytest.approx(4, abs=1e-9)
    _replay_with_planned(
        [
            {"id": "A", "capacity": 10, "refill_rate": 3},
            {"id": "X", "capacity": 100, "refill_rate": 0},
        ],
        [
            {"id": "r0", "timestamp": 0, "cost": 1, "chain": ["X"]},
            {"id": "r1", "timestamp": 1, "cost": 5, "chain": ["A"]},
            {"id": "r2", "timestamp": 2, "cost": 5, "chain": ["A"]},
        ],
        {"A": 4, "X": 1},
    )

    # 分数结果（t_start=0 由 X 锚定）：速率 3/s，t=1 扣 5、t=2.5 扣 5
    # 逆序 req2=5；req1 = 5 + max(0, 5 - 3*1.5) = 5.5；首事件前补 3 -> 初始 2.5
    resp2 = _plan(
        [
            {"id": "B", "capacity": 10, "refill_rate": 3},
            {"id": "X", "capacity": 100, "refill_rate": 0},
        ],
        [
            {"id": "r0", "timestamp": 0, "cost": 1, "chain": ["X"]},
            {"id": "s1", "timestamp": 1, "cost": 5, "chain": ["B"]},
            {"id": "s2", "timestamp": 2.5, "cost": 5, "chain": ["B"]},
        ],
    )
    required_b = resp2.buckets[0].required_initial
    assert required_b == pytest.approx(2.5, abs=1e-9)
    _replay_with_planned(
        [
            {"id": "B", "capacity": 10, "refill_rate": 3},
            {"id": "X", "capacity": 100, "refill_rate": 0},
        ],
        [
            {"id": "r0", "timestamp": 0, "cost": 1, "chain": ["X"]},
            {"id": "s1", "timestamp": 1, "cost": 5, "chain": ["B"]},
            {"id": "s2", "timestamp": 2.5, "cost": 5, "chain": ["B"]},
        ],
        {"B": required_b, "X": 1},
    )


def test_absolute_timestamps_planned_by_elapsed_only():
    resp = _plan(
        [{"id": "A", "capacity": 10, "refill_rate": 1}],
        [
            {"id": "r1", "timestamp": 1_000_000, "cost": 10, "chain": ["A"]},
            {"id": "r2", "timestamp": 1_000_002, "cost": 2, "chain": ["A"]},
        ],
    )
    # 首事件在起始时刻 -> 初始 10；2 秒补 2 恰好覆盖第二次扣减
    assert resp.buckets[0].required_initial == 10


# ---------- 不可行场景 ----------

def test_unreachable_same_timestamp_exceeds_capacity():
    # 容量 5、同刻两扣各 4：满桶也只能承担一次
    resp = _plan(
        [
            {"id": "A", "capacity": 100, "refill_rate": 10},
            {"id": "B", "capacity": 5, "refill_rate": 5},
        ],
        [
            {"id": "req-1", "timestamp": 0, "cost": 4, "chain": ["A", "B"]},
            {"id": "req-2", "timestamp": 0, "cost": 4, "chain": ["A", "B"]},
        ],
    )
    assert resp.reachable is False
    assert resp.buckets is None
    assert resp.final_balances is None
    failure = resp.failure
    assert failure.request_id == "req-2"
    assert failure.index == 1
    assert failure.timestamp == 0
    assert failure.bucket == "B"


def test_unreachable_cost_exceeds_capacity():
    resp = _plan(
        [{"id": "A", "capacity": 10, "refill_rate": 100}],
        [{"id": "r1", "timestamp": 0, "cost": 11, "chain": ["A"]}],
    )
    assert resp.reachable is False
    assert resp.failure.request_id == "r1"
    assert resp.failure.bucket == "A"


def test_unreachable_zero_refill_late_request():
    # t=0 扣 3 可承担；t=100 零补充再扣 1 也无补充 -> 在第二个请求失败
    resp = _plan(
        [{"id": "A", "capacity": 3, "refill_rate": 0}],
        [
            {"id": "r1", "timestamp": 0, "cost": 3, "chain": ["A"]},
            {"id": "r2", "timestamp": 100, "cost": 1, "chain": ["A"]},
        ],
    )
    assert resp.reachable is False
    assert resp.failure.request_id == "r2"
    assert resp.failure.index == 1
    assert resp.failure.bucket == "A"


def test_unreachable_reports_first_insufficient_bucket_in_chain():
    # A=1 不足，C 虽也不足，但链上首个不足的是 A
    resp = _plan(
        [
            {"id": "A", "capacity": 1, "refill_rate": 0},
            {"id": "B", "capacity": 10, "refill_rate": 0},
            {"id": "C", "capacity": 1, "refill_rate": 0},
        ],
        [{"id": "r1", "timestamp": 0, "cost": 5, "chain": ["A", "B", "C"]}],
    )
    assert resp.reachable is False
    assert resp.failure.request_id == "r1"
    assert resp.failure.bucket == "A"


def test_unreachable_processing_order_uses_chronological_sort():
    # 乱序输入：按时间首个不可行的是 t=1 的 r_late，而非数组在前的 r_very_late
    resp = _plan(
        [{"id": "A", "capacity": 5, "refill_rate": 0}],
        [
            {"id": "r_very_late", "timestamp": 9, "cost": 5, "chain": ["A"]},
            {"id": "r_early", "timestamp": 0, "cost": 5, "chain": ["A"]},
            {"id": "r_late", "timestamp": 1, "cost": 1, "chain": ["A"]},
        ],
    )
    assert resp.reachable is False
    assert resp.failure.request_id == "r_late"
    assert resp.failure.index == 2
    assert resp.failure.timestamp == 1
    assert resp.failure.bucket == "A"


def test_unreachable_consumption_not_skipped_due_to_other_buckets():
    # 关键语义：即使在 /simulate 中 t=0 请求会因 B 不足而整链拒绝（A 不扣），
    # 规划时 A 的消耗仍必须全量计入 —— 三次各 4 共 12 > A 的容量 10。
    resp = _plan(
        [
            {"id": "A", "capacity": 10, "refill_rate": 0},
            {"id": "B", "capacity": 2, "refill_rate": 0},
        ],
        [
            {"id": "r1", "timestamp": 0, "cost": 4, "chain": ["A", "B"]},
            {"id": "r2", "timestamp": 1, "cost": 4, "chain": ["A", "B"]},
            {"id": "r3", "timestamp": 2, "cost": 4, "chain": ["A", "B"]},
        ],
    )
    assert resp.reachable is False
    # 首个失败请求即 r1（B 在 t=0 满桶也只有 2 < 4），定位桶 B
    assert resp.failure.request_id == "r1"
    assert resp.failure.bucket == "B"

    # 对照：若把 B 容量加大到足够，则 A 因消耗全量计入（12 > 10）在 r3 失败
    resp2 = _plan(
        [
            {"id": "A", "capacity": 10, "refill_rate": 0},
            {"id": "B", "capacity": 100, "refill_rate": 0},
        ],
        [
            {"id": "r1", "timestamp": 0, "cost": 4, "chain": ["A", "B"]},
            {"id": "r2", "timestamp": 1, "cost": 4, "chain": ["A", "B"]},
            {"id": "r3", "timestamp": 2, "cost": 4, "chain": ["A", "B"]},
        ],
    )
    assert resp2.reachable is False
    assert resp2.failure.request_id == "r3"
    assert resp2.failure.bucket == "A"


# ---------- 随机交叉验证：规划结果与模拟引擎一致 ----------

def test_random_plans_match_full_start_simulation():
    rng = random.Random(20260921)
    for _ in range(300):
        n_buckets = rng.randint(1, 3)
        buckets = []
        for j in range(n_buckets):
            buckets.append(
                {
                    "id": f"b{j}",
                    "capacity": rng.choice([1, 5, 10, 20]),
                    "refill_rate": rng.choice([0, 1, 2, 5]),
                }
            )
        n_requests = rng.randint(1, 6)
        requests = []
        for k in range(n_requests):
            chain_size = rng.randint(1, n_buckets)
            chain = rng.sample([b["id"] for b in buckets], chain_size)
            requests.append(
                {
                    "id": f"r{k}",
                    "timestamp": rng.choice([0, 0, 1, 2, 5, 10]),
                    "cost": rng.choice([1, 2, 4, 5, 11]),
                    "chain": chain,
                }
            )

        payload = SimulateRequest.model_validate(
            {"buckets": buckets, "requests": requests}
        )
        plan = plan_initial_balances(payload)

        # 对照：满桶起步 + 全量扣减的模拟（直接用引擎、initial 全满）
        full = simulate(payload)

        if plan.reachable:
            # 满桶必然也全部放行
            assert full.rejected_count == 0
            # 用规划值回放必须全部放行，且终态与规划返回一致
            planned = {b.id: b.required_initial for b in plan.buckets}
            replay = _replay_with_planned(buckets, requests, planned)
            assert plan.final_balances == replay.final_balances
            caps = {b.id: b.capacity for b in payload.buckets}
            for bucket in plan.buckets:
                assert -1e-9 <= bucket.required_initial <= caps[bucket.id] + 1e-9
        else:
            # 首个拒绝：满桶模拟中按处理顺序（时间、同刻按输入下标）的第一个被拒请求
            first_rejected = min(
                (r for r in full.results if not r.allowed),
                key=lambda r: (r.timestamp, r.index),
            )
            assert plan.failure.request_id == first_rejected.request_id
            assert plan.failure.bucket == first_rejected.rejected_by


# ---------- HTTP ----------

def test_http_plan_ok():
    response = client.post(
        "/plan-initial-balances",
        json={
            "buckets": [
                {"id": "tenant_quota", "capacity": 100, "refill_rate": 10, "initial": 100},
                {"id": "api_qps", "capacity": 10, "refill_rate": 5},
                {"id": "user_quota", "capacity": 20, "refill_rate": 1, "initial": 3},
            ],
            "requests": [
                {"id": "req-1", "timestamp": 0, "cost": 4,
                 "chain": ["tenant_quota", "api_qps", "user_quota"]},
                {"id": "req-2", "timestamp": 1, "cost": 4,
                 "chain": ["tenant_quota", "api_qps"]},
                {"id": "req-3", "timestamp": 2, "cost": 4,
                 "chain": ["tenant_quota", "api_qps", "user_quota"]},
            ],
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["reachable"] is True
    assert data["failure"] is None
    by_id = {b["id"]: b for b in data["buckets"]}
    assert by_id["tenant_quota"]["required_initial"] == 4
    assert by_id["api_qps"]["required_initial"] == 4
    assert by_id["user_quota"]["required_initial"] == 6
    assert by_id["user_quota"]["delta"] == 3
    assert data["final_balances"] == {"tenant_quota": 12.0, "api_qps": 2.0, "user_quota": 0.0}


def test_http_plan_unreachable():
    response = client.post(
        "/plan-initial-balances",
        json={
            "buckets": [{"id": "A", "capacity": 3, "refill_rate": 0}],
            "requests": [
                {"id": "r1", "timestamp": 0, "cost": 3, "chain": ["A"]},
                {"id": "r2", "timestamp": 100, "cost": 1, "chain": ["A"]},
            ],
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["reachable"] is False
    assert data["buckets"] is None
    assert data["final_balances"] is None
    assert data["failure"] == {
        "request_id": "r2",
        "index": 1,
        "timestamp": 100.0,
        "bucket": "A",
    }


def test_http_plan_validation_error_is_422():
    response = client.post(
        "/plan-initial-balances",
        json={
            "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
            "requests": [{"id": "r", "timestamp": 0, "cost": 1, "chain": ["GHOST"]}],
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(e["type"] == "unknown_bucket_ref" for e in detail)


def test_http_plan_extra_field_rejected():
    response = client.post(
        "/plan-initial-balances",
        json={
            "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
            "requests": [{"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"]}],
            "oops": 1,
        },
    )
    assert response.status_code == 422
