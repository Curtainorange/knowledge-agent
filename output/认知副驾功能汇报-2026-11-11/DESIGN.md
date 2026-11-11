# DESIGN —— 认知副驾 · 功能汇报（2026-11-11）

## 一、全局视觉约定

### ① 画布与安全区
- 画布 1280×720
- padding：上 60 / 左右 60，下 0（页脚落在底部 60px 带内，y≈700）
- 内容区 1160×600

### ② 配色（与产品 UI 同源，靛蓝系）

| 槽位 | Hex | 用途 |
|---|---|---|
| 主色 | `#4F46E5` | 编号、强调块、细线高亮、关键词 |
| 辅色 | `#C7D2FE` / `#EEF2FF` | 卡片底、分隔、图表第二序列 |
| 强调色 | `#B45309`（底 `#FEF3C7`） | 关键警示/结论，一页 ≤ 2 处 |
| 底色 | `#FFFFFF` | 主背景 |
| 正文色 | `#1F2937`；muted `#64748B`；淡 `#94A3B8` | 三级文本 |

### ③ 字体
- 中文 Microsoft YaHei；西文 Arial（跟随中文，不单独换字体）
- 阶梯：封面主标 64 / 页面主标 34 / 卡片小标 24 / 正文 18 / 图注 15 / 页脚 14

### ④ 视觉母题
- 页眉左「认知副驾 · 功能汇报」+ 右日期（14px / `#94A3B8`）
- 主标题下 **1px 贯通细线**（`#E2E8F0`），全篇一致
- 页脚右下页码（14px），封面/封底/章节页不加

### ⑤ 配图清单

| 文件 | 等级 | 来源 | 真实内容 | 使用页 |
|---|---|---|---|---|
| `chat_plan2.png` | L1 | 真实截图（本机 8010） | 对话工作台：开场白 + 需求确认卡 | P06 |
| `chat_suggest.png` | L1 | 真实截图 | 建议卡：为什么 / 判断 / 依据 2 条 / 我去做了 | P07 |
| `knowledge.png` | L1 | 真实截图 | 知识库：24 条已向量化、来源分布、列表 | P09 |
| `reader_sel.png` | L1 | 真实截图 | 阅读器：划词浮层「存摘录 / 写想法」 | P10 |
| `books.png` | L1 | 真实截图 | 书架：14 本、进度条、四个入口 | P11 |
| `mine_web.png` | L1 | 真实截图 | L1 挖掘：全网找书模式 | P14 |
| `conflicts.png` | L1 | 真实截图 | 冲突卡：立场对立 ⇄、置信度 93%、依据/建议 | P15 |
| `brief.png` | L1 | 真实截图 | 认知简报：主题分布 10 项 + 结构信号 | P17 |
| `l4.png` | L1 | 真实截图 | 路径修正：目标 / 排期参数 / 周计划 | P18 |
| `l5.png` | L1 | 真实截图 | 健康诊断：归因 / 建议 / 推理链 | P19 |
| `notify.png` | L1 | 真实截图 | 通知：推送频率 / 送达通道 / 通知列表 | P20 |
| `fig_funnel.png` | L1 | matplotlib 自绘 | L2 五级漏斗 | P16 |
| `fig_grounding.png` | L1 | matplotlib | 依据三级 + 引用校验 | P21 |
| `fig_probe.png` | L2 | matplotlib | 理解度判定矩阵 | P22 |
| `fig_loop.png` | L2 | matplotlib | 通读 loop 三件套 | P22 |
| `fig_pipeline.png` | L2 | matplotlib | 对话链路 | P06（备用，未用则不出） |

## 二、页面清单（24 页）

| # | type | role | rhythm | layout | visual | 说明 |
|---|---|---|---|---|---|---|
| 01 | cover | hero | peak | 大字报式 | 无图 | 标题 + 副标 + 汇报人/日期 |
| 02 | catalog | supporting | valley | 经典纵排 | 无图 | 4 章 + 页码 |
| 03 | section | transition | transition | 章节大字 | 无图 | 01 认识这个产品 |
| 04 | content | hero | peak | 三栏非均分 | 巨型数字 3 | 三个断层（收藏/阅读/积累） |
| 05 | content | supporting | valley | 分层模块图 | 无图（Box 绘制） | 八个模块一页看清 |
| 06 | content | supporting | valley | 左图右文 | L1 chat_plan2 | 对话即入口 + 16 能力 |
| 07 | content | supporting | valley | 上图下文 | L1 chat_suggest | 卡片协议 + 依据校验 |
| 08 | section | transition | transition | 章节大字 | 无图 | 02 知识怎么进来、怎么被读 |
| 09 | content | supporting | valley | 左图右文 | L1 knowledge | 三条入库路径 + 统计 |
| 10 | content | supporting | valley | 左图右文 | L1 reader_sel | 划词即录入 + 高亮 |
| 11 | content | supporting | valley | 右图左文 | L1 books | 四项书籍能力 + 两本账 |
| 12 | content | supporting | valley | 表格 + 结论条 | 无图 | 微信读书三类落成 |
| 13 | section | transition | transition | 章节大字 | 无图 | 03 五层能力 |
| 14 | content | supporting | valley | 左图右文 | L1 mine_web | L1 挖掘 + 全网找书 |
| 15 | content | supporting | valley | 右图左文 | L1 conflicts | L2 冲突卡四要素 |
| 16 | content | hero | peak | 单图主图 | L1 fig_funnel | 五级漏斗 + 评测 |
| 17 | content | supporting | valley | 上图下文 | L1 brief | L3 简报的依据 |
| 18 | content | supporting | valley | 左图右文 | L1 l4 | 规则引擎管约束 |
| 19 | content | supporting | valley | 右图左文 | L1 l5 | 归因诊断 |
| 20 | content | supporting | valley | 左图右文 | L1 notify | 抑制链 + 送达通道 |
| 21 | section | transition | transition | 章节大字 | 无图 | 04 技术重点与工程保障 |
| 22 | content | hero | peak | 大图上 + 三要点 | L1 fig_grounding | 依据三级 |
| 23 | content | supporting | valley | 双图并排 | L2 fig_probe + fig_loop | 判定矩阵 / 循环三件套 |
| 24 | ending | hero | peak | 收束金句 + 落款 | 无图 | 一句话 + 汇报人 |

## 三、纪律
- 正文 18px，卡片小标 24px，主标题 34px；同页样式组合 ≤ 4 种
- 分区间距 32px，区内标题-正文间距 12px，段间距 8px（严格递增）
- 一页 ≤ 3 个浅色底容器；不用阴影；圆角统一 10px
- 数字口径：67 天 / 310 次提交 / 144 个后端源文件 / 20 张表 / 16 个路由模块 / 13 个页面 / 1176 项测试
