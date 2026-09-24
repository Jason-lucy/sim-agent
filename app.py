"""sim-agent 网页界面（P3）：自然语言 → 仿真模型 → 数字与图表。

验收标准：陌生人拿到链接，无需说明即可完成一次建模并看懂结果。
- 一键生成并运行（默认预填示例，直接点按钮即可）
- spec 参数确认表单：改完参数点「重新运行」立即见效（不经 LLM）
- 侧栏经验库概览：缓存/示例/规则条数（P2.5 经验引擎）
- 云端环境文件系统只读时，经验库自动降级为临时积累（不影响功能）

运行：streamlit run app.py
"""
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import streamlit as st

st.set_page_config(page_title="sim-agent · 自然语言仿真建模", page_icon="🔬", layout="wide")

# 云端凭据：Streamlit Cloud 的 Secrets → 环境变量（.env 不进 Git）
try:
    for _k, _v in st.secrets.items():
        os.environ.setdefault(str(_k), str(_v))
except Exception:
    pass

# 本机凭据：加载项目 .env（不覆盖已有环境变量）
from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

import pandas as pd
import plotly.graph_objects as go

from src.memory.store import ExperienceStore
from src.pipeline import run_scene, run_spec_dict, theoretical_rho_dict

RUNS_DIR = PROJECT_ROOT / "runs" / "app"

SAMPLES = {
    "客服中心（最常见）": (
        "一个客服中心，10 个坐席接听电话，客户来电平均每 30 秒一通，"
        "每通电话平均通话 4 分钟，通话时长服从指数分布。仿真一个工作日 8 小时。"
    ),
    "奶茶店（等位区有限，客满流失）": (
        "网红奶茶店有 3 个制作工位，店内最多容纳 15 位顾客排队，满员后新客直接走掉。"
        "顾客平均每 1.5 分钟来一位，每单制作平均 2 分钟（指数分布）。营业一整天 8 小时，看看流失多少客人。"
    ),
    "门诊流程（挂号→就诊→取药）": (
        "医院门诊流程：先在 2 个挂号窗口挂号（平均 3 分钟），再到 5 个诊室就诊（平均 10 分钟），"
        "最后 2 个取药窗口取药（平均 4 分钟）。患者平均每 5 分钟到达一位。运行 8 小时。"
    ),
    "早高峰地铁安检（分时段客流）": (
        "地铁早高峰有 4 条安检通道，每人过检平均 10 秒（指数分布）。"
        "7:00-7:30 为高峰，乘客平均每 4 秒来一位；7:30-8:30 平峰，平均每 12 秒一位。仿真前 90 分钟。"
    ),
    "银行 VIP 优先（优先级队列）": (
        "银行网点 4 个窗口，每位客户办理平均 8 分钟（指数分布），客户平均每 4 分钟来一位；"
        "其中 20% 是 VIP 客户，享受优先服务。营业一天 480 分钟。"
    ),
}


@st.cache_resource
def get_store() -> ExperienceStore:
    return ExperienceStore()


def result_field(res: dict, names: list, default=None):
    for n in names:
        if n in res and res[n] is not None:
            return res[n]
    return default


def make_conclusion(spec: dict, res: dict) -> list:
    """从结果生成人话结论。只引用确定存在的字段，缺失就跳过。"""
    lines = []
    util = res.get("server_utilization")
    if util is not None:
        pct = util * 100
        if util >= 0.9:
            lines.append(f"🔴 资源利用率 {pct:.0f}%，已接近饱和——任何客流波动都会造成长队。建议增加服务台或提前分流。")
        elif util >= 0.75:
            lines.append(f"🟠 资源利用率 {pct:.0f}%，偏忙。高峰时段排队会明显，可考虑在高峰期临时加人。")
        elif util >= 0.5:
            lines.append(f"🟢 资源利用率 {pct:.0f}%，运行健康，顾客等待可控。")
        else:
            lines.append(f"🔵 资源利用率 {pct:.0f}%，较空闲。若成本压力大，可评估缩减台位。")
    avg_wait = res.get("avg_wait_min")
    if avg_wait is not None:
        svc = None
        sp = spec.get("service_process") or {}
        svc = sp.get("mean_service_time")
        if svc and avg_wait > svc:
            lines.append(f"⏳ 平均等待 {avg_wait:.1f} 分钟已超过单次服务时长（{svc:.1f} 分钟），顾客体验偏差，值得优化。")
        elif avg_wait is not None:
            lines.append(f"⏳ 平均等待 {avg_wait:.1f} 分钟，低于单次服务时长，排队体验可接受。")
    served = result_field(res, ["served", "finished"])
    arrivals = result_field(res, ["arrivals", "entered"])
    if served is not None and arrivals is not None and spec.get("model_type") == "finite_queue" and served < arrivals:
        lines.append(f"🚪 共到达 {arrivals:.0f} 人、完成服务 {served:.0f} 人——约 {arrivals - served:.0f} 人因等位区满员流失，可评估扩大等位区或加快服务。")
    stages = res.get("stages")
    if stages:
        bottleneck = max(stages, key=lambda s: s.get("server_utilization", 0))
        lines.append(f"🔗 全流程瓶颈在「{bottleneck.get('name', '?')}」阶段（利用率 {bottleneck['server_utilization']:.0%}），扩容应优先扩这里而不是平均用力。")
    return lines


