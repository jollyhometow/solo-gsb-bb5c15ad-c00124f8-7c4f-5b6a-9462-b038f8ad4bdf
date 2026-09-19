# 有状态 API 场景模拟器（Stateful API Scenario Simulator）

用于前后端 / 多服务**联调**的有状态接口模拟器。用一份 YAML 描述路由、请求匹配条件、
响应模板和状态迁移；服务按**会话令牌**隔离状态，并在 **单个 SQLite 事务**内完成
「选择最高优先级匹配 → 渲染响应 → 推进状态」，并发请求不会重复触发同一条迁移。

- Python 3.12 · FastAPI · SQLite（WAL）· Jinja2（沙箱）· PyYAML
- 零外部依赖，镜像启动即自动完成依赖安装与数据库初始化

---

## 一条命令启动

```bash
docker compose up --build -d
```

启动后：

| 用途 | 地址 |
| --- | --- |
| 服务基址 | <http://localhost:8000> |
| 健康检查 | <http://localhost:8000/_sim/health> |
| 交互式 API 文档（Swagger UI） | <http://localhost:8000/docs> |
| OpenAPI 描述 | <http://localhost:8000/openapi.json> |

容器首次启动时会自动建表并载入示例场景 `order-demo`（`scenarios/example.yaml`）。
数据持久化在 Docker 卷 `sim-data`（容器内 `/data/sim.db`）。

停止 / 清空：

```bash
docker compose down            # 停止（保留数据卷）
docker compose down -v         # 停止并删除所有会话数据
```

---

## 必要配置（环境变量，均有默认值）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SIM_DB_PATH` | `/data/sim.db` | SQLite 数据库文件路径 |
| `SIM_SCENARIO_FILE` | `/app/scenarios/example.yaml` | 启动时自动载入的 YAML 场景 |
| `SIM_AUTOLOAD_SCENARIO` | `true` | 无活动场景时是否自动载入上述文件 |
| `SIM_BUSY_TIMEOUT_MS` | `10000` | 写锁等待毫秒数（并发排队上限） |

模拟端口由 Compose 映射控制：修改 `docker-compose.yml` 中 `"8000:8000"`
左侧端口即可（例如 `"18000:8000"`）。

本地（无 Docker）运行：

```bash
pip install -r requirements.txt
SIM_DB_PATH=./sim.db uvicorn app.main:app --reload
```

---

## 快速试跑（示例场景：下单 → 支付 → 发货）

会话通过请求头 **`X-Session-Token`** 隔离；不带该头时服务会生成并通过响应头
`x-session-token` 返回新令牌，后续请求带上即可。

```bash
# 1) 下单（带上 idempotency-key 是支付规则的前置匹配条件之一）
curl -s -X POST http://localhost:8000/orders \
  -H 'Content-Type: application/json' \
  -H 'X-Session-Token: sess-001' \
  -d '{"amount": 100, "items": [{"sku": "SKU-1001"}]}'

# 2) 支付（金额必须与下单一致；状态必须是 awaiting_payment）
curl -s -X POST http://localhost:8000/orders/pay \
  -H 'Content-Type: application/json' \
  -H 'X-Session-Token: sess-001' \
  -H 'Idempotency-Key: k-1' \
  -d '{"amount": 100}'

# 3) 发货
curl -s -X POST http://localhost:8000/orders/ship \
  -H 'Content-Type: application/json' \
  -H 'X-Session-Token: sess-001' \
  -d '{"carrier": "sf"}'

# 4) 查询会话当前状态（联调排查用）
curl -s http://localhost:8000/_sim/sessions/sess-001 | python -m json.tool
```

再开一个不同的 `X-Session-Token` 即可得到完全独立的状态机。

---

## 管理接口

所有管理接口挂在 `/_sim` 前缀下，不会与业务路由冲突。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/_sim/health` | 健康检查、当前活动场景名 |
| POST | `/_sim/scenarios/validate` | 仅校验 YAML/JSON 场景，不落库 |
| POST | `/_sim/scenarios?activate=true` | 创建或版本化更新场景（默认立即激活） |
| GET | `/_sim/scenarios` | 列出全部场景及版本 |
| GET | `/_sim/scenarios/{name}` | 查看某个场景的完整定义 |
| POST | `/_sim/scenarios/{name}/activate` | 切换活动场景 |
| DELETE | `/_sim/scenarios/{name}` | 删除场景（级联删除其会话与事件） |
| GET | `/_sim/sessions?scenario=name` | 列出会话（省略 `scenario` 用活动场景） |
| GET | `/_sim/sessions/{token}?events=true` | **会话状态查询**：state/vars/fires/事件流 |
| GET | `/_sim/sessions/{token}/events` | 单会话事件流（倒序，支持 `limit`/`offset`） |
| POST | `/_sim/sessions/{token}/reset` | 把会话重置为初始状态 |

请求头：
- `X-Session-Token`：会话标识（业务请求）；缺省自动生成。
- `X-Scenario-Name`：指定业务请求使用的场景；缺省使用当前活动场景。

业务响应还会带回 `x-sim-rule`（命中规则）、`x-sim-state`（迁移后状态）等头，
方便联调时核对。没有任何规则命中时返回 `501` 并在响应体中说明
`route_not_found` / `no_rule_matched` 及当前状态。

---

## YAML 场景格式

```yaml
name: my-scenario            # 场景唯一名（必填）
description: 可选说明
initial_state: init          # 新会话初始状态
initial_vars:                # 新会话初始变量（可在模板中引用）
  currency: CNY

