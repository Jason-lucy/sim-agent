"""P2.5 趋势图：从 history.jsonl 画「正确率 / 耗时随版本演进」曲线。

用法（项目根目录）：python -m evals.trend
输出：evals/trend.html（P3 界面直接嵌入复用）

数据来源：
- evals/history.jsonl：每次评测追加一条（mode=full 主跑 / mode=replay-empty 空库对照）
- 首条为 P2 基线补录（来源：P2 最终 eval_report @d892795，经验引擎上线前的真实成绩）
"""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import plotly.graph_objects as go

HISTORY_JSONL = Path(__file__).resolve().parent / "history.jsonl"
OUT_HTML = Path(__file__).resolve().parent / "trend.html"


def load_history() -> list:
    rows = []
    for line in HISTORY_JSONL.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def main() -> int:
    if not HISTORY_JSONL.exists():
        print("history.jsonl 不存在：先跑 python -m evals.run_evals")
        return 1
    rows = load_history()
    if not rows:
        print("history.jsonl 为空")
        return 1

    full = [r for r in rows if r.get("mode") == "full"]
    empty = [r for r in rows if r.get("mode") == "replay-empty"]
    xs = list(range(1, len(full) + 1))
    labels = [f"{r['ts'][:16]}\n@{r.get('commit', '?')}" for r in full]
    first_rates = [r["first_try"] / r["total"] * 100 for r in full]
    pass_rates = [r["passed"] / r["total"] * 100 for r in full]
    tokens = [r.get("tokens", 0) for r in full]

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=xs, y=pass_rates, mode="lines+markers+text", name="最终通过率（含自修复）",
        text=[f"{v:.1f}%" for v in pass_rates], textposition="top center",
        line=dict(color="#2E7D32", width=2)))
    fig.add_trace(go.Scatter(
        x=xs, y=first_rates, mode="lines+markers+text", name="一次通过率",
        text=[f"{v:.1f}%" for v in first_rates], textposition="bottom center",
        line=dict(color="#1565C0", width=2, dash="dot")))
    for i, r in enumerate(full):
        if r.get("note"):
            fig.add_annotation(x=xs[i], y=pass_rates[i], text=r["note"], showarrow=True,
                               arrowhead=1, yshift=-30, font=dict(size=10, color="#888"))

    if empty:
        ex_x = [len(full) + 0.3] * len(empty)
        ex_y = [r["first_try"] / r["total"] * 100 for r in empty]
        fig.add_trace(go.Scatter(
            x=ex_x, y=ex_y, mode="markers", name="B组空库对照（一次通过率）",
            marker=dict(color="#E24B4A", size=12, symbol="diamond")))

    fig.update_layout(
        title="sim-agent 正确率演进（经验引擎 P2.5）—— 目标：随经验积累持续提升",
        xaxis=dict(title="评测轮次", tickvals=xs, ticktext=labels, tickfont=dict(size=9)),
        yaxis=dict(title="通过率 %", range=[50, 102]),
        legend=dict(orientation="h", y=1.12),
        template="plotly_white", height=480)
    fig.write_html(str(OUT_HTML), include_plotlyjs="cdn")
    print(f"趋势图已输出：{OUT_HTML}（{len(full)} 个主跑数据点 + {len(empty)} 个对照点）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
