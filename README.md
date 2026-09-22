# fastapi-token-service

CaloPlan 的 Token Usage / Quota 微服务（独立 FastAPI，端口 **9096**）。

职责：LLM Token 使用记录、配额管理、防滥用控制。**不**是计费/Billing 系统。

```
Client（只读 UI）
   │  GET usage / quota / remaining
   ▼
fastapi-chat-service（门户转发，仅 GET）
   │  check / consume（内部密钥）
   ▼
fastapi-token-service
   │
   ▼
Meta Storage（mservice-fastapi-metastorage，复用现有基础设施）
```

## 安全模型

- **Client 永远不是可信来源**：写端点（`check` / `consume`）不对外暴露。
- 写端点需 **双认证**：用户 JWT + 内部密钥 `X-Token-Internal-Key`。
  - 内部密钥为空（`TOKEN_INTERNAL_KEYS=[]`）时，写端点一律 **403**（安全默认）。
  - 部署上 token-service 不映射公网端口，仅 chat-service 同网络可访问。
- 查询端点（`usage` / `quota` / `remaining`）经 chat-service 门户转发，client 不直连。
- 所有 Token 检查、扣减只发生在 Server；真实 LLM usage 由 chat-service 取得后回传。

## API

所有端点前缀 `/api/v1/token`，均需 `Authorization: Bearer <jwt>`。

| 方法 | 路径 | 认证 | 说明 |
|---|---|---|---|
| POST | `/check` | JWT + 内部密钥 | LLM 调用前检查 `estimated_tokens` |
| POST | `/consume` | JWT + 内部密钥 | LLM 完成后按真实 usage 记账 |
| GET | `/usage` | JWT | 使用快照（累计+当日+当月） |
| GET | `/quota` | JWT | 当前用户生效配额 |
| GET | `/remaining` | JWT | 今日已用 / 今日剩余 |

`check` 返回 `{allowed, reason, usage, quota}`；`reason`：
`per_request_exceeded` / `daily_limit_exceeded` / `meta_unavailable`。

## Meta 数据类型注册

复用现有 Meta Storage，**不引入新数据库**。首次部署需在 metastorage 注册两个 type。

- 接口：`POST /api/v1/types`，**需 superuser / GLOBAL scope**（用超管 JWT 调）。
- schema 格式是 metastorage 自定义的 `{ "fields": { 字段名: { type, required } } }`，**不是 JSON Schema**。
- metastorage 创建 entry 时按此 schema 动态校验，**未声明的字段会被静默丢弃**；所以下面只列代码真正落库的字段。
  - `today_*` / `month_*` 是读时聚合出来的，**不落库，不要注册**；
  - `daily_usage` 是动态日期 key 的嵌套字典，用裸 `dict` 保留全部内容（含 model/provider）。

```bash
# 1) token_usage（每用户单份，entity_key = user_id）
curl -X POST http://localhost:9093/api/v1/types \
  -H "Authorization: Bearer <superuser_jwt>" \
  -H "Content-Type: application/json" \
  -d '{
    "type_name": "token_usage",
    "service_name": "default",
    "description": "CaloPlan LLM Token 使用记录（每用户单份，entity_key=user_id）",
    "schema_json": {
      "fields": {
        "user_id":           { "type": "integer", "required": true },
        "input_tokens":      { "type": "integer" },
        "output_tokens":     { "type": "integer" },
        "total_tokens":      { "type": "integer" },
        "request_count":     { "type": "integer" },
        "daily_usage":       { "type": "dict" },
        "updated_at":        { "type": "string" }
      }
    }
  }'

# 2) token_quota（每用户单份覆盖；不配则回落到服务端全局默认配额）
curl -X POST http://localhost:9093/api/v1/types \
  -H "Authorization: Bearer <superuser_jwt>" \
  -H "Content-Type: application/json" \
  -d '{
    "type_name": "token_quota",
    "service_name": "default",
    "description": "CaloPlan 每用户 Token 配额覆盖（可选）",
    "schema_json": {
      "fields": {
        "user_id":            { "type": "integer", "required": true },
        "per_request_limit":  { "type": "integer" },
        "daily_limit":        { "type": "integer" },
        "updated_at":         { "type": "string" }
      }
    }
  }'
```