routes:
  - path: /orders            # 精确路径（必填，以 / 开头）
    methods: [POST]          # GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS
    rules:
      - name: bad-request    # 规则名在场景内唯一
        priority: 10         # 数字越小优先级越高；同值按声明顺序
        when_state: init     # 可选：仅当当前状态等于该值
        when_state_not: paid # 可选：仅当当前状态不等于该值
        match:               # 多个条件之间是 AND
          - body: amount     # 三选一：header/query/body
            exists: false
          - header: x-flag
            equals: "on"
        response:
          status: 400
          headers:
            content-type: application/json
          body:              # 可为字符串、对象或数组（值支持 Jinja）
            error: missing_field
        transition:          # 可选：命中并渲染成功后一次性推进
          set:
            state: awaiting_payment     # 保留键 state 表示迁移状态
            order_id: "{{ uuid() }}"
          remove: [temp_flag]          # 删除会话变量
```

### 匹配操作符（`header` / `query` / `body` 通用）

| 操作符 | 含义 |
| --- | --- |
| 直接写 `value:`（或标量） | 等于（`equals` 的简写） |
| `equals` / `not_equals` | 等于 / 不等于 |
| `contains` / `not_contains` | 字符串子串、列表包含元素、字典包含键 |
| `starts_with` / `ends_with` | 字符串前后缀 |
| `gt` `gte` `lt` `lte` | 数值大小比较（双方按数字解析） |
| `regex` | 正则搜索（Python `re.search`） |
| `in` | 值出现在给定列表中 |
| `exists: true/false` | 字段是否存在（不参与取值比较） |

- `header`：HTTP 请求头名（大小写不敏感）。
- `query`：查询参数名。
- `body`：JSON-Path 风格，支持 `a.b.c`、`items[0].sku`、`a['x.y']`；
  请求体非 JSON 时 `body` 条件一律不命中。
- 期望值若是字符串会先经 Jinja 渲染，因此可以写
  `equals: "{{ vars.last_amount }}"`；整体表达式 `{{ ... }}` 会保留
  变量的 JSON 数值/布尔/空类型，避免 `"100"` 与 `100` 比较失败。

### 响应模板（Jinja2 沙箱）

模板上下文：

| 变量 | 内容 |
| --- | --- |
| `state` | 迁移前当前状态 |
| `vars` | 会话变量字典 |
| `request.method/path/query/headers/body` | 本次请求 |
| `session.token` | 会话令牌 |
| `session.fires` | 各规则命中次数（渲染时已计入本次） |
| `session.request_count` | 此前累计请求数 |
| `rule.name` / `rule.priority` | 命中规则信息 |

全局函数：`now(fmt?)`（默认 ISO8601 UTC）、`uuid()`（32 位 hex）。
`status` 必须是整数；`headers` 的值与 `body` 的字符串/对象叶子节点都会被渲染。
未定义变量渲染为空字符串（不报错）。模板中可调用
`{{ raise("业务拒绝") }}` 主动中断（返回 422，状态不推进）。

### 校验规则

`POST /_sim/scenarios/validate` 与载入时会检查：

- 结构合法性（Pydantic：未知字段拒绝、方法名合法、路径以 `/` 开头等）；
- 路由路径、规则名不重复；
- 所有响应模板可被 Jinja2 编译；
- `when_state` / `when_state_not` / 迁移目标状态可达
  （必须是初始状态或某条迁移的目标）。

同名校验失败返回 `422`，响应体指出出错位置与原因。

---

## 并发与一致性说明

- 每个模拟请求在服务端获取 **`BEGIN IMMEDIATE`** 写事务（WAL 模式 +
  `busy_timeout` 排队），事务内顺序完成：会话读取/创建 → 最高优先级匹配
  → 响应渲染 → 状态/变量/命中计数更新 → 事件落库。
- 同一会话的并发请求在数据库层串行化：后一个请求只能看到前一个请求提交后的状态，
  因此**同一条迁移不会被并发请求重复触发**；不同会话互不阻塞（写锁只在提交瞬间竞争）。
- 每次请求都追加一条不可变事件（`seq` 单调递增），可通过会话查询接口回放状态轨迹。