def util_fig(spec: dict, res: dict):
    """利用率对比图：多阶段给各阶段明细，其余给实测 vs 理论。"""
    rho = theoretical_rho_dict(spec)
    mt = spec.get("model_type")
    stages = res.get("stages")
    if stages:
        names = [s.get("name", f"阶段{i + 1}") for i, s in enumerate(stages)]
        fig = go.Figure()
        fig.add_bar(y=[s["server_utilization"] * 100 for s in stages], x=names,
                    marker_color="#185FA5", name="实测利用率")
        fig.add_scatter(y=[s["theoretical_rho"] * 100 for s in stages], x=names,
                        mode="markers", marker=dict(color="#D85A30", size=12, symbol="diamond"),
                        name="理论 ρ")
        fig.update_layout(yaxis_title="利用率 %", height=320)
        return fig
    fig = go.Figure()
    vals, labels = [], []
    u = res.get("server_utilization")
    if u is not None:
        vals.append(u * 100)
        labels.append("实测利用率")
    if rho is not None:
        vals.append(rho * 100)
        labels.append("理论 ρ（排队论）")
    fig.add_bar(y=vals, x=labels, marker_color=["#185FA5", "#888780"][: len(vals)], width=0.45)
    fig.update_layout(yaxis_title="利用率 %", height=300)
    if mt == "peak_arrival" and spec.get("peak_periods"):
        pp = spec["peak_periods"]
        rates = [60.0 / p["mean_interval"] for p in pp]
        fig2 = go.Figure()
        fig2.add_scatter(x=[f"{p['start_min'] / 60:.1f}h" for p in pp], y=rates,
                         mode="lines+markers", line=dict(color="#1D9E75", width=3))
        fig2.update_layout(yaxis_title="到达率（人/分钟）", xaxis_title="仿真时间", height=280)
        return fig, fig2
    return fig, None


def wait_fig(res: dict):
    avg = res.get("avg_wait_min")
    mx = res.get("max_wait_min")
    if avg is None and mx is None:
        return None
    stages = res.get("stages")
    if stages:
        names = [s.get("name", f"阶段{i + 1}") for i, s in enumerate(stages)]
        fig = go.Figure()
        fig.add_bar(y=[s.get("avg_wait_min", 0) for s in stages], x=names,
                    marker_color="#BA7517", name="平均等待")
        fig.add_bar(y=[s.get("max_wait_min", 0) for s in stages], x=names,
                    marker_color="#EF9F27", name="最长等待")
        fig.update_layout(barmode="group", yaxis_title="分钟", height=320)
        return fig
    fig = go.Figure()
    fig.add_bar(y=[avg or 0, mx or 0], x=["平均等待", "最长等待"],
                marker_color=["#BA7517", "#EF9F27"], width=0.45)
    fig.update_layout(yaxis_title="分钟", height=300)
    return fig


