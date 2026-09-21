# PLAN — fastapi-token-service + caloplan-token

> 状态：待确认（仅计划，未实现）
> 日期：2026-09-21
> 目录：
>
> `D:\Xia_Project\RNProject\fastapi-token-service`
>
> （新服务）、
>
> `D:\Xia_Project\RNProject\caloplan-token`
>
> （新 SDK）



***

## 1. 背景与目标

为 CaloPlan 增加 LLM Token 使用记录、配额管理与防滥用控制，作为**基础设施能力**存在：



* **fastapi-token-service**：独立部署的 FastAPI 服务，Server 侧唯一可信的 Token 执行方（记录 / 配额 / 消耗 / 策略）。

* **caloplan-token**：调用该服务的 TypeScript SDK，封装 `check / consume / getUsage / getQuota / getRemaining`。

**不是计费系统**：不做支付 / Billing / Stripe / 复杂 Ledger。当前目标 = 资源使用记录 + 限制 + 防滥用。

**安全原则（贯穿全部设计）**：Client 永远不是 Token 的可信来源。Client 只能查询展示，不能决定消耗、剩余或是否允许调用；所有检查、扣减在 Server 完成，`user_id` 一律从 JWT 取，body 不允许指定。



***

## 2. 现状探查结论（已读代码）



| 项目                              | 关键结论                                                                                                                                                                                                                                                                                                                         |
| ------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `fastapi-chat-service`          | 主消费者。LLM 调用集中在 `app/ai/service.py` 的 `run_chat` / `run_chat_stream` / `resolve_approval`，Controller（`routes/infra.py`）只透传 ——Token Guard 放 service 层即可全覆盖，无需改 Controller。JWT 认证：`core/security.py`（RS256 + JWKS 多 kid）+ `core/dependencies.py`（`get_current_user`，CurrentUser 含 `sub/user_id/service_name/role/type`）。端口 9095 |
| `fastapi-file-service`          | 最新服务模板：`app/main.py`(create\_app) + `app/api/v1/routes/` + `core/{config,security,dependencies}` + `schemas/` + `services/` + `utils/logger.py`。端口 9094                                                                                                                                                                      |
| `mservice-fastapi-metastorage`  | Meta API：`POST/GET/PUT/DELETE /api/v1/entries[/{type}/{key}]`、batch、versions、rollback。**PUT 为 read-modify-write（deep merge + version 自增），无 expected\_version / CAS**。创建 entry 时按 type 的 `schema_json` 动态校验 data。端口 9093                                                                                                      |
| `mservice-fastapi-user`         | JWT 签发方（RS256），端口 8000                                                                                                                                                                                                                                                                                                       |
| `caloplan-chat`                 | TS SDK 模式样板：`client / transport / mapper / models / errors / 单例`，ESM + NodeNext + strict，`tsx --test`。端口 9095 对接                                                                                                                                                                                                             |
| `caloplan-core / caloplan-user` | 纯仓储 / 服务 + 仓储模式，本次不触碰                                                                                                                                                                                                                                                                                                        |

**Meta 客户端对接事实**（对齐 chat service 的 `app/ai/tools/meta_client.py`）：请求头注入当前用户 JWT（`Authorization: Bearer <token>`），错误映射 401/403/404/409/422，`user_id` 由服务端注入。



***

## 3. 总体架构



