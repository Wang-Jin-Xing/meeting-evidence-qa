"""P3 评测框架冒烟测试:数据集结构 + mock 基线跑通 + 报告自描述。"""
import json
from pathlib import Path

DATASET = Path(__file__).resolve().parents[1] / "eval" / "dataset.json"


def test_dataset_structure_and_counterexamples():
    """数据集:按会谈划分 dev/test,且覆盖四类反例(型号错配/否定/
    日期归属/无答案)。"""
    data = json.loads(DATASET.read_text(encoding="utf-8"))
    splits = {m["split"] for m in data["meetings"]}
    assert splits == {"dev", "test"}, "必须按会谈划分 dev/test"
    assert data["split_by"] == "meeting"

    categories = set()
    for m in data["meetings"]:
        assert m["corpus"], "会谈必须有语料"
        for q in m["questions"]:
            if q.get("no_answer"):
                categories.add(q["category"])
            else:
                assert q["answer_clue"] and q["evidence_text"], q
    # 四类反例:型号错配、日期归属、无答案;否定以"有答案但答案为否定"
    # 形式存在(见 dev/test 问题集)
    assert "型号错配" in " ".join(categories) or any(
        "型号错配" in c for c in categories
    ), categories
    assert any("无答案" in c for c in categories), categories
    assert any("日期归属" in c for c in categories), categories
    negation = [
        q for m in data["meetings"] for q in m["questions"]
        if not q.get("no_answer") and "不支持" in q.get("answer_clue", "")
    ]
    assert negation, "必须包含否定反例(答案为否定信息)"


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
