"""Bug 回归：全跳过/全重复 Session 不再卡 UPLOADING；进度汇总口径相关后端状态。"""

from __future__ import annotations

from photo_gateway.db import init_db
from photo_gateway import store as st


def _session_with_items(tmp_path, confs, precheck="POSSIBLE_DUPLICATE"):
    """建 1 个 session + confs 数量 item，返回 (conn, session_id)。item 处于 PRECHECK 未决。"""
    conn = init_db(tmp_path / "db.sqlite")
    now = "2026-09-01 10:00:00"
    conn.execute("INSERT INTO devices(id,name,enabled,created_at,updated_at) VALUES ('d','d',1,?,?)", (now, now))
    conn.execute("INSERT INTO upload_sessions(id,source_device,status,created_at) VALUES ('S','d','UPLOADING',?)", (now,))
    for i, conf in enumerate(confs):
        conn.execute(
            "INSERT INTO upload_items(session_id,source_device,client_item_id,original_filename,"
            "file_size,status,precheck_status,user_confirmation,created_at,updated_at)"
            " VALUES ('S','d',?,?,1,'PRECHECK',?,?,?,?)",
            (f"c{i}", f"f{i}.jpg", precheck, conf, now, now),
        )
    conn.commit()
    return conn, "S"


def test_all_skipped_session_finalizes_completed(tmp_path):
    conn, sid = _session_with_items(tmp_path, [2, 2])   # 两张疑似重复，均选择“跳过”
    assert st.finalize_session(conn, sid) is None or True
    row = conn.execute(
        "SELECT status, completed_at, total_files, skipped_files, uploaded_files"
        " FROM upload_sessions WHERE id='S'").fetchone()
    assert row["status"] == "COMPLETED", row["status"]      # 不再卡 UPLOADING
    assert row["completed_at"] is not None
    assert row["skipped_files"] == 2 and row["uploaded_files"] == 0
    conn.close()


def test_reupload_confirmed_keeps_session_open_until_uploaded(tmp_path):
    conn, sid = _session_with_items(tmp_path, [0, 2])       # 一张待重传(未决 conf0)
    conn.execute("UPDATE upload_items SET user_confirmation=1, status='UPLOADING'"
                 " WHERE session_id='S' AND client_item_id='c0'")
    conn.commit()
    st.finalize_session(conn, sid)
    row = conn.execute("SELECT status FROM upload_sessions WHERE id='S'").fetchone()
    assert row["status"] == "UPLOADING"   # 还有 UPLOADING 项 → 不提前终态
    # 模拟 Worker 已归档该文件(UPLOADED→COMPLETED) + 另一个跳过项决定完 → 终态
    conn.execute("UPDATE upload_items SET status='COMPLETED' WHERE session_id='S' AND client_item_id='c0'")
    conn.execute("UPDATE upload_items SET user_confirmation=2 WHERE session_id='S' AND client_item_id='c1'")
    conn.commit()
    st.finalize_session(conn, sid)
    row = conn.execute("SELECT status, completed_at FROM upload_sessions WHERE id='S'").fetchone()
    assert row["status"] == "COMPLETED" and row["completed_at"] is not None
    conn.close()


def test_no_match_waiting_upload_not_finalized(tmp_path):
    conn, sid = _session_with_items(tmp_path, [0], precheck="NO_MATCH")  # 还没传
    st.finalize_session(conn, sid)
    row = conn.execute("SELECT status FROM upload_sessions WHERE id='S'").fetchone()
    assert row["status"] == "UPLOADING"   # NO_MATCH 未传 → 不应 COMPLETED
    conn.close()
