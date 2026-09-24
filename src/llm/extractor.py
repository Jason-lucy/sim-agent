"""LLM 层：自然语言 → 合法 ModelSpec（防线①）+ spec 自修复（防线③后半）。

流程：
- extract_spec：LLM(JSON mode) → Pydantic 校验 → 不过则把校验错误回灌 LLM 重试
- repair_spec：脚本运行/渲染失败后，把「当前 spec + 报错」回灌 LLM 修正 spec
"""
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import ValidationError

from ..schemas.model_spec import ModelSpec

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_SYSTEM_PROMPT = """你是一个离散事件仿真建模助手。把用户的中文业务场景描述转换为一个 JSON 对象，只输出 JSON，不要输出任何其他文字。

第一步：根据场景特征选择 model_type（五选一）：
- "single_queue_multi_server"：一组并行服务台 + 一个排队队列（默认，最常见）
- "finite_queue"：队列容量有限，排满后新到者直接离开（场景提到"最多容纳/等位区上限/最多排 N 人"）
- "multi_stage_line"：顾客要依次经过 2~4 个环节办理（场景提到"先…再…最后…"的连续流程）
- "peak_arrival"：到达强度分时段变化（场景提到"高峰/平峰/开门后一段时间人多"）
- "priority_queue"：部分客户有优先权（场景提到"VIP/金卡/老人/头等舱优先"）

第二步：按 model_type 输出对应字段（一律单位=分钟）：

公共字段（所有类型必填）：
  "scenario_name": 简短中文场景名, "simulation": {"duration": 分钟数, "random_seed": 42, "replications": 1}

- single_queue_multi_server：
  "arrival_process": {"distribution": "exponential", "mean_interval": 平均到达间隔},
  "service_process": {"distribution": "exponential", "mean_service_time": 平均服务时长},
  "num_servers": 并行服务台数
- finite_queue：同上，另加 "queue_capacity": 排队容量上限（正整数）
- priority_queue：同 single_queue_multi_server，另加 "vip_share": 优先客户占比（0~1 小数，如 20% 写 0.2）
- multi_stage_line：
  "arrival_process": 同上,
  "stages": [{"name": "阶段名", "num_servers": 该阶段服务台数, "mean_service_time": 该阶段平均服务时长}, …]
  （stages 2~4 个，严格按流程先后排序；每个阶段独立给服务台数和服务时长）
- peak_arrival：
  "service_process": 同上, "num_servers": 同上,
  "peak_periods": [{"start_min": 开始分钟, "end_min": 结束分钟, "mean_interval": 该时段平均到达间隔}, …]
  （时段 2~4 个，必须从 0 开始、首尾相接、连续覆盖整个 duration，不允许缝隙或重叠）

换算规则：
- 所有时间统一为分钟："每 30 秒" → 0.5；"每 20 秒" → 0.3333；"每小时来 60 位" → 1.0；"每分钟来 4 位" → 0.25
- duration：半天 = 240，一个工作日/8 小时 = 480；用户明确说"营业 X 小时"就换算
- 分布只支持 exponential；用户说"平均 X 分钟"就是指数均值，直接使用
- 场景描述的是"每位顾客都要依次经过各环节"才用 multi_stage_line；只是"每个窗口独立接待"就是 single_queue_multi_server
- 如果缺少某项信息，按行业常识补全最合理的值，并在 scenario_name 开头加"[假设]"标记
- 不要发明结构里没有的字段；数字一律用数值类型，不要加单位或文字"""


def _get_client() -> OpenAI:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("LLM_BASE_URL")
    api_key = os.getenv("LLM_API_KEY")
    if not base_url or not api_key:
        raise RuntimeError("缺少 LLM 配置：请复制 .env.example 为 .env 并填入 LLM_BASE_URL / LLM_API_KEY")
    return OpenAI(base_url=base_url, api_key=api_key)


def _strip_code_fences(text: str) -> str:
    """防御：即使 JSON mode 也有极小概率返回 markdown 代码块包裹。"""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text else text
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return text


def _chat_json(client: OpenAI, model: str, user_content: str, temperature: float) -> tuple:
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": user_content}],
        response_format={"type": "json_object"},
        temperature=temperature,
    )
    content = _strip_code_fences(resp.choices[0].message.content or "")
    tokens = getattr(resp.usage, "total_tokens", 0) if resp.usage else 0
    return content, tokens