def show_results(spec: dict, result: dict, prefix: str):
    res = result["results"]
    served = result_field(res, ["served", "finished"])
    arrivals = result_field(res, ["arrivals", "entered"])
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("到达", f"{arrivals:.0f}" if arrivals is not None else "—")
    c2.metric("完成服务", f"{served:.0f}" if served is not None else "—")
    c3.metric("平均等待", f"{res.get('avg_wait_min', 0):.1f} min")
    c4.metric("最长等待", f"{res.get('max_wait_min', 0):.1f} min")
    util = res.get("server_utilization")
    c5.metric("利用率", f"{util:.0%}" if util is not None else "—")

    f1 = util_fig(spec, res)
    f2 = wait_fig(res)
    if isinstance(f1, tuple):
        f1, f_extra = f1
        if f_extra:
            st.plotly_chart(f_extra, width="stretch", key=f"{prefix}_extra")
    col_a, col_b = st.columns(2)
    with col_a:
        if f1:
            st.plotly_chart(f1, width="stretch", key=f"{prefix}_util")
    with col_b:
        if f2:
            st.plotly_chart(f2, width="stretch", key=f"{prefix}_wait")

    st.subheader("📌 结论")
    for line in make_conclusion(spec, res):
        st.markdown(f"- {line}")
    if result.get("warnings"):
        for w in result["warnings"]:
            st.warning(w)
    if result.get("params", {}).get("random_seed") is not None:
        st.caption(f"随机种子 {result['params']['random_seed']}：同参数同种子 → 结果可复现。")

    with st.expander("查看模型参数（spec JSON）"):
        st.json(spec)


def edit_spec_form(spec: dict):
    """参数确认表单：改完点重新运行，直接渲染仿真（不经 LLM）。"""
    mt = spec.get("model_type")
    edited = json.loads(json.dumps(spec))  # 深拷贝
    with st.form(f"spec_form_{mt}"):
        st.markdown("**确认 / 修改模型参数**（单位：分钟。改完点击下方按钮立即重跑）")
        r1, r2, r3 = st.columns(3)
        sim = spec.get("simulation", {})
        edited["simulation"]["duration"] = r1.number_input("仿真时长", min_value=10.0, value=float(sim.get("duration", 480)), step=30.0)
        edited["simulation"]["random_seed"] = int(r2.number_input("随机种子", min_value=0, value=int(sim.get("random_seed", 42)), step=1))
        ap = spec.get("arrival_process") or {}
        if ap:
            edited["arrival_process"]["mean_interval"] = r3.number_input("平均到达间隔", min_value=0.01, value=float(ap.get("mean_interval", 1.0)), step=0.1)

        if mt in ("single_queue_multi_server", "finite_queue", "priority_queue"):
            sp = spec.get("service_process") or {}
            a, b, c = st.columns(3)
            edited["service_process"]["mean_service_time"] = a.number_input("平均服务时长", min_value=0.01, value=float(sp.get("mean_service_time", 5.0)), step=0.5)
            edited["num_servers"] = int(b.number_input("服务台数量", min_value=1, value=int(spec.get("num_servers", 1)), step=1))
            if mt == "finite_queue":
                edited["queue_capacity"] = int(c.number_input("等位区容量", min_value=0, value=int(spec.get("queue_capacity", 10)), step=1))
            if mt == "priority_queue":
                edited["vip_share"] = c.number_input("VIP 占比（0~1）", min_value=0.0, max_value=0.95, value=float(spec.get("vip_share", 0.2)), step=0.05)
        elif mt == "multi_stage_line":
            st.markdown("各阶段参数（可增删行）")
            df = pd.DataFrame(spec.get("stages", []))
            if not df.empty:
                df = df.rename(columns={"name": "阶段名", "num_servers": "服务台数", "mean_service_time": "平均服务时长"})
            edited_df = st.data_editor(df, num_rows="dynamic", use_container_width=True, key="stages_editor")
            edited["stages"] = [
                {"name": str(r["阶段名"]), "num_servers": int(r["服务台数"]), "mean_service_time": float(r["平均服务时长"])}
                for _, r in edited_df.iterrows()
            ]
        elif mt == "peak_arrival":
            st.markdown("分时段到达参数（须从 0 开始、连续覆盖整个仿真时长）")
            df = pd.DataFrame(spec.get("peak_periods", []))
            edited_df = st.data_editor(df, num_rows="dynamic", use_container_width=True, key="periods_editor")
            edited["peak_periods"] = [
                {"start_min": float(r["start_min"]), "end_min": float(r["end_min"]), "mean_interval": float(r["mean_interval"])}
                for _, r in edited_df.iterrows()
            ]
        submitted = st.form_submit_button("🔁 用修改后的参数重新运行", type="primary", width="stretch")
    return submitted, edited


