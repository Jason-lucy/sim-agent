"""P2/P2.5 一键回归：跑 30 条评测集，输出通过率。

用法（项目根目录）：
  python -m evals.run_evals                 # 满库模式：30 条全跑，经验引擎开启（缓存+示例注入+写回）
  python -m evals.run_evals --replay-empty  # 空库对照：只重跑 8 条易错场景（B 组），无经验无缓存

验收标准（P2）：一次通过率 ≥70%；自修复后最终通过率 ≥90%。
验收标准（P2.5 验收门 B）：B 组满库一次通过率 ≥ 空库对照。
每条场景的断言：
  A1 管线跑通（抽取→渲染→运行，含自修复）
  A2 model_type 正确
  A3 关键参数与期望一致（数值 rel≤5%；整数精确；vip_share abs≤0.05）
  A4 结果合理性（利用率≤100%、等待≥0、有服务完成量）
  A5 利用率对齐理论值：容差 = max(0.10, 2.5σ)（见 decision-log D-007）
"""
import json
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from src.memory.store import ExperienceStore
from src.pipeline import run_scene, theoretical_rho

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCENES_FILE = Path(__file__).resolve().parent / "scenes.json"
REPORT_TXT = Path(__file__).resolve().parent / "eval_report.txt"
REPORT_JSON = Path(__file__).resolve().parent / "eval_report.json"
REPORT_EMPTY_TXT = Path(__file__).resolve().parent / "eval_report_empty.txt"
REPORT_EMPTY_JSON = Path(__file__).resolve().parent / "eval_report_empty.json"
HISTORY_JSONL = Path(__file__).resolve().parent / "history.jsonl"
GIT_EXE = r"C:\Users\Hr\.workbuddy\binaries\PortableGit\versions\1.2.0\cmd\git.exe"

# B 组：8 条历史易错场景（P2 四轮迭代中出现过 FAIL/A5 边缘失败），用于经验对照
HARD_SCENE_IDS = ["E07", "E13", "E14", "E17", "E19", "E23", "E26", "E28"]

TOL = {"numeric_rel": 0.05, "vip_share_abs": 0.05, "util_vs_rho_abs": 0.10}


def close(a, b, rel=TOL["numeric_rel"]) -> bool:
    return abs(float(a) - float(b)) <= rel * max(abs(float(b)), 1e-9)


