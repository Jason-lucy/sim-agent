"""模板渲染器 v2：按 model_type 选模板，把 spec 填进 __TOKEN__ 占位。

设计要点（decision-log D-002 / D-007）：
- 用 __TOKEN__ 占位 + 字符串替换，而不是 str.format / Jinja2——
  生成的 SimPy 代码里有大量真实花括号，format 会炸；
  M1 用零依赖方案，P2 模板多了仍保持零依赖（Jinja2 记入待议）。
- 结构化数据（阶段表/时段表）用 Python repr 内嵌为字面量，
  模板在运行时读取——模板本体保持静态，杜绝「LLM 写自由代码」。
"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = PROJECT_ROOT / "templates"

TEMPLATE_MAP = {
    "single_queue_multi_server": "mmc_queue.py.tpl",
    "finite_queue": "finite_queue.py.tpl",
    "multi_stage_line": "multi_stage_line.py.tpl",
    "peak_arrival": "peak_arrival.py.tpl",
    "priority_queue": "priority_queue.py.tpl",
}


def _fmt_num(v) -> str:
    """数值转简洁字符串：480.0 → 480，0.5 → 0.5。"""
    return f"{float(v):.6g}"


def _escape_text(s: str) -> str:
    """场景名进代码字符串字面量：防引号/换行注入。"""
    return s.replace("\\", "＼").replace('"', "'").replace("\n", " ").strip()


def build_tokens(spec) -> dict:
    """按 model_type 组装占位符 → 值 的映射。"""
    tokens = {
        "__SCENARIO_NAME__": _escape_text(spec.scenario_name),
        "__DURATION__": _fmt_num(spec.simulation.duration),
        "__SEED__": str(int(spec.simulation.random_seed)),
    }
    mt = spec.model_type

    if mt in ("single_queue_multi_server", "finite_queue", "priority_queue"):
        tokens["__MEAN_INTERVAL__"] = _fmt_num(spec.arrival_process.mean_interval)
        tokens["__MEAN_SERVICE__"] = _fmt_num(spec.service_process.mean_service_time)
        tokens["__NUM_SERVERS__"] = str(int(spec.num_servers))
    if mt == "finite_queue":
        tokens["__QUEUE_CAPACITY__"] = str(int(spec.queue_capacity))
    if mt == "priority_queue":
        tokens["__VIP_SHARE__"] = _fmt_num(spec.vip_share)
    if mt == "multi_stage_line":
        tokens["__MEAN_INTERVAL__"] = _fmt_num(spec.arrival_process.mean_interval)
        stages_data = [
            {"name": s.name, "servers": int(s.num_servers), "mean_service": float(s.mean_service_time)}
            for s in spec.stages
        ]
        tokens["__STAGES_DATA__"] = repr(stages_data)
    if mt == "peak_arrival":
        tokens["__MEAN_SERVICE__"] = _fmt_num(spec.service_process.mean_service_time)
        tokens["__NUM_SERVERS__"] = str(int(spec.num_servers))
        periods_data = [
            {"start": float(p.start_min), "end": float(p.end_min), "mean_interval": float(p.mean_interval)}
            for p in spec.peak_periods
        ]
        tokens["__PERIODS_DATA__"] = repr(periods_data)
    return tokens


def render(spec, out_path: Path) -> Path:
    """把 spec 渲染进对应模板并写出。模板由 spec.model_type 决定。"""
    template_name = TEMPLATE_MAP[spec.model_type]
    template_path = TEMPLATES_DIR / template_name
    if not template_path.exists():
        raise FileNotFoundError(f"模板不存在：{template_path}")

    code = template_path.read_text(encoding="utf-8")
    tokens = build_tokens(spec)
    for token, value in tokens.items():
        code = code.replace(token, value)

    # 残留检查：替换后不应再有 __XXX__ 占位符（检查 token 本身，不是值）
    leftover = [t for t in tokens if t in code]
    if leftover:
        raise RuntimeError(f"模板渲染后有残留占位符：{leftover}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(code, encoding="utf-8")
    return out_path
