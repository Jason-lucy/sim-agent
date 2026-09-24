# ============================================================
# 自动生成：sim-agent (P2) · 模板 priority_queue（优先级队列，非抢占）
# 场景：__SCENARIO_NAME__
# VIP 类顾客排队优先（simpy.PriorityResource，低 priority 值先服务）
# 不可抢占：正在服务中的顾客不会被 VIP 打断
# ============================================================
import json
import random

import simpy

MEAN_INTERVAL = __MEAN_INTERVAL__   # 平均到达间隔（分钟）
MEAN_SERVICE = __MEAN_SERVICE__     # 平均服务时长（分钟）
NUM_SERVERS = __NUM_SERVERS__       # 并行服务台数量
VIP_SHARE = __VIP_SHARE__           # VIP（优先）顾客占比 0~1
SIM_DURATION = __DURATION__
RANDOM_SEED = __SEED__

stats = {
    "arrivals": 0,
    "vip": 0,
    "normal": 0,
    "served": 0,
    "vip_served": 0,
    "normal_served": 0,
    "wait_sum": 0.0,
    "wait_max": 0.0,
    "vip_wait_sum": 0.0,
    "normal_wait_sum": 0.0,
    "service_time_sum": 0.0,
    "queue_area": 0.0,
    "last_t": 0.0,
}


def customer(env, cid, resource):
    stats["arrivals"] += 1
    is_vip = random.random() < VIP_SHARE
    arrival_t = env.now
    priority = 0 if is_vip else 1  # 数值越小越优先
    cls = "vip" if is_vip else "normal"
    with resource.request(priority=priority) as req:
        yield req
        wait = env.now - arrival_t
        stats["wait_sum"] += wait
        if cls == "vip":
            stats["vip"] += 1
            stats["vip_wait_sum"] += wait
        else:
            stats["normal"] += 1
            stats["normal_wait_sum"] += wait
        if wait > stats["wait_max"]:
            stats["wait_max"] = wait
        service_start = env.now
        yield env.timeout(random.expovariate(1.0 / MEAN_SERVICE))
        stats["service_time_sum"] += env.now - service_start
        stats["served"] += 1
        if cls == "vip":
            stats["vip_served"] += 1
        else:
            stats["normal_served"] += 1


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
resource = simpy.PriorityResource(env, capacity=NUM_SERVERS)
env.process(arrivals(env, resource))
env.process(monitor(env, resource))
env.run(until=SIM_DURATION)

T = env.now
served = stats["served"]
avg_wait = stats["wait_sum"] / served if served else 0.0
utilization = stats["service_time_sum"] / (NUM_SERVERS * T) if T > 0 else 0.0
avg_queue = stats["queue_area"] / T if T > 0 else 0.0
rho = (MEAN_SERVICE / MEAN_INTERVAL) / NUM_SERVERS

vip_w = stats["vip_wait_sum"] / stats["vip_served"] if stats["vip_served"] else 0.0
normal_w = stats["normal_wait_sum"] / stats["normal_served"] if stats["normal_served"] else 0.0

result = {
    "scenario_name": "__SCENARIO_NAME__",
    "model_type": "priority_queue",
    "params": {
        "mean_interval_min": MEAN_INTERVAL,
        "mean_service_min": MEAN_SERVICE,
        "num_servers": NUM_SERVERS,
        "vip_share": VIP_SHARE,
        "duration_min": SIM_DURATION,
        "random_seed": RANDOM_SEED,
    },
    "results": {
        "sim_time_min": round(T, 2),
        "arrivals": stats["arrivals"],
        "served": served,
        "vip_share_actual": round(stats["vip"] / stats["arrivals"], 4) if stats["arrivals"] else 0.0,
        "avg_wait_min": round(avg_wait, 3),
        "avg_wait_vip_min": round(vip_w, 3),
        "avg_wait_normal_min": round(normal_w, 3),
        "max_wait_min": round(stats["wait_max"], 3),
        "server_utilization": round(utilization, 4),
        "theoretical_rho": round(rho, 4),
        "avg_queue_length": round(avg_queue, 3),
    },
    "warnings": [],
}
if utilization > 0.999 or rho >= 1.0:
    result["warnings"].append("系统不稳定：到达强度达到或超过服务能力")
if normal_w > 0 and vip_w > 0 and normal_w > 2 * vip_w and normal_w > MEAN_SERVICE:
    result["warnings"].append("优先级使普通顾客等待显著变长，注意公平性")

print("RESULT_JSON:" + json.dumps(result, ensure_ascii=False))
