"""依赖声明必须覆盖 `app/` 的顶层 import。

为什么需要这条：本地是 anaconda，第三方包一大堆；`app/` 里 `import` 一个没在
`pyproject.toml` 声明过的包，本机永远绿，**干净环境一次都跑不起来**。

实际踩过两次：
- `numpy`（`app/retrieval/embedding.py`）——本地靠 anaconda 自带掩盖；
- `bs4`（`app/books/parser.py`）——文档字符串写着「已装依赖 bs4」，其实从没声明过。
  后果不是某个用例失败，而是 `tests/conftest.py` 加载即 ImportError，
  整个 pytest 以 **exit code 4（用法错误）** 退出——CI 日志里只看到「用法错误」，
  极为误导。

判定口径：
- 只看 `app/`（运行时面），且只看**模块顶层** import——函数内的 import 是有意为之的
  可选依赖（如 fastembed / onnxruntime），不该逼进必装清单；
- 顶层 `try: import x except ImportError:` 里的同样视为可选，跳过；
- 模块名 → 发行包名用 `importlib.metadata.packages_distributions()` 反查
  （`bs4` → `beautifulsoup4`、`jwt` → `PyJWT`），再和 `pyproject.toml` 的
  `[project].dependencies` 按 PEP 503 归一化后比对；
- 本机没装、反查不到的模块跳过（这类只能由 CI 发现），但会在输出里列出来。
"""
from __future__ import annotations

import ast
import sys
import tomllib
from importlib.metadata import packages_distributions
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"


def _norm(name: str) -> str:
    """PEP 503 归一化：大小写、`_` / `.` 与 `-` 等价。"""
    return name.lower().replace("_", "-").replace(".", "-")


def _declared_distributions() -> set[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared: set[str] = set()
    for spec in data["project"]["dependencies"]:
        # 去掉版本约束：`pydantic>=2.7` → `pydantic`；`foo[bar]>=1` → `foo`
        name = spec.split("[")[0]
        for sep in (">", "<", "=", "!", "~", ";", " "):
            name = name.split(sep)[0]
        declared.add(_norm(name.strip()))
    return declared


def _top_level_imports(path: Path) -> set[str]:
    """模块顶层 import 的根包名。函数内的、以及顶层 try/except ImportError 里的一律不算。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
        # ast.Try 的 body 里通常是 `import fastembed` 这类可选依赖 —— 有意忽略
    return found


def test_app_top_level_imports_are_declared() -> None:
    """`app/` 里顶层 import 的第三方包，必须在 pyproject 的 dependencies 里声明。"""
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    mapping = packages_distributions()
    declared = _declared_distributions()

    missing: list[str] = []
    unknown: list[str] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for module in _top_level_imports(path):
            if module in stdlib or module == "app":
                continue
            dists = mapping.get(module)
            if not dists:
                unknown.append(f"{module}（{path.relative_to(ROOT).as_posix()}）")
                continue
            if not any(_norm(d) in declared for d in dists):
                missing.append(
                    f"{module} → 发行包 {dists}（{path.relative_to(ROOT).as_posix()}）"
                )

    assert not missing, (
        "以下第三方包被 app/ 顶层 import，但 pyproject.toml 里没声明——"
        "干净环境会 ImportError，CI 报成 exit code 4：\n  " + "\n  ".join(sorted(missing))
    )
    # 本机没装的反查不到发行包名，只能靠 CI 兜；这里只提示，不判失败，避免机器差异误伤
    if unknown:
        print("提示：以下模块本机未安装、无法反查发行包名，已跳过：", sorted(unknown))
