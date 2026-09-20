# 认知副驾（Cognitive Copilot）· P0 骨架

个人知识学习智能体的最小可运行骨架，验证「一次模型调用全链路可追踪、可计费」。

## 技术栈

- Python 3.12 · FastAPI · SQLAlchemy 2.0 · pydantic-settings
- LLM：DeepSeek（OpenAI 兼容协议，单一模型 `deepseek-flash`，每请求 `reasoning` 开关切换思考模式；旧名 `deepseek-v4-flash` 仍可调用，会重定向到同一模型）
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
| `/books.html` | 书架：上传 .txt / .epub / .pdf，进度一览，点击进入阅读 |
| `/reader.html` | 滚动阅读器：章节导航、字号调节、进度自动保存、划词记入知识库 |
| `/mine.html` | L1 认知挖掘：按模糊线索找回，线索不足自动追问，命中给出摘要与候选 |

- 顶部导航在各页之间切换并高亮当前页；未登录访问业务页会自动跳回登录页
- 令牌存于浏览器本地，过期或失效时自动登出
- 从挖掘的命中卡片可直接跳到知识库页并展开对应条目

## 书籍阅读：知识在阅读中自然沉淀

不必「先读书、再回头录入」——**录入就发生在阅读里**。把整本书拖进书架即可开始：

1. **上传**：`.txt` / `.epub` / `.pdf`，自动解析章节——txt 按章节标题切分，epub 按 spine 结构，
   pdf 优先用书内书签分章（没有书签则按页分章，标题「第 N 页」）
2. **阅读**：滚动式，字号可调，进度自动保存到章节内的精确位置，下次自动续读
3. **划线即录入**：
   - 选中文字 → **存摘录**（一键），或按 `Ctrl/Cmd + Enter` 直接存，不打断阅读
   - 选中文字 → **写想法**：把自己的思考与原文一起存成一条知识
4. **回头看**：
   - 已录入的原文在阅读时**自动高亮**（黄色 = 纯摘录，绿色 = 含想法），一眼看出哪些段落消化过
   - 顶部「本书知识」面板列出全书已录知识，点任意一条**跳回原文位置**

摘录连同你的想法会以 `source=book` 成为知识条目并自动向量化，因此**能被 L1 挖掘命中**——
包括你写下的思考本身，而不只是原文。

书籍文件存本地 `data/books/`（已加入 .gitignore），元数据与解析结果入库。

> **PDF 说明**：读的是 PDF 的**文本层**，不是原版排版——图片、表格、公式不会保留。
> 抽文本时会顺带清洗：剔除每页重复的页眉页脚、把被硬换行拆断的段落接回去。
> 靠扫描图片拼成的「扫描版 PDF」没有文本层，无法解析（上传时会明确提示），加密 PDF 同理。
> 单份上限 100MB / 2000 页；解析在请求内同步完成，页数多时需要等待数秒到数十秒。

> 首次录入会加载本地 BGE 向量模型（约需几十秒），后续请求走缓存；
> 模型不可用时自动降级为关键词召回，并在列表中标出。

## 微信读书同步：让划线回到自己的库

微信读书在 2026-05 上线了官方 AI Skills 接口，可以**只读**你自己的阅读数据（书架、划线、
想法、阅读进度与统计）。本项目用它把**你划的线和你写的想法**同步进知识库——不是把书搬进来
（官方不提供正文），而是让这些思考进入 L1/L2/L3 的闭环。

```bash
# 1. 到 https://weread.qq.com/r/weread-skills 扫码登录，新建 API Key（wrk- 开头）
# 2. 写进 .env（已被 .gitignore 忽略，请勿提交或分享）
WEREAD_API_KEY=wrk-xxxxxxxx
# 3. 书架页点「同步微信读书」
```

| 微信读书里的内容 | 落成 | 标签 |
|---|---|---|
| 划线 | 一条知识（划线原文） | 微信读书 / 划线 |
| 带划线的想法 | 一条知识（原文 + 【我的想法】） | 微信读书 / 读书笔记 |
| 整本书评 / 章节点评 | 一条知识（想法本身） | 微信读书 / 书评 |

- **幂等**：按 `bookmarkId` / `reviewId` 去重，反复点击不会重复；**你自己删掉的条目也不会被同步复活**
- **可追溯**：每条都带书名、章节、位置与 `deepLink`，能跳回微信读书原文
- **格式同构**：与站内「书籍摘录」完全一致，因此被 L1 挖掘、被 L2 检测冲突时，与你手写的知识同等对待
- 单次同步有条数上限（默认 300），未扫完的书会提示「还有 N 本未扫描」，再点一次继续

> **番茄小说这类平台不建议接**：没有官方开放接口，网上能搜到的「API」多为第三方逆向或盗版中转，
> 既违反服务条款也有版权风险。

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

## 数据备份与导出

**备份（全库）** —— 你的知识、书籍、阅读记录都在 `dev.db` 与 `data/` 里，建议定期备份：

```bash
python scripts/backup.py            # 备份到 backups/backup_<时间戳>/，保留最近 10 份
python scripts/backup.py --keep 20  # 保留最近 20 份
python scripts/backup.py --list     # 查看已有备份
```

> 数据库用 SQLite 在线备份 API 取快照，**服务器运行中执行也安全**。
> `backups/` 已在 `.gitignore` 中，不会进版本库。

**导出（当前用户的知识）** —— 界面里走「知识库 → 知识列表 → 导出 MD / 导出 JSON」，
或直接调接口：

```bash
curl -H "Authorization: Bearer <token>" \
  "http://127.0.0.1:8000/api/v1/knowledge/export?format=markdown" -o knowledge.md
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