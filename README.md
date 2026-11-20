# 实验室 FHIR R4 报告接入研究库（Lab FHIR Research Ingest API）

FastAPI 实现的 FHIR R4 事务型 Bundle 接入服务。检验科报告包以 transaction
Bundle 提交，服务在**单数据库事务**内完成条件建患、观测/报告的新增或版本化
更新，并通过**接入单号幂等账本**保证“同内容重试复用首次回执、异内容拒绝、
响应丢失/进程重启不重复应用”。

支持两种数据库：PostgreSQL（生产，行锁 MVCC）与 SQLite（单文件，可被**多个
API 实例共享**）。SQLite 模式下通过显式 `BEGIN IMMEDIATE` 写事务 + WAL 把
“校验—写入—提交”放进同一个写锁内，保证双实例并发提交的结果等价于依次提交。

## 1. 一键启动（Docker Compose，双实例共享 SQLite）

```bash
docker compose up --build
```

启动 **两个 API 实例**，它们挂载同一个命名卷 `sqlite_data`，共用同一个数据库
文件 `/data/research.db`：

- 实例 1：<http://localhost:8001/docs>
- 实例 2：<http://localhost:8002/docs>
- OpenAPI：`/openapi.json`；健康检查：`/health`

两个实例都可从 `/docs` 直接发起 `POST /fhir` 并查询结果；无论请求落在哪个
实例，读到的都是同一个库。容器启动时执行 `app/db_init.py` 建表（并发建表
由写锁 + 重试保证安全）。

> 如需 PostgreSQL，把两个服务的 `DATABASE_URL` 改为
> `postgresql+psycopg2://...` 并去掉共享卷即可；业务语义完全一致。

## 2. 提交报告包

`POST /fhir`，请求头必须携带接入单号：

```
Idempotency-Key: LAB-ACC-1001
Content-Type: application/json
```

示例（注意条目故意乱序：报告 → 观测 → 患者，依赖 `urn:uuid` 前向引用）：

```json
{
  "resourceType": "Bundle",
  "type": "transaction",
  "entry": [
    {
      "fullUrl": "urn:uuid:3c3f9d7e-0003-4b2a-9e1a-000000000001",
      "resource": {
        "resourceType": "DiagnosticReport",
        "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "24323-8"}]},
        "subject": {"reference": "urn:uuid:3c3f9d7e-0001-4b2a-9e1a-000000000001"},
        "result": [
          {"reference": "urn:uuid:3c3f9d7e-0002-4b2a-9e1a-000000000001"}
        ]
      },
      "request": {"method": "POST", "url": "DiagnosticReport"}
    },
    {
      "fullUrl": "urn:uuid:3c3f9d7e-0002-4b2a-9e1a-000000000001",
      "resource": {
        "resourceType": "Observation",
        "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "2345-7"}]},
        "subject": {"reference": "urn:uuid:3c3f9d7e-0001-4b2a-9e1a-000000000001"},
        "valueQuantity": {"value": 5.5, "unit": "mmol/L"}
      },
      "request": {"method": "POST", "url": "Observation"}
    },
    {
      "fullUrl": "urn:uuid:3c3f9d7e-0001-4b2a-9e1a-000000000001",
      "resource": {
        "resourceType": "Patient",
        "identifier": [{"system": "http://hospital/mrn", "value": "MRN-001"}]
      },
      "request": {"method": "POST", "url": "Patient"}
    }
  ]
}
```

### 更新（带 If-Match 的乐观锁）

```json
{
  "resourceType": "Bundle",
  "type": "transaction",
  "entry": [
    {
      "resource": {
        "resourceType": "Observation",
        "id": "<已有观测id>",
        "status": "final",
        "code": {"coding": [{"code": "2345-7"}]},
        "subject": {"reference": "Patient/<患者id>"},
        "valueQuantity": {"value": 6.8, "unit": "mmol/L"}
      },
      "request": {
        "method": "PUT",
        "url": "Observation/<已有观测id>",
        "ifMatch": "W/\"1\""
      }
    }
  ]
}
```

- `ifMatch` 与当前版本不一致 → `412 Precondition Failed`，整包拒绝。
- 更新包内引用已存在资源请使用绝对引用（`Patient/<id>`、`Observation/<id>`）；
  `urn:uuid` 仅在同一 Bundle 内、且对应条目存在时有效。

## 3. 回执与读取接口

提交成功返回 `transaction-response` Bundle：

- `entry[].response`：FHIR 风格的 status / etag / location；
- `resourceMap[]`：按请求顺序给出每个条目的 `resourceType / id / version / action / fullUrl` 映射。