```
&#x20;                   ┌─────────────────────┐

&#x20;                   │       Client        │  RN / Web —— 仅展示

&#x20;                   │  usage / quota UI   │  caloplan-token (TS SDK, 只读方法)

&#x20;                   └──────────┬──────────┘

&#x20;                              │ JWT（对话 + 查询门户）

&#x20;                              ▼

&#x20;                   ┌─────────────────────┐

&#x20;                   │ fastapi-chat-service│  Python (FastAPI)

&#x20;                   │  ai/service.py      │

&#x20;                   │  ├─ TokenGuard ─────┼── 所有 LLM 路径统一过闸（check/consume）

&#x20;                   │  └─ 查询门户 ───────┼── GET /api/v1/token/\* 转发（供 client 展示）

&#x20;                   └──────────┬──────────┘

&#x20;                              │ 内部密钥 X-Token-Internal-Key + 用户 JWT

&#x20;                        caloplan-token   TS SDK（Server 侧场景）

&#x20;                              │

&#x20;                              ▼

&#x20;                   ┌─────────────────────┐

&#x20;                   │ fastapi-token-service│ 端口 9096，仅内网（不映射公网）

&#x20;                   │  POST check/consume │ ← 内部：内部密钥 + 用户 JWT 双认证

&#x20;                   │  GET  usage/quota/  │ ← 内部：用户 JWT（经 chat 门户转发）

&#x20;                   │      remaining      │

&#x20;                   └──────────┬──────────┘

&#x20;                              ▼

&#x20;                        Meta Storage        type: token\_usage / token\_quota
```

**选择性暴露（已确认）**：



* **写端点（check /consume）不暴露**：仅服务间调用（chat service），需要内部密钥 + 用户 JWT 双重认证；token service 不映射公网端口，只能内网访问。

* **查询端点（usage /quota/remaining）**：client 不直连 token service，后期经 **chat service 门户**转发暴露（响应同构）；token service 的查询端点仅供 chat service / 管理侧内网调用。

LLM 调用时序：



```
Chat → TokenGuard.check()（fail-closed，拒绝则 503）

&#x20;    → LLM

&#x20;    → 真实 usage

&#x20;    → TokenGuard.consume()（以 Server 获得的真实 usage 为准）

&#x20;    → Chat response
```



***

## 4. 职责边界



| 负责（token 服务）                                         | 不负责（保持隔离）                                   |
| ---------------------------------------------------- | ------------------------------------------- |
| Token Usage / Quota / Consumption / Policy           | Meal / Food / Nutrition / Chat 业务 / User 业务 |
| 防滥用强制执行                                              | 计费 / 支付 / Ledger                            |
| 配额规则统一管理                                             | 让 `caloplan-core` 感知 Token（core 不感知）        |
| Chat Service 不直接操作 Token 的 Meta 数据（经 SDK / HTTP 客户端） | 为 Token 单独引入新数据库（复用 Meta）                   |



***

## 5. fastapi-token-service 设计

### 5.1 项目结构（对齐 fastapi-file-service 模板）



```
fastapi-token-service/

├── app/

│   ├── main.py                     # create\_app() + lifespan + /health

│   ├── api/v1/\_\_init\_\_.py          # api\_router 聚合

│   ├── api/v1/routes/token.py      # 5 个端点

│   ├── core/config.py              # Settings（含配额全局配置）

│   ├── core/security.py            # RS256 + JWKS（复制 chat/file 同款）

│   ├── core/dependencies.py        # get\_current\_user / require\_superuser

│   ├── schemas/token.py            # 请求/响应 Pydantic 模型

│   ├── services/token\_service.py   # 业务：check / consume / 配额解析

│   ├── clients/meta\_client.py      # Meta HTTP 客户端（对齐 meta\_client.py 模式）

│   ├── utils/logger.py             # 同款

│   └── proxy/log\_proxy.py          # 同款（按需）

├── tests/                          # pytest（asyncio\_mode=auto）

├── requirements.txt / requirements-dev.txt

├── Dockerfile / docker-compose.yml / .env.example / pytest.ini / README.md
```

**不引入**：数据库（无 SQLAlchemy）、Redis（v1 不用，见 5.5 升级路径）。

### 5.2 配置项（Settings）



