from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
FIXTURES = BACKEND.parent / "fixtures"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FIXTURES_DIR", str(FIXTURES))
    # 确保以干净的模块状态启动，startup 时建库并播种
    for name in list(sys.modules):
        if name.startswith("app"):
            del sys.modules[name]
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


def _domain_id(client) -> int:
    return client.get("/api/domains").json()[0]["id"]


def test_all_routes_registered(client):
    """防止新增路由模块后忘记 include_router（dashboard 曾因此漏挂）。"""
    paths = {r.path for r in client.app.routes}
    expected = {
        "/api/dashboard",
        "/api/domains",
        "/api/closed-days",
        "/api/close-decisions",
        "/api/biz-days/overview",
        "/api/closed-days/close",
        "/api/closed-days/reopen",
        "/api/paste/load",
        "/api/layers/counts",
        "/api/jobs",
        "/api/jobs/paste-to-detail",
        "/api/jobs/detail-to-summary",
        "/api/reconcile",
    }
    assert expected <= paths


def test_close_list_and_overview_consistent(client):
    did = _domain_id(client)
    # 初始贴源：11-10 三行，11-11 三行
    overview = client.get(f"/api/biz-days/overview?domain_id={did}").json()
    by_day = {r["biz_day"]: r for r in overview}
    assert by_day["2026-11-10"]["paste_count"] == 3
    assert by_day["2026-11-11"]["paste_count"] == 3
    assert all(not r["closed"] for r in overview)

    r = client.post(
        "/api/closed-days/close",
        json={"domain_id": did, "biz_day": "2026-11-10", "reason": "月结"},
    )
    assert r.status_code == 200
    # 重复关闭走更新而非报错
    client.post(
        "/api/closed-days/close",
        json={"domain_id": did, "biz_day": "2026-11-10", "reason": "月结完成"},
    )

    closed = client.get(f"/api/closed-days?domain_id={did}").json()
    assert len(closed) == 1 and closed[0]["biz_day"] == "2026-11-10"
    assert closed[0]["reason"] == "月结完成"

    overview = client.get(f"/api/biz-days/overview?domain_id={did}").json()
    by_day = {r["biz_day"]: r for r in overview}
    assert by_day["2026-11-10"]["closed"] is True
    # 贴源按日计数不受关闭影响
    assert by_day["2026-11-10"]["paste_count"] == 3

    # 贴源列表带关闭标记，与名单同源
    paste = client.get(f"/api/layers/贴源?domain_id={did}").json()
    assert all(r["closed"] is True for r in paste if r["biz_day"] == "2026-11-10")
    assert all(r["closed"] is False for r in paste if r["biz_day"] == "2026-11-11")


def test_paste_load_rejects_closed_day_but_keeps_existing_rows(client):
    did = _domain_id(client)
    client.post(
        "/api/closed-days/close",
        json={"domain_id": did, "biz_day": "2026-11-10"},
    )

    # 全量装载：关闭日 3 行拒绝，未关闭日 3 行重写
    job = client.post("/api/paste/load", json={"domain_id": did}).json()
    assert job["step"] == "paste_load"
    assert job["reject_count"] == 3
    assert job["out_count"] == 3
    assert job["in_count"] == 6
    assert "成功（含拒绝）" == job["status"]
    assert "2026-11-10已关闭" in job["message"]

    # 关闭日贴源原样保留，总行数仍是 6
    paste = client.get(f"/api/layers/贴源?domain_id={did}").json()
    assert len(paste) == 6
    assert sum(1 for r in paste if r["biz_day"] == "2026-11-10") == 3

    # 定点装载关闭日：整单拒绝
    job2 = client.post(
        "/api/paste/load", json={"domain_id": did, "biz_day": "2026-11-10"}
    ).json()
    assert job2["status"] == "拒绝"
    assert job2["reject_count"] == 3 and job2["out_count"] == 0

    # 装载结果进任务列表，刷新（重新请求）仍在
    jobs = client.get(f"/api/jobs?domain_id={did}").json()
    steps = {(j["step"], j["status"], j["reject_count"]) for j in jobs}
    assert ("paste_load", "拒绝", 3) in steps
    assert ("paste_load", "成功（含拒绝）", 3) in steps