读取接口（均支持按患者维度）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/patients?identifier=system|value` | 按标识查患者 |
| GET | `/patients/{id}` | 患者摘要（含报告/观测计数） |
| GET | `/patients/{id}/observations` | 该患者全部观测 |
| GET | `/patients/{id}/diagnostic-reports` | 该患者全部报告（含观测 id 列表） |
| GET | `/observations/{id}` | 单个观测（响应头带 `ETag: W/"v"`） |
| GET | `/diagnostic-reports/{id}` | 单个报告 |
| GET | `/observations/{id}/_history` | 观测全部历史版本 |
| GET | `/diagnostic-reports/{id}/_history` | 报告全部历史版本 |
| GET | `/patients/{id}/_history` | 患者历史版本 |

## 4. 业务规则与失败语义

1. **条目上限 20**，仅允许 `Patient / Observation / DiagnosticReport`；
   超限或类型不符 → `400`。
2. **患者条件创建**：按 `identifier.system/value` 命中则复用，否则新建；
   包内标识重复 → `400`；并发同标识由数据库唯一约束 + 行锁保证只建一份。
3. **乱序前向引用**：`urn:uuid` 在解析阶段统一绑定，与条目顺序无关；
   无法解析、引用不存在的外部资源 → `422`，整包拒绝。
4. **患者一致性**：每份报告与其全部观测必须同属一个患者；把观测改挂其他
   患者时，若已有引用报告未在**同一 Bundle** 内一并更正 → `422`。
5. **原子性**：任何校验/版本/引用失败都会回滚整个事务，不留任何资源、患者
   标识、报告-观测关联或版本记录；其他患者已提交的数据不受影响，后续合法
   Bundle 仍可正常提交。原子性在 SQLite 下同样成立：事务以显式 `BEGIN` 开启
   （驱动 `isolation_level=None`），`SAVEPOINT` 不会把外层事务提前提交。
6. **双实例并发（共享 SQLite）**：每个写请求先取 `BEGIN IMMEDIATE` 写锁，
   “归属校验 → 写入 → 提交”全程持有该锁，因此双实例的提交等价于依次提交，
   不会出现校验通过后另一实例改坏归属却仍落库（写偏斜）。竞争失败的一方返回
   `422`（业务校验未通过）或 `503`（写锁短暂竞争，可用同一单号重试），绝不
   静默合并。仅更新既有资源的包同样在写锁内校验，另一实例无法在“校验后、
   写入前”插入无关患者的检验包。
7. **幂等**：
   - 同 `Idempotency-Key` + 同内容（规范化 JSON 哈希，与键序/空白无关）→
     直接返回首次回执，不重复落库；
   - 同 key + 异内容 → `409`（已完成记录及其回执保持不变）；
   - 失败的提交会释放幂等槽，修正后可用同一单号重试；
   - 幂等状态持久化在数据库，进程重启/响应丢失/跨实例重试仍然安全。
8. 所有错误均返回 FHIR `OperationOutcome`（`application/fhir+json`）。

## 5. 本地开发与自测（无需 Docker）

```bash
pip install -r requirements.txt

# SQLite（含双实例共享文件场景）
export DATABASE_URL="sqlite:////tmp/research.db"
python -m app.db_init
uvicorn app.main:app --reload
pytest -q

# PostgreSQL（并发/行锁语义）
export DATABASE_URL="postgresql+psycopg2://research:research@localhost:5432/researchdb"
```

测试默认连接 `postgresql+psycopg2://research@localhost:5433/researchdb_test?host=/tmp`，
可用 `DATABASE_URL` 覆盖。`tests/test_sqlite_two_instances.py` 用两个独立
engine/会话工厂模拟两个实例共享同一 SQLite 文件，覆盖归属校验竞争、写锁串行化、
拒收回滚与单号复用。

## 6. 目录结构

```
app/
  main.py            FastAPI 入口、异常处理
  config.py          环境配置
  database.py        引擎/会话/建表（PG 行锁；SQLite 显式 BEGIN IMMEDIATE + WAL）
  models.py          ORM（患者/标识/观测/报告/关联/版本/幂等账本，PG JSONB）
  errors.py          OperationOutcome 错误体系
  fhir_utils.py      ETag/版本工具
  routers/           /fhir 提交（写会话）与资源读取（读会话）路由
  services/
    bundle.py        Bundle 校验、幂等、事务处理、患者一致性规则
    queries.py       读取与历史查询
scripts/entrypoint.sh  容器启动（建表，支持并发）
scripts/smoke.sh       端到端冒烟（可用 SECOND_BASE_URL 验证双实例）
tests/                 pytest 用例（含双实例 SQLite 并发、重启、原子性）
docker-compose.yml     api-1 + api-2 两实例共享 SQLite 卷，含健康检查
```
