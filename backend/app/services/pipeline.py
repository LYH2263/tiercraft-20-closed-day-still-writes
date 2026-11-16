from __future__ import annotations

import json
import re

from app.db import FIXTURES_DIR, connect, rows_to_dicts

STEP_LABEL = {
    "paste_load": "贴源装载",
    "paste_to_detail": "贴源到明细",
    "detail_to_summary": "明细到汇总",
}

BIZ_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 统一的拒绝原因模板，界面与任务记录均引用这一套措辞
CLOSED_LOAD_REASON = "业务日{day}已关闭，贴源装载被拒绝（跳过{count}行）"
CLOSED_JOB_REASON = "业务日{day}已关闭，{step}被拒绝"
CLOSED_SKIP_REASON = "业务日{day}已关闭，已跳过该日{count}行"

# 拒绝原因码：稳定可机读，说明文案在 message 里随名单刷新保持一致
REASON_PASTE_CLOSED = "BIZ_DAY_CLOSED_PASTE_LOAD"
REASON_STEP_CLOSED = "BIZ_DAY_CLOSED_LAYER_JOB"
REASON_SKIP_CLOSED = "BIZ_DAY_CLOSED_SKIPPED"


class RejectError(ValueError):
    """整单被关闭态拒绝：调用方据此返回 400，且拒绝已落任务结果。"""

    def __init__(self, message: str, job: dict):
        super().__init__(message)
        self.job = job


def validate_biz_day(biz_day: str) -> str:
    biz_day = (biz_day or "").strip()
    if not BIZ_DAY_RE.match(biz_day):
        raise ValueError("业务日格式应为 YYYY-MM-DD")
    return biz_day


def list_domains() -> list[dict]:
    conn = connect()
    try:
        return rows_to_dicts(conn.execute("SELECT * FROM domains ORDER BY id").fetchall())
    finally:
        conn.close()


def get_domain(domain_id: int) -> dict | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM domains WHERE id=?", (domain_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _closed_days(conn, domain_id: int) -> set[str]:
    rows = conn.execute(
        "SELECT biz_day FROM closed_biz_days WHERE domain_id=?", (domain_id,)
    ).fetchall()
    return {r["biz_day"] for r in rows}


def list_closed_biz_days(domain_id: int | None = None) -> list[dict]:
    conn = connect()
    try:
        if domain_id is None:
            rows = conn.execute(
                """
                SELECT c.*, d.name AS domain_name
                FROM closed_biz_days c JOIN domains d ON d.id = c.domain_id
                ORDER BY c.domain_id, c.biz_day
                """
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM closed_biz_days WHERE domain_id=? ORDER BY biz_day",
                (domain_id,),
            ).fetchall()
        return rows_to_dicts(rows)
    finally:
        conn.close()


def close_biz_day(domain_id: int, biz_day: str, reason: str = "") -> dict:
    biz_day = validate_biz_day(biz_day)
    domain = get_domain(domain_id)
    if not domain:
        raise ValueError("主题域不存在")
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO closed_biz_days(domain_id, biz_day, reason)
            VALUES (?, ?, ?)
            ON CONFLICT(domain_id, biz_day) DO UPDATE SET reason=excluded.reason
            """,
            (domain_id, biz_day, (reason or "").strip()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM closed_biz_days WHERE domain_id=? AND biz_day=?",
            (domain_id, biz_day),
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def reopen_biz_day(domain_id: int, biz_day: str) -> None:
    biz_day = validate_biz_day(biz_day)
    conn = connect()
    try:
        conn.execute(
            "DELETE FROM closed_biz_days WHERE domain_id=? AND biz_day=?",
            (domain_id, biz_day),
        )
        conn.commit()
    finally:
        conn.close()


def _record_close_decisions(
    conn,
    domain_id: int,
    job_id: int | None,
    step: str,
    decisions: dict[str, tuple[bool, bool, str]],
) -> None:
    """落关闭裁决证明：decisions 为 {业务日: (是否关闭, 写路径是否放行, 原因码)}。"""
    for day in sorted(decisions):
        is_closed, write_allowed, reason_code = decisions[day]
        conn.execute(
            """
            INSERT INTO close_decisions(
              domain_id, biz_day, is_closed, write_allowed, job_id, step, reason_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                domain_id,
                day,
                1 if is_closed else 0,
                1 if write_allowed else 0,
                job_id,
                step,
                reason_code,
            ),
        )


