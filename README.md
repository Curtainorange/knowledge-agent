# 认知副驾（Cognitive Copilot）· P0 骨架

个人知识学习智能体的最小可运行骨架，验证「一次模型调用全链路可追踪、可计费」。

## 技术栈

- Python 3.12 · FastAPI · SQLAlchemy 2.0 · pydantic-settings
- LLM：DeepSeek（OpenAI 兼容协议，单一模型 `deepseek-v4-flash`，每请求 `reasoning` 开关切换思考模式）
- 无 `DEEPSEEK_API_KEY` 时网关自动路由到 **MockProvider**（确定性、零成本，测试与本地演示）

## 快速开始

```bash
# 1. 安装依赖
pip install -e .[dev]

# 2. 环境变量（可选；不配置 KEY 则用 MockProvider）
cp .env.example .env

# 3. 启动
uvicorn app.main:app --reload
```

验证：

```bash
# 健康检查
curl http://127.0.0.1:8000/health

# 对话（返回 reply 与同一条 request_id）
curl -X POST http://127.0.0.1:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"conversation_id": null, "message": "你好"}'
```

## 界面

启动后打开 <http://127.0.0.1:8000/> 即可使用单页界面（`web/index.html`，原生 JS、无外部依赖，
由后端同源托管因此无需 CORS）：

- **首次进入**需注册账号（或登录已有账号），令牌保存在浏览器本地
- **左栏**：录入知识（入口即算向量）+ 知识库列表；点条目展开全文，拖滑块记录阅读进度，可软删
- **右栏**：L1 挖掘对话 —— 输入模糊线索，线索不足时自动追问（最多 3 轮），命中后返回条目摘要与阅读提醒
- **顶部**：显示当前账号，可刷新或退出登录

> 首次录入会加载本地 BGE 向量模型（约需几十秒），后续请求走缓存。

## 认证

采用 Bearer Token（JWT）：Access Token 有效期 2 小时，Refresh Token 7 天（见系统设计 9.3.1）。

| 接口 | 说明 |
|---|---|
| `POST /api/v1/auth/register` | 注册并直接下发令牌 |
| `POST /api/v1/auth/login` | 登录换取令牌 |
| `POST /api/v1/auth/refresh` | 用 refresh token 换发新的 access token |
| `GET /api/v1/auth/me` | 当前账号信息 |

- 除 `/health`、`/`（登录页）与上述 auth 接口外，**所有接口都要求** `Authorization: Bearer <access_token>`
- 校验通过后由依赖注入把 `user_id` 传给业务层，端点自身不重复认证
- 仓储层仍保留 `user_id` 结构性强过滤，构成第二道防线（即使猜到别人的条目 id 也取不到数据）
- 密码以 PBKDF2-HMAC-SHA256 加盐哈希存储，明文不落库、不入日志
- 登录失败不区分「账号不存在」与「密码错误」，避免被用来枚举账号

> `JWT_SECRET` 未配置时会在进程内随机生成并告警，服务重启后旧 token 全部失效——仅供本机开发；
> 生产请在 `.env` 中显式配置（生成方式见 `.env.example`）。

## 向量模型与降级

检索依赖本地 BGE（`BAAI/bge-small-zh-v1.5`，512 维）。首次录入触发下载，之后走缓存
（默认 `~/.cache/huggingface`）。

若模型缓存丢失且当前无法访问 HF（离线或网络受限），条目仍会正常入库，只是
`embed_status=embed_failed`，检索自动**降级为关键词召回**，不阻断主链路。
此时功能可用、语义精度下降；网络恢复后重新录入（或对已有条目触发一次文本更新）即可补齐向量。

## 测试

```bash
pytest          # 全程 Mock，不触发真实 API
```

## 目录结构

```
app/
├── main.py            # FastAPI 入口 + request_id 中间件
├── core/              # obs：config / 结构化日志 / request_id 链路
├── domain/            # SQLAlchemy ORM 实体 + 仓储（强制 user_id 过滤）
├── llm/               # 统一模型网关（唯一收口点）
│   ├── gateway.py     #   策略路由（task_type → reasoning 开关）+ 重试 + 成本
│   ├── retry.py       #   2 次重试 + 指数退避 + 抖动（仅可重试错误）
│   ├── cost.py        #   计费：token → 估算费用 → CostLog
│   └── deepseek/      #   DeepSeekProvider / MockProvider
├── agent/             # 最小对话 orchestrator
├── api/               # HTTP 端点
└── capabilities|workers|ingestion|retrieval|feedback|push/
                       # L1~L5 能力与独立链路的占位模块（P0 只建边界）
```

## 设计约束（P0 落地项）

1. **唯一收口**：所有模型调用走 `llm` 网关，业务不感知供应商细节。
2. **可追踪**：`X-Request-Id` / contextvar 贯穿「API → agent → 网关 → provider」，日志统一带 `request_id`。
3. **可计费**：每次模型调用 token 与估算费用写入 `cost_logs`，可按 user/task/date 归因。
4. **可替换**：`LLMProvider` 抽象接口，`DeepSeekProvider` 为当前实现，预留替换能力。
5. **结构性防越权**：仓储层强制 `user_id` 过滤。