```
APP\_NAME = "Token Service"        # PORT = 9096

USER\_SERVICE\_URL                  # JWKS 拉取（默认 http://localhost:8000）

META\_SERVICE\_URL                  # 默认 http://localhost:9093

ALLOWED\_SERVICE\_NAMES = \["default"]   # 对齐 chat/file

SUPERUSER\_USERNAMES / SUPERUSER\_USER\_IDS

\# 全局配额（额度规则统一由本服务管理，Chat Service 不硬编码）

TOKEN\_PER\_REQUEST\_LIMIT = 15000   # 单次请求上限

TOKEN\_DAILY\_LIMIT = 100000        # 每日上限

TOKEN\_MONTHLY\_LIMIT = None        # 预留，不实现

\# 选择性暴露：写端点（check/consume）内部访问密钥（chat service 持有；支持多值轮换）

TOKEN\_INTERNAL\_KEYS: List\[str] = \[]   # 为空 = 写端点禁用（安全默认）

\# 故障与调用

TOKEN\_META\_TIMEOUT\_SECONDS = 10.0

TOKEN\_FAIL\_CLOSED = True          # Meta 不可用时 check 一律拒绝
```

### 5.3 Meta 数据模型（复用现有 Meta Storage，不引入新数据库）

注册两个 type（经 metastorage 类型管理，schema\_json 宽松校验）：

`token_usage`（entity\_key = str (user\_id)，用户单例，无独立 id）：



```
{

&#x20; "user\_id": 123,

&#x20; "input\_tokens": 0,

&#x20; "output\_tokens": 0,

&#x20; "total\_tokens": 0,

&#x20; "request\_count": 0,

&#x20; "daily\_usage": {

&#x20;   "2026-09-21": { "input\_tokens": 0, "output\_tokens": 0, "total\_tokens": 0, "request\_count": 0 }

&#x20; },

&#x20; "updated\_at": "2026-09-21T12:00:00.000Z"

}
```



* `daily_usage` 按 UTC 日期键存储；月度统计由读取时聚合 daily 推导，**不单独落库**（不过度设计）。

* 首用惰性创建：check/consume 时 404 → 按当前结构初始化。

`token_quota`（entity\_key = str (user\_id)，可选 per-user 覆盖）：



```
{

&#x20; "user\_id": 123,

&#x20; "per\_request\_limit": null,

&#x20; "daily\_limit": null,

&#x20; "updated\_at": "2026-09-21T12:00:00.000Z"

}
```



* 配额解析：per-user entry 有值覆盖全局 settings；无 entry 或字段为 null → 全局配置。

* v1 只实现 `per_request_limit` / `daily_limit` 两个维度；`monthly_limit` 预留字段不参与强制。

### 5.4 API（prefix `/api/v1`，对齐现有风格；user\_id 一律来自 JWT）



| 方法 / 路径                | 访问控制                   | 请求体                                                | 响应                                                              | 说明                                                                                              |
| ---------------------- | ---------------------- | -------------------------------------------------- | --------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| `POST /token/check`    | **内部**：内部密钥 + 用户 JWT   | `{estimated_tokens?: int}`                         | `{allowed, reason?, usage, quota}`                              | LLM 调用前检查。`allowed=false` 附原因（per\_request\_exceeded /daily\_limit\_exceeded/meta\_unavailable） |
| `POST /token/consume`  | **内部**：内部密钥 + 用户 JWT   | `{input_tokens, output_tokens, model?, provider?}` | `{usage, quota, over_limit?}`                                   | LLM 完成后以真实 usage 记账。**不接受 total\_tokens 由客户端指定**，服务端 `input+output` 计算；model/provider 仅记录       |
| `GET /token/usage`     | 用户 JWT（内网；经 chat 门户转发） | —                                                  | `{usage}`                                                       | 当前用户累计 + 今日 + 本月                                                                                |
| `GET /token/quota`     | 用户 JWT（内网；经 chat 门户转发） | —                                                  | `{quota}`                                                       | 当前用户生效配额（per\_request /daily）                                                                   |
| `GET /token/remaining` | 用户 JWT（内网；经 chat 门户转发） | —                                                  | `{per_request:{limit,remaining}, daily:{limit,used,remaining}}` | 供 UI 展示（今日已用 / 剩余）                                                                              |