def list_close_decisions(
    domain_id: int | None = None,
    job_id: int | None = None,
    load_id: int | None = None,
) -> list[dict]:
    """关闭裁决证明只读查询；load_id 即贴源装载任务（layer_jobs）编号。"""
    sql = """
        SELECT c.*, d.name AS domain_name
        FROM close_decisions c JOIN domains d ON d.id = c.domain_id
        WHERE 1=1
    """
    args: list = []
    if domain_id is not None:
        sql += " AND c.domain_id=?"
        args.append(domain_id)
    if job_id is not None:
        sql += " AND c.job_id=?"
        args.append(job_id)
    if load_id is not None:
        sql += " AND c.job_id=? AND c.step='paste_load'"
        args.append(load_id)
    sql += " ORDER BY c.id"
    conn = connect()
    try:
        rows = conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["is_closed"] = bool(d["is_closed"])
            d["write_allowed"] = bool(d["write_allowed"])
            out.append(d)
        return out
    finally:
        conn.close()


def biz_day_overview(domain_id: int) -> list[dict]:
    """贴源按日计数 + 关闭名单合并视图，供业务日关闭界面使用。"""
    domain = get_domain(domain_id)
    if not domain:
        raise ValueError("主题域不存在")
    conn = connect()
    try:
        paste_rows = conn.execute(
            """
            SELECT biz_day, COUNT(*) AS paste_count
            FROM layer_paste WHERE domain_id=? GROUP BY biz_day
            """,
            (domain_id,),
        ).fetchall()
        closed = {
            r["biz_day"]: r
            for r in conn.execute(
                "SELECT * FROM closed_biz_days WHERE domain_id=?", (domain_id,)
            ).fetchall()
        }
        days = {r["biz_day"] for r in paste_rows} | set(closed)
        out = []
        for day in sorted(days):
            c = closed.get(day)
            out.append(
                {
                    "biz_day": day,
                    "paste_count": next(
                        (int(r["paste_count"]) for r in paste_rows if r["biz_day"] == day), 0
                    ),
                    "closed": c is not None,
                    "reason": c["reason"] if c else "",
                    "closed_at": c["created_at"] if c else None,
                }
            )
        return out
    finally:
        conn.close()


def layer_rows(table: str, domain_id: int | None = None) -> list[dict]:
    allowed = {
        "paste": "layer_paste",
        "detail": "layer_detail",
        "summary": "layer_summary",
    }
    if table not in allowed:
        raise ValueError("未知分层")
    sql = f"SELECT * FROM {allowed[table]}"
    args: tuple = ()
    if domain_id is not None:
        sql += " WHERE domain_id=?"
        args = (domain_id,)
    sql += " ORDER BY id"
    conn = connect()
    try:
        rows = rows_to_dicts(conn.execute(sql, args).fetchall())
    finally:
        conn.close()
    if table == "paste":
        # 贴源列表标注所属业务日是否已关闭，与关闭名单同源
        closed_map: dict[tuple, bool] = {}
        conn = connect()
        try:
            cr = conn.execute("SELECT domain_id, biz_day FROM closed_biz_days").fetchall()
            closed_map = {(r["domain_id"], r["biz_day"]): True for r in cr}
        finally:
            conn.close()
        for r in rows:
            r["closed"] = closed_map.get((r["domain_id"], r["biz_day"]), False)
    return rows


def layer_counts(domain_id: int | None = None) -> dict:
    conn = connect()
    try:
        where = " WHERE domain_id=?" if domain_id is not None else ""
        args = (domain_id,) if domain_id is not None else ()
        paste = conn.execute(f"SELECT COUNT(*) AS c FROM layer_paste{where}", args).fetchone()["c"]
        detail = conn.execute(f"SELECT COUNT(*) AS c FROM layer_detail{where}", args).fetchone()["c"]
        summary = conn.execute(f"SELECT COUNT(*) AS c FROM layer_summary{where}", args).fetchone()["c"]
        closed = conn.execute(
            f"SELECT COUNT(*) AS c FROM closed_biz_days{where}", args
        ).fetchone()["c"]
        return {
            "贴源层": int(paste),
            "明细层": int(detail),
            "汇总层": int(summary),
            "已关闭业务日": int(closed),
        }
    finally:
        conn.close()


