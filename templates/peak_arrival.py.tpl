# ============================================================
# 自动生成：sim-agent (P2) · 模板 peak_arrival（分时段到达）
# 场景：__SCENARIO_NAME__
# 非齐次泊松到达，用薄化法（thinning）实现：
# 按最高到达率生成候选到达，再按各时段真实强度以概率接受
# ============================================================
import json
import random

import simpy

MEAN_SERVICE = __MEAN_SERVICE__     # 平均服务时长（分钟）
NUM_SERVERS = __NUM_SERVERS__       # 并行服务台数量
SIM_DURATION = __DURATION__
RANDOM_SEED = __SEED__

# 时段定义（由 spec 渲染生成，首尾相接覆盖全仿真时长）：
PERIODS = __PERIODS_DATA__   # [{"start","end","mean_interval"}, ...]

RATE_MAX = 1.0 / min(p["mean_interval"] for p in PERIODS)

stats = {
    "arrivals": 0,
    "served": 0,
    "wait_sum": 0.0,
    "wait_max": 0.0,
    "service_time_sum": 0.0,
    "queue_area": 0.0,
    "last_t": 0.0,
    "period": [
        {"arrivals": 0, "served": 0, "wait_sum": 0.0}
        for _ in PERIODS
    ],
}


def period_index(t):
    for i, p in enumerate(PERIODS):
        if p["start"] <= t < p["end"]:
            return i
    return len(PERIODS) - 1


def rate_at(t):
    return 1.0 / PERIODS[period_index(t)]["mean_interval"]


def customer(env, cid, resource, arrival_t, pid):
    stats["arrivals"] += 1
    stats["period"][pid]["arrivals"] += 1
    with resource.request() as req:
        yield req
        wait = env.now - arrival_t
        stats["wait_sum"] += wait
        stats["period"][pid]["wait_sum"] += wait
        if wait > stats["wait_max"]:
            stats["wait_max"] = wait
        service_start = env.now
        yield env.timeout(random.expovariate(1.0 / MEAN_SERVICE))
        stats["service_time_sum"] += env.now - service_start
        stats["served"] += 1
        stats["period"][pid]["served"] += 1


def arrivals(env, resource):
    cid = 0
    while True:
        yield env.timeout(random.expovariate(RATE_MAX))
        t = env.now
        if t >= SIM_DURATION:
            break
        pid = period_index(t)
        # 薄化：以 rate(t)/RATE_MAX 的概率接受该候选到达
        if random.random() <= rate_at(t) / RATE_MAX:
            cid += 1
            env.process(customer(env, cid, resource, t, pid))


def monitor(env, resource):
    while True:
        stats["queue_area"] += len(resource.queue) * (env.now - stats["last_t"])
        stats["last_t"] = env.now
        yield env.timeout(0.05)


random.seed(RANDOM_SEED)
env = simpy.Environment()
resource = simpy.Resource(env, capacity=NUM_SERVERS)
env.process(arrivals(env, resource))
env.process(monitor(env, resource))
env.run(until=SIM_DURATION)

T = env.now
served = stats["served"]
avg_wait = stats["wait_sum"] / served if served else 0.0
utilization = stats["service_time_sum"] / (NUM_SERVERS * T) if T > 0 else 0.0
avg_queue = stats["queue_area"] / T if T > 0 else 0.0
expected_arrivals = sum((p["end"] - p["start"]) / p["mean_interval"] for p in PERIODS)
avg_rate = expected_arrivals / T if T > 0 else 0.0
rho = MEAN_SERVICE * avg_rate / NUM_SERVERS

period_results = []
for i, p in enumerate(PERIODS):
    ps = stats["period"][i]
    period_results.append({
        "start_min": p["start"],
        "end_min": p["end"],
        "mean_interval_min": p["mean_interval"],
        "arrivals": ps["arrivals"],
        "served": ps["served"],
        "avg_wait_min": round(ps["wait_sum"] / ps["served"], 3) if ps["served"] else 0.0,
        "local_rho": round(MEAN_SERVICE / p["mean_interval"] / NUM_SERVERS, 4),
    })

result = {
    "scenario_name": "__SCENARIO_NAME__",
    "model_type": "peak_arrival",
    "params": {
        "mean_service_min": MEAN_SERVICE,
        "num_servers": NUM_SERVERS,
        "periods": PERIODS,
        "duration_min": SIM_DURATION,
        "random_seed": RANDOM_SEED,
    },
    "results": {
        "sim_time_min": round(T, 2),
        "arrivals": stats["arrivals"],
        "served": served,
        "expected_arrivals": round(expected_arrivals, 1),
        "avg_wait_min": round(avg_wait, 3),
        "max_wait_min": round(stats["wait_max"], 3),
        "server_utilization": round(utilization, 4),
        "theoretical_rho": round(rho, 4),
        "avg_queue_length": round(avg_queue, 3),
        "periods": period_results,
    },
    "warnings": [],
}
for pr in period_results:
    if pr["local_rho"] >= 1.0:
        result["warnings"].append(
            f"时段 [{pr['start_min']},{pr['end_min']}) 服务能力不足（局部 ρ={pr['local_rho']:.2f}），高峰将积压"
        )
if utilization > 0.999:
    result["warnings"].append("整体系统不稳定")

print("RESULT_JSON:" + json.dumps(result, ensure_ascii=False))
