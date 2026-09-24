"""P2.5 经验引擎单元测试：防污染闸门 / 缓存一致性与代际失效 / 相似度检索 / 坏行容错。

运行（项目根）：python -m pytest tests/test_memory.py -v
"""
import json

import pytest

from src.memory.retrieve import rules_for, similarity, top_exemplars
from src.memory.store import ExperienceStore, compute_generation, normalize_scene

SPEC_OK = {
    "model_type": "single_queue_multi_server",
    "scenario_name": "客服中心",
    "num_servers": 10,
}
RESULT_OK = {"results": {"served": 100, "avg_wait_min": 1.2, "server_utilization": 0.8}}


@pytest.fixture()
def store(tmp_path):
    return ExperienceStore(root=tmp_path / "experience", generation="gen-A")


# ---------- 防污染闸门（验收门 C） ----------

def test_gate_rejects_unvalidated(store):
    """未通过合理性验证的运行必须被闸门拦截，不得进示例层。"""
    with pytest.raises(ValueError):
        store.add_exemplar("客服中心场景", SPEC_OK, first_try=True, validated=False)
    assert store.all_exemplars() == []


def test_gate_rejects_empty_spec(store):
    assert store.add_exemplar("客服中心场景", {}, first_try=True, validated=True) is False
    assert store.add_exemplar("客服中心场景", None, first_try=True, validated=True) is False
    assert store.all_exemplars() == []


def test_gate_accepts_validated(store):
    assert store.add_exemplar("客服中心场景", SPEC_OK, first_try=True, validated=True) is True
    assert len(store.all_exemplars()) == 1


# ---------- 缓存一致性与代际失效（验收门 D） ----------

def test_cache_roundtrip(store):
    store.cache_put("客服中心 10 坐席场景", SPEC_OK, RESULT_OK)
    hit = store.cache_get("客服中心 10 坐席场景")
    assert hit is not None
    assert hit["spec"]["num_servers"] == 10
    assert store.cache_get("完全不同的场景描述") is None


def test_cache_invalidated_by_generation_change(tmp_path):
    """模板/spec schema 变化 → 代际号变化 → 同场景缓存失配（绝不返回旧代码结果）。"""
    s1 = ExperienceStore(root=tmp_path / "exp", generation="gen-A")
    s1.cache_put("客服中心场景", SPEC_OK, RESULT_OK)
    assert s1.cache_get("客服中心场景") is not None

    s2 = ExperienceStore(root=tmp_path / "exp", generation="gen-B")  # 模拟模板修改
    assert s2.cache_get("客服中心场景") is None


def test_generation_depends_on_file_content():
    g1 = compute_generation({"templates/a.py.tpl": "code v1", "src/schemas/model_spec.py": "schema"})
    g2 = compute_generation({"templates/a.py.tpl": "code v2", "src/schemas/model_spec.py": "schema"})
    g3 = compute_generation({"templates/a.py.tpl": "code v1", "src/schemas/model_spec.py": "schema"})
    assert g1 != g2
    assert g1 == g3


def test_cache_tolerates_bad_lines(store):
    """崩溃残留的半行 JSON 不得拖垮缓存读取。"""
    store.cache_put("场景甲", SPEC_OK, RESULT_OK)
    cache_file = store.root / "cache.jsonl"
    lines = cache_file.read_text(encoding="utf-8").splitlines()
    lines.append('{"fingerprint": "broken-json"')  # 半行垃圾
    cache_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert store.cache_get("场景甲") is not None


# ---------- 相似度检索（示例层与规则层） ----------

def test_similarity_ranks_same_domain_higher():
    s_hot = similarity("银行网点 4 个窗口，客户平均每 2 分钟一位，办理 5 分钟",
                       "银行网点 5 个窗口，客户平均每 3 分钟一位，办理 6 分钟")
    s_irr = similarity("银行网点 4 个窗口，客户平均每 2 分钟一位，办理 5 分钟",
                       "洗车行三道工序：吸尘、清洗、交车检查")
    assert s_hot > s_irr


def test_top_exemplars_excludes_identical_scene(store):
    """同文本案例不作为示例注入——示例注入验证的是迁移，不是抄答案。"""
    store.add_exemplar("银行网点有 4 个窗口，客户平均每 2 分钟一位", SPEC_OK, first_try=True, validated=True)
    got = top_exemplars(store, "银行网点有 4 个窗口，客户平均每 2 分钟一位", k=3)
    assert got == []


def test_top_exemplars_prefers_first_try(store):
    """相似度相同时，一次通过的案例优先作为示范。"""
    store.add_exemplar("机场值机柜台 12 个，旅客平均每 20 秒一位", dict(SPEC_OK, num_servers=12),
                       first_try=False, validated=True)
    store.add_exemplar("机场值机岛 12 个柜台，旅客平均每 20 秒到达一位", dict(SPEC_OK, num_servers=12),
                       first_try=True, validated=True)
    got = top_exemplars(store, "机场值机大厅 12 个柜台，旅客平均每 20 秒来一位", k=1)
    assert len(got) == 1
    assert got[0]["first_try"] is True


def test_rules_match_by_keyword_and_confirm_flag(store):
    store.add_rule(["vip", "优先"], "VIP 占比用 0~1 小数表示，20% 写 0.2", confirmed=False)
    store.add_rule(["高峰"], "高峰时段定义必须连续覆盖整个 duration", confirmed=True)
    assert rules_for(store, "银行高峰期窗口排队") == ["高峰时段定义必须连续覆盖整个 duration"]
    assert rules_for(store, "VIP 优先柜台") == []  # 未确认的规则不注入


# ---------- 规范化 ----------

def test_normalize_scene_ignores_punct_and_space():
    assert normalize_scene("客服中心，10 个坐席！") == normalize_scene("客服中心10个坐席")
