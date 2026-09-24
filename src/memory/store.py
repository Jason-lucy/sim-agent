"""P2.5 经验引擎：三层记忆的存储层（JSONL 明文，随 Git 可审计）。

分区：
- cache.jsonl     缓存层：场景指纹 → 最终 spec 与结果（同场景第二次 0 次 LLM 调用）
- exemplars.jsonl 示例层：验证通过的成功案例，检索后作为 few-shot 注入抽取 prompt
- rules.jsonl     规则层：失败轨迹提炼的避坑提示（人工确认 confirmed=True 才注入）

防污染闸门（本模块是最后防线，管线层是第一道）：
- add_exemplar 必须显式传 validated=True 且 spec 非空，否则拒绝写入——
  只有通过合理性验证（sanity_check 无违规）的运行才有资格当示范
- JSONL 读取容忍坏行：崩溃残留的半行 JSON 被跳过，不拖垮整个库

缓存代际（cache generation）：
- 指纹 = sha1(规范化场景文本 + 当前代际号)。模板 / schema 任一内容变化
  → 代际号变化 → 所有旧指纹失配 → 缓存整体失效，绝不命中旧代码的结果。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 引擎版本：迭代经验机制本身时手动 +1，强制全量缓存失效
ENGINE_VERSION = "p25-1"


def normalize_scene(text: str) -> str:
    """场景文本规范化：去空白与标点、转小写。同义书写 → 同一指纹。"""
    t = re.sub(r"[\s，。；：、！？（）【】《》“”‘’…·—,\.;:!\?\(\)\[\]<>\"']+", "", text or "")
    return t.lower()


def compute_generation(files: dict) -> str:
    """由关键源文件内容计算缓存代际号。files = {相对路径: 文本内容}。"""
    h = hashlib.sha1()
    h.update(ENGINE_VERSION.encode("utf-8"))
    for rel in sorted(files):
        h.update(rel.encode("utf-8"))
        h.update((files[rel] or "").encode("utf-8"))
    return h.hexdigest()[:16]


def _collect_generation_files(root: Path) -> dict:
    """收集参与代际计算的文件：全部模板 + spec schema。"""
    files = {}
    tpl_dir = root / "templates"
    if tpl_dir.exists():
        for p in sorted(tpl_dir.glob("*.tpl")):
            files[f"templates/{p.name}"] = p.read_text(encoding="utf-8", errors="replace")
    schema = root / "src" / "schemas" / "model_spec.py"
    if schema.exists():
        files["src/schemas/model_spec.py"] = schema.read_text(encoding="utf-8", errors="replace")
    return files


class ExperienceStore:
    """经验库。root 默认项目根 experience/ 目录（进 Git）。

    只读文件系统容错（Streamlit Cloud 等环境）：root 不可写时自动降级到
    系统临时目录；临时目录也不可写则进入 disabled 模式——读取返回空、
    写入静默跳过，绝不拖垮调用方（界面照常出结果，只是不积累经验）。
    """

    def __init__(self, root: Path = None, generation: str = None):
        self.disabled = False
        self._generation = generation  # None = 自动按真实源文件计算
        try:
            self.root = Path(root) if root else PROJECT_ROOT / "experience"
            self.root.mkdir(parents=True, exist_ok=True)
            probe = self.root / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
        except OSError:
            try:
                import tempfile
                self.root = Path(tempfile.gettempdir()) / "sim-agent-experience"
                self.root.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.disabled = True
        if not self.disabled:
            for name in ("cache.jsonl", "exemplars.jsonl", "rules.jsonl"):
                try:
                    (self.root / name).touch(exist_ok=True)
                except OSError:
                    self.disabled = True
                    break

    # ---------- 基础 ----------

    @property
    def generation(self) -> str:
        if self._generation is not None:
            return self._generation
        return compute_generation(_collect_generation_files(PROJECT_ROOT))

    def fingerprint(self, scene_text: str) -> str:
        norm = normalize_scene(scene_text)
        return hashlib.sha1(f"{self.generation}|{norm}".encode("utf-8")).hexdigest()[:24]

    def _read_jsonl(self, name: str) -> list:
        if self.disabled:
            return []
        rows = []
        path = self.root / name
        if not path.exists():
            return rows
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # 坏行跳过，不拖垮整库
        return rows

    def _append(self, name: str, record: dict) -> None:
        if self.disabled:
            return
        try:
            with (self.root / name).open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 只读环境：静默放弃积累，不影响主流程

    # ---------- 缓存层 ----------

    def cache_get(self, scene_text: str) -> dict | None:
        """按指纹取缓存（同指纹取最新一条）。未命中返回 None。"""
        fp = self.fingerprint(scene_text)
        hit = None
        for row in self._read_jsonl("cache.jsonl"):
            if row.get("fingerprint") == fp:
                hit = row
        if hit is None:
            return None
        return {"spec": hit["spec"], "result": hit["result"], "saved_at": hit.get("saved_at")}

    def cache_put(self, scene_text: str, spec: dict, result: dict) -> None:
        self._append("cache.jsonl", {
            "fingerprint": self.fingerprint(scene_text),
            "generation": self.generation,
            "scene_norm": normalize_scene(scene_text)[:200],
            "spec": spec,
            "result": result,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })

    # ---------- 示例层 ----------

    def add_exemplar(self, scene_text: str, spec: dict, first_try: bool, validated: bool = False) -> bool:
        """写入成功案例。validated=True 表示该案例已通过合理性验证——防污染闸门。

        未通过验证（validated=False）或 spec 为空的写入一律拒绝。
        """
        if not validated:
            raise ValueError("防污染闸门：未通过验证的运行不得进示例层（validated=False）")
        if not spec or not isinstance(spec, dict):
            return False
        self._append("exemplars.jsonl", {
            "scene_norm": normalize_scene(scene_text)[:200],
            "scene_text": scene_text,
            "model_type": spec.get("model_type"),
            "spec": spec,
            "first_try": bool(first_try),
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        return True

    def all_exemplars(self) -> list:
        return self._read_jsonl("exemplars.jsonl")

    # ---------- 规则层 ----------

    def add_rule(self, keywords: list, advice: str, confirmed: bool = False, source: str = "") -> None:
        """写入避坑规则。confirmed=False 仅存档，不参与注入（需人工确认）。"""
        self._append("rules.jsonl", {
            "keywords": keywords,
            "advice": advice,
            "confirmed": bool(confirmed),
            "source": source,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })

    def all_rules(self) -> list:
        return self._read_jsonl("rules.jsonl")

    # ---------- 概览 ----------

    def stats(self) -> dict:
        return {
            "cache": len(self._read_jsonl("cache.jsonl")),
            "exemplars": len(self.all_exemplars()),
            "rules": len(self.all_rules()),
            "rules_confirmed": sum(1 for r in self.all_rules() if r.get("confirmed")),
            "generation": self.generation,
        }
