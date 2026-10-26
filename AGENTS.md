# 注意事项

> 这个文件是**指令层**：写的是「必须遵守」，不是「建议」。凡是能被测试钉住的，都在测试里钉住了。

## 一、通用约定

1. 每次改动完成后，都必须创建一个对应的 Git commit，以便后续追踪和回滚。
2. 每次改动后，都必须编写或更新相关测试，并在交付给用户前，确保所有测试和验证全部通过。
3. 每完成一项开发事项，更新 `dev_logs/` 当日日志（YYYY-MM-DD.md）的「今日完成」；待跟进项归入「待办事项」。无当日日志则按 `TEMPLATE.md` 模板创建并补填星期。

## 二、Harness 约定（外壳层）

### 1. 先写一条会失败的检查，再让它通过

任何新增的护栏、白名单、约定，落地顺序固定为三步：**写检查 → 跑一次确认它确实红（红的理由得是对的）→ 再写实现让它绿**。
先写实现再补测试，等于给自己写了一份「已经通过」的证明——这种证明没有信息量。

### 2. 交付前必须跑的一条命令

```bash
python scripts/harness_check.py
```

它校验**模型可触达面**：工具、文件、网络、密钥四类边界是否与 `app/llm/policy.py` 的白名单一致。
退出码非 0 就是有东西偷偷扩大了模型的权限。清单文档见 `docs/harness/模型可触达面清单.md`。

完整门禁（交付前全绿才算完成）：

```bash
python scripts/harness_check.py
python -m pytest --no-header -p no:warnings
node --check web/assets/agent.js   # 仅前端改动
```

### 2.1 门禁的触发点（本地钩子 + GitHub Actions）

写在这里的约定只有两种结局：要么有人执行，要么形同不存在。所以门禁挂两处自动触发：

```bash
git config core.hooksPath .githooks            # 每台机器一次：commit 前自动跑
git config harness.python "<解释器绝对路径>"    # 本机 pytest 只装在 anaconda 里，需显式指定
```

- **`.githooks/pre-commit`**：跑 `harness_check.py` + `tests/test_harness_*.py`，约 2 秒。
  找不到 python 或 pytest 时**明确跳过并打印原因**，不假装通过、也不拿环境问题拦提交。
  临时绕过：`git commit --no-verify`（能绕，但日志里会留下没跑门禁的提交，别习惯性用）。
- **`.github/workflows/ci.yml`**：push 到 main / 任何 PR 时跑同一套 + 全量 pytest + `node --check`。
  触发器里加了 `workflow_dispatch`，改 CI 本身时能立刻手动验一次。

两处跑的是同一个 `harness_check.py` 和同一套测试——本地绿、远端红（或反过来），
都说明有一边没对齐，先查环境，别急着改代码。

### 3. 扩大模型可触达面 = 先登记，再写代码

新增工具、新增出网主机、新增凭据字段、新增 LLM 任务类型，一律先在 `app/llm/policy.py` 登记（写清用途），
再跑 `python scripts/harness_check.py --write-doc` 重生成清单，最后才写代码。
**顺序反了会被 `tests/test_harness_surface.py` 拦下**，这是设计如此，别去改测试。

模型层（`app/llm/`）**禁止文件 IO**：书籍 / PDF / epub 的解析属于业务层（`app/books/`），
模型只该拿到拼好的 prompt 文本，不该拿到路径。

### 4. Mock 测不出来的参数，必须跑真机

阈值、召回分数、嵌入维度这类**依赖真实模型分布**的参数，测试里走的是 Mock / HashEmbedding，
分数分布与真实 ONNX 完全不同——**改完测试全绿也可能真机不可用**（`4ce1605` 就是这么交付的：
阈值 0.55 把实测 0.49-0.51 的相关内容全滤了）。改这类参数必须跑：

```bash
python scripts/smoke_modules.py     # 真实嵌入 + 阈值
python scripts/smoke_llm.py         # 模型链路连通性
```

### 5. 跨层契约改动必须有一条端到端断言

前端 `data-*` 属性名、按钮点击路径、后端字段名，两侧任一方改动都要有一条真正走完整链路的断言。
历史上两次翻车都是「单侧看着都对」：`target_id` 前端判空 + 后端非空，两处防御叠加使按钮永不生效（`e9886ba`）；
`probe.level` 前端放 `data-target`、后端读 `value`，接触史按钮必 422，而当时的端到端测试**把参数传反了**，
等于没测（`4ce1605`）。

### 6. 单次改动控制在 5 个文件以内

一次改动不超过 5 个文件、配一个提交。目的只有一个：**回滚就是 `git revert <sha>` 一条命令**。
跨 10 个文件的改动没法干净回滚，最后只能整段重写。

## 三、边界纪律

- 破坏性 / 基础设施类改动（响应体统一、对象存储）暂缓，见 `dev_logs/设计决策与经验.md`。
- 排查「数据丢失」全程只读，不边查边改。
- 改 `.env` 或后端代码后必须重启服务；改路由/页面后行为不对就直接重启，别只信 `--reload`。
