"""P3 评测框架冒烟测试:数据集结构 + mock 基线跑通 + 报告自描述。"""
import json
from pathlib import Path

import pytest

EVAL_DIR = Path(__file__).resolve().parents[1] / "eval"
DATASET = EVAL_DIR / "dataset.json"
DATASET_EXT = EVAL_DIR / "dataset_dev_ext.json"

# 冻结集必须仍有 dev/test 两份;扩充集只允许 dev —— 新数据不得进入已冻结的 test,
# 否则"未在 test 上调参"的声明(D-63)当场失效。
_EXPECT = {
    DATASET: {"dev", "test"},
    DATASET_EXT: {"dev"},
}


def _validate(data, path):
    splits = {m["split"] for m in data["meetings"]}
    assert splits == _EXPECT[path], f"{path.name} 的 split 不该是 {splits}"
    assert data["split_by"] == "meeting"

    categories = set()
    for m in data["meetings"]:
        assert m["corpus"], "会谈必须有语料"
        assert m.get("corrected_corpus"), f"{m['id']} 缺人工校对语料(隔离 ASR 误差用)"
        assert len(m["corrected_corpus"]) == len(m["corpus"]), (
            "校对语料条数须与语料一致(校对不改时轴)"
        )
        assert m.get("permission_probes"), f"{m['id']} 缺越权探针"
        for p in m["permission_probes"]:
            assert p["kind"] in {
                "cross_org", "same_org_plain_user", "whitelisted"
            }, p
            assert p["expect_status"] in (200, 404), p
        for q in m["questions"]:
            if q.get("no_answer"):
                categories.add(q["category"])
                assert q.get("note") or "无答案" in q["category"], (
                    f"{q['qid']}: 判为无答案要写明为什么(避免把'我没写到'当成'不该有答案')"
                )
                continue
            assert q["answer_clue"] and q["evidence_text"], q
            # 金标必须真在语料里:否则"未命中"根本无从判断是检索问题还是标注错
            lines = [t for _, t in m["corpus"]] + [t for _, t in m["corrected_corpus"]]
            assert any(q["evidence_text"] in ln for ln in lines), (
                f"{q['qid']}: evidence_text「{q['evidence_text']}」在语料中找不到逐字出处"
            )
            assert any(q["answer_clue"] in ln for ln in lines), (
                f"{q['qid']}: answer_clue「{q['answer_clue']}」在语料中找不到逐字出处"
            )
    assert any("型号错配" in c for c in categories), categories
    assert any("无答案" in c for c in categories), categories
    assert any("日期归属" in c for c in categories), categories
    probes = [p for m in data["meetings"] for p in m["permission_probes"]]
    assert any(p["expect_status"] == 404 for p in probes), "必须有越权被拒样本"
    assert any(p["expect_status"] == 200 for p in probes), "必须有正向对照,防止全 404 掩盖问题"
    # 否定反例:答案本身是一句"不支持/不含"式的否定,必须至少有一题
    negation = [
        q for m in data["meetings"] for q in m["questions"]
        if not q.get("no_answer")
        and ("否定" in q.get("category", "")
             or any(k in q.get("answer_clue", "") for k in ("不支持", "不含", "不能")))
    ]
    assert negation, f"{path.name} 缺否定类可解答题(否定最容易被模型答成正面承诺)"


@pytest.mark.parametrize("path", [DATASET, DATASET_EXT], ids=["frozen", "dev_ext"])
def test_dataset_structure_and_counterexamples(path):
    """两份评测集:按会谈划分、覆盖四类反例(型号错配/否定/日期归属/无答案),
    且每道可答题的金标都能在语料里逐字找到。"""
    _validate(json.loads(path.read_text(encoding="utf-8")), path)


def test_dev_ext_does_not_touch_frozen_ids():
    """扩充集不得复用冻结集的会谈/题目 id,也不得含 test 会谈:
    否则同名会被覆盖,历史基线报告就不再可比。"""
    frozen = json.loads(DATASET.read_text(encoding="utf-8"))
    ext = json.loads(DATASET_EXT.read_text(encoding="utf-8"))
    fm = {m["id"] for m in frozen["meetings"]}
    em = {m["id"] for m in ext["meetings"]}
    assert not (fm & em), fm & em
    fq = {q["qid"] for m in frozen["meetings"] for q in m["questions"]}
    eq = {q["qid"] for m in ext["meetings"] for q in m["questions"]}
    assert not (fq & eq), fq & eq
    assert all(m["split"] != "test" for m in ext["meetings"])
    assert all(m.get("corpus_origin") == "authored_fictional" for m in ext["meetings"]), \
        "自造语料必须逐条标来源,防止被误当成真实录音质量"