* 认证策略：


  * 所有端点先经 `get_current_user`（RS256 + JWKS，与 chat/file 一致），`user_id` 唯一来自 JWT；请求体 / 查询参数**不接受 user\_id 字段**（防伪造）。

  * **写端点（check/consume）追加内部密钥校验**：请求头 `X-Token-Internal-Key` 必须命中 `TOKEN_INTERNAL_KEYS`，否则 403。`TOKEN_INTERNAL_KEYS` 为空时写端点直接 403（安全默认，防误开放）。

  * 部署边界：token service 在 docker-compose 中**不映射公网端口**，仅服务内网可访问（docker 内部网络），查询端点不直接对 client 开放。

* 查询端点由 chat service 门户转发（见 §7）：chat 门户端点同构透传，client 只与 chat service 通信。

* superuser 查询他人（可选 `?user_id=`，仅 superuser 放行）—— 作为小扩展随实现带上，不阻塞主路径。

* 错误响应沿用 FastAPI 默认 `{"detail": ...}`（与 chat/meta 一致），SDK 按状态码分类。

### 5.5 核心逻辑与并发一致性

**check 语义**（保守，防超卖）：



1. 读当前 usage + 解析生效配额；

2. `estimated_tokens` 缺省时以 `per_request_limit` 作硬上限（仅校验日常剩余）；提供时校验 `estimated <= per_request_limit` 且 `daily_used + estimated <= daily_limit`；

3. Meta 不可达 / 超时 → `allowed=false, reason=meta_unavailable`（**fail-closed**）。

**consume 语义**：



1. 累加 `total/input/output`、`request_count`、当日 `daily_usage[date]`；

2. 若累计超过配额 → 照常记账，响应带 `over_limit=true`（实际已发生无法追回，由下一次 check 拦截，直到窗口重置）；

3. `model/provider` 存入当日记录（可追踪）。

**并发事实与方案（第 9 节要求）**：



* 事实：Meta PUT 是 read-modify-write（无 expected\_version / 无 CAS），同用户并发 consume 会丢更新（last-write-wins），理论上可超卖。

* **v1 方案（简单可靠）**：进程内 `per-user asyncio.Lock`（`dict[user_id, Lock]`），check /consume 在锁内串行执行 read → compute → write。当前各服务均为**单实例部署**（docker-compose 单副本），锁内串行保证同用户额度不超卖。

* **明确边界**：多实例横向扩容时进程内锁失效。

* **升级路径（后续，不在本期实现）**：

1. Redis 原子计数（`INCRBY` + 当日 TTL）做热路径扣减，Meta 仅持久汇总（Redis 基建 chat service 已有）；

2. 或 metastorage 增加 `expected_version` CAS（PUT 带 if-version-match），Meta 侧提供原子更新。

* consume 幂等性：v1 不做（chat service 单次尽力提交）；后续可加 `usage_id` 去重，本期记录为待办。

### 5.6 故障策略（第 10 节要求）



| 场景                          | 行为                                                           |
| --------------------------- | ------------------------------------------------------------ |
| check 时 token service 自身不可用 | 503（服务自身已承担防滥用职责，**不无限放开**）                                  |
| check 时 Meta 不可用            | `allowed=false`（fail-closed），chat service 拒绝 LLM 调用          |
| consume 时 Meta 不可用          | 记日志告警，不阻塞用户响应（Token 已消耗，无法追回；check 仍按 Meta 中已有记录执行）—— 上报监控关注 |

### 5.7 测试计划（pytest）



* check：允许 /per\_request 拒绝 /daily 拒绝 /estimated 缺省 / Meta 不可用 fail-closed / 错误映射；

* consume：首次创建、累加正确性、当日滚动（跨日期）、over\_limit 标记；

* quota：无 per-user entry 用全局、有 entry 覆盖、null 回退；

* 并发：同一用户并发 consume（多 task 打锁）最终 total 精确、不超卖；

* 认证：无 token 401、service\_name 白名单 403、body 带 user\_id 被忽略 / 拒绝；

* 路由：5 端点响应结构与 SDK wire 契约一致（契约测试）。



***

## 6. caloplan-token SDK 设计（TS，对齐 caloplan-chat 简化版）

**定位**：客户端（RN/Web）**只读展示** + 未来 Server 侧 TS 场景调用 check/consume。无 SSE、无本地历史 /cache。



