"""纪要人工修订与确认(D-62)。

三条不变量:
1. **AI 原始版本永不改写**:编辑只追加新版本,`parent_version_id` 记下来源版本;
2. **确认属于具体版本**:确认写在该版本行上(confirmed_by/confirmed_at),
   编辑出的新版本一律回到 pending_review —— 不把别人没确认过的内容当成已确认;
3. **并发编辑不静默覆盖**:编辑必须声明基于哪个版本,若它已不是最新版则 409,
   让人先看到"有人在你之后改过",而不是后写的悄悄盖掉先写的。
"""
import json

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.services.minutes_validator import validate_minutes


class MinutesVersionNotFound(Exception):
    """版本不存在或不属于该会谈(对外一律 404,不区分两种情况)。"""


class StaleMinutesEdit(Exception):
    """编辑基版本已不是最新版:并发保护,要求基于最新版重来。"""

    def __init__(self, base_version_id: int, latest_version_no: int):
        self.base_version_id = base_version_id
        self.latest_version_no = latest_version_no
        super().__init__(
            f"基版本 {base_version_id} 已非最新(当前最新 v{latest_version_no})")


def _segment_ids(conn, meeting_id: int) -> set[int]:
    """本会谈**全部**录音版本下的片段 id —— 证据校验的合法域。

    限定"经录音→会谈归属",不接受跨会谈 id,否则可以把别人会谈的原文
    挂成自己纪要的证据。
    """
    rows = conn.execute(
        text(
            "SELECT s.id FROM transcript_segment s"
            " JOIN transcript_version v ON v.id = s.version_id"
            " JOIN recording r ON r.id = v.recording_id"
            " WHERE r.meeting_id = :m"
        ),
        {"m": meeting_id},
    ).all()
    return {r[0] for r in rows}


def _brief(conn, version_id: int) -> dict:
    row = conn.execute(
        text(
            "SELECT id, version_no, source_type, status, created_by, created_at,"
            " confirmed_by, confirmed_at, parent_version_id"
            " FROM minutes_version WHERE id = :id"
        ),
        {"id": version_id},
    ).fetchone()
    return dict(row._mapping)


def edit_minutes(eng: Engine, meeting_id: int, *, base_version_id: int,
                 content: dict, actor_id: int) -> dict:
    """基于 base_version_id 追加一个 human_edit 版本(pending_review)。"""
    with eng.begin() as conn:
        # 锁住基版本行:并发第二个事务会等到提交,然后看到它已不是最新版
        locked = conn.execute(
            text("SELECT id FROM minutes_version WHERE id = :id AND meeting_id = :m"
                 " FOR UPDATE"),
            {"id": base_version_id, "m": meeting_id},
        ).fetchone()
        if locked is None:
            raise MinutesVersionNotFound(str(base_version_id))

        latest_no = conn.execute(
            text("SELECT max(version_no) FROM minutes_version WHERE meeting_id = :m"),
            {"m": meeting_id},
        ).scalar()
        base_no = conn.execute(
            text("SELECT version_no FROM minutes_version WHERE id = :id"),
            {"id": base_version_id},
        ).scalar()
        if base_no != latest_no:
            raise StaleMinutesEdit(base_version_id, latest_no)

        parsed = validate_minutes(content, _segment_ids(conn, meeting_id))
        payload = {"items": [i.model_dump() for i in parsed.items]}

        new_id = conn.execute(
            text(
                "INSERT INTO minutes_version"
                " (meeting_id, job_id, version_no, source_type, parent_version_id,"
                "  status, content, created_by)"
                " VALUES (:mid, NULL, :no, 'human_edit', :parent, 'pending_review',"
                "  CAST(:content AS jsonb), :actor)"
                " RETURNING id"
            ),
            {"mid": meeting_id, "no": latest_no + 1, "parent": base_version_id,
             "content": json.dumps(payload, ensure_ascii=False), "actor": actor_id},
        ).scalar_one()
        return _brief(conn, new_id)


def confirm_minutes(eng: Engine, meeting_id: int, *, version_id: int,
                    actor_id: int) -> dict:
    """确认**指定版本**。同一版本重复确认幂等;确认不改内容、不产生新版本。"""
    with eng.begin() as conn:
        row = conn.execute(
            text("SELECT id, status, confirmed_at FROM minutes_version"
                 " WHERE id = :id AND meeting_id = :m FOR UPDATE"),
            {"id": version_id, "m": meeting_id},
        ).fetchone()
        if row is None:
            raise MinutesVersionNotFound(str(version_id))
        if row.status == "confirmed" and row.confirmed_at is not None:
            return _brief(conn, version_id)      # 幂等:不刷新时间、不换确认人

        conn.execute(
            text("UPDATE minutes_version SET status = 'confirmed',"
                 " confirmed_by = :actor, confirmed_at = clock_timestamp()"
                 " WHERE id = :id"),
            {"actor": actor_id, "id": version_id},
        )
        return _brief(conn, version_id)
