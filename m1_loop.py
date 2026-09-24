"""P1 最小闭环（已切换到统一管线 src/pipeline.py，含自修复）。

验收标准（大纲）：3 个硬编码场景 ≥1 个跑出合理数字；全流程无人工改代码。
用法：python m1_loop.py　结果写入 runs/m1_report.txt
"""
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from src.memory.store import ExperienceStore
from src.pipeline import run_scene

PROJECT_ROOT = Path(__file__).resolve().parent
RUNS_DIR = PROJECT_ROOT / "runs"

SCENES = [
    {
        "id": "A",
        "text": "一个客服中心，10 个坐席接听电话，客户来电平均每 30 秒一通，"
                "每通电话平均通话 4 分钟，通话时长服从指数分布。仿真一个工作日 8 小时。",
    },
    {
        "id": "B",
        "text": "银行网点有 4 个服务窗口，每位客户平均办理 5 分钟，办理时间服从指数分布，"
                "客户平均每 2 分钟到达一位。模拟营业 480 分钟。",
    },
    {
        "id": "C",
        "text": "医院挂号处有 3 个窗口，每位患者挂号平均需要 6 分钟（指数分布），"
                "患者平均每 3.5 分钟到达一位。运行 8 小时看看排队情况。",
    },
]


def main() -> int:
    RUNS_DIR.mkdir(exist_ok=True)
    store = ExperienceStore()  # P2.5：日常运行也积累经验（缓存 + 成功案例写回）
    stats0 = store.stats()
    lines = [f"sim-agent M1 最小闭环（统一管线版） · {time.strftime('%Y-%m-%d %H:%M:%S')}", "=" * 62]
    lines.append(f"经验库：缓存 {stats0['cache']} 条 · 示例 {stats0['exemplars']} 条 · 规则 {stats0['rules_confirmed']}/{stats0['rules']} 条确认")
    passed = 0

    for scene in SCENES:
        sid, text = scene["id"], scene["text"]
        lines += ["", f"【场景 {sid}】{text}", "-" * 62]
        trace = run_scene(text, RUNS_DIR, f"scene_{sid}", store=store)

        if trace.get("cache_hit"):
            lines.append("  ⚡ 缓存命中：0 次 LLM 调用，直接返回历史结果")
            r = trace["result"]["results"]
            lines.append(f"     平均等待 {r['avg_wait_min']} min | 利用率 {r['server_utilization']:.1%}")
            passed += 1
            continue

        if not trace["ok"]:
            lines.append(f"  ✗ 管线失败（stage={trace['stage']}，attempts={trace['attempts']}，repairs={trace['repairs']}）")
            for e in trace["errors"][-2:]:
                lines.append(f"    {e[:200]}")
            continue

        spec, result = trace["spec"], trace["result"]
        params = result["params"]
        issues = trace.get("sanity_issues", [])
        status = "PASS" if not issues else "FAIL"
        passed += int(not issues)
        r = result["results"]
        lines.append(f"  ① spec 抽取成功（第 {trace['attempts']} 次尝试）：{spec['scenario_name']}")
        lines.append(f"  ② 代码生成 + ③ 沙箱运行成功（自修复 {trace['repairs']} 次）")
        lines.append(f"  ④ 验收[{status}] 到达={r.get('arrivals')} 完成={r.get('served', r.get('finished'))}")
        lines.append(f"     平均等待 {r['avg_wait_min']} min（最长 {r['max_wait_min']} min）"
                     f" | 利用率 {r['server_utilization']:.1%}（理论 ρ={r['theoretical_rho']}）")
        for w in result["warnings"]:
            lines.append(f"     ⚠ {w}")
        for issue in issues:
            lines.append(f"     ✗ {issue}")

    lines += ["", "=" * 62]
    verdict = "通过" if passed >= 1 else "未通过"
    lines.append(f"P1 验收：{passed}/{len(SCENES)} 场景跑出合理数字 —— 判定：{verdict}")
    report = "\n".join(lines)
    (RUNS_DIR / "m1_report.txt").write_text(report + "\n", encoding="utf-8")
    print(report)
    return 0 if passed >= 1 else 1


if __name__ == "__main__":
    sys.exit(main())