* **两个使用场景，两个 baseURL**：


  * Client 场景：baseURL 指向 **chat service 门户**（`/api/v1/token/usage|quota|remaining`，响应同构），只暴露查询方法；

  * Server 侧场景：baseURL 指向 **token service**，check/consume 额外注入内部密钥头（`X-Token-Internal-Key`，由调用方配置提供）。

* SDK 方法：`check(req)` / `consume(req)` / `getUsage()` / `getQuota()` / `getRemaining()`；客户端文档明确 check/consume 仅服务端使用。



```
caloplan-token/

├── src/

│   ├── models/            # 前端领域类型（camelCase）：TokenUsage / TokenQuota / TokenRemaining / CheckResult / ConsumeResult

│   ├── transport/         # wire 契约（snake\_case，严格对齐 §5.4 响应）+ TokenTransport（fetch + Bearer + 错误分类）

│   ├── mapper/            # wire ↔ 领域 转换（集中，不手写字段穿透）

│   ├── errors/            # TokenError（kind: network / auth / backend），isTokenError 判断

│   ├── cptoken.ts         # createCPToken({baseURL, tokenProvider, fetchImpl?}) / getCPToken()

│   └── index.ts           # 分段聚合导出

├── package.json / tsconfig.json（ESM + NodeNext + strict，脚本同 caloplan-chat）

└── src/\*\*/\*.test.ts       # tsx --test，stub fetchImpl
```



* 方法：`check(req)` / `consume(req)` / `getUsage()` / `getQuota()` / `getRemaining()`；

* Token 仅透传（外部 `tokenProvider` 注入），SDK 内无登录 / 刷新；

* 错误分类对齐 caloplan-chat：401/403 → auth，其余 → backend，网络 → network；

* 测试：请求路径 / 请求头 /body 字段 / 响应映射（snake→camel）/ 错误分类 /token 为空不发请求。



***

## 7. fastapi-chat-service 集成设计（不重复 HTTP 细节）

**关键决策**：caloplan-token 是 TS SDK，无法在 Python 服务内运行。因此 Python 侧采用**单一&#x20;**`TokenClient`（对齐现有 `MetaClient` 模式：httpx 连接池 + 注入当前用户 JWT + 错误映射），并配 `TokenGuard` 统一闸门 ——HTTP 细节只存在于这一个类，Controller 零改动，满足 "不散落、不复制"。

新增（仅改 chat service 的 app/ai/ 与 config）：



| 文件                       | 职责                                                                                                                                                                                                           |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `app/ai/token_client.py` | `TokenClient`：check /consume/getUsage /getQuota/getRemaining，注入当前请求 JWT + 内部密钥头（`X-Token-Internal-Key`）                                                                                                      |
| `app/ai/token_guard.py`  | `TokenGuard`：`guard_llm_call(user, estimated_tokens)` async 上下文管理器 —— 进入时 `check()`（fail-closed，拒绝 → 503），退出时 `consume()`（用 pydantic-ai `result.usage` 真实值）；`estimated_tokens` 由当前轮 message 字符数 × 换算系数得出     |
| `app/routes/token.py`    | **查询门户**（供 client 展示）：`GET /api/v1/token/usage`、`GET /api/v1/token/quota`、`GET /api/v1/token/remaining`，用户 JWT 认证，内部转发 token service（同构响应）。**不放行 check/consume**——client 永远无法触达写接口                           |
| `app/ai/service.py`      | 在 `run_chat` / `run_chat_stream` / `resolve_approval` 的 `agent.run` / `agent.run_stream` 外包 `TokenGuard`（3 处，统一在调用层；Controller 不动）                                                                           |
| `app/core/config.py`     | 新增 `TOKEN_SERVICE_URL=http://localhost:9096`、`TOKEN_ENABLED=true`、`TOKEN_INTERNAL_KEY=`（写接口内部密钥，需与 token service 配置一致）、`TOKEN_ESTIMATE_FACTOR`、`TOKEN_REQUEST_TIMEOUT_SECONDS=10.0`、`TOKEN_FAIL_CLOSED=true` |



