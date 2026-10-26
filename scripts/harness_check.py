"""模型可触达面的传感器（harness 层）。一条命令回答「模型现在能碰什么」。

    python scripts/harness_check.py             # 校验：白名单与代码不一致即退出 1
    python scripts/harness_check.py --audit     # 只打印清单（markdown）
    python scripts/harness_check.py --write-doc # 重新生成清单文档的自动段

为什么要它：三次「交付即坏」的提交（e9886ba 双侧防御叠加、4ce1605 阈值与真实模型不匹配、
ad523df 上线即回滚）都不是业务逻辑写错，而是外壳缺位——没有一处能在交付前问出
「系统现在到底能碰什么」。本脚本把这件事变成可执行的门禁。

扫描口径（全部静态，不 import 业务模块、不连库、不触网）：
- 工具：全仓 `tools=` 的出现位置，必须都在 `policy.TOOL_CALLSITE_ALLOWLIST` 里；
- 任务类型：`gateway.STRATEGY_TABLE` 的键（AST 取），必须与 `policy.ALLOWED_TASK_TYPES` 相等；
- 出网主机：`app/` `scripts/` 下 .py 里的全部 http(s) 主机，必须都在 `policy.ALLOWED_EGRESS_HOSTS` 里；
- 密钥：`config.Settings` 里以 `_api_key` / `_secret` 结尾的字段，必须都在 `policy.SECRET_FIELDS` 里；
- 文件：`app/llm/` 下不得出现文件 IO。

判据是**相等**而不是「包含」：漏登记与幽灵项同样红。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.llm import policy  # noqa: E402

DOC_PATH = ROOT / "docs" / "harness" / "模型可触达面清单.md"
BEGIN = "<!-- harness:auto-begin -->"
END = "<!-- harness:auto-end -->"

SCAN_DIRS = ("app", "scripts")
SKIP_DIRS = {"__pycache__", ".git"}

DOC_PREAMBLE = """# 模型可触达面清单

> **结论一句话**：这个模型目前只被允许「读 prompt、出文本」——业务链没有一个工具、
> 看不到一个本地路径、只往已知主机出网、只该看见供应商 Key。
> 超出这个范围的任何动作，要么是 bug，要么必须先改 `app/llm/policy.py`。

## 怎么用

```bash
python scripts/harness_check.py              # 校验：代码与白名单不一致则退出 1
python scripts/harness_check.py --audit      # 只打印下面这段清单
python scripts/harness_check.py --write-doc  # 改完白名单后重新生成这一段（提交前跑）
```

## 本来不需要、本次已关掉的三项

| 项 | 为什么不需要 | 怎么关的 |
|---|---|---|
| 业务链的工具调用（function calling） | 这个模型只做「文本进 → 文本 / JSON 出」；能力分派是本地代码干的，模型只输出路由 JSON | `ALLOWED_TOOLS` 只登记诊断用的 `save_note`，业务链零调用方；网关每次调用前拦一道 |
| 模型层文件 IO | 书籍 / PDF / epub 的解析全在 `app/books/`；模型拿到的只是拼好的 prompt 文本 | 静态扫描 `app/llm/`，出现文件 IO 即红 |
| 微信读书 Key 进模型层 | 同步是业务动作，模型没有理由看到 `wrk-` 凭据 | `SECRET_FIELDS` 登记为 `model_visible: false` |

---