> - `service_name` 必须与调用方 JWT 的 service_name 一致（CaloPlan 默认 `default`），否则 entry 归属到别的 service。
> - 改 schema 用 `PUT /api/v1/types/{type_name}`，metastorage 仅允许**新增字段**（向后兼容）。
> - 两个 type_name 必须与 `app/services/token_service.py` 中 `TYPE_USAGE` / `TYPE_QUOTA` 常量一致。

## 配置（.env）

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | 9096 | 服务端口 |
| `USER_SERVICE_URL` | http://localhost:8000 | JWKS 公钥来源 |
| `META_SERVICE_URL` | http://localhost:9093 | Meta Storage |
| `TOKEN_PER_REQUEST_LIMIT` | 15000 | 单次请求上限 |
| `TOKEN_DAILY_LIMIT` | 100000 | 每日上限 |
| `TOKEN_MONTHLY_LIMIT` | 空（预留） | 月度上限，当前未启用 |
| `TOKEN_INTERNAL_KEYS` | 空 | 写端点允许的内部密钥列表；**为空则写端点全部 403** |
| `TOKEN_FAIL_CLOSED` | true | Meta 不可用时 check 拒绝（fail-closed） |

## 并发模型

Meta Storage 的 PUT 为 read-modify-write、无 CAS/expected_version。
当前实现：**进程内 per-user `asyncio.Lock`**，串行化同一用户的 check/consume，
避免"双 check 都放行 → 双 consume 超限"的竞态。

边界与升级路径（已在代码注释中标注）：
- 单实例部署足够；多实例水平扩展时进程内锁失效。
- 升级方案：改用 Meta Storage 的版本条件写（CAS）/ 分布式锁 / Redis 限流。
- 当前目标优先实现简单、可靠。

## 故障策略

- `check` 时 Meta 不可用 → `allowed=false, reason=meta_unavailable`（fail-closed，拒绝 LLM）。
- `consume` / 查询失败 → 路由映射 503。
- 不为 Token 服务故障而无限放开 LLM。

## 本地运行

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
pytest          # 13 passed
uvicorn app.main:app --port 9096 --reload
```

## 相关项目（CaloPlan 全家桶）

CaloPlan 全栈项目统一托管在 GitHub Organization [caloplan](https://github.com/caloplan)：

| 类型 | 项目 | 与本项目关系 |
| --- | --- | --- |
| 前端 | [coloplan-v2](https://github.com/caloplan/coloplan-v2) | 上层客户端：经 chat 门户只读展示 Token 用量 / 配额 |
| SDK | [caloplan-token](https://github.com/caloplan/caloplan-token) | 客户端 / 服务端 SDK：check / consume / usage / quota / remaining |
| SDK | [caloplan-chat](https://github.com/caloplan/caloplan-chat) | AI 对话模块（主要消费者：`token_guard` 在所有 LLM 调用路径过闸） |
| SDK | [caloplan-core](https://github.com/caloplan/caloplan-core) | 餐食 / 食物领域模块（**不感知** Token，Token 属基础设施） |
| SDK | [caloplan-user](https://github.com/caloplan/caloplan-user) | 用户 / 身体 / 营养目标模块（JWT 用户身份来源） |
| SDK | [caloplan-cache](https://github.com/caloplan/caloplan-cache) | 本地缓存（兄弟 SDK） |
| 微服务（本仓库） | [fastapi-token-service](https://github.com/caloplan/fastapi-token-service) | LLM Token 用量 / 配额微服务 |
| 微服务 | [fastapi-chat-service](https://github.com/caloplan/fastapi-chat-service) | AI 对话服务（门户转发只读查询；内部密钥调用写端点） |
| 微服务 | [mservice-fastapi-user](https://github.com/caloplan/mservice-fastapi-user) | 认证 / 用户微服务（JWKS 公钥来源，校验用户 JWT） |
| 微服务 | [mservice-fastapi-metastorage](https://github.com/caloplan/mservice-fastapi-metastorage) | 元数据存储（Token 用量 / 配额落库复用，不引入新数据库） |
| 微服务 | [fastapi-file-service](https://github.com/caloplan/fastapi-file-service) | 图片上传微服务 |
