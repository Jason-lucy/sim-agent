# ============================================================
# 自动生成：sim-agent (P2) · 模板 multi_stage_line（多阶段服务链）
# 场景：__SCENARIO_NAME__
# 顾客 100% 顺流经过每个阶段（分流/返工逻辑属 P3+ 范围）
# ============================================================
import json
import random

import simpy

MEAN_INTERVAL = __MEAN_INTERVAL__   # 平均到达间隔（分钟）
SIM_DURATION = __DURATION__
RANDOM_SEED = __SEED__

# 阶段定义（由 spec 渲染生成）：[{"name","servers","mean_service"}, ...]
STAGES = __STAGES_DATA__

stats = {
    "stage": [
        {"arrivals": 0, "served": 0, "wait_sum": 0.0, "wait_max": 0.0,
         "service_time_sum": 0.0, "queue_area": 0.0, "last_t": 0.0}
        for _ in STAGES
    ],
    "entered": 0,
    "finished": 0,
    "system_time_sum": 0.0,
    "system_time_max": 0.0,
}


def customer(env, cid, resources):
    stats["entered"] += 1
    arrival_t = env.now
    for i, stage in enumerate(STAGES):
        st = stats["stage"][i]
        st["arrivals"] += 1
        arrive_stage_t = env.now
        with resources[i].request() as req:
            yield req
            wait = env.now - arrive_stage_t
            st["wait_sum"] += wait
            if wait > st["wait_max"]:
                st["wait_max"] = wait
            service_start = env.now
            yield env.timeout(random.expovariate(1.0 / stage["mean_service"]))
            st["service_time_sum"] += env.now - service_start
            st["served"] += 1
    stats["finished"] += 1
    sys_time = env.now - arrival_t
    stats["system_time_sum"] += sys_time
    if sys_time > stats["system_time_max"]:
        stats["system_time_max"] = sys_time


def arrivals(env, resources):
    cid = 0
    while True:
        yield env.timeout(random.expovariate(1.0 / MEAN_INTERVAL))
        cid += 1
        env.process(customer(env, cid, resources))


def make_monitor(env, resources, i):
    def monitor():
        while True:
            stats["stage"][i]["queue_area"] += len(resources[i].queue) * (env.now - stats["stage"][i]["last_t"])
            stats["stage"][i]["last_t"] = env.now
            yield env.timeout(0.05)
    return monitor


random.seed(RANDOM_SEED)
env = simpy.Environment()
resources = [simpy.Resource(env, capacity=s["servers"]) for s in STAGES]
env.process(arrivals(env, resources))
for i in range(len(STAGES)):
    env.process(make_monitor(env, resources, i)())
env.run(until=SIM_DURATION)

T = env.now
stage_results = []
max_rho = 0.0
for i, stage in enumerate(STAGES):
    st = stats["stage"][i]
    util = st["service_time_sum"] / (stage["servers"] * T) if T > 0 else 0.0
    rho = (stage["mean_service"] / MEAN_INTERVAL) / stage["servers"]
    max_rho = max(max_rho, rho)
    stage_results.append({
        "name": stage["name"],
        "servers": stage["servers"],
        "mean_service_min": stage["mean_service"],
        "arrivals": st["arrivals"],
        "served": st["served"],
        "avg_wait_min": round(st["wait_sum"] / st["served"], 3) if st["served"] else 0.0,
        "max_wait_min": round(st["wait_max"], 3),
        "server_utilization": round(util, 4),
        "theoretical_rho": round(rho, 4),
        "avg_queue_length": round(st["queue_area"] / T, 3) if T > 0 else 0.0,
    })

finished = stats["finished"]
result = {
    "scenario_name": "__SCENARIO_NAME__",
    "model_type": "multi_stage_line",
    "params": {
        "mean_interval_min": MEAN_INTERVAL,
        "stages": STAGES,
        "duration_min": SIM_DURATION,
        "random_seed": RANDOM_SEED,
    },
    "results": {
        "sim_time_min": round(T, 2),
        "entered": stats["entered"],
        "finished": finished,
        "unfinished_at_end": stats["entered"] - finished,
        "avg_system_time_min": round(stats["system_time_sum"] / finished, 3) if finished else 0.0,
        "max_system_time_min": round(stats["system_time_max"], 3),
        "server_utilization": max(s["server_utilization"] for s in stage_results),
        "theoretical_rho": round(max_rho, 4),
        "stages": stage_results,
    },
    "warnings": [],
}
for s in stage_results:
    if s["theoretical_rho"] >= 1.0 or s["server_utilization"] > 0.999:
        result["warnings"].append(f"阶段「{s['name']}」服务能力不足（ρ={s['theoretical_rho']:.2f}），将成为整条链的瓶颈")
if finished > 0 and result["results"]["avg_system_time_min"] > sum(s["mean_service"] for s in STAGES) * 2:
    result["warnings"].append("平均在系统时间超过各阶段服务时长总和的 2 倍，排队可能过长")

print("RESULT_JSON:" + json.dumps(result, ensure_ascii=False))