def check_scene(entry: dict, trace: dict) -> tuple:
    """返回 (passed, [失败断言描述])。"""
    if not trace["ok"]:
        return False, [f"A1 管线未跑通（stage={trace['stage']}）: {trace['errors'][-1][:180]}"]

    spec = trace["spec"]
    res = trace["result"]["results"]
    failures = []
    exp = entry["expect"]

    # A2 model_type
    if spec["model_type"] != entry["model_type"]:
        failures.append(f"A2 model_type={spec['model_type']}，期望 {entry['model_type']}")

    # A3 参数解析正确性
    if "num_servers" in exp and spec.get("num_servers") != exp["num_servers"]:
        failures.append(f"A3 num_servers={spec.get('num_servers')}，期望 {exp['num_servers']}")
    if "mean_interval" in exp:
        got = spec["arrival_process"]["mean_interval"]
        if not close(got, exp["mean_interval"]):
            failures.append(f"A3 mean_interval={got}，期望 {exp['mean_interval']}")
    if "mean_service" in exp:
        got = spec["service_process"]["mean_service_time"]
        if not close(got, exp["mean_service"]):
            failures.append(f"A3 mean_service={got}，期望 {exp['mean_service']}")
    if not close(spec["simulation"]["duration"], exp["duration"], 0.01):
        failures.append(f"A3 duration={spec['simulation']['duration']}，期望 {exp['duration']}")
    if "queue_capacity" in exp and spec.get("queue_capacity") != exp["queue_capacity"]:
        failures.append(f"A3 queue_capacity={spec.get('queue_capacity')}，期望 {exp['queue_capacity']}")
    if "vip_share" in exp:
        got = spec.get("vip_share")
        if got is None or abs(got - exp["vip_share"]) > TOL["vip_share_abs"]:
            failures.append(f"A3 vip_share={got}，期望 {exp['vip_share']}")
    if "stages" in exp:
        stages = spec.get("stages") or []
        if len(stages) != len(exp["stages"]):
            failures.append(f"A3 阶段数={len(stages)}，期望 {len(exp['stages'])}")
        else:
            for i, (es, got_stage) in enumerate(zip(exp["stages"], stages)):
                if got_stage["num_servers"] != es[0] or not close(got_stage["mean_service_time"], es[1]):
                    failures.append(
                        f"A3 阶段{i + 1}={got_stage['num_servers']}×{got_stage['mean_service_time']}min，期望 {es[0]}×{es[1]}min"
                    )
    if "period_count" in exp:
        periods = spec.get("peak_periods") or []
        if len(periods) != exp["period_count"]:
            failures.append(f"A3 时段数={len(periods)}，期望 {exp['period_count']}")
        else:
            if abs(periods[0]["start_min"]) > 0.01:
                failures.append(f"A3 第一时段 start={periods[0]['start_min']}，期望 0")
            if not close(periods[-1]["end_min"], exp["duration"], 0.01):
                failures.append(f"A3 末时段 end={periods[-1]['end_min']}，期望 ≈{exp['duration']}")
            if "period_intervals" in exp:
                for i, (ei, gp) in enumerate(zip(exp["period_intervals"], periods)):
                    if not close(gp["mean_interval"], ei):
                        failures.append(f"A3 时段{i + 1} mean_interval={gp['mean_interval']}，期望 {ei}")

    # A4 结果合理性
    util = res.get("server_utilization", 0.0)
    if util > 1.0001:
        failures.append(f"A4 利用率 {util:.2%} > 100%")
    if res.get("avg_wait_min", 0) < 0 or res.get("max_wait_min", 0) < 0:
        failures.append("A4 出现负等待时间")
    served = res.get("served", 0) if "served" in res else res.get("finished", 0)
    if served <= 0:
        failures.append("A4 无服务完成量")

    # A5 利用率对齐理论值（自适应容差）
    # 容差 = max(0.10, 2.5σ)。σ=利用率估计的噪声量级（见 _util_noise_sd）。
    # 大样本场景（期望到达多）σ 小，仍严格卡 10pp；小样本场景按 2.5σ 放宽，
    # 避免断言本身产生统计误报（实测教训见 decision-log D-007 修订）。
    rho = theoretical_rho_from_dict(spec)
    expected_arrivals = _expected_arrivals(spec)
    if rho is not None and rho < 0.9 and expected_arrivals >= 60:
        tol = max(TOL["util_vs_rho_abs"], 2.5 * _util_noise_sd(spec))
        if abs(util - rho) > tol:
            failures.append(f"A5 利用率 {util:.3f} 与理论 rho={rho:.3f} 偏差 {abs(util - rho):.3f} 超过容差 {tol:.3f}")

    return (len(failures) == 0), failures


def theoretical_rho_from_dict(spec: dict):
    mt = spec["model_type"]
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


def _expected_arrivals(spec: dict) -> float:
    """期望到达数（A5 统计适用门槛用）。"""
    try:
        mt = spec["model_type"]
        if mt == "peak_arrival":
            return sum((p["end_min"] - p["start_min"]) / p["mean_interval"] for p in spec["peak_periods"])
        return spec["simulation"]["duration"] / spec["arrival_process"]["mean_interval"]
    except (TypeError, KeyError, ZeroDivisionError):
        return 0.0


def _util_noise_sd(spec: dict) -> float:
    """利用率估计的噪声量级（一阶近似）：

    σ ≈ E[S]·√(2n) / (c·T)，n=期望到达数。
    因子 2 合并了到达数实现波动与服务时长抽样的联合噪声。
    """
    try:
        mt = spec["model_type"]
        dur = spec["simulation"]["duration"]
        if mt == "multi_stage_line":
            interval = spec["arrival_process"]["mean_interval"]
            stage = max(spec["stages"], key=lambda s: (s["mean_service_time"] / interval) / s["num_servers"])
            c, s_mean = stage["num_servers"], stage["mean_service_time"]
        else:
            c = spec["num_servers"]
            s_mean = spec["service_process"]["mean_service_time"]
        n = _expected_arrivals(spec)
        if n <= 0 or c <= 0 or dur <= 0:
            return 0.0
        return s_mean * (2 * n) ** 0.5 / (c * dur)
    except (TypeError, KeyError, ZeroDivisionError):
        return 0.0