* 未来新增 `/regenerate` / `/continue` 等 LLM 入口：只要复用 `run_*` 编排函数即自动受控；若另起新编排函数，同样在调用层包 `TokenGuard`（不会忘在 Controller）。

* `TOKEN_ENABLED=false` 可一键旁路（本地无 token service 时开发用；生产默认 true）。

* 测试：TokenGuard 单测（stub TokenClient：check 拒绝 → 503 且不调 LLM；consume 失败不阻断）+ 已有 chat 测试回归。



***

## 8. 实施顺序



| 里程碑 | 内容                                                                                           | 验收                                                           |
| --- | -------------------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| M1  | token-service 骨架：config /security/dependencies /main/logger /health                          | `uvicorn` 启动，`/health` 200，/docs 可用                          |
| M2  | MetaClient + TokenRepository（读写 usage/quota，惰性创建）+ 配额解析                                      | 单测绿                                                          |
| M3  | TokenService（check /consume）+ 5 端点                                                           | curl 全端点通，单测绿                                                |
| M4  | per-user 并发锁 + fail-closed + 契约测试                                                            | 并发测试绿，pytest 全绿                                              |
| M5  | caloplan-token SDK（models /transport/mapper /errors/cptoken /index + 测试）                     | `tsc --noEmit` + `npm test` 绿                                |
| M6  | chat service 集成（TokenClient + TokenGuard + 查询门户路由 + config + 测试）                             | chat 单测绿，TokenGuard 用例覆盖 check 拒绝 /consume 失败；门户端点只转发查询、不暴露写 |
| M7  | Dockerfile / docker-compose（token service **不映射公网端口**，仅服务内网）/.env.example/ README / 类型注册脚本说明 | 按 README 可一键起服务；token service 公网不可达                          |

**顺序依赖**：M3 依赖 M2；M5 的 wire 契约依赖 M3 的响应结构；M6 依赖 M5 的契约（以 Python 端为权威源，SDK 对齐）。



***

## 9. 不做清单（红线）



* 不把 Token 逻辑放进 `caloplan-core`；不修改任何既有模块行为

* 不让 Client 扣减 / 决定 Token；不相信 Client 提交的 usage / 剩余量

* 不让 Chat Service 直接操作 Token 的 Meta 数据

* 不为 Token 引入新数据库（复用 Meta）

* 不实现支付 / Billing / Stripe / 复杂 Ledger

* 不在多个 Chat endpoint 复制 quota 判断（统一 TokenGuard）

* 不重造 Meta 存储机制



***

## 10. 风险与开放问题（请确认）

**已确认的决策**：



1. **轻量 FastAPI 服务**：不做 pip 包；写接口（check/consume）不对外暴露（内部密钥 + 用户 JWT 双认证，不映射公网端口），查询经 chat 门户转发。

2. **Python 侧对接**：TS SDK 不能跑在 Python chat service 内，chat service 用单一 `TokenClient`（对齐 MetaClient 模式）+ `TokenGuard` 统一闸门；`caloplan-token` TS SDK 供 client 展示（指向 chat 门户）与 Server 侧 TS 场景（指向 token service）使用。

**待确认 / 已知限制**：



1. **estimated\_tokens 估算**：v1 用当前轮输入字符数做代理（简单可靠）；更精确的 tokenizer 估算留待后续。

2. **consume 幂等**：v1 不做去重（单次尽力提交）；是否需要 `usage_id` 幂等键可后续加。

3. **Meta 类型注册**：需在 metastorage 注册 `token_usage` / `token_quota` 两个 type（含宽松 schema\_json）；计划以管理侧注册或启动时确保两种方式之一落地（实现时确认 types API 现有能力）。

4. **端口**：token service 取 **9096**（8000/9093/9094/9095 已占用）。

5. **内部密钥分发**：`TOKEN_INTERNAL_KEYS`（token 侧）与 `TOKEN_INTERNAL_KEY`（chat 侧）由部署环境配置注入，不入库不入代码。

确认后按 M1→M7 顺序实施。