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

启动后打开 <http://127.0.0.1:8000/> 进入登录页。前端按功能拆成三个独立页面
（原生 JS/CSS、无外部依赖，由后端同源托管因此无需 CORS）：

| 页面 | 职责 |
|---|---|
| `/login.html` | 注册 / 登录（入口 `/` 即此页） |
| `/knowledge.html` | 知识录入与管理：展开全文、拖滑块记录阅读进度、软删除 |
| `/books.html` | 书架：上传 .txt / .epub，进度一览，点击进入阅读 |
| `/reader.html` | 滚动阅读器：章节导航、字号调节、进度自动保存、划词记入知识库 |
| `/mine.html` | L1 认知挖掘：按模糊线索找回，线索不足自动追问，命中给出摘要与候选 |

- 顶部导航在各页之间切换并高亮当前页；未登录访问业务页会自动跳回登录页
- 令牌存于浏览器本地，过期或失效时自动登出
- 从挖掘的命中卡片可直接跳到知识库页并展开对应条目

## 书籍阅读

不想逐条手动录入？直接把整本书拖进书架，边读边把要点划进知识库：

1. **上传**：`.txt` / `.epub`，上传后自动解析章节（txt 按章节标题切分，epub 按 spine 结构）
2. **阅读**：滚动式阅读，字号可调，进度自动保存到章节内精确位置
3. **划词存知识**：选中任意文字 → 「记入知识库」，这段摘录会以 `source=book` 成为
   知识条目并**自动向量化**，之后就能被 L1 挖掘直接命中

书籍文件存本地 `data/books/`（已加入 .gitignore），元数据与解析结果入库。

> 首次录入会加载本地 BGE 向量模型（约需几十秒），后续请求走缓存；
> 模型不可用时自动降级为关键词召回，并在列表中标出。

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

## 向量模型（语义检索）

检索依赖 BGE 中文模型（`BAAI/bge-small-zh-v1.5`，512 维）。三种后端：

| backend | 说明 |
|---|---|
| `local`（默认，推荐） | 从 `EMBEDDING_LOCAL_DIR` 加载本地 ONNX，**完全离线** |
| `bge` | 由 fastembed 自动从 HuggingFace 下载（需能访问 HF） |
| `hash` | 确定性哈希，零依赖但语义弱，仅测试 / 应急 |

首次使用先取模型（约 23MB，支持断点续传）：

```bash
python scripts/fetch_embedding_model.py
```

> **为什么要这一步**：部分网络环境下 `huggingface.co` / `hf-mirror.com` 不可达
> ——DNS 能解析但 TCP 连接建立不起来，此时 fastembed 无法自动下载模型。
> 该脚本从可达的 ModelScope 镜像拉取同一模型到本地，之后长期可用、不再联网。

若模型缺失或加载失败，条目仍会正常入库，只是 `embed_status=embed_failed`，
检索自动**降级为关键词召回**，不阻断主链路；补齐模型后重新录入（或对已有条目
触发一次文本更新）即可补上向量。

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