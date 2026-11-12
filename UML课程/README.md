# UML 课程 · 需求分析与系统设计（对象模型）

认知副驾（Cognitive Copilot）的面向对象分析与设计产物：**分析阶段的领域类 + 分析类（BCE）**，**设计阶段的类图 + 顺序图 + 包图**。已与产品需求文档合并为一份合订本。

## 先看什么

| 文件 | 用途 |
|---|---|
| `认知副驾-需求分析与系统设计-V4.0.docx` | **合订本（57 页）**：第一篇需求分析（1–12 章）+ 第二篇系统设计（13–17 章）+ 附录 A–D |
| `认知副驾-需求分析与系统设计-V4.0.pdf` | 合订本投屏 / 打印版 |
| `分析设计文档.docx` | 只含设计篇的单册，对象模型汇报用 |
| `分析设计文档.md` | 设计篇正文源，改内容先改这里 |

## 目录结构

```
UML课程/
├─ 认知副驾-需求分析与系统设计-V4.0.docx / .pdf   合订本（主交付）
├─ 分析设计文档.docx / .pdf                       设计篇单册
├─ 分析设计文档.md                                设计篇正文源，改内容先改这里
├─ _build/                                        合订本构建链
│   ├─ parse_req.py        规范版需求 docx → HTML → 结构化块
│   ├─ docx_kit.py         通用排版（封面/目录/图表/页眉页脚）
│   ├─ build_merged.py     合并、重编号、渲染
│   ├─ reqdoc.html         规范版导出件（含 reqdoc.files/ 的图）
│   └─ reqdoc_blocks.json  解析后的中间结果
├─ diagrams/                          图的源与产物（12 张）
│   ├─ _style.puml                   统一风格（正交走线 / 300dpi / 字体 / 配色）
│   ├─ a1a_domain_knowledge.puml     领域类图（知识资产与冲突）
│   ├─ a1b_domain_plan.puml          领域类图（目标与计划）
│   ├─ a1c_domain_diagnosis.puml     领域类图（行为、诊断、简报）
│   ├─ a2a_analysis_scan.puml        分析类图（L2 扫描）
│   ├─ a2b_analysis_feedback.puml    分析类图（L2 反馈与越权）
│   ├─ a3_state_conflict.puml        状态图（冲突生命周期）
│   ├─ d2_package.puml               包图（分层依赖）
│   ├─ d1a_design_l2.puml            设计类图（接口 → 应用 → 端口）
│   ├─ d1b_design_entity.puml        设计类图（领域实体）
│   ├─ d1c_design_ports.puml         设计类图（端口与实现）
│   ├─ d3_seq_scan.puml              顺序图（L2 扫描）
│   ├─ d4_seq_feedback.puml          顺序图（L2 反馈与越权）
│   └─ *.png / *.svg                 每张 .puml 的位图与矢量产物
├─ tools/
│   ├─ plantuml.jar                   渲染器（需 Java）
│   ├─ render_pdf.ps1                 把单册 PDF 逐页转 PNG（自查排版用）
│   └─ render_merged.ps1              把合订本 PDF 逐页转 PNG
└─ build_docx.py                      md → docx 的排版脚本
```

## 改动流程

**只改图**：编辑 `diagrams/*.puml` → 重渲染 → 重建 docx（图会自动按新尺寸嵌入）。

**只改文字**：编辑 `分析设计文档.md` → 重建 docx。

```bash
cd D:/知识学习智能体/UML课程

# 1. 重渲染全部图（png 用于 docx，svg 用于放大查看）
#    注意逐个列文件，不要把 _style.puml 也丢进去（它不是一张图）
java -jar tools/plantuml.jar -charset UTF-8 -tpng diagrams/*.puml
java -jar tools/plantuml.jar -charset UTF-8 -tsvg diagrams/*.puml

# 2. 重建设计篇单册
python build_docx.py

# 3. 重建合订本（需求篇 + 设计篇）
python _build/build_merged.py
```

## 图的规矩（改图前先看）

| 规矩 | 为什么 |
|---|---|
| 风格只写在 `diagrams/_style.puml`，用 `!include _style.puml` 引用 | 十张图各自写一遍 skinparam，改一次配色要改十处，早晚不一致 |
| `skinparam linetype ortho`（已在 _style 里） | 连线只走横竖两向，不做斜穿；斜线是"看着乱"的第一来源 |
| `.puml` 里不写 `title` | 图号与图名由文档图注给，同一张 PNG 才能同时供单册与合订本 |
| 一张图只回答一个问题 | 图上元素越多，缩到 15.5×16.5 cm 版面里字越小；超过 25 cm 宽的图基本就"糊"了 |
| 短横边不要带两端多重性 | PlantUML 正交模式下会把 `1` 和 `0..*` 叠在同一个点上，读成 `01..*`；改成竖排或加长这条边 |
| 类框里的成员用真 UML 语法（`+ op(x : T) : R`） | 职责描述放正文或注释里，别塞进属性栏——否则既不像类图也不像表 |

> 检查办法：渲染后看图宽高（`nat` 尺寸 = 像素/300×2.54 cm）。宽度超过 25 cm、或高度超过 22 cm 的图，
> 塞进版面后字号会掉到 10pt 以下，就该拆图了。

## 合订本的编号规则

| 项 | 规则 | 例子 |
|---|---|---|
| 章 | 全篇连续，需求 1–12、设计 13–17 | 13 建模基线与追踪方式 |
| 节 | 跟随章号 | 14.3.1 领域类规格表 |
| 图 | 章-序，图注在图下 | 图 14-1 |
| 表 | 章-序，表号在表上（无表名） | 表 14-2 |
| 附录 | A 验收标准、B 参考资料、C 图清单、D 编号索引 | 附录 C |

## 文档口径

- 证据基线：规范版需求文档 V3.1、第 3 课 Spec（IN/OUT/BR/AT）、第 4 课对象模型 R2.1、当前代码实现。
- 编号（IN-01、OUT-01、BR-L2-01/02、AT-L2-01–04、TABLE 8）沿用前两次作业，不另起一套。
- 分析模型不写技术词汇；设计模型必须给可见性、类型、依赖方向。
- 15.7 节（原 4.7）列了"设计与现状的差距"，那是技术债清单，不是免责声明——汇报时按那张表讲排期。
- 图内标题不带编号，编号统一由文档图注给出：同一套 PNG 可以同时供单册与合订本使用。