def test_frozen_dataset_short_question_still_present():
    """扩充集的动机是短疑问句;留一条结构断言确保这类题不会在后续编辑中消失。"""
    ext = json.loads(DATASET_EXT.read_text(encoding="utf-8"))
    shorts = [q["question"] for m in ext["meetings"] for q in m["questions"]
              if not q.get("no_answer") and len(q["question"]) <= 6]
    assert len(shorts) >= 8, shorts
    assert "保修几年?" in shorts


def test_eval_mock_baseline_report(tmp_path):
    """跑通完整评测(mock 基线):报告结构、版本记录、mock 单列声明。

    不断言具体指标数值——mock 基线的指标本身是被测量对象,
    已知反例误报按 D-32 如实记录。
    """
    from eval.run_eval import main

    out = tmp_path / "report.json"
    report = main(["--out", str(out)])
    assert out.exists()

    meta = report["meta"]
    assert meta["is_mock"] is True
    assert "mock" in meta["declaration"]
    assert "不用于声称真实效果提升" in meta["declaration"]
    # 版本记录:模型/提示词/数据/检索参数/耗时
    assert meta["providers"]["asr"] and meta["providers"]["embed"]
    assert meta["prompt_version"]
    assert meta["dataset"]["sha256"]
    assert meta["retrieval"]["top_k"] and meta["retrieval"]["evidence_threshold"]
    assert meta["embedding_dim"] == 1024
    assert "D-03" in meta["vector_dim_note"]  # 真实维度核对提示

    # 可复现性:仅 git_rev 不足以复现未提交代码,须记录工作区脏状态与指纹
    git = meta["git"]
    assert git["available"] is True, git
    assert git["rev"] and git["branch"]
    assert isinstance(git["dirty"], bool)
    assert len(git["content_fingerprint"]) == 16
    assert "无法复现" in git["note"]
    if git["dirty"]:
        assert git["modified_files"] or git["untracked_files"], (
            "脏工作区必须列出改动/未跟踪文件,否则指纹无意义"
        )
    # 评测写库如实记录
    assert meta["database"], "报告必须记录实际写入的数据库"

    for split in ("dev", "test"):
        block = report["splits"][split]
        s = block["summary"]
        assert s["total"] >= 6
        assert s["answer_questions"] >= 3 and s["no_answer_questions"] >= 2
        for key in ("evidence_hit_rate", "no_answer_correct_rate",
                    "no_answer_false_positive_rate", "missed_rate"):
            assert key in s
        # 每题结果含时延与判定,可追溯
        for row in block["questions"]:
            assert row["latency_ms"] >= 0
            assert isinstance(row["correct"], bool)

    # 反例必须按类别汇总(型号错配误报在 mock 基线中出现并如实记录)
    by_cat = report["splits"]["dev"]["summary"]["by_category"]
    assert any("型号错配" in cat for cat in by_cat)

    # 越权探针单独统计,且必须全对(权限是正确性,不是可调参的效果指标)
    for split in ("dev", "test"):
        block = report["splits"][split]
        ps = block["permission_summary"]
        assert ps["n"] >= 2, block
        assert ps["correct"] == ps["n"], block["permission_probes"]
        for p in block["permission_probes"]:
            assert p["status"] == p["expected_status"], p
            if p["status"] == 200:
                assert p["citations_within_meeting"] is True, p

    # 费用口径:mock 全部 unknown 且不计价(不得汇总成 0 当作"没花钱")
    cost = report["meta"]["cost"]
    assert cost["by_basis"].keys() == {"unknown"}, cost
    assert cost["by_basis"]["unknown"]["priced_calls"] == 0
    assert cost["by_basis"]["unknown"]["sum_cents"] is None
    assert cost["unpriced_real_calls"] == 0
    assert "不得" in cost["note"]
    # Provider 身份来自实际配置,不是硬编码 mock
    assert report["meta"]["provider_config"] == {"asr": "mock", "llm": "mock",
                                                "embed": "mock"}
    assert report["meta"]["providers_detail"]["all_mock"] is True
    assert report["meta"]["transcript_mode"] == "asr"


def test_eval_human_corrected_mode_report(tmp_path):
    """人工校对转写模式:问答基于校对版本,报告如实标注来源。"""
    from eval.run_eval import main

    out = tmp_path / "report-corrected.json"
    report = main(["--split", "dev", "--transcript", "human_corrected",
                   "--out", str(out)])
    assert report["meta"]["transcript_mode"] == "human_corrected"
    block = report["splits"]["dev"]
    assert block["summary"]["total"] >= 6
    assert block["permission_summary"]["correct"] == block["permission_summary"]["n"]
