"""站内链接一律用相对路径 —— 80 端口子路径部署（/cognition/）的前提。

为什么要有这条护栏：
    前端原本全站写死根绝对路径（`/assets/app.js`、`/api/v1/chat`、`/app.html`）。
    部署在同一 IP 的 80 端口子路径下时，浏览器会把 `/api/v1/chat` 解析成
    `http://host/api/v1/chat`——打到别人家的根路径上，整站 404。而本地根路径
    开发时一切正常，属于典型的「本机全绿、上线全挂」。

    所以规则只有一条：**站内引用只写相对路径**（页面都在 web/ 同一层，
    相对路径在根部署与子路径部署下都成立）。这条测试同时钉住两端：
      - 前端文件里不得出现指向站内的根绝对路径；
      - 后端产出的 href 不得以 `/` 开头（卡片深链会原样进浏览器）。

    故意不检查注释：注释里的路径不产生请求，剥掉再判更贴近真实行为。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)

# 属性式：href="/assets/x"、data-path="/l4.html"
_HTML_ABS = re.compile(r'(?:href|src|content|data-path|data-go)="/[A-Za-z]')
# JS 字符串式：'/api/v1/...'、"/assets/..."、'/app.html'、'/api/'
_JS_ABS = re.compile(r"""['"]/(?:api/|assets/|sw\.js|[a-z0-9_]+\.html)""")
# manifest：start_url / scope / icons[].src
_MANIFEST_ABS = re.compile(r'"(?:start_url|scope|src)"\s*:\s*"/')


def _strip_comments(text: str) -> str:
    return _HTML_COMMENT.sub("", _BLOCK_COMMENT.sub("", text))


def _frontend_files() -> list[Path]:
    return [
        *sorted(WEB.glob("*.html")),
        *sorted(WEB.glob("assets/*.js")),
        WEB / "sw.js",
        WEB / "manifest.webmanifest",
    ]


def _pattern_for(path: Path) -> re.Pattern[str]:
    if path.suffix == ".webmanifest":
        return _MANIFEST_ABS
    if path.suffix == ".html":
        return _HTML_ABS
    return _JS_ABS


# sw.js 里这个字面量是用来**比较**的（判断请求是不是接口），不是要跳转的链接，
# 属于本规则的合理例外。只放行这一种精确写法，别的绝对路径照报。
_ALLOWED = {
    "sw.js": re.compile(r"\.indexOf\('/api/'\)"),
}


@pytest.mark.parametrize("path", _frontend_files(), ids=lambda p: p.name)
def test_frontend_uses_relative_paths(path: Path) -> None:
    pattern = _pattern_for(path)
    text = _strip_comments(path.read_text(encoding="utf-8"))
    allowed = _ALLOWED.get(path.name)
    if allowed is not None:
        text = allowed.sub("", text)
    hits = [
        f"第 {i + 1} 行: {line.strip()[:120]}"
        for i, line in enumerate(text.splitlines())
        if pattern.search(line)
    ]
    assert not hits, (
        f"{path.name} 仍有指向站内根的绝对路径，子路径部署时会 404。"
        f"改成相对路径（去掉前导 /）即可：\n  " + "\n  ".join(hits[:8])
    )


# 后端产出的 href 会原样塞进浏览器（卡片深链 `<a href="...">`），
# 所以后端也必须给相对路径。只扫 app/agent：`app/main.py` 里的
# `@app.get("/app.html")` 是**服务端路由**，本来就必须是绝对路径。
_BACKEND_HREF_ABS = re.compile(r'"/[a-z0-9_]+\.html')


def test_backend_agent_links_are_relative() -> None:
    offenders = []
    for path in sorted((ROOT / "app" / "agent").glob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""'):
                continue
            if _BACKEND_HREF_ABS.search(line):
                offenders.append(f"{path.name}:{lineno}  {stripped[:100]}")
    assert not offenders, (
        "后端产出的站内 href 必须以相对路径给出（子路径部署时 /xxx.html 会打到域名根）：\n  "
        + "\n  ".join(offenders[:8])
    )