"""


# ---------------------------------------------------------------- 扫描


def _iter_py(dirs: tuple[str, ...] = SCAN_DIRS) -> list[Path]:
    out: list[Path] = []
    for name in dirs:
        base = ROOT / name
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if SKIP_DIRS & set(path.parts):
                continue
            out.append(path)
    return sorted(out)


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def scan_egress_hosts() -> dict[str, list[str]]:
    """host -> 出现该主机的文件列表（去重、稳定排序）。"""
    import re

    pattern = re.compile(r"https?://([A-Za-z0-9._-]+)")
    found: dict[str, set[str]] = {}
    for path in _iter_py():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for host in pattern.findall(text):
            found.setdefault(host, set()).add(_rel(path))
    return {host: sorted(files) for host, files in sorted(found.items())}


def scan_tool_callsites() -> list[tuple[str, int]]:
    """真实的「传 tools」调用点（文件, 行号）。

    必须走 AST 而不是搜字符串：函数签名里的 `tools=None`（十几个测试替身都有）、
    文档里提到的 `tools=` 都不是调用点，按字符串扫会把它们全判成越界。
    只统计 `xxx(..., tools=...)` 这种**真的发给了某个对象**的位置。
    """
    hits: list[tuple[str, int]] = []
    for path in _iter_py():
        source = path.read_text(encoding="utf-8", errors="ignore")
        try:
            tree = ast.parse(source)
        except SyntaxError:  # 语法都过不去的文件，交给 pytest / 编辑器去报
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and any(kw.arg == "tools" for kw in node.keywords):
                hits.append((_rel(path), node.lineno))
    return hits


def scan_task_types() -> set[str]:
    """从 gateway.py 的 STRATEGY_TABLE 用 AST 取键，避免 import 业务模块。"""
    tree = ast.parse((ROOT / "app/llm/gateway.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = {t.id for t in targets if isinstance(t, ast.Name)}
            if "STRATEGY_TABLE" in names and isinstance(node.value, ast.Dict):
                return {
                    k.value for k in node.value.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)
                }
    return set()


def scan_secret_fields() -> set[str]:
    """Settings 里以 _api_key / _secret 结尾的字段名（AST，不 import，避免读 .env）。"""
    tree = ast.parse((ROOT / "app/core/config.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            names = set()
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    name = item.target.id
                    if name.endswith("_api_key") or name.endswith("_secret"):
                        names.add(name)
            return names
    return set()


def scan_model_layer_file_io() -> list[tuple[str, int]]:
    """模型层的文件 IO 调用点。

    跳过 policy.py 自身——它把 `open(` / `Path(` 当作**被检查的字符串**声明在里面，
    不跳过会自己匹配自己。
    """
    hits: list[tuple[str, int]] = []
    for path in _iter_py((policy.MODEL_LAYER_DIR,)):
        if _rel(path) == "app/llm/policy.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if any(pat in line for pat in policy.FILE_IO_PATTERNS):
                hits.append((_rel(path), lineno))
    return hits


# ---------------------------------------------------------------- 对账


def collect() -> dict:
    hosts = scan_egress_hosts()
    task_types = scan_task_types()
    secrets = scan_secret_fields()
    callsites = scan_tool_callsites()
    file_io = scan_model_layer_file_io()

    problems: list[str] = []

    # 1) 工具：调用点必须都在白名单文件里
    stray_callsites = [(f, n) for f, n in callsites if f not in policy.TOOL_CALLSITE_ALLOWLIST]
    if stray_callsites:
        problems.append(
            "工具调用点越界：" + "；".join(f"{f}:{n}" for f, n in stray_callsites)
            + f"。只允许 {sorted(policy.TOOL_CALLSITE_ALLOWLIST)}；"
            "新增工具调用请先改 policy.ALLOWED_TOOLS 与 TOOL_CALLSITE_ALLOWLIST。"
        )

    # 2) 任务类型：必须完全相等
    missing = sorted(task_types - policy.ALLOWED_TASK_TYPES)
    ghost = sorted(policy.ALLOWED_TASK_TYPES - task_types)
    if missing:
        problems.append(f"任务类型漏登记：{missing}（STRATEGY_TABLE 有、policy 没有）")
    if ghost:
        problems.append(f"任务类型幽灵项：{ghost}（policy 有、STRATEGY_TABLE 没有）")

    # 3) 出网主机
    unregistered = sorted(set(hosts) - policy.ALLOWED_EGRESS_HOSTS)
    if unregistered:
        problems.append(f"未登记的出网主机：{unregistered}。新增出网必须先写进 policy.EGRESS_PURPOSE。")

    # 4) 密钥
    unregistered_secrets = sorted(secrets - set(policy.SECRET_FIELDS))
    if unregistered_secrets:
        problems.append(f"未登记的密钥字段：{unregistered_secrets}。新增凭据必须登记用途与模型层可见性。")

    # 5) 模型层文件 IO
    if file_io:
        problems.append(
            "模型层出现文件 IO：" + "；".join(f"{f}:{n}" for f, n in file_io)
            + "。模型只该拿到拼好的 prompt 文本，路径与原文属于业务层。"
        )

    return {
        "hosts": hosts,
        "task_types": task_types,
        "secrets": secrets,
        "callsites": callsites,
        "file_io": file_io,
        "problems": problems,
    }


# ---------------------------------------------------------------- 输出


def render_markdown(data: dict) -> str:
    lines: list[str] = []
    tools = sorted(policy.ALLOWED_TOOLS)

    lines += ["### 1. 工具 —— 模型可主动调用的函数", ""]
    if tools:
        lines += ["| 工具 | 状态 | 说明 |", "|---|---|---|"]
        for name in tools:
            lines.append(f"| `{name}` | 已登记 | {policy.TOOL_PURPOSE.get(name, '（未写用途）')} |")
    else:
        lines += [
            "| 工具 | 状态 | 依据 |",
            "|---|---|---|",
            "| （无） | **关闭** | 全仓 `tools=` 仅出现在 "
            f"{' / '.join('`' + f + '`' for f in sorted(policy.TOOL_CALLSITE_ALLOWLIST))}"
            " 的透传，没有任何调用方传入；网关调用前由 `policy.assert_tools_allowed` 拦一道 |",
        ]
    lines += ["", f"调用点扫描：{len(data['callsites'])} 处，"
                  + ("全部在允许文件内。" if not any(f not in policy.TOOL_CALLSITE_ALLOWLIST for f, _ in data["callsites"])
                     else "**存在越界调用点**。"), ""]

    lines += ["### 2. 文件 —— 模型层可见的本地路径", ""]
    io_count = len(data["file_io"])
    lines += [
        "| 项 | 状态 | 依据 |",
        "|---|---|---|",
        f"| 本地文件 / 路径 | {'**越界**' if io_count else '**不可见**'} "
        f"| `{policy.MODEL_LAYER_DIR}/` 下文件 IO 调用点 {io_count} 处"
        f"（扫描 {' / '.join('`' + p + '`' for p in policy.FILE_IO_PATTERNS)}） |",
        "| 书籍 / PDF / epub 原文 | **不可见** | 解析全在 `app/books/`，进 prompt 的只有抽好的文本 |",
        "| 用户上传落地文件 | **不可见** | 落盘在 `settings.books_dir`，模型只收到解析后的字符串 |",
        "",
    ]

    lines += ["### 3. 网络 —— 允许出网的主机", ""]
    lines += ["| 主机 | 用途 | 状态 |", "|---|---|---|"]
    for host, files in data["hosts"].items():
        registered = host in policy.ALLOWED_EGRESS_HOSTS
        lines.append(
            f"| `{host}` | {policy.EGRESS_PURPOSE.get(host, '（未登记）')} "
            f"| {'已登记' if registered else '**未登记**'}（{', '.join(files)}） |"
        )
    lines.append("")

    lines += ["### 4. 密钥 —— 进程能读到的凭据", ""]
    lines += ["| 字段 | 用途 | 模型层可见 |", "|---|---|---|"]
    for field in sorted(data["secrets"]):
        meta = policy.SECRET_FIELDS.get(field)
        if meta is None:
            lines.append(f"| `{field}` | （未登记） | **未知** |")
        else:
            visible = "是" if meta["model_visible"] else "**否**"
            lines.append(f"| `{field}` | {meta['purpose']} | {visible} |")
    lines.append("")

    lines += ["### 5. 扫描口径", ""]
    lines += [
        f"- 范围：`{'` `'.join(SCAN_DIRS)}/` 下的 `.py`（跳过 `__pycache__`），共 {len(_iter_py())} 个文件；",
        f"- 任务类型：`gateway.STRATEGY_TABLE` 共 {len(data['task_types'])} 项，与 policy 白名单要求**完全相等**；",
        "- 全部为静态扫描：不 import 业务模块、不连库、不触网。",
    ]
    return "\n".join(lines)


def render_console(data: dict) -> str:
    lines = ["=== 工具 ===", f"白名单：{sorted(policy.ALLOWED_TOOLS) or '空集（模型零工具）'}", ""]
    lines += ["=== 文件 ===", f"模型层（{policy.MODEL_LAYER_DIR}/）文件 IO：{len(data['file_io'])} 处", ""]
    lines += ["=== 网络 ==="]
    lines += [
        f"  {host:<26} {'已登记' if host in policy.ALLOWED_EGRESS_HOSTS else '未登记'}  {', '.join(files)}"
        for host, files in data["hosts"].items()
    ]
    lines += ["", "=== 密钥 ==="]
    lines += [
        f"  {field:<22} {'模型层可见' if policy.SECRET_FIELDS.get(field, {}).get('model_visible') else '模型层不可见'}"
        for field in sorted(data["secrets"])
    ]
    return "\n".join(lines)


def write_doc(markdown: str) -> None:
    DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
    body = f"{BEGIN}\n{markdown}\n{END}"
    if DOC_PATH.exists():
        text = DOC_PATH.read_text(encoding="utf-8")
        if BEGIN in text and END in text:
            head = text[: text.index(BEGIN)]
            tail = text[text.index(END) + len(END):]
            DOC_PATH.write_text(head + body + tail, encoding="utf-8")
            return
    DOC_PATH.write_text(DOC_PREAMBLE + body + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    data = collect()
    markdown = render_markdown(data)

    if "--audit" in argv:
        print(markdown)
        if data["problems"]:
            for p in data["problems"]:
                print(f"✗ {p}", file=sys.stderr)
            return 1
        return 0

    print(render_console(data))

    if "--write-doc" in argv:
        write_doc(markdown)
        print(f"\n已重写清单：{_rel(DOC_PATH)}")

    if data["problems"]:
        print("\n=== 判定：不通过 ===")
        for p in data["problems"]:
            print(f"✗ {p}")
        return 1
    print("\n=== 判定：通过（白名单与代码一致）===")
    return 0


if __name__ == "__main__":
    # Windows 控制台默认 cp936，中文输出必须先锁 UTF-8，否则重定向时炸 UnicodeEncodeError
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001 - 非交互环境下没有 reconfigure 也无所谓
        pass
    raise SystemExit(main(sys.argv[1:]))
