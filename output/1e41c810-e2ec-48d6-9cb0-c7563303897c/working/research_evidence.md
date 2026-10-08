# 本地证据库（Phase 2 研究快照）

> 本次建模**不做外部网络检索**：全部素材来自项目本地代码、课件与既有 Spec。
> 理由：① 需求来源必须是项目决策，网上材料不能作为本项目需求依据（第 3 课 P12）；
> ② 课件纪律要求"无需求依据的类/属性/操作/关系只能删除或列为待确认"，
> 因此信息必须来自可核查的本地事实，而非检索推断。
> 本文件即 Phase 2 的"信息库快照"，记录动笔前已核实的证据条目。

## A. 代码证据（真实交互与端点）

| 能力域 | 端点（app/api/endpoints/） | 建模用途 |
|---|---|---|
| 账号与会话 | `auth.py`：POST /register、/login、/refresh、/password、DELETE /account、GET /me | UC-01/02 |
| 偏好 | `preferences.py`：GET/PUT /api/v1/preferences | UC-03 |
| 知识条目 | `knowledge.py`：POST/GET/PATCH/DELETE /items、GET /stats、GET /export | UC-04/05/06 |
| 书籍 | `books.py`：POST ""（上传）、GET ""、GET /reading-log、GET /{id}、GET /{id}/chapter/{i}、PATCH /{id}/progress、DELETE /{id}、POST/GET /{id}/notes | UC-07/08/09 |
| 微信读书 | `weread.py`：GET /status、POST /sync | UC-10 |
| L1 | `l1.py`：POST /api/v1/l1/mine | UC-11 |
| L2 | `l2.py`：POST /scan、GET /conflicts、PATCH /conflicts/{id}/state、GET /judgments、POST /judgments/{pair_key}/rejudge | UC-12/13/14 |
| L3 | `l3.py`：POST /brief、POST /question | UC-15 |
| L4 | `l4.py`：POST /goals、GET /goals、POST /goals/{id}/plan、GET /plan、POST /deviation/check、POST /deviation/{plan_id}/decision | UC-16/17/18 |
| L5 | `l5.py`：POST /diagnosis、GET /diagnosis/latest、POST /diagnosis/{id}/decision | UC-19/20 |
| 推送 | `push.py`：GET /jobs、POST /jobs/{id}/delivered；`events.py`：GET /api/v1/events | UC-21 |
| 对话入口 | `agent.py`：POST /start、POST /chat、GET /conversation/{id}、POST /actions、GET /capabilities | 交互渠道 |
| 系统 | `doctor.py`：GET /api/v1/system/doctor | 非功能（自检） |

## B. 领域实体证据（app/domain/models/）

`user.py` / `knowledge_item.py` / `claim.py` / `conflict.py` / `l2_judgment_log.py` /
`book.py` / `book_agent_reading.py` / `learning_goal.py` / `learning_plan.py` /
`plan_task.py` / `learning_event.py` / `cognitive_diagnosis.py` / `conversation.py` /
`push_job.py` / `push_log.py` / `cost_log.py` / `task_run.py`

→ 对象模型候选类的**事实来源**（可核查为"真实存在"）。

## C. 既有 Spec 与作业（口径基线）

| 来源 | 内容 |
|---|---|
| `docs/第3课_用例模型与Spec驱动_作业.docx` | 第 3 课作业：范围/非范围（IN-01/OUT-01）、角色目标、规则（BR-L2-01/02）、越权失败例（TABLE 8） |
| `UML/第4课_对象模型与Spec驱动_周宇航.docx` | 第 4 课作业：R1 手工初稿 → H1 审查单（E01–E07）→ R2 → 验收 → R2.1 最终对象模型 |
| `docs/需求分析文档.md` V3.0 | 需求基线：12 条用例总览与用例规约（本文档在其基础上按课件规范升级为用例模型） |
| `认知副驾.md` | 五层能力梯度与模型层定义 |

## D. 课件方法论证据

| 课件 | 页脚 | 采用内容 |
|---|---|---|
| 第 3 课 | P12 | 范围/非范围表；"范围来源是项目决策" |
| 第 3 课 | P20 | 角色目标表；"内部算法、数据库、AI 建模助手不自动成为运行时角色" |
| 第 3 课 | P22–P24 | 用例定义与特征；用例分析是功能分解技术 |
| 第 3 课 | P27–P28 | 用例粒度（界面动作不是独立用例）；从用例发现对象职责 |
| 第 3 课 | P32/P34/P35/P36 | include / extend / 泛化 的判据与方向 |
| 第 3 课 | P37 | 关系审查表（逐条给证据） |
| 第 3 课 | P40–P46 | 用例描述字段；基本流编号；异常流回退到出错步骤 |
| 第 3 课 | P47 | 验收与追踪（边界、越权、失效） |
| 第 3 课 | P48–P53 | H0/A1/H1/A2/H2 流程；A1/A2 提示纪律；人工决策（模型修订 vs 业务变更） |
| 第 4 课 | P06–P07 | 用例模型 vs 对象模型；人机责任 |
| 第 4 课 | P12–P13 | 类图三区域；不放外键式 ID；分析对象模型层次 |
| 第 4 课 | P17/P22 | 属性筛选；操作要保护业务规则（反贫血模型） |
| 第 4 课 | P27–P32 | 从 Spec 识别类；候选筛选；CRC；候选对象登记与决定 |
| 第 4 课 | P34–P39 | 关联/多重性/组合/泛化/依赖 |
| 第 4 课 | P46–P52 | R1→H1(E 编号)→R2→追踪与验收 |
| 第 4 课 | P53 | OOA→OOD：分析类图不被技术层反向污染 |

## E. 关键约束（动笔红线）

1. 不得新增业务：包括"别的智能体都能自动改写笔记""加个推荐算法"这类无来源需求。
2. 无依据的类/属性/操作/关系 → 删除或列入待确认，不得补成事实。
3. 规则数值（0.5 置信度门槛、3 次忽略收敛、20 对上限）来自 `config.py` 初值，标注"待冻结"。
4. 业务变更（如改规则值）必须人工批准，AI 只能提方案（第 3 课 P53、P68）。
