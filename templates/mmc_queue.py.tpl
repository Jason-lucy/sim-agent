# ============================================================
# 自动生成：sim-agent (M1) · 模板 mmc_queue（单队列多服务台 M/M/c）
# 场景：__SCENARIO_NAME__
# 由 spec 驱动生成，请勿手改本文件的参数——修改请改 spec 后重新生成
# ============================================================
import json
import random

import simpy

MEAN_INTERVAL = __MEAN_INTERVAL__   # 平均到达间隔（分钟）
MEAN_SERVICE = __MEAN_SERVICE__     # 平均服务时长（分钟）
NUM_SERVERS = __NUM_SERVERS__       # 并行服务台数量
SIM_DURATION = __DURATION__         # 仿真时长（分钟）
RANDOM_SEED = __SEED__

stats = {
    "arrivals": 0,
    "served": 0,
    "wait_sum": 0.0,
    "wait_max": 0.0,
    "service_time_sum": 0.0,
    "queue_area": 0.0,
    "last_t": 0.0,
}


def customer(env, cid, resource):
    stats["arrivals"] += 1
    arrival_t = env.now
    with resource.request() as req:  # FIFO 排队
        yield req
        wait = env.now - arrival_t
        stats["wait_sum"] += wait
        if wait > stats["wait_max"]:
            stats["wait_max"] = wait
        service_start = env.now
        yield env.timeout(random.expovariate(1.0 / MEAN_SERVICE))
        # env.run 停止时在服务的顾客只累计到当前时刻的忙时（轻微低估利用率，P2 改进）
        stats["service_time_sum"] += env.now - service_start
        stats["served"] += 1


def arrivals(env, resource):
    cid = 0
    while True:
        yield env.timeout(random.expovariate(1.0 / MEAN_INTERVAL))
        cid += 1
        env.process(customer(env, cid, resource))


def monitor(env, resource):
    # 时间加权队列长度采样（步长 0.05 分钟）
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
arrival_rate = stats["arrivals"] / T if T > 0 else 0.0
rho = (MEAN_SERVICE / MEAN_INTERVAL) / NUM_SERVERS  # 理论利用率（到达强度/服务能力）

result = {
    "scenario_name": "__SCENARIO_NAME__",
    "model_type": "single_queue_multi_server",
    "params": {
        "mean_interval_min": MEAN_INTERVAL,
        "mean_service_min": MEAN_SERVICE,
        "num_servers": NUM_SERVERS,
        "duration_min": SIM_DURATION,
        "random_seed": RANDOM_SEED,
    },
    "results": {
        "sim_time_min": round(T, 2),
        "arrivals": stats["arrivals"],
        "served": served,
        "unfinished_at_end": stats["arrivals"] - served,
        "arrival_rate_per_min": round(arrival_rate, 4),
        "avg_wait_min": round(avg_wait, 3),
        "max_wait_min": round(stats["wait_max"], 3),
        "server_utilization": round(utilization, 4),
        "theoretical_rho": round(rho, 4),
        "avg_queue_length": round(avg_queue, 3),
    },
    "warnings": [],
}
if utilization > 0.999 or rho >= 1.0:
    result["warnings"].append("系统不稳定：到达强度达到或超过服务能力，队列将无限增长，结果不代表稳态")
if served > 0 and avg_wait > MEAN_SERVICE:
    result["warnings"].append("平均等待超过平均服务时长，顾客体验可能较差")

print("RESULT_JSON:" + json.dumps(result, ensure_ascii=False))
