# 05-tiercraft（筑层）

筑层 — 数仓分层建设台：贴源入仓 → 明细清洗标准化 → 汇总落表 → 层间行数/键对账。

**不是** 多源 Feed 归并，**不是** 作业依赖编排，**不是** CDC 变更流按序落库，**不是** 质检隔离签发。
**也不是** 报表看板 / CRUD 后台 / 电商订单前台 / OA 工单。

## 启动

```bash
docker compose up --build
```

本地开发：

```bash
# 后端
cd backend
pip install -r requirements.txt
set DATA_DIR=..\data
uvicorn app.main:app --reload --port 10500

# 前端
cd frontend
npm install
npm run dev
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://localhost:5500 |
| 接口 | http://localhost:10500 |

## 主链

登记主题域 → 贴源层装载夹具 → 明细层清洗标准化 → 汇总层聚合 → 层间对账报告与各层列表数字同源

## 业务日关闭

主题域可维护业务日关闭名单（独立业务日关闭表 `closed_biz_days`，界面「业务日关闭」），关闭后形成写路径双拒绝，数据落在本地 SQLite，刷新与重启后仍在：

- 已关闭日禁止再写入该日贴源：重新装载时跳过该日全部行，装载结果按日记录拒绝行数与原因；针对关闭日的定点装载整单拒绝。
- 已关闭日禁止触发针对该日的贴源到明细、明细到汇总：定点重算直接拒绝（HTTP 400）并写一条「拒绝」任务；全量重跑不触碰关闭日已有数据，自动跳过并在任务结果注明跳过行数。
- 拒绝原因与计数进入装载/任务结果（`reject_count`、说明文案与机读原因码 `reason_code`），原因码随关闭名单刷新保持一致，与贴源按日计数、关闭名单、任务列表同源；重新打开业务日后行为恢复。

### 拒绝原因码（layer_jobs.reason_code）

| 原因码 | 含义 |
| --- | --- |
| `BIZ_DAY_CLOSED_PASTE_LOAD` | 已关闭日贴源装载被拒绝/跳过 |
| `BIZ_DAY_CLOSED_LAYER_JOB` | 已关闭日定点分层任务（贴源到明细、明细到汇总）被拒绝 |
| `BIZ_DAY_CLOSED_SKIPPED` | 全量重跑跳过已关闭日 |

### 关闭裁决证明（close_decisions）

每次装载或分层任务对其涉及的每个目标日落一条裁决证明，字段含主题域、目标日 `biz_day`、是否关闭 `is_closed`、写路径是否放行 `write_allowed`、关联任务编号 `job_id`、环节 `step` 与原因码；历史证明不随后续重新打开而改写。

只读接口（仅 GET，写方法返回 405）：

- `GET /api/close-decisions?job_id=<任务编号>`：按分层任务编号查询
- `GET /api/close-decisions?load_id=<装载任务编号>`：按贴源装载 id 查询
- 另支持 `domain_id` 过滤；主题域「业务日关闭」界面与「分层任务」「贴源层」界面均展示关闭名单与证明字段。

- 未关闭日行为与此前完全一致。

## 界面约定

页面文案全部中文；分层称「贴源层 / 明细层 / 汇总层」，界面不出现英文缩写站名。
