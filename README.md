# 药物警戒案例处理系统

使用 Python 标准库实现的独立原型，覆盖多渠道案例接入、去重、随访更正、严重性医学裁定、分国家报告、逾期升级、跨区域权限和案例合并审计。

## 运行

要求 Python 3.11+。

```bash
python3 app.py --db pharmacovigilance.db
```

默认监听 `127.0.0.1:8201`。首页为 `http://127.0.0.1:8201/`，健康检查为 `/health`。

所有接口使用请求头 `X-User-Id`、`X-Role` 和区域角色必需的 `X-Region`。角色为 `reporter`、`regional_lead`、`medical_reviewer`、`global_admin`。

## 主要接口

- `POST /api/cases`：录入案例，`dedupe_key` 相同则返回已存在案例。
- `GET /api/cases`、`GET /api/cases/{id}`：按权限查询。
- `POST /api/cases/{id}/followups`：用 `expected_revision` 防止覆盖随访。
- `POST /api/cases/{id}/medical-review`：医学审核员更新严重性、死亡和关联性。
- `POST /api/cases/{id}/reports`、`POST /api/reports/{id}/submit`：生成并提交分国家报告，提交后进入待回执。
- `POST /api/reports/{id}/receipt`：登记监管回执。受理（`decision=accepted`）必填 `regulator_ref`，报告完成并锁定案例内容；退件（`decision=rejected`）必填 `reason` 和 `correction_due_at`，报告回到待更正。
- `POST /api/reports/{id}/resubmit`：退件后补件重提（同一国家），必填 `correction_note`。
- `GET /api/reports/{id}`：报告详情及历次提交、回执记录。
- `GET /api/reports/awaiting-receipt`：已提交但超过回执期限仍未收到监管结果的报告（按区域过滤）。
- `POST /api/cases/{id}/merge`：全局管理员合并重复案例。
- `POST /api/escalate-overdue`、`GET /api/overdue`：逾期检查与升级。

## 回执处置

报告状态机：`pending`/`overdue` → 提交 → `submitted`（待回执）→ 受理 → `accepted`（完成并锁定）；`submitted` → 退件 → `correction_pending`（待更正）→ 补件重提 → `submitted`。

- 提交时可传 `receipt_due_at` 指定期望回执期限，默认提交后 30 天；到期未收到结果的报告在首页"提醒"区单独列出。
- 受理后报告锁定，案例的随访和医学审核被拒绝（`case_locked`）。
- 每次提交和回执都写入 `submissions`、`receipts` 表，历史随时可查。
- 区域负责人只能提交、登记回执和补件重提本区域报告。
- 三层分开落地：`receipts.py` 为回执判定（纯规则校验与状态流转），`app.py` 的 `Repository` 负责数据记录，`static/index.html` 提供页面操作。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

该实现使用请求头模拟身份，不含生产级登录、签名和密钥管理；SQLite 与标准库 HTTP 服务适合单机原型。分国家规则采用内置严重 15 天、死亡 7 天、非严重 90 天规则，接入真实监管网关前需按当地法规扩展。
