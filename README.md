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
- `POST /api/reports/{id}/receipts`：登记监管回执。`outcome=accepted` 需 `authority_ref`（监管编号），受理后报告完成并锁定；`outcome=rejected` 需 `reason`（退件原因）和 `correction_due_at`（补件期限），报告回到待更正，修正后重提同一国家。
- `GET /api/reports/{id}`：报告的历次提交与回执记录。
- `GET /api/receipts/overdue`：已提交但超过回执期限未收到结果的报告，按区域过滤。
- `POST /api/cases/{id}/merge`：全局管理员合并重复案例。
- `POST /api/escalate-overdue`、`GET /api/overdue`：逾期检查与升级。

## 报告状态流转

`pending`/`overdue` → 提交 → `awaiting_receipt`（待回执）→ 受理 → `accepted`（完成并锁定内容）；退件 → `correction_pending`（待更正）→ 重提 → `awaiting_receipt`。回执判定（`ReceiptPolicy` 纯状态机）、数据记录（`submissions`/`receipts` 表）与页面操作（`static/index.html`）分层实现。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

该实现使用请求头模拟身份，不含生产级登录、签名和密钥管理；SQLite 与标准库 HTTP 服务适合单机原型。分国家规则采用内置严重 15 天、死亡 7 天、非严重 90 天规则，接入真实监管网关前需按当地法规扩展。