def list_jobs(domain_id: int | None = None) -> list[dict]:
    conn = connect()
    try:
        if domain_id is None:
            rows = conn.execute("SELECT * FROM layer_jobs ORDER BY id DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM layer_jobs WHERE domain_id=? ORDER BY id DESC", (domain_id,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["step_label"] = STEP_LABEL.get(d["step"], d["step"])
            out.append(d)
        return out
    finally:
        conn.close()


def _insert_job(
    conn,
    domain_id: int,
    step: str,
    status: str,
    message: str,
    in_count: int,
    out_count: int,
    drop_count: int,
    reject_count: int = 0,
    biz_day: str | None = None,
    reason_code: str = "",
) -> dict:
    cur = conn.execute(
        """
        INSERT INTO layer_jobs(
          domain_id, step, status, message, in_count, out_count, drop_count,
          reject_count, reason_code, biz_day
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            domain_id,
            step,
            status,
            message,
            in_count,
            out_count,
            drop_count,
            reject_count,
            reason_code,
            biz_day,
        ),
    )
    return dict(conn.execute("SELECT * FROM layer_jobs WHERE id=?", (int(cur.lastrowid),)).fetchone())


def _job_status(processed: int, rejected: int) -> str:
    if processed == 0 and rejected > 0:
        return "拒绝"
    if rejected > 0:
        return "成功（含拒绝）"
    return "成功"


def load_paste_fixture(domain_id: int, biz_day: str | None = None) -> dict:
    """重新装载主题域贴源夹具；已关闭业务日的行一律拒绝并计数。"""
    domain = get_domain(domain_id)
    if not domain:
        raise ValueError("主题域不存在")
    if biz_day is not None:
        biz_day = validate_biz_day(biz_day)
    path = FIXTURES_DIR / domain["fixture_file"]
    if not path.exists():
        alt = FIXTURES_DIR.parent / "fixtures" / domain["fixture_file"]
        path = alt if alt.exists() else path
    if not path.exists():
        raise ValueError(f"夹具文件不存在：{domain['fixture_file']}")
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        records.append(json.loads(line))
    if biz_day is not None:
        records = [obj for obj in records if (obj.get("业务日") or "") == biz_day]

    conn = connect()
    try:
        closed = _closed_days(conn, domain_id)
        decisions: dict[str, tuple[bool, bool, str]] = {}

        # 定点装载关闭日：整单拒绝，不写贴源，拒绝落任务结果与裁决证明
        if biz_day is not None and biz_day in closed:
            reject_count = len(records)
            message = CLOSED_LOAD_REASON.format(day=biz_day, count=reject_count)
            job = _insert_job(
                conn,
                domain_id,
                "paste_load",
                "拒绝",
                message,
                reject_count,
                0,
                0,
                reject_count,
                biz_day,
                REASON_PASTE_CLOSED,
            )
            decisions[biz_day] = (True, False, REASON_PASTE_CLOSED)
            _record_close_decisions(conn, domain_id, job["id"], "paste_load", decisions)
            conn.commit()
            job["step_label"] = STEP_LABEL["paste_load"]
            return job

        # 全量装载：已关闭日全部拒绝跳过且不重写其贴源；未关闭日整批重写
        accepted: list[dict] = []
        rejected_days: dict[str, int] = {}
        for obj in records:
            day = obj.get("业务日") or ""
            if day in closed:
                rejected_days[day] = rejected_days.get(day, 0) + 1
                continue
            accepted.append(obj)

        target_days = {(obj.get("业务日") or "") for obj in accepted}
        if target_days:
            placeholders = ",".join("?" for _ in target_days)
            conn.execute(
                f"DELETE FROM layer_paste WHERE domain_id=? AND biz_day IN ({placeholders})",
                (domain_id, *target_days),
            )
        for obj in accepted:
            conn.execute(
                """
                INSERT INTO layer_paste(
                  domain_id, biz_day, store_code, store_name, category, amount, qty, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    domain_id,
                    obj.get("业务日") or "",
                    obj.get("门店编码") or "",
                    obj.get("门店名称") or "",
                    obj.get("品类") or "",
                    float(obj.get("金额") or 0),
                    int(obj.get("件数") or 0),
                    json.dumps(obj, ensure_ascii=False),
                ),
            )

        reject_count = sum(rejected_days.values())
        # 裁决证明：拒绝日与放行日都要落，与本次装载实际裁决同源
        for day, count in sorted(rejected_days.items()):
            decisions[day] = (True, False, REASON_PASTE_CLOSED)
        for day in sorted(target_days):
            decisions[day] = (False, True, "")

        notes = [
            CLOSED_LOAD_REASON.format(day=day, count=count)
            for day, count in sorted(rejected_days.items())
        ]
        message = f"贴源装载完成，写入{len(accepted)}行"
        if notes:
            message += "；" + "；".join(notes)
        if not records:
            message = "夹具中没有匹配的业务日数据，未写入任何行"
        status = _job_status(len(accepted), reject_count)
        reason_code = REASON_PASTE_CLOSED if reject_count else ""
        job = _insert_job(
            conn,
            domain_id,
            "paste_load",
            status,
            message,
            len(records),
            len(accepted),
            0,
            reject_count,
            biz_day,
            reason_code,
        )
        _record_close_decisions(conn, domain_id, job["id"], "paste_load", decisions)
        conn.commit()
        job["step_label"] = STEP_LABEL["paste_load"]
        return job
    finally:
        conn.close()


def run_paste_to_detail(domain_id: int, biz_day: str | None = None) -> dict:
    domain = get_domain(domain_id)
    if not domain:
        raise ValueError("主题域不存在")
    if biz_day is not None:
        biz_day = validate_biz_day(biz_day)
    conn = connect()
    try:
        closed = _closed_days(conn, domain_id)
        decisions: dict[str, tuple[bool, bool, str]] = {}

        # 定点重算关闭日：整单拒绝（400），拒绝已落任务结果与裁决证明
        if biz_day is not None and biz_day in closed:
            paste = conn.execute(
                "SELECT * FROM layer_paste WHERE domain_id=? AND biz_day=? ORDER BY id",
                (domain_id, biz_day),
            ).fetchall()
            reject_count = len(paste)
            message = CLOSED_JOB_REASON.format(
                day=biz_day, count=reject_count, step=STEP_LABEL["paste_to_detail"]
            )
            job = _insert_job(
                conn,
                domain_id,
                "paste_to_detail",
                "拒绝",
                message,
                reject_count,
                0,
                0,
                reject_count,
                biz_day,
                REASON_STEP_CLOSED,
            )
            decisions[biz_day] = (True, False, REASON_STEP_CLOSED)
            _record_close_decisions(conn, domain_id, job["id"], "paste_to_detail", decisions)
            conn.commit()
            job["step_label"] = STEP_LABEL["paste_to_detail"]
            raise RejectError(message, job)

        if biz_day is not None:
            conn.execute(
                "DELETE FROM layer_detail WHERE domain_id=? AND biz_day=?",
                (domain_id, biz_day),
            )
            paste = conn.execute(
                "SELECT * FROM layer_paste WHERE domain_id=? AND biz_day=? ORDER BY id",
                (domain_id, biz_day),
            ).fetchall()
            target_days = {biz_day}
        else:
            paste = conn.execute(
                "SELECT * FROM layer_paste WHERE domain_id=? ORDER BY id", (domain_id,)
            ).fetchall()
            # 关闭名单中的日子即便没有贴源行也要给出裁决
            target_days = {r["biz_day"] for r in paste} | closed
            open_days = target_days - closed
            if open_days:
                placeholders = ",".join("?" for _ in open_days)
                conn.execute(
                    f"DELETE FROM layer_detail WHERE domain_id=? AND biz_day IN ({placeholders})",
                    (domain_id, *sorted(open_days)),
                )

        out = drop = reject_count = 0
        rejected_days: dict[str, int] = {}
        for row in paste:
            if row["biz_day"] in closed:
                # 全量重跑不触碰关闭日：跳过并计数，已有明细原样保留
                reject_count += 1
                rejected_days[row["biz_day"]] = rejected_days.get(row["biz_day"], 0) + 1
                continue
            store_name = (row["store_name"] or "").strip()
            amount = float(row["amount"])
            if not store_name or amount < 0:
                drop += 1
                continue
            conn.execute(
                """
                INSERT INTO layer_detail(
                  domain_id, biz_day, store_code, store_name, category, amount, qty, clean_flag
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    domain_id,
                    row["biz_day"],
                    row["store_code"],
                    store_name,
                    row["category"],
                    amount,
                    int(row["qty"]),
                    "已清洗",
                ),
            )
            out += 1

        for day in sorted(target_days):
            if day in closed:
                decisions[day] = (True, False, REASON_SKIP_CLOSED)
            else:
                decisions[day] = (False, True, "")

        notes = [
            CLOSED_SKIP_REASON.format(day=day, count=count)
            for day, count in sorted(rejected_days.items())
        ]
        message = "清洗完成，剔除门店名为空或金额为负的行"
        if notes:
            message += "；" + "；".join(notes)
        reason_code = REASON_SKIP_CLOSED if reject_count else ""
        status = _job_status(out, reject_count)
        job = _insert_job(
            conn,
            domain_id,
            "paste_to_detail",
            status,
            message,
            len(paste),
            out,
            drop,
            reject_count,
            biz_day,
            reason_code,
        )
        _record_close_decisions(conn, domain_id, job["id"], "paste_to_detail", decisions)
        conn.commit()
        job["step_label"] = STEP_LABEL["paste_to_detail"]
        return job
    finally:
        conn.close()


def run_detail_to_summary(domain_id: int, biz_day: str | None = None) -> dict:
    domain = get_domain(domain_id)
    if not domain:
        raise ValueError("主题域不存在")
    if biz_day is not None:
        biz_day = validate_biz_day(biz_day)
    conn = connect()
    try:
        closed = _closed_days(conn, domain_id)
        decisions: dict[str, tuple[bool, bool, str]] = {}

        # 定点重算关闭日：整单拒绝（400），拒绝已落任务结果与裁决证明
        if biz_day is not None and biz_day in closed:
            detail = conn.execute(
                "SELECT * FROM layer_detail WHERE domain_id=? AND biz_day=? ORDER BY id",
                (domain_id, biz_day),
            ).fetchall()
            reject_count = len(detail)
            message = CLOSED_JOB_REASON.format(
                day=biz_day, count=reject_count, step=STEP_LABEL["detail_to_summary"]
            )
            job = _insert_job(
                conn,
                domain_id,
                "detail_to_summary",
                "拒绝",
                message,
                reject_count,
                0,
                0,
                reject_count,
                biz_day,
                REASON_STEP_CLOSED,
            )
            decisions[biz_day] = (True, False, REASON_STEP_CLOSED)
            _record_close_decisions(conn, domain_id, job["id"], "detail_to_summary", decisions)
            conn.commit()
            job["step_label"] = STEP_LABEL["detail_to_summary"]
            raise RejectError(message, job)

        if biz_day is not None:
            conn.execute(
                "DELETE FROM layer_summary WHERE domain_id=? AND biz_day=?",
                (domain_id, biz_day),
            )
            detail = conn.execute(
                "SELECT * FROM layer_detail WHERE domain_id=? AND biz_day=? ORDER BY id",
                (domain_id, biz_day),
            ).fetchall()
            target_days = {biz_day}
        else:
            detail = conn.execute(
                "SELECT * FROM layer_detail WHERE domain_id=? ORDER BY id", (domain_id,)
            ).fetchall()
            # 关闭名单中的日子即便没有明细行也要给出裁决
            target_days = {r["biz_day"] for r in detail} | closed
            open_days = target_days - closed
            if open_days:
                placeholders = ",".join("?" for _ in open_days)
                conn.execute(
                    f"DELETE FROM layer_summary WHERE domain_id=? AND biz_day IN ({placeholders})",
                    (domain_id, *sorted(open_days)),
                )

        buckets: dict[tuple, dict] = {}
        reject_count = 0
        rejected_days: dict[str, int] = {}
        for row in detail:
            if row["biz_day"] in closed:
                # 全量重跑不触碰关闭日：跳过并计数，已有汇总原样保留
                reject_count += 1
                rejected_days[row["biz_day"]] = rejected_days.get(row["biz_day"], 0) + 1
                continue
            key = (row["biz_day"], row["store_code"])
            b = buckets.setdefault(
                key,
                {"biz_day": row["biz_day"], "store_code": row["store_code"], "amount": 0.0, "qty": 0, "n": 0},
            )
            b["amount"] += float(row["amount"])
            b["qty"] += int(row["qty"])
            b["n"] += 1
        for b in buckets.values():
            conn.execute(
                """
                INSERT INTO layer_summary(
                  domain_id, biz_day, store_code, amount_sum, qty_sum, line_count
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (domain_id, b["biz_day"], b["store_code"], b["amount"], b["qty"], b["n"]),
            )

        for day in sorted(target_days):
            if day in closed:
                decisions[day] = (True, False, REASON_SKIP_CLOSED)
            else:
                decisions[day] = (False, True, "")

        notes = [
            CLOSED_SKIP_REASON.format(day=day, count=count)
            for day, count in sorted(rejected_days.items())
        ]
        message = "按业务日+门店编码汇总"
        if notes:
            message += "；" + "；".join(notes)
        reason_code = REASON_SKIP_CLOSED if reject_count else ""
        status = _job_status(len(buckets), reject_count)
        job = _insert_job(
            conn,
            domain_id,
            "detail_to_summary",
            status,
            message,
            len(detail),
            len(buckets),
            0,
            reject_count,
            biz_day,
            reason_code,
        )
        _record_close_decisions(conn, domain_id, job["id"], "detail_to_summary", decisions)
        conn.commit()
        job["step_label"] = STEP_LABEL["detail_to_summary"]
        return job
    finally:
        conn.close()


def run_reconcile(domain_id: int) -> dict:
    domain = get_domain(domain_id)
    if not domain:
        raise ValueError("主题域不存在")
    conn = connect()
    try:
        paste_count = int(
            conn.execute("SELECT COUNT(*) AS c FROM layer_paste WHERE domain_id=?", (domain_id,)).fetchone()[
                "c"
            ]
        )
        detail_count = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM layer_detail WHERE domain_id=?", (domain_id,)
            ).fetchone()["c"]
        )
        summary = conn.execute(
            "SELECT COALESCE(SUM(line_count),0) AS s, COALESCE(SUM(amount_sum),0) AS a FROM layer_summary WHERE domain_id=?",
            (domain_id,),
        ).fetchone()
        summary_line_sum = int(summary["s"])
        summary_amount = float(summary["a"])
        detail_amount = float(
            conn.execute(
                "SELECT COALESCE(SUM(amount),0) AS a FROM layer_detail WHERE domain_id=?",
                (domain_id,),
            ).fetchone()["a"]
        )
        closed_rows = conn.execute(
            "SELECT biz_day FROM closed_biz_days WHERE domain_id=? ORDER BY biz_day",
            (domain_id,),
        ).fetchall()
        closed_days = [r["biz_day"] for r in closed_rows]
        key_mismatch = 1 if summary_line_sum != detail_count else 0
        amount_drift = round(summary_amount - detail_amount, 4)
        status = "一致" if key_mismatch == 0 and abs(amount_drift) < 0.0001 else "存在差异"
        detail = {
            "贴源行数": paste_count,
            "明细行数": detail_count,
            "汇总行数合计": summary_line_sum,
            "明细金额合计": detail_amount,
            "汇总金额合计": summary_amount,
            "键对齐": key_mismatch == 0,
            "金额漂移": amount_drift,
            "已关闭业务日": closed_days,
        }
        cur = conn.execute(
            """
            INSERT INTO reconcile_reports(
              domain_id, paste_count, detail_count, summary_line_sum, key_mismatch, amount_drift, status, detail_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                domain_id,
                paste_count,
                detail_count,
                summary_line_sum,
                key_mismatch,
                amount_drift,
                status,
                json.dumps(detail, ensure_ascii=False),
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM reconcile_reports WHERE id=?", (int(cur.lastrowid),)).fetchone()
        d = dict(row)
        d["detail"] = json.loads(d.pop("detail_json"))
        return d
    finally:
        conn.close()


def list_reconcile(domain_id: int | None = None) -> list[dict]:
    conn = connect()
    try:
        if domain_id is None:
            rows = conn.execute("SELECT * FROM reconcile_reports ORDER BY id DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM reconcile_reports WHERE domain_id=? ORDER BY id DESC",
                (domain_id,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json"))
            out.append(d)
        return out
    finally:
        conn.close()


def dashboard() -> dict:
    domains = list_domains()
    counts = layer_counts()
    jobs = list_jobs()[:5]
    reports = list_reconcile()[:5]
    return {
        "主题域数": len(domains),
        "分层计数": counts,
        "最近任务": [
            {
                "编号": j["id"],
                "步骤": STEP_LABEL.get(j["step"], j["step"]),
                "状态": j["status"],
                "写入": j["out_count"],
                "剔除": j["drop_count"],
                "拒绝": j["reject_count"],
            }
            for j in jobs
        ],
        "最近对账": [
            {
                "编号": r["id"],
                "状态": r["status"],
                "明细行数": r["detail_count"],
                "汇总行数合计": r["summary_line_sum"],
            }
            for r in reports
        ],
        "主题域": [{"编号": d["id"], "名称": d["name"], "业务键": d["biz_key"]} for d in domains],
    }
