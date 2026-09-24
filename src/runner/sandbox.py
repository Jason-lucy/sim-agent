"""沙箱执行器：子进程运行生成脚本，捕获超时/崩溃，解析 RESULT_JSON。

当前形态是「受控子进程 + 超时」的轻量沙箱。P2/P3 若开放用户自定义代码，
再升级为资源受限的执行方案（记入 decision-log 待议）。
"""
import json
import subprocess
import sys
from pathlib import Path

RESULT_MARKER = "RESULT_JSON:"


def run_generated_script(script_path: Path, timeout_sec: int = 120) -> dict:
    """运行生成脚本，返回 {"ok": True, "result": dict} 或 {"ok": False, "error": str, ...}。"""
    env = {"PYTHONIOENCODING": "utf-8", "PATH": ""}
    try:
        proc = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
            cwd=str(script_path.parent),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"执行超时（超过 {timeout_sec}s），已终止"}
    except Exception as e:
        return {"ok": False, "error": f"子进程启动失败：{e}"}

    if proc.returncode != 0:
        return {
            "ok": False,
            "error": f"脚本执行失败（退出码 {proc.returncode}）",
            "stderr_tail": proc.stderr[-2000:] if proc.stderr else "",
        }

    # 从后往前找 RESULT_JSON 标记行（最后一条结果优先）
    for line in reversed((proc.stdout or "").splitlines()):
        if line.startswith(RESULT_MARKER):
            try:
                return {"ok": True, "result": json.loads(line[len(RESULT_MARKER):])}
            except json.JSONDecodeError as e:
                return {"ok": False, "error": f"结果 JSON 解析失败：{e}"}
    return {"ok": False, "error": "输出中未找到 RESULT_JSON 标记", "stdout_tail": (proc.stdout or "")[-1000:]}
