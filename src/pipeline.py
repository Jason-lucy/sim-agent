"""端到端管线：场景文本 → spec → 代码 → 沙箱运行 → 结果（含防线③自修复循环）。

run_scene 返回完整 trace（每一步状态、尝试次数、错误信息），
供 m1_loop 和评测回归共用——单一执行路径，避免两处逻辑漂移。

P2.5 经验引擎接入（store 非空时启用）：
- 抽取前查缓存：同场景命中 → 0 次 LLM 调用直接返回（use_cache 控制）
- 抽取前检索示例与规则注入 prompt（use_experience 控制）
- 结束后按防污染闸门写回：仅「跑通且合理性验证无违规」的运行才进缓存/示例层
"""
import json
import time
from pathlib import Path

from pydantic import ValidationError

from .codegen.renderer import render
from .llm.extractor import extract_spec, repair_spec
from .memory.retrieve import rules_for, top_exemplars
from .runner.sandbox import run_generated_script
from .schemas.model_spec import ModelSpec

MAX_REPAIRS = 3


def sanity_check(result: dict) -> list:
    """运行结果合理性检查。返回违规项列表，空 = 通过。"""
    r = result["results"]
    issues = []
    util = r.get("server_utilization")
    if util is not None and util > 1.0001:
        issues.append(f"利用率 >100%：{util:.2%}")
    for key in ("avg_wait_min", "max_wait_min"):
        if key in r and r[key] < 0:
            issues.append(f"出现负等待时间（{key}）")
    if r.get("served", 0) <= 0 and r.get("results", {}).get("served", 0) <= 0:
        pass  # 模板保证有 served；这里兜底
    if "served" in r and r["served"] <= 0:
        issues.append("没有服务完成量")
    return issues


def run_scene(scene_text: str, runs_dir: Path, scene_id: str = "scene",
              store=None, use_cache: bool = True, use_experience: bool = True) -> dict:
    """执行完整管线，返回 trace：

    {
      "scene_id", "ok", "stage"（止步于哪一步）,
      "attempts"（抽取尝试次数）, "repairs"（自修复次数）,
      "spec": dict | None, "result": dict | None,
      "errors": [每步错误], "elapsed_sec",
      "cache_hit": bool, "n_exemplars": int, "n_rules": int, "tokens": int
    }

    store：ExperienceStore 实例（None = 不接经验引擎，行为与 P2 完全一致）。
    use_cache=False 可绕过缓存（对照评测用）；use_experience=False 关闭示例/规则注入。
    """
    t0 = time.time()
    trace = {
        "scene_id": scene_id,
        "ok": False,
        "stage": "extract",
        "attempts": 0,
        "repairs": 0,
        "spec": None,
        "result": None,
        "errors": [],
        "elapsed_sec": 0.0,
        "cache_hit": False,
        "n_exemplars": 0,
        "n_rules": 0,
        "tokens": 0,
    }
    runs_dir = Path(runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)

    # ⓪ 缓存层：同场景指纹命中 → 0 次 LLM 调用直接返回（代际号保证不命中旧代码结果）
    if store is not None and use_cache:
        hit = store.cache_get(scene_text)
        if hit:
            trace.update({
                "ok": True, "stage": "cache", "cache_hit": True,
                "spec": hit["spec"], "result": hit["result"],
                "elapsed_sec": time.time() - t0,
            })
            return trace

    # ⓪' 经验检索：同类型成功案例 + 避坑规则，注入抽取 prompt
    exemplars, rules = [], []
    if store is not None and use_experience:
        exemplars = top_exemplars(store, scene_text, k=3)
        rules = rules_for(store, scene_text)
        trace["n_exemplars"] = len(exemplars)
        trace["n_rules"] = len(rules)

    # ① LLM 抽取（防线①：schema 校验 + 错误回灌，内部已重试）
    ex = extract_spec(scene_text, exemplars=exemplars, rules=rules)
    trace["attempts"] = ex["attempts"]
    trace["tokens"] = ex.get("tokens", 0)
    if not ex["ok"]:
        trace["errors"].append(f"抽取失败：{ex['error']}")
        trace["elapsed_sec"] = time.time() - t0
        return trace
    spec = ex["spec"]
    trace["spec"] = spec.model_dump()

    # ② 模板渲染（防线②：模板优先，LLM 不写自由代码）
    # ③ 沙箱运行 + 失败自修复（防线③：报错回灌 LLM 修 spec，≤MAX_REPAIRS 次）
    current_spec = spec
    for repair_round in range(MAX_REPAIRS + 1):  # 首轮 + 至多 3 次修复
        try:
            script = render(current_spec, runs_dir / f"{scene_id}.py")
            trace["stage"] = "run"
        except Exception as e:
            err = f"渲染失败：{e}"
            trace["errors"].append(err)
            rep = repair_spec(current_spec, err)
            if rep["ok"] and repair_round < MAX_REPAIRS:
                trace["repairs"] += 1
                current_spec = rep["spec"]
                continue
            trace["elapsed_sec"] = time.time() - t0
            return trace

        run = run_generated_script(script, timeout_sec=120)
        if run["ok"]:
            trace["ok"] = True
            trace["stage"] = "done"
            trace["spec"] = current_spec.model_dump()
            trace["result"] = run["result"]
            issues = sanity_check(run["result"])
            if issues:
                trace["ok"] = True  # 能跑，但合理性有问题——交给上层断言判定
                trace["sanity_issues"] = issues

            # 经验写回（防污染闸门：跑通 且 合理性验证无违规 才有资格入库）
            if store is not None and not issues:
                spec_dict = current_spec.model_dump()
                store.cache_put(scene_text, spec_dict, run["result"])
                first_try = (trace["attempts"] == 1 and trace["repairs"] == 0)
                store.add_exemplar(scene_text, spec_dict, first_try=first_try, validated=True)

            trace["elapsed_sec"] = time.time() - t0
            return trace

        err = run.get("error", "未知运行错误")
        if run.get("stderr_tail"):
            err += "\n" + run["stderr_tail"]
        trace["errors"].append(f"第 {repair_round + 1} 次运行失败：{err[:500]}")

        if repair_round >= MAX_REPAIRS:
            trace["stage"] = "run"
            trace["elapsed_sec"] = time.time() - t0
            return trace
        rep = repair_spec(current_spec, err)
        if not rep["ok"]:
            trace["stage"] = "run"
            trace["errors"].append(f"自修复失败：{rep['error'][:300]}")
            trace["elapsed_sec"] = time.time() - t0
            return trace
        trace["repairs"] += 1
        current_spec = rep["spec"]
        trace["spec"] = current_spec.model_dump()

    trace["elapsed_sec"] = time.time() - t0
    return trace