def _build_experience_blocks(exemplars: list, rules: list) -> str:
    """把检索到的成功案例与避坑规则拼成 prompt 附加段（P2.5 经验注入）。"""
    blocks = ""
    if exemplars:
        parts = []
        for i, ex in enumerate(exemplars, 1):
            spec_json = json.dumps(ex["spec"], ensure_ascii=False)
            parts.append(f"示例{i}场景：{ex['scene_text']}\n示例{i}验证正确的 spec：\n{spec_json}")
        blocks += ("\n\n以下是历史上同类场景被验证正确的示例——仅供学习结构选择、单位换算口径与字段组织，"
                   "不要照抄示例里的数字：\n\n" + "\n\n".join(parts))
    if rules:
        blocks += "\n\n注意事项（来自历史失败案例的教训，务必遵守）：\n" + "\n".join(f"- {r}" for r in rules)
    return blocks


def extract_spec(scene_text: str, max_attempts: int = 3, temperature: float = 0.2,
                 exemplars: list = None, rules: list = None) -> dict:
    """把一段中文场景描述抽取为合法 ModelSpec。

    exemplars / rules：经验引擎注入项（成功案例 few-shot / 避坑提示），可为 None。
    返回 {"ok": True, "spec": ModelSpec, "attempts": n, "tokens": int}
      或 {"ok": False, "error": str, "attempts": n, "tokens": int}
    """
    client = _get_client()
    model = os.getenv("LLM_MODEL", "deepseek-chat")
    feedback = ""
    last_error = ""
    exp_block = _build_experience_blocks(exemplars or [], rules or [])
    tokens_used = 0

    for attempt in range(1, max_attempts + 1):
        user_content = f"{_SYSTEM_PROMPT}{exp_block}\n\n用户场景：{scene_text}"
        if feedback:
            user_content += f"\n\n{feedback}"
        try:
            raw, tk = _chat_json(client, model, user_content, temperature)
            tokens_used += tk
            data = json.loads(raw)
            spec = ModelSpec.model_validate(data)
            return {"ok": True, "spec": spec, "attempts": attempt, "tokens": tokens_used}
        except json.JSONDecodeError as e:
            last_error = f"输出不是合法 JSON：{e}"
        except ValidationError as e:
            last_error = f"JSON 未通过 schema 校验：{e}"
        except Exception as e:  # LLM 调用本身失败（网络/配额等）
            return {"ok": False, "error": f"LLM 调用失败：{e}", "attempts": attempt, "tokens": tokens_used}
        feedback = f"上一次输出未通过校验，错误：{last_error}。请修正后重新输出完整 JSON（仍然只输出 JSON）。"

    return {"ok": False, "error": f"重试 {max_attempts} 次仍未通过校验，最后错误：{last_error}", "attempts": max_attempts, "tokens": tokens_used}


def repair_spec(spec: ModelSpec, error: str, max_attempts: int = 2) -> dict:
    """防线③后半：脚本运行/渲染失败后，把当前 spec + 报错回灌 LLM 修正。

    返回 {"ok": True, "spec": ModelSpec} 或 {"ok": False, "error": str}
    """
    client = _get_client()
    model = os.getenv("LLM_MODEL", "deepseek-chat")
    feedback = ""
    last_error = error

    for attempt in range(1, max_attempts + 1):
        user_content = (
            f"{_SYSTEM_PROMPT}\n\n"
            f"用户场景对应的当前 spec JSON 如下，但它导致生成的仿真脚本运行失败。\n"
            f"当前 spec：\n{spec.model_dump_json(indent=2)}\n\n"
            f"失败信息：\n{error}\n\n"
            f"请分析失败原因（最常见：参数取值不合理、时段/阶段定义与 duration 矛盾、数值类型错误），"
            f"输出修正后的完整 JSON spec。仍然只输出 JSON。"
        )
        if feedback:
            user_content += f"\n\n{feedback}"
        try:
            raw, _tk = _chat_json(client, model, user_content, temperature=0.1)
            data = json.loads(raw)
            new_spec = ModelSpec.model_validate(data)
            return {"ok": True, "spec": new_spec}
        except json.JSONDecodeError as e:
            last_error = f"输出不是合法 JSON：{e}"
        except ValidationError as e:
            last_error = f"JSON 未通过 schema 校验：{e}"
        except Exception as e:
            return {"ok": False, "error": f"LLM 调用失败：{e}"}
        feedback = f"上一次修正输出仍未通过校验，错误：{last_error}。请重新输出完整 JSON。"

    return {"ok": False, "error": f"自修复 {max_attempts} 次仍未通过校验，最后错误：{last_error}"}
