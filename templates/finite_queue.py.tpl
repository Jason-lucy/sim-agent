# ============================================================
# 自动生成：sim-agent (P2) · 模板 finite_queue（有限队列 M/M/c/K）
# 场景：__SCENARIO_NAME__
# 等位区容量 K：排满后新到顾客直接离开（流失）
# ============================================================
import json
import random

import simpy

MEAN_INTERVAL = __MEAN_INTERVAL__   # 平均到达间隔（分钟）
MEAN_SERVICE = __MEAN_SERVICE__     # 平均服务时长（分钟）
NUM_SERVERS = __NUM_SERVERS__       # 并行服务台数量
QUEUE_CAPACITY = __QUEUE_CAPACITY__ # 等位区容量（不含正在服务的）
SIM_DURATION = __DURATION__
RANDOM_SEED = __SEED__

stats = {
    "arrivals": 0,
    "balked": 0,
    "served": 0,
    "wait_sum": 0.0,
    "wait_max": 0.0,
    "service_time_sum": 0.0,
    "queue_area": 0.0,
    "last_t": 0.0,
}


def customer(env, cid, resource):
    stats["arrivals"] += 1
    # 到达瞬间检查等位区：满则直接流失（M/M/c/K 近似，P3 改进点已记录）
    if len(resource.queue) >= QUEUE_CAPACITY:
        stats["balked"] += 1
        return
    arrival_t = env.now
    with resource.request() as req:
        yield req
        wait = env.now - arrival_t
        stats["wait_sum"] += wait
        if wait > stats["wait_max"]:
            stats["wait_max"] = wait
        service_start = env.now
        yield env.timeout(random.expovariate(1.0 / MEAN_SERVICE))
        stats["service_time_sum"] += env.now - service_start
        stats["served"] += 1


def arrivals(env, resource):
    cid = 0
    while True:
        yield env.timeout(random.expovariate(1.0 / MEAN_INTERVAL))
        cid += 1
        env.process(customer(env, cid, resource))


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
balked = stats["balked"]
avg_wait = stats["wait_sum"] / served if served else 0.0
utilization = stats["service_time_sum"] / (NUM_SERVERS * T) if T > 0 else 0.0
avg_queue = stats["queue_area"] / T if T > 0 else 0.0
loss_rate = balked / stats["arrivals"] if stats["arrivals"] else 0.0
rho = (MEAN_SERVICE / MEAN_INTERVAL) / NUM_SERVERS

result = {
    "scenario_name": "__SCENARIO_NAME__",
    "model_type": "finite_queue",
    "params": {
        "mean_interval_min": MEAN_INTERVAL,
        "mean_service_min": MEAN_SERVICE,
        "num_servers": NUM_SERVERS,
        "queue_capacity": QUEUE_CAPACITY,
        "duration_min": SIM_DURATION,
        "random_seed": RANDOM_SEED,
    },
    "results": {
        "sim_time_min": round(T, 2),
        "arrivals": stats["arrivals"],
        "served": served,
        "balked": balked,
        "loss_rate": round(loss_rate, 4),
        "arrival_rate_per_min": round(stats["arrivals"] / T, 4) if T > 0 else 0.0,
        "avg_wait_min": round(avg_wait, 3),
        "max_wait_min": round(stats["wait_max"], 3),
        "server_utilization": round(utilization, 4),
        "theoretical_rho": round(rho, 4),
        "avg_queue_length": round(avg_queue, 3),
    },
    "warnings": [],
}
if loss_rate > 0.3:
    result["warnings"].append(f"顾客流失率较高：{loss_rate:.1%}，建议增加服务台或等位容量")
if utilization > 0.999 or rho >= 1.0:
    result["warnings"].append("系统不稳定：到达强度达到或超过服务能力")
if served > 0 and avg_wait > MEAN_SERVICE:
    result["warnings"].append("平均等待超过平均服务时长，顾客体验可能较差")

print("RESULT_JSON:" + json.dumps(result, ensure_ascii=False))