def theoretical_rho(spec) -> float:
    """按排队论粗算整体利用率（到达强度/服务能力），供评测断言对照。

    multi_stage 取各阶段最大值；peak_arrival 用时间加权平均到达率。
    """
    mt = spec.model_type
    if mt in ("single_queue_multi_server", "finite_queue", "priority_queue"):
        return (spec.service_process.mean_service_time / spec.arrival_process.mean_interval) / spec.num_servers
    if mt == "multi_stage_line":
        interval = spec.arrival_process.mean_interval
        return max((s.mean_service_time / interval) / s.num_servers for s in spec.stages)
    if mt == "peak_arrival":
        dur = spec.simulation.duration
        expected_arrivals = sum((p.end_min - p.start_min) / p.mean_interval for p in spec.peak_periods)
        avg_rate = expected_arrivals / dur
        return (spec.service_process.mean_service_time * avg_rate) / spec.num_servers
    return float("nan")


def theoretical_rho_dict(spec: dict) -> float | None:
    """theoretical_rho 的 dict 版（app 界面与趋势分析复用；解析失败返回 None）。"""
    mt = spec.get("model_type")
    try:
        if mt in ("single_queue_multi_server", "finite_queue", "priority_queue"):
            return (spec["service_process"]["mean_service_time"] / spec["arrival_process"]["mean_interval"]) / spec["num_servers"]
        if mt == "multi_stage_line":
            interval = spec["arrival_process"]["mean_interval"]
            return max((s["mean_service_time"] / interval) / s["num_servers"] for s in spec["stages"])
        if mt == "peak_arrival":
            dur = spec["simulation"]["duration"]
            exp_arr = sum((p["end_min"] - p["start_min"]) / p["mean_interval"] for p in spec["peak_periods"])
            return (spec["service_process"]["mean_service_time"] * (exp_arr / dur)) / spec["num_servers"]
    except (TypeError, KeyError, ZeroDivisionError):
        return None
    return None


def run_spec_dict(spec_dict: dict, runs_dir: Path, scene_id: str = "scene") -> dict:
    """从已有 spec dict 直接「渲染→运行」（P3 界面「改参数重跑」路径，不经 LLM）。

    trace 结构与 run_scene 一致；不写经验库（改参重跑属人工探索，不作为示范素材）。
    """
    t0 = time.time()
    trace = {
        "scene_id": scene_id, "ok": False, "stage": "render",
        "attempts": 1, "repairs": 0, "spec": None, "result": None,
        "errors": [], "elapsed_sec": 0.0,
        "cache_hit": False, "n_exemplars": 0, "n_rules": 0, "tokens": 0,
    }
    runs_dir = Path(runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    try:
        spec = ModelSpec.model_validate(spec_dict)
    except ValidationError as e:
        trace["errors"].append(f"修改后的参数未通过 schema 校验：{e}")
        trace["elapsed_sec"] = time.time() - t0
        return trace
    trace["spec"] = spec.model_dump()
    try:
        script = render(spec, runs_dir / f"{scene_id}.py")
        trace["stage"] = "run"
    except Exception as e:
        trace["errors"].append(f"渲染失败：{e}")
        trace["elapsed_sec"] = time.time() - t0
        return trace
    run = run_generated_script(script, timeout_sec=120)
    if run["ok"]:
        trace["ok"] = True
        trace["stage"] = "done"
        trace["result"] = run["result"]
        issues = sanity_check(run["result"])
        if issues:
            trace["sanity_issues"] = issues
    else:
        err = run.get("error", "未知运行错误")
        if run.get("stderr_tail"):
            err += "\n" + run["stderr_tail"]
        trace["errors"].append(f"运行失败：{err[:500]}")
    trace["elapsed_sec"] = time.time() - t0
    return trace
