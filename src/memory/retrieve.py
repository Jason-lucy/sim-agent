"""P2.5 经验引擎：检索层——示例检索（中文 bigram 相似度）与规则匹配。

设计取舍（决策日志 D-008）：
- 不上 embedding：字符 bigram Jaccard 相似度零依赖、完全可解释，
  场景族锁定排队/服务类，关键词重叠足以支撑「找同类场景」；
  后续场景族扩宽再升级向量检索。
- 检索排除与查询完全同文本的案例：示例注入要验证的是「经验迁移」，
  不是「把答案抄回去」（对照评测的公平性依赖这一点）。
"""
from __future__ import annotations

from .store import ExperienceStore, normalize_scene


def _bigrams(text: str) -> set:
    t = normalize_scene(text)
    if len(t) < 2:
        return {t} if t else set()
    return {t[i:i + 2] for i in range(len(t) - 1)}


def similarity(a: str, b: str) -> float:
    """Jaccard 相似度（字符 bigram 视角）。"""
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return 0.0
    return len(ba & bb) / len(ba | bb)


def top_exemplars(store: ExperienceStore, scene_text: str, k: int = 3, min_sim: float = 0.05) -> list:
    """检索与查询场景最相似的 k 条成功案例（同文本案例排除、first_try 优先）。"""
    scored = []
    for ex in store.all_exemplars():
        if normalize_scene(ex.get("scene_text", "")) == normalize_scene(scene_text):
            continue
        s = similarity(scene_text, ex.get("scene_text", ""))
        if s >= min_sim:
            scored.append((s, 1 if ex.get("first_try") else 0, ex))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [ex for _, _, ex in scored[:k]]


def rules_for(store: ExperienceStore, scene_text: str) -> list:
    """返回与场景相关的已确认避坑提示（advice 字符串列表）。"""
    norm = normalize_scene(scene_text)
    advices = []
    for rule in store.all_rules():
        if not rule.get("confirmed"):
            continue
        for kw in rule.get("keywords", []):
            if kw and kw in norm:
                advices.append(rule["advice"])
                break
    return advices
