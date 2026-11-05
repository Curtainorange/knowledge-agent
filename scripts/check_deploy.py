"""上线自检：确认**这台机器**上的代码、配置、数据库、联网检索都能正常工作。

## 为什么单独一个脚本

`pytest` 证明「逻辑对」，`harness_check.py` 证明「边界没越」。两者都在开发机上跑，
都回答不了上线前真正要问的那句话：**「服务器上现在跑的，是不是我刚推的这份代码，
配置够不够，它连得上外网吗。」**

历史上吃过三次同类亏——
- `.env` 只在启动时读，改完不重启，代码是对的、服务还跑在旧配置上；
- 服务器拉到了新代码但库里缺表缺列（本项目的建表与补列都在启动时做，库没起来过就还是旧结构）；
- 本机 TLS 有中间人代理、服务器没有（或反过来），联网检索在一侧永远失败。

所以这个脚本只做**只读核对**：不建表、不写库、不改配置。可以随时在服务器上跑。

用法：
    python scripts/check_deploy.py              # 全套（含一次真实检索，约 2 秒、计一次费用）
    python scripts/check_deploy.py --no-network # 跳过联网探测（离线机房 / 不想花钱时）
    python scripts/check_deploy.py --strict     # 警告也当失败（上线闸门用）

退出码 0 = 可以上线；非 0 = 有几项必须先修。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# 一切路径（.env、sqlite:///./dev.db、alembic.ini）都是相对项目根写的，
# 在别处执行本脚本会读到别的库、别的配置——先切到根目录，别让检查本身成为误导。
os.chdir(ROOT)

OK = "ok"
WARN = "warn"
FAIL = "fail"

_MARK = {OK: "  ✓", WARN: "  !", FAIL: "  ✗"}
_results: list[tuple[str, str, str]] = []  # (级别, 小节, 说明)


def _record(level: str, section: str, message: str) -> None:
    _results.append((level, section, message))
    print(f"{_MARK[level]} {message}")


def _section(title: str) -> None:
    print(f"\n=== {title} ===")


# --------------------------------------------------------------------------- 1. 代码


def check_code() -> None:
    _section("代码（服务器上是不是新版本）")

    py = sys.version_info
    level = OK if py >= (3, 12) else FAIL
    _record(level, "code", f"Python {py.major}.{py.minor}.{py.micro}（要求 ≥3.12）")

    try:
        from app.llm import prompts
        from app.agent import router as router_mod
    except Exception as exc:  # 导入就炸，后面全没意义
        _record(FAIL, "code", f"导入 app 失败：{exc}")
        return

    route_version = str(getattr(prompts.AGENT_ROUTE, "version", ""))
    # 这一条是「拉的是不是新代码」的硬判据：v8 起才有 research 能力与 unhandled 约束
    major = 0
    if route_version.startswith("v") and route_version[1:].isdigit():
        major = int(route_version[1:])
    level = OK if major >= 8 else FAIL
    _record(
        level,
        "code",
        f"AGENT_ROUTE {route_version or '（未知）'}（联网找材料要求 ≥ v8；v8 之前没有 research 能力）",
    )

    has_advisor = hasattr(prompts, "RESEARCH_ADVISOR")
    _record(OK if has_advisor else FAIL, "code", "RESEARCH_ADVISOR 提示词存在" if has_advisor else "缺少 RESEARCH_ADVISOR")

    task_types = getattr(prompts, "PROMPT_VERSION_BY_TASK_TYPE", {})
    registered = "web_research" in task_types
    _record(OK if registered else FAIL, "code", f"web_research 已登记任务类型：{registered}")

    caps = getattr(router_mod, "KNOWN_CAPABILITIES", frozenset())
    _record(OK if "research" in caps else FAIL, "code", f"路由能力表含 research：{'research' in caps}")

    agent_js = ROOT / "web" / "assets" / "agent.js"
    if not agent_js.exists():
        _record(FAIL, "code", "缺少 web/assets/agent.js")
    else:
        text = agent_js.read_text(encoding="utf-8", errors="ignore")
        has_renderer = "research" in text and "safeHref" in text
        _record(
            OK if has_renderer else FAIL,
            "code",
            "前端 agent.js 含 research 卡片渲染器与 safeHref" if has_renderer else "前端 agent.js 是旧版（无 research 渲染器）",
        )

    # 站内链接必须是相对路径。这条与 tests/test_web_relative_paths.py 是同一条规则，
    # 但那个要装 pytest 才能跑，上线现场只会跑本脚本——所以这里独立判一次。
    # 10-27 踩过：前端写 /assets/style.css，挂在 /cognition/ 子路径下时浏览器把它解析到
    # 域名根，样式与接口全 404，而本机根路径开发下一切正常。
    offenders = _absolute_links()
    _record(
        OK if not offenders else FAIL,
        "code",
        "前端站内链接全为相对路径（子路径部署的前提）"
        if not offenders
        else f"前端仍有 {len(offenders)} 处根绝对路径（子路径部署会 404）：{', '.join(offenders[:5])}",
    )


# 属性式 href="/assets/x"，以及 JS 字符串式 '/api/v1/…'、'/app.html'。
_ABS_LINK = re.compile(r"""(?:href|src)="/[A-Za-z]|['"]/(?:api/|assets/|[a-z0-9_]+\.html)""")

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"//[^\n]*")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


def _blank(match: re.Match[str]) -> str:
    """把注释替换成等量换行——直接删会让后面的行号全错位。"""
    return "\n" * match.group(0).count("\n")


def _strip_comments(path: Path, text: str) -> str:
    """注释里的路径不产生请求，先剥掉再判，才贴近真实行为。

    （`sw.js` 的注释里就写着 `/cognition/api/v1/chat` 举例，不剥会误报。）
    """
    text = _HTML_COMMENT.sub(_blank, text)
    if path.suffix == ".js":
        text = _BLOCK_COMMENT.sub(_blank, text)
        text = _LINE_COMMENT.sub("", text)
    return text


def _absolute_links() -> list[str]:
    """扫描前端文件，返回仍指向站内根的绝对路径位置。"""
    targets = [
        *sorted((ROOT / "web").glob("*.html")),
        *sorted((ROOT / "web" / "assets").glob("*.js")),
        ROOT / "web" / "sw.js",
    ]
    offenders: list[str] = []
    for path in targets:
        if not path.exists():
            continue
        text = _strip_comments(path, path.read_text(encoding="utf-8", errors="ignore"))
        for lineno, line in enumerate(text.splitlines(), 1):
            # sw.js 里这个字面量是用来比较的（判断是不是接口），不是要跳转的链接
            if path.name == "sw.js" and "indexOf('/api/')" in line:
                continue
            if _ABS_LINK.search(line):
                offenders.append(f"{path.name}:{lineno}")
    return offenders


# --------------------------------------------------------------------------- 2. 配置


def check_config() -> None:
    _section("配置（.env 读没读到）")

    try:
        from app.core.config import settings
    except Exception as exc:
        _record(FAIL, "config", f"读取配置失败：{exc}")
        return

    provider = settings.model_provider
    _record(OK if provider == "deepseek" else WARN, "config", f"模型供应商：{provider}")
    _record(OK if settings.active_model else WARN, "config", f"模型名：{settings.active_model}")

    base_url = (settings.deepseek_base_url or "").rstrip("/")
    official = base_url == "https://api.deepseek.com"
    _record(
        OK if official else WARN,
        "config",
        f"DEEPSEEK_BASE_URL = {base_url}"
        + ("" if official else "（非官方地址：中转站未必提供 /v1/tools/search，联网检索会失败）"),
    )

    has_key = bool((settings.deepseek_api_key or "").strip())
    _record(OK if has_key else WARN, "config", "DEEPSEEK_API_KEY 已配置" if has_key else "未配置 DEEPSEEK_API_KEY：联网检索会被前置检查拦下")

    env_file = ROOT / ".env"
    _record(OK if env_file.exists() else WARN, "config", f".env {'存在' if env_file.exists() else '不存在（只用默认值）'}")

    # .env 只在启动时读取——这是「改了没生效」的头号原因，每次都提醒
    _record(OK, "config", "提醒：改过 .env 必须重启服务进程，热重载读不到新的 .env")


# --------------------------------------------------------------------------- 3. 数据库


def check_db() -> None:
    _section("数据库（表齐不齐、迁移到没到位）")

    try:
        from sqlalchemy import create_engine, inspect, text

        from app.core.config import settings
    except Exception as exc:
        _record(FAIL, "db", f"准备 SQLAlchemy 失败：{exc}")
        return

    url = settings.database_url
    try:
        engine = create_engine(url)
        tables = inspect(engine).get_table_names()
    except Exception as exc:
        _record(FAIL, "db", f"连不上数据库 {url}：{exc}")
        return

    _record(OK, "db", f"连接正常，{len(tables)} 张表")

    if "alembic_version" not in tables:
        _record(FAIL, "db", "没有 alembic_version 表：还没执行过 alembic upgrade head")
        return

    try:
        with engine.connect() as conn:
            current = conn.execute(text("select version_num from alembic_version")).fetchone()
    except Exception as exc:
        _record(FAIL, "db", f"读 alembic_version 失败：{exc}")
        return

    cur = current[0] if current else None
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config(str(ROOT / "alembic.ini"))
        head = ScriptDirectory.from_config(cfg).get_current_head()
    except Exception as exc:
        _record(WARN, "db", f"算不出迁移 head：{exc}")
        return

    if cur == head:
        _record(OK, "db", f"迁移已到位（{cur}）")
    else:
        # 刻意只报提醒：本项目建表与补列都在启动时做（`app/domain/db.py` 的漂移修复），
        # alembic 迁移落后是常态，把它判成失败会产生假警报。
        _record(WARN, "db", f"迁移未到 head：当前 {cur}，最新 {head}（本项目启动时自动补表补列，通常无碍）")


# --------------------------------------------------------------------------- 4. 联网


def check_network() -> None:
    _section("联网检索（本次升级新增的出网路径）")

    try:
        from app.llm import websearch
    except Exception as exc:
        _record(FAIL, "net", f"导入 websearch 失败：{exc}")
        return

    if not websearch.available():
        _record(WARN, "net", "没有 Key，跳过联网探测")
        return

    result = websearch.search("Python 官方教程", count=3)
    if not result.get("ok"):
        _record(FAIL, "net", f"检索失败：{result.get('error') or '未知原因'}")
        _record(
            WARN,
            "net",
            "排查顺序：① 出网白名单是否放行 api.deepseek.com；② 若在 TLS 中间人代理后面，"
            "确认系统证书库里有该根证书（代码会用系统证书库重试一次）；③ 中转站是否提供该接口。",
        )
        return

    items = result.get("results") or []
    _record(OK, "net", f"检索成功，返回 {len(items)} 条")
    for item in items[:3]:
        title = (item.get("title") or "")[:40]
        url = (item.get("url") or "")[:60]
        print(f"      - {title} | {url}")


# --------------------------------------------------------------------------- 汇总


def main() -> int:
    parser = argparse.ArgumentParser(description="上线自检")
    parser.add_argument("--no-network", action="store_true", help="跳过联网探测（不花钱）")
    parser.add_argument("--strict", action="store_true", help="警告也当失败")
    args = parser.parse_args()

    print("认知副驾 · 上线自检")
    print(f"工作目录：{ROOT}")

    check_code()
    check_config()
    check_db()
    if args.no_network:
        _section("联网检索")
        _record(WARN, "net", "已按 --no-network 跳过")
    else:
        check_network()

    fails = [r for r in _results if r[0] == FAIL]
    warns = [r for r in _results if r[0] == WARN]

    print("\n" + "=" * 60)
    if fails:
        print(f"=== 判定：不通过（{len(fails)} 项必须先修，{len(warns)} 项提醒）===")
        for _, section, message in fails:
            print(f"  ✗ [{section}] {message}")
        return 1
    if warns and args.strict:
        print(f"=== 判定：不通过（--strict，{len(warns)} 项提醒被当作失败）===")
        for _, section, message in warns:
            print(f"  ! [{section}] {message}")
        return 1
    print(f"=== 判定：通过（{len(_results)} 项检查，{len(warns)} 项提醒）===")
    if warns:
        for _, section, message in warns:
            print(f"  ! [{section}] {message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
