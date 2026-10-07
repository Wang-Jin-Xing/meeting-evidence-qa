"""片段行踪判定:五支病因的归类口径。

这些用例只测纯函数(classify / like_pattern),**不碰数据库** ——
判定口径固定,二十道题的归类才不会漂,所以它必须可以在任何环境下
单独复跑:

    python tests/test_trace_segment.py

pytest 收集时走同一批函数(tests/conftest.py 会先要求独立测试库,
那是集成测试的门槛,与这里的纯逻辑无关)。
"""
import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.trace_segment import (  # noqa: E402
    V_BELOW_THRESHOLD,
    V_INACTIVE,
    V_INDEX_MISSING,
    V_PASSED,
    V_PERMISSION,
    V_RANK_UNKNOWN,
    V_RANKED_OUT,
    V_VERSION_MISMATCH,
    ChunkState,
    Context,
    TextHit,
    Trace,
    classify,
    like_pattern,
)

THRESHOLD = 0.66
TOP_K = 5


def _ctx(**kw) -> Context:
    base = dict(
        recording_id=7,
        meeting_id=3,
        org_id=1,
        meeting_title="某会谈",
        active_transcript_version_id=12,
        active_search_version_id=12,
    )
    base.update(kw)
    return Context(**base)


def _hit(**kw) -> TextHit:
    base = dict(
        segment_id=12,
        version_id=12,
        version_no=3,
        source_type="human_correction",
        seq=3,
        start_ms=30000,
        end_ms=40000,
        speaker="厂商",
        text="资质文件我们 5 月 20 日前送到。",
    )
    base.update(kw)
    return TextHit(**base)


def _chunk(**kw) -> ChunkState:
    base = dict(
        chunk_id=88,
        version_id=12,
        org_id=1,
        meeting_id=3,
        is_active=True,
        has_embedding=True,
    )
    base.update(kw)
    return ChunkState(**base)


def _trace(chunk=None, chunk_given=True, rank=None, similarity=None, total=41,
           hit=None) -> Trace:
    return Trace(
        hit=hit or _hit(),
        chunk=_chunk() if (chunk_given and chunk is None) else chunk,
        rank=rank,
        similarity=similarity,
        total_ranked=total,
    )


def _code(trace: Trace, ctx: Context | None = None) -> str:
    code, _ = classify(trace, ctx or _ctx(), threshold=THRESHOLD, top_k=TOP_K)
    return code


# ---- 转义 -----------------------------------------------------------------


def test_like_pattern_escapes_wildcards():
    """关键词里的 % _ \\ 必须转义,否则「50%」会匹配到任意文本。"""
    assert like_pattern("资质文件") == "%资质文件%"
    assert like_pattern("50%") == "%50\\%%"
    assert like_pattern("a_b") == "%a\\_b%"
    assert like_pattern("c\\d") == "%c\\\\d%"


# ---- 关①:没进候选的四种 --------------------------------------------------


def test_index_row_missing():
    """原文在,索引行压根没有 → 索引未建。"""
    assert _code(_trace(chunk_given=False)) == V_INDEX_MISSING


def test_embedding_column_null():
    """索引行在,但向量列为空 → 同样算未建成功。"""
    assert _code(_trace(chunk=_chunk(has_embedding=False))) == V_INDEX_MISSING


def test_version_not_on_current_pointer():
    """索引行属于旧版本,门牌号已经向前走了 → 不属于当前版本。"""
    code = _code(_trace(chunk=_chunk(version_id=11)))
    assert code == V_VERSION_MISMATCH


def test_version_matches_but_switch_off():
    """版本号对得上,生效开关却是关的 → 异常状态(发布时本应一并打开)。"""
    assert _code(_trace(chunk=_chunk(is_active=False))) == V_INACTIVE


def test_permission_mismatch_org():
    assert _code(_trace(chunk=_chunk(org_id=2))) == V_PERMISSION


def test_permission_mismatch_meeting():
    assert _code(_trace(chunk=_chunk(meeting_id=99))) == V_PERMISSION


def test_ranked_out_of_top_k():
    """门是开的,但排在 top_k 之外 → 压根没进候选。"""
    code, note = classify(
        _trace(rank=9, similarity=0.55), _ctx(), threshold=THRESHOLD, top_k=TOP_K
    )
    assert code == V_RANKED_OUT
    assert "第 9 名" in note


def test_gate_open_without_rank_is_unknown():
    """门是开的但没算排名 → 不能瞎猜,如实说无法判定。"""
    assert _code(_trace()) == V_RANK_UNKNOWN


def test_recording_with_no_published_search_version():
    """录音没有任何已发布检索版本 → 整条检索都取不到,先于单行判定。"""
    code = _code(_trace(), _ctx(active_search_version_id=None))
    assert code == V_INDEX_MISSING


# ---- 关②:进了候选但没过线 ------------------------------------------------


def test_below_threshold_tiny_gap():
    """D-59 的真实数字:0.6566 对门槛 0.66,差 0.0034。"""
    code, note = classify(
        _trace(rank=4, similarity=0.6566), _ctx(), threshold=THRESHOLD, top_k=TOP_K
    )
    assert code == V_BELOW_THRESHOLD
    assert "差一点点" in note


def test_below_threshold_big_gap():
    """差距大时不该说「差一点点」—— 那是误导,应当提示更像表示不准。"""
    code, note = classify(
        _trace(rank=2, similarity=0.41), _ctx(), threshold=THRESHOLD, top_k=TOP_K
    )
    assert code == V_BELOW_THRESHOLD
    assert "差一点点" not in note
    assert "表示不准" in note


def test_passed_threshold_points_elsewhere():
    """过线了 → 病不在关①关②,该去看答案归属(关④)。"""
    code, note = classify(
        _trace(rank=1, similarity=0.81), _ctx(), threshold=THRESHOLD, top_k=TOP_K
    )
    assert code == V_PASSED
    assert "归属" in note


def test_threshold_boundary_is_inclusive():
    """正好等于门槛算过线(与产品里 >= 的写法一致)。"""
    code = _code(_trace(rank=3, similarity=THRESHOLD))
    assert code == V_PASSED


def test_old_version_hit_is_not_rescued_by_rank():
    """旧版本的片段即便相似度极高,也不该被排名判成「差一点点」。"""
    code = _code(_trace(chunk=_chunk(version_id=11), rank=1, similarity=0.99))
    assert code == V_VERSION_MISMATCH


if __name__ == "__main__":
    cases = [
        (name, fn)
        for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    failed = []
    for name, fn in cases:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception:
            failed.append(name)
            print(f"FAIL  {name}")
            traceback.print_exc()
    print(f"\n{len(cases) - len(failed)} passed, {len(failed)} failed")
    raise SystemExit(1 if failed else 0)