def _git_commit() -> str:
    try:
        out = subprocess.run([GIT_EXE, "rev-parse", "--short", "HEAD"], cwd=PROJECT_ROOT,
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _append_history(mode: str, total: int, passed: int, first_try: int, elapsed: float, tokens: int, note: str = "") -> None:
    rec = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": mode,
        "commit": _git_commit(),
        "total": total,
        "passed": passed,
        "first_try": first_try,
        "elapsed_sec": round(elapsed, 1),
        "tokens": tokens,
    }
    if note:
        rec["note"] = note
    with HISTORY_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _run_batch(scenes: list, runs_dir: Path, store, report_txt: Path, report_json: Path,
               title: str, threshold_line: bool = True) -> dict:
    """跑一批场景并写报告。返回汇总 dict。"""
    lines = [f"{title} · {time.strftime('%Y-%m-%d %H:%M:%S')} · 共 {len(scenes)} 条", "=" * 70]
    if store is not None:
        st = store.stats()
        lines.append(f"经验库：缓存 {st['cache']} 条 · 示例 {st['exemplars']} 条 · 规则 {st['rules_confirmed']}/{st['rules']} 条确认")
    else:
        lines.append("经验引擎：关闭（无缓存、无示例注入、无写回）")
    rows = []
    n_ok = n_first_try = n_tokens = 0
    t0 = time.time()

    for entry in scenes:
        sid = entry["id"]
        trace = run_scene(entry["text"], runs_dir, f"eval_{sid}", store=store)
        passed, failures = check_scene(entry, trace)
        first_try = passed and trace["attempts"] == 1 and trace["repairs"] == 0
        n_ok += int(passed)
        n_first_try += int(first_try)
        n_tokens += trace.get("tokens", 0)
        res = trace.get("result", {}).get("results", {}) if trace["ok"] else {}
        row = {
            "id": sid,
            "passed": passed,
            "first_try": first_try,
            "attempts": trace["attempts"],
            "repairs": trace["repairs"],
            "cache_hit": trace.get("cache_hit", False),
            "n_exemplars": trace.get("n_exemplars", 0),
            "model_type": trace["spec"]["model_type"] if trace["spec"] else None,
            "util": res.get("server_utilization"),
            "rho": theoretical_rho_from_dict(trace["spec"]) if trace["spec"] else None,
            "avg_wait_min": res.get("avg_wait_min"),
            "failures": failures,
        }
        rows.append(row)
        mark = "PASS" if passed else "FAIL"
        extra = "" if passed else " ｜ " + "; ".join(failures[:2])
        exp_info = f" exp={row['n_exemplars']}" if store is not None else ""
        cache_info = " ⚡缓存" if row["cache_hit"] else ""
        lines.append(f"[{mark}] {sid} {row['model_type']} util={row['util']}{cache_info}{exp_info} "
                     f"attempts={row['attempts']} repairs={row['repairs']}{extra}")

    total = len(scenes)
    pass_rate = n_ok / total
    first_rate = n_first_try / total
    elapsed = time.time() - t0
    lines.append("=" * 70)
    lines.append(f"管线跑通：{sum(1 for r in rows if r['passed'])}/{total}")
    lines.append(f"一次通过率：{n_first_try}/{total} = {first_rate:.1%}")
    lines.append(f"最终通过率（含自修复）：{n_ok}/{total} = {pass_rate:.1%}")
    if threshold_line:
        lines.append(f"P2 验收线：一次 ≥70%，最终 ≥90%")
    lines.append(f"总耗时 {elapsed:.0f}s · LLM tokens {n_tokens}")
    verdict = "通过" if (pass_rate >= 0.9 and first_rate >= 0.7) else "未通过"
    if threshold_line:
        lines.append(f"P2 验收判定：{verdict}")

    report = "\n".join(lines)
    report_txt.write_text(report + "\n", encoding="utf-8")
    report_json.write_text(json.dumps(
        {"total": total, "passed": n_ok, "first_try": n_first_try,
         "pass_rate": pass_rate, "first_try_rate": first_rate,
         "elapsed_sec": elapsed, "tokens": n_tokens, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"total": total, "passed": n_ok, "first_try": n_first_try, "elapsed": elapsed, "tokens": n_tokens, "rows": rows}


def main() -> int:
    data = json.loads(SCENES_FILE.read_text(encoding="utf-8"))
    scenes = data["scenes"]
    runs_dir = PROJECT_ROOT / "runs" / "evals"
    replay_empty = "--replay-empty" in sys.argv

    if replay_empty:
        # 空库对照：只跑 B 组易错场景，无经验无缓存（store=None = P2 原始行为）
        hard = [s for s in scenes if s["id"] in HARD_SCENE_IDS]
        summary = _run_batch(hard, runs_dir, store=None,
                             report_txt=REPORT_EMPTY_TXT, report_json=REPORT_EMPTY_JSON,
                             title=f"sim-agent P2.5 空库对照（B 组 {len(hard)} 条易错场景）", threshold_line=False)

        # 从满库报告读取 B 组成绩对比
        compare = ["B 组经验对照（满库 = 主跑，空库 = 本次）", "-" * 46]
        try:
            full = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
            full_rows = {r["id"]: r for r in full["rows"] if r["id"] in HARD_SCENE_IDS}
            empty_rows = {r["id"]: r for r in summary["rows"]}
            full_first = sum(1 for r in full_rows.values() if r["first_try"])
            empty_first = sum(1 for r in empty_rows.values() if r["first_try"])
            full_pass = sum(1 for r in full_rows.values() if r["passed"])
            empty_pass = sum(1 for r in empty_rows.values() if r["passed"])
            for sid in HARD_SCENE_IDS:
                fr, er = full_rows.get(sid, {}), empty_rows.get(sid, {})
                mark = "↑" if (er.get("first_try") and not fr.get("first_try")) else ("↓" if (fr.get("first_try") and not er.get("first_try")) else "=")
                fr_s = "一次通过" if fr.get("first_try") else ("修复通过" if fr.get("passed") else "失败")
                er_s = "一次通过" if er.get("first_try") else ("修复通过" if er.get("passed") else "失败")
                compare.append(f"  {sid}: 满库 {fr_s} | 空库 {er_s} {mark}")
            compare.append("-" * 46)
            compare.append(f"B 组一次通过率：满库 {full_first}/{len(HARD_SCENE_IDS)} vs 空库 {empty_first}/{len(HARD_SCENE_IDS)}")
            compare.append(f"B 组最终通过率：满库 {full_pass}/{len(HARD_SCENE_IDS)} vs 空库 {empty_pass}/{len(HARD_SCENE_IDS)}")
            verdict_b = "通过（满库 ≥ 空库）" if full_first >= empty_first else "未通过（满库 < 空库，需检查示例质量）"
            compare.append(f"P2.5 验收门 B 判定：{verdict_b}")
            compare.append("")
        except FileNotFoundError:
            compare.append("（未找到满库报告 eval_report.json，先跑 python -m evals.run_evals）")

        report = summary  # 供 history
        empty_report_txt = REPORT_EMPTY_TXT.read_text(encoding="utf-8")
        (REPORT_EMPTY_TXT).write_text(empty_report_txt + "\n".join(compare) + "\n", encoding="utf-8")
        print("\n".join(compare))
        _append_history("replay-empty", summary["total"], summary["passed"], summary["first_try"],
                        summary["elapsed"], summary["tokens"], note="B 组空库对照")
        return 0 if "通过（满库" in verdict_b else 1

    # 满库主跑：30 条全跑，经验引擎全开
    store = ExperienceStore()
    summary = _run_batch(scenes, runs_dir, store=store,
                         report_txt=REPORT_TXT, report_json=REPORT_JSON,
                         title=f"sim-agent P2/P2.5 评测回归（满库，经验引擎开启）")
    st = store.stats()
    print(f"经验库现状：缓存 {st['cache']} 条 · 示例 {st['exemplars']} 条 · 规则 {st['rules_confirmed']}/{st['rules']} 条确认 · 代际 {st['generation']}")
    _append_history("full", summary["total"], summary["passed"], summary["first_try"],
                    summary["elapsed"], summary["tokens"])
    rows = summary["rows"]
    return 0 if (summary["passed"] / summary["total"] >= 0.9 and summary["first_try"] / summary["total"] >= 0.7) else 1


if __name__ == "__main__":
    sys.exit(main())