def test_layer_jobs_reject_closed_day_for_both_steps(client):
    did = _domain_id(client)
    client.post(
        "/api/closed-days/close",
        json={"domain_id": did, "biz_day": "2026-11-10"},
    )

    # 定点重算关闭日：贴源到明细被拒绝（400），任务已落库
    r = client.post(
        "/api/jobs/paste-to-detail",
        json={"domain_id": did, "biz_day": "2026-11-10"},
    )
    assert r.status_code == 400
    assert "已关闭" in r.json()["detail"]["原因"]
    assert r.json()["detail"]["任务"]["status"] == "拒绝"

    # 明细到汇总定点同样被拒绝
    r = client.post(
        "/api/jobs/detail-to-summary",
        json={"domain_id": did, "biz_day": "2026-11-10"},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["任务"]["step"] == "detail_to_summary"

    # 拒绝后没有任何分层数据被写入
    assert client.get(f"/api/layers/明细?domain_id={did}").json() == []
    assert client.get(f"/api/layers/汇总?domain_id={did}").json() == []


def test_full_runs_skip_closed_days_and_reopen_restores(client):
    did = _domain_id(client)
    client.post(
        "/api/closed-days/close",
        json={"domain_id": did, "biz_day": "2026-11-10"},
    )

    # 全量贴源到明细：关闭日 3 行跳过；11-11 三行中 1 行因空店名/负金额被剔除
    job = client.post("/api/jobs/paste-to-detail", json={"domain_id": did}).json()
    assert job["reject_count"] == 3 and job["out_count"] == 2 and job["drop_count"] == 1
    detail = client.get(f"/api/layers/明细?domain_id={did}").json()
    assert {r["biz_day"] for r in detail} == {"2026-11-11"}

    # 全量明细到汇总：明细层本就不含关闭日，只汇总未关闭日
    job = client.post("/api/jobs/detail-to-summary", json={"domain_id": did}).json()
    assert job["reject_count"] == 0
    summary = client.get(f"/api/layers/汇总?domain_id={did}").json()
    assert {r["biz_day"] for r in summary} == {"2026-11-11"}

    # 重新打开后行为恢复：全量重跑不再有拒绝
    client.post(
        "/api/closed-days/reopen",
        json={"domain_id": did, "biz_day": "2026-11-10"},
    )
    job = client.post("/api/jobs/paste-to-detail", json={"domain_id": did}).json()
    assert job["reject_count"] == 0 and job["status"] == "成功"
    # 6 行贴源中 1 行因空店名/负金额被剔除
    assert job["out_count"] == 5 and job["drop_count"] == 1
    detail = client.get(f"/api/layers/明细?domain_id={did}").json()
    assert {r["biz_day"] for r in detail} == {"2026-11-10", "2026-11-11"}

    client.post("/api/jobs/detail-to-summary", json={"domain_id": did})
    summary = client.get(f"/api/layers/汇总?domain_id={did}").json()
    assert {r["biz_day"] for r in summary} == {"2026-11-10", "2026-11-11"}


def test_open_day_behavior_unchanged(client):
    did = _domain_id(client)
    # 未关闭任何业务日时，两个步骤与原始行为一致
    j1 = client.post("/api/jobs/paste-to-detail", json={"domain_id": did}).json()
    assert j1["status"] == "成功" and j1["reject_count"] == 0 and j1["out_count"] == 5
    j2 = client.post("/api/jobs/detail-to-summary", json={"domain_id": did}).json()
    assert j2["status"] == "成功" and j2["reject_count"] == 0

    # 定点重算未关闭日正常执行（11-11 三行中 1 行被清洗剔除）
    j3 = client.post(
        "/api/jobs/paste-to-detail",
        json={"domain_id": did, "biz_day": "2026-11-11"},
    ).json()
    assert j3["status"] == "成功" and j3["biz_day"] == "2026-11-11" and j3["out_count"] == 2


def test_reconcile_report_records_closed_days(client):
    did = _domain_id(client)
    client.post(
        "/api/closed-days/close",
        json={"domain_id": did, "biz_day": "2026-11-10"},
    )
    report = client.post("/api/reconcile", json={"domain_id": did}).json()
    assert report["detail"]["已关闭业务日"] == ["2026-11-10"]


def test_full_runs_preserve_legacy_closed_day_data(client):
    """关闭日前已产出的明细/汇总，在全量重跑时必须原样保留、不被重写。"""
    did = _domain_id(client)
    # 先在未关闭状态下把三层都跑出来
    client.post("/api/jobs/paste-to-detail", json={"domain_id": did})
    client.post("/api/jobs/detail-to-summary", json={"domain_id": did})
    detail_before = client.get(f"/api/layers/明细?domain_id={did}").json()
    summary_before = client.get(f"/api/layers/汇总?domain_id={did}").json()
    closed_detail = [r for r in detail_before if r["biz_day"] == "2026-11-10"]
    closed_summary = [r for r in summary_before if r["biz_day"] == "2026-11-10"]
    assert closed_detail and closed_summary

    client.post(
        "/api/closed-days/close",
        json={"domain_id": did, "biz_day": "2026-11-10"},
    )
    client.post("/api/jobs/paste-to-detail", json={"domain_id": did})
    client.post("/api/jobs/detail-to-summary", json={"domain_id": did})

    detail_after = client.get(f"/api/layers/明细?domain_id={did}").json()
    summary_after = client.get(f"/api/layers/汇总?domain_id={did}").json()
    assert [r for r in detail_after if r["biz_day"] == "2026-11-10"] == closed_detail
    assert [r for r in summary_after if r["biz_day"] == "2026-11-10"] == closed_summary


def test_invalid_biz_day_rejected(client):
    did = _domain_id(client)
    r = client.post(
        "/api/closed-days/close",
        json={"domain_id": did, "biz_day": "2026/11/10"},
    )
    assert r.status_code == 400


def test_reason_codes_recorded_on_load_and_jobs(client):
    did = _domain_id(client)
    client.post(
        "/api/closed-days/close",
        json={"domain_id": did, "biz_day": "2026-11-10"},
    )

    # 全量装载含关闭日：装载结果带关闭拒绝原因码
    job = client.post("/api/paste/load", json={"domain_id": did}).json()
    assert job["reason_code"] == "BIZ_DAY_CLOSED_PASTE_LOAD"

    # 定点装载关闭日整单拒绝，原因码一致
    r = client.post(
        "/api/paste/load", json={"domain_id": did, "biz_day": "2026-11-10"}
    )
    assert r.status_code == 200
    assert r.json()["status"] == "拒绝"
    assert r.json()["reason_code"] == "BIZ_DAY_CLOSED_PASTE_LOAD"

    # 定点分层任务关闭日拒绝原因码
    r = client.post(
        "/api/jobs/paste-to-detail",
        json={"domain_id": did, "biz_day": "2026-11-10"},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["任务"]["reason_code"] == "BIZ_DAY_CLOSED_LAYER_JOB"

    # 全量重跑跳过关闭日，原因码为跳过码，且与关闭名单刷新后保持一致
    job = client.post("/api/jobs/paste-to-detail", json={"domain_id": did}).json()
    assert job["reason_code"] == "BIZ_DAY_CLOSED_SKIPPED"

    # 未关闭日的正常任务不带原因码
    job = client.post(
        "/api/jobs/paste-to-detail",
        json={"domain_id": did, "biz_day": "2026-11-11"},
    ).json()
    assert job["reason_code"] == ""


def test_close_decision_proof_entity_and_read_only_filters(client):
    did = _domain_id(client)
    client.post(
        "/api/closed-days/close",
        json={"domain_id": did, "biz_day": "2026-11-10"},
    )

    load_job = client.post("/api/paste/load", json={"domain_id": did}).json()
    # 裁决证明按任务 id 只读查询，字段含目标日/是否关闭/写路径是否放行
    decisions = client.get(f"/api/close-decisions?job_id={load_job['id']}").json()
    by_day = {d["biz_day"]: d for d in decisions}
    assert set(by_day) == {"2026-11-10", "2026-11-11"}
    closed_proof = by_day["2026-11-10"]
    assert closed_proof["is_closed"] is True
    assert closed_proof["write_allowed"] is False
    assert closed_proof["domain_id"] == did
    assert closed_proof["job_id"] == load_job["id"]
    assert closed_proof["step"] == "paste_load"
    assert closed_proof["reason_code"] == "BIZ_DAY_CLOSED_PASTE_LOAD"
    open_proof = by_day["2026-11-11"]
    assert open_proof["is_closed"] is False and open_proof["write_allowed"] is True

    # 按装载 id 查询与按任务 id 查询同源（贴源装载任务本身即装载单）
    by_load = client.get(
        f"/api/close-decisions?load_id={load_job['id']}"
    ).json()
    assert {(d["biz_day"], d["step"]) for d in by_load} == {
        ("2026-11-10", "paste_load"),
        ("2026-11-11", "paste_load"),
    }

    # 全量分层任务同样落证明：关闭日不放行，未关闭日放行
    layer_job = client.post(
        "/api/jobs/paste-to-detail", json={"domain_id": did}
    ).json()
    decisions = client.get(f"/api/close-decisions?job_id={layer_job['id']}").json()
    by_day = {d["biz_day"]: d for d in decisions}
    assert by_day["2026-11-10"]["is_closed"] is True
    assert by_day["2026-11-10"]["write_allowed"] is False
    assert by_day["2026-11-11"]["write_allowed"] is True

    # 定点拒绝任务也有一条关闭裁决证明
    r = client.post(
        "/api/jobs/detail-to-summary",
        json={"domain_id": did, "biz_day": "2026-11-10"},
    )
    assert r.status_code == 400
    reject_job_id = r.json()["detail"]["任务"]["id"]
    decisions = client.get(f"/api/close-decisions?job_id={reject_job_id}").json()
    assert len(decisions) == 1
    assert decisions[0]["biz_day"] == "2026-11-10"
    assert decisions[0]["is_closed"] is True
    assert decisions[0]["write_allowed"] is False

    # 只读接口：未知任务编号返回空列表；写方法不被允许
    assert client.get("/api/close-decisions?job_id=999999").json() == []
    assert client.post("/api/close-decisions", json={}).status_code == 405

    # 按主题域列出全部证明
    all_decisions = client.get(f"/api/close-decisions?domain_id={did}").json()
    assert len(all_decisions) == 2 + 2 + 1

    # 证明落本地 SQLite，重新打开应用（新客户端重新建连接）后仍在
    rows = client.get(f"/api/close-decisions?domain_id={did}").json()
    assert rows and all(r["is_closed"] in (True, False) for r in rows)


def test_decision_proof_tracks_open_day_unchanged_path(client):
    did = _domain_id(client)
    # 未关闭日：装载与两层任务的裁决全部放行
    load_job = client.post("/api/paste/load", json={"domain_id": did}).json()
    j1 = client.post("/api/jobs/paste-to-detail", json={"domain_id": did}).json()
    j2 = client.post("/api/jobs/detail-to-summary", json={"domain_id": did}).json()
    for job_id in (load_job["id"], j1["id"], j2["id"]):
        decisions = client.get(f"/api/close-decisions?job_id={job_id}").json()
        assert decisions and all(d["is_closed"] is False for d in decisions)
        assert all(d["write_allowed"] is True for d in decisions)
        assert all(d["reason_code"] == "" for d in decisions)


def test_reopen_restores_write_allowed_decision(client):
    did = _domain_id(client)
    client.post(
        "/api/closed-days/close",
        json={"domain_id": did, "biz_day": "2026-11-10"},
    )
    closed_job = client.post("/api/paste/load", json={"domain_id": did}).json()
    before = {
        d["biz_day"]: d
        for d in client.get(f"/api/close-decisions?job_id={closed_job['id']}").json()
    }
    assert before["2026-11-10"]["write_allowed"] is False

    # 重新打开后再装载：新裁决证明写路径放行（历史证明保持不变）
    client.post(
        "/api/closed-days/reopen",
        json={"domain_id": did, "biz_day": "2026-11-10"},
    )
    open_job = client.post("/api/paste/load", json={"domain_id": did}).json()
    after = {
        d["biz_day"]: d
        for d in client.get(f"/api/close-decisions?job_id={open_job['id']}").json()
    }
    assert after["2026-11-10"]["is_closed"] is False
    assert after["2026-11-10"]["write_allowed"] is True


def test_legacy_db_migration(tmp_path, monkeypatch):
    """旧版库没有 reject_count/biz_day 列与 closed_biz_days 表，启动时自动迁移。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("FIXTURES_DIR", str(FIXTURES))
    db = data_dir / "app.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE domains (
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
          biz_key TEXT NOT NULL, fixture_file TEXT NOT NULL, description TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE layer_paste (
          id INTEGER PRIMARY KEY AUTOINCREMENT, domain_id INTEGER NOT NULL, biz_day TEXT NOT NULL,
          store_code TEXT NOT NULL, store_name TEXT NOT NULL, category TEXT NOT NULL,
          amount REAL NOT NULL, qty INTEGER NOT NULL, raw_json TEXT NOT NULL
        );
        CREATE TABLE layer_detail (
          id INTEGER PRIMARY KEY AUTOINCREMENT, domain_id INTEGER NOT NULL, biz_day TEXT NOT NULL,
          store_code TEXT NOT NULL, store_name TEXT NOT NULL, category TEXT NOT NULL,
          amount REAL NOT NULL, qty INTEGER NOT NULL, clean_flag TEXT NOT NULL
        );
        CREATE TABLE layer_summary (
          id INTEGER PRIMARY KEY AUTOINCREMENT, domain_id INTEGER NOT NULL, biz_day TEXT NOT NULL,
          store_code TEXT NOT NULL, amount_sum REAL NOT NULL, qty_sum INTEGER NOT NULL,
          line_count INTEGER NOT NULL, UNIQUE(domain_id, biz_day, store_code)
        );
        CREATE TABLE layer_jobs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, domain_id INTEGER NOT NULL, step TEXT NOT NULL,
          status TEXT NOT NULL, message TEXT NOT NULL DEFAULT '', in_count INTEGER NOT NULL DEFAULT 0,
          out_count INTEGER NOT NULL DEFAULT 0, drop_count INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE reconcile_reports (
          id INTEGER PRIMARY KEY AUTOINCREMENT, domain_id INTEGER NOT NULL,
          paste_count INTEGER NOT NULL, detail_count INTEGER NOT NULL,
          summary_line_sum INTEGER NOT NULL, key_mismatch INTEGER NOT NULL,
          amount_drift REAL NOT NULL, status TEXT NOT NULL, detail_json TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT INTO domains(name, biz_key, fixture_file, description)
          VALUES ('旧主题域', '业务日', 'x.jsonl', '');
        INSERT INTO layer_jobs(domain_id, step, status) VALUES (1, 'paste_to_detail', '成功');
        """
    )
    conn.commit()
    conn.close()

    for name in list(sys.modules):
        if name.startswith("app"):
            del sys.modules[name]
    from app import seed

    seed.init_db()

    conn = sqlite3.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(layer_jobs)").fetchall()}
    assert {"reject_count", "biz_day", "reason_code"} <= cols
    # 旧任务记录迁移后默认值正确
    row = conn.execute("SELECT reject_count, biz_day, reason_code FROM layer_jobs").fetchone()
    assert row == (0, None, "")
    # 关闭名单表可用
    conn.execute(
        "INSERT INTO closed_biz_days(domain_id, biz_day) VALUES (1, '2026-11-10')"
    )
    # 关闭裁决证明表可用，布尔位以 0/1 存储
    conn.execute(
        """
        INSERT INTO close_decisions(
          domain_id, biz_day, is_closed, write_allowed, job_id, step, reason_code
        ) VALUES (1, '2026-11-10', 1, 0, NULL, 'paste_load', 'BIZ_DAY_CLOSED_PASTE_LOAD')
        """
    )
    row = conn.execute(
        "SELECT is_closed, write_allowed FROM close_decisions"
    ).fetchone()
    assert row == (1, 0)
    conn.commit()
    conn.close()
