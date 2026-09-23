"""真实 Embedding 集成测试(默认跳过)。

只在显式启用本地模型时运行——它需要模型权重与数十秒加载时间,
不属于常规套件:

    EMBED_PROVIDER=bge-m3 python -m pytest tests/test_real_embedding.py -v

验证的是**接入契约**,不是检索效果:维度一致、文档与查询向量同模型、
索引原子发布时旧索引完整保留。
"""
import os

import pytest
from sqlalchemy import text

from app.core.config import Settings
from app.core.db import engine
from app.models.transcript import EMBEDDING_DIM

REAL_EMBED = os.environ.get("EMBED_PROVIDER") == "bge-m3"

pytestmark = pytest.mark.skipif(
    not REAL_EMBED,
    reason="需显式设 EMBED_PROVIDER=bge-m3 才跑真实 Embedding 集成测试",
)


@pytest.fixture(scope="module")
def embed():
    from app.ai.registry import build

    return build("embed", "bge-m3", Settings())


def _q(sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).fetchall()


def test_model_output_dim_equals_vector_column(embed):
    """模型实际输出维度必须等于向量列定义,否则入库即报错(D-03)。"""
    vec = embed.embed_batch(["X200监护仪的注册证5月20日前补齐"])[0]
    column = _q(
        "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a"
        " JOIN pg_class c ON c.oid = a.attrelid"
        " WHERE c.relname = 'embedding_chunk' AND a.attname = 'embedding'"
    )[0][0]
    assert len(vec) == EMBEDDING_DIM == embed.embedding_dim
    assert column == f"vector({EMBEDDING_DIM})"
    assert embed.is_mock is False


def test_document_and_query_vectors_come_from_same_provider(embed):
    """文档向量与查询向量必须同模型同配置:同句相似度应为 1(归一化后)。

    混用(真实查询 + mock 文档)不会报错但会让检索结果失去意义,
    因此这里锁定"两侧都取自工厂"这一事实。
    """
    from app.ai.registry import build_providers

    _asr, _llm, embed_again = build_providers()
    assert embed_again.name == embed.name
    assert embed_again.model_name == embed.model_name
    doc = embed.embed_batch(["培训由厂商负责"])[0]
    query = embed_again.embed_batch(["培训由厂商负责"])[0]
    assert max(abs(a - b) for a, b in zip(doc, query)) < 1e-6


def test_real_vectors_rebuild_index_atomically(client, session, two_orgs, embed):
    """重建索引:发布前新版本不可见、旧版本完整可见;发布后旧行保留但不可见。"""
    from app.services import transcript_correction
    from app.worker import stages
    from tests.conftest import claim_and_load, upload_recording

    meeting, job_id = upload_recording(client, session, two_orgs["org_a"],
                                       two_orgs["a_admin"], seconds=30)
    epoch, ctx = claim_and_load(job_id)
    stages.run_prepare(engine, ctx, epoch, 10, lambda: False)
    lines = [(None, "今天讨论设备采购与交付"),
             (None, "注册证5月20日前补齐"),
             (None, "整机保修两年由厂商负责")]
    info = transcript_correction.import_initial_human_transcript(
        engine, recording_id=ctx.recording_id, lines=lines,
        created_by=two_orgs["a_admin"].id,
    )
    v1 = info["version_id"]
    transcript_correction.rebuild_search_index(
        engine, recording_id=ctx.recording_id, version_id=v1, embed=embed)

    lines2 = [lines[0], (None, "注册证与检测报告在5月20日之前补齐"), lines[2]]
    v2 = transcript_correction.import_corrected_transcript(
        engine, recording_id=ctx.recording_id, lines=lines2,
        created_by=two_orgs["a_admin"].id,
    )["version_id"]

    assert _q("SELECT count(*) FROM embedding_chunk WHERE version_id=:v"
              " AND is_active", v=v2)[0][0] == 0          # 未发布不可见
    assert _q("SELECT count(*) FROM embedding_chunk WHERE version_id=:v"
              " AND is_active", v=v1)[0][0] == 3          # 旧索引完整可用

    transcript_correction.rebuild_search_index(
        engine, recording_id=ctx.recording_id, version_id=v2, embed=embed)

    assert _q("SELECT active_search_version_id FROM recording WHERE id=:r",
              r=ctx.recording_id)[0][0] == v2
    assert _q("SELECT count(*) FROM embedding_chunk WHERE version_id=:v",
              v=v1)[0][0] == 3, "旧索引行必须保留(历史可追溯)"
    assert _q("SELECT count(*) FROM embedding_chunk WHERE version_id=:v"
              " AND is_active", v=v1)[0][0] == 0
    # 真实向量写入后,pgvector 余弦距离可用且同句距离≈0
    row = _q(
        "SELECT 1 - (a.embedding <=> b.embedding) FROM embedding_chunk a,"
        " embedding_chunk b WHERE a.version_id=:v AND b.version_id=:v"
        " AND a.segment_id=b.segment_id LIMIT 1", v=v2)[0][0]
    assert 0.999 <= row <= 1.001