def main():
    store = get_store()
    st.session_state.setdefault("use_experience", True)
    st.session_state.setdefault("trace", None)
    st.session_state.setdefault("spec", None)

    with st.sidebar:
        st.title("🔬 sim-agent")
        st.caption("把一段中文业务描述，变成**可运行、可验证**的仿真模型。"
                   "专注排队 / 服务类场景：客服、银行、门诊、餐饮、收银……")
        st.divider()
        st.markdown("**🧠 经验引擎**")
        if st.session_state["use_experience"]:
            s = store.stats()
            st.metric("已积累成功案例", s["exemplars"])
            cc1, cc2 = st.columns(2)
            cc1.metric("缓存", s["cache"])
            cc2.metric("避坑规则", s["rules_confirmed"])
            st.caption("同场景第二次运行秒回（0 次模型调用）；"
                       "成功案例自动注入，让抽取越来越准。云端演示环境经验为临时积累，本机版永久保存。")
        else:
            st.caption("经验引擎已关闭：每次全量调用模型抽取，不读不写经验库。")
        st.toggle("使用历史经验", key="use_experience")
        st.divider()
        st.caption("⚠️ 本工具为个人作品集项目，与任何商业仿真软件无关；"
                   "结果为随机仿真估计值，重要决策请以真实数据校准。")

    st.header("用一句话，得到一个可运行的仿真模型")
    st.markdown("描述你的**排队 / 服务场景**（谁来、多久服务一次、几个台位、营业多久），剩下的交给它。")

    sample_key = st.selectbox("先选一个示例，或直接改写下框", list(SAMPLES.keys()))
    default_text = st.session_state.get("scene_text", SAMPLES[sample_key])
    scene_text = st.text_area(
        "场景描述", value=default_text, height=110,
        placeholder="例：早餐铺有 4 个收银台，顾客平均每 90 秒来一位，点单平均 2 分钟…",
    )
    if st.button("🚀 生成并运行仿真", type="primary", use_container_width=True):
        if not scene_text.strip():
            st.error("请先输入场景描述。")
        elif not os.getenv("LLM_API_KEY"):
            st.error("未配置模型 API Key：本机请复制 .env.example 为 .env 并填入；云端请在 Secrets 里配置 LLM_API_KEY / LLM_BASE_URL。")
        else:
            st.session_state["scene_text"] = scene_text
            st.session_state["trace"] = None
            st.session_state["spec"] = None
            with st.spinner("正在理解场景、抽取模型参数…（首次约 3~10 秒）"):
                use_store = store if st.session_state["use_experience"] else None
                trace = run_scene(scene_text, RUNS_DIR, "app_scene", store=use_store)
            st.session_state["trace"] = trace
            st.session_state["spec"] = trace.get("spec")

    trace = st.session_state.get("trace")
    if trace is None:
        st.info("👈 在上方输入场景（或直接用预填示例）→ 点「生成并运行仿真」。")
        return

    if trace.get("cache_hit"):
        st.success("⚡ 命中经验缓存：同场景此前跑过，本次 0 次模型调用直接返回。")
    if not trace["ok"]:
        st.error("管线未能跑通，错误信息：")
        for e in trace["errors"][-3:]:
            st.code(e[:400])
        return
    st.success(f"仿真完成（参数抽取尝试 {trace['attempts']} 次 · 自修复 {trace['repairs']} 次"
               f" · 耗时 {trace['elapsed_sec']:.1f}s）")
    show_results(trace["spec"], trace["result"], "main")

    st.divider()
    submitted, edited = edit_spec_form(trace["spec"] or {})
    if submitted:
        with st.spinner("正在按新参数重新仿真…"):
            tr2 = run_spec_dict(edited, RUNS_DIR, "app_scene_edit")
        if not tr2["ok"]:
            st.error("修改后的参数未能运行：")
            for e in tr2["errors"][-2:]:
                st.code(e[:400])
        else:
            st.session_state["trace"] = tr2
            st.session_state["spec"] = tr2["spec"]
            st.rerun()


main()
