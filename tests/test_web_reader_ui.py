"""阅读页前端验证：页码标记不产生字符 + 阅读设置落 CSS 变量。

为什么单独一层：划词偏移由 `Range.toString().length` 决定，页码若以真字符进 DOM，
所有高亮与摘录的 char_start 都会整体错位——而这个错误**后端看不见**
（接口只返回 JSON）、浏览器里也**不报错**，只能在前端这一层钉死。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
READER_HTML = ROOT / "web" / "reader.html"
STYLE_CSS = ROOT / "web" / "assets" / "style.css"
HARNESS = Path(__file__).resolve().parent / "js" / "reader_render.js"


def _find_node() -> str | None:
    """先找 PATH；本机 node 装在 workbuddy binaries 下，不在 PATH 里。"""
    found = shutil.which("node")
    if found:
        return found
    base = Path.home() / ".workbuddy" / "binaries" / "node" / "versions"
    for candidate in sorted(base.glob("*/node.exe"), reverse=True):
        if candidate.is_file():
            return str(candidate)
    return None


def _html() -> str:
    return READER_HTML.read_text(encoding="utf-8")


def _css() -> str:
    return STYLE_CSS.read_text(encoding="utf-8")


# ---------- 源码层不变量（不需要 node）------------------------------------


def test_page_mark_html_has_no_text():
    """页码标记必须是空 span：显示值走 data-show，由 CSS 伪元素画出来。

    这一条是「划词偏移不错位」的根：伪元素内容不进 DOM 文本节点，
    `getSelectionInfo` 数文本长度时就不会把「P.42」算进去。
    """
    html = _html()
    assert "data-show=" in html
    assert "content:attr(data-show)" in _css(), "页码必须由伪元素渲染，不能写进文本节点"


def test_page_mark_is_not_selectable():
    """页码不参与划词，否则选中一段跨页文字会带上「P.42」。"""
    assert ".page-mark{" in _css()
    block = _css().split(".page-mark{", 1)[1].split("}", 1)[0]
    assert "user-select:none" in block


def test_reader_prefs_are_validated_before_use():
    """localStorage 里坏掉的偏好要回落到默认值，不能让阅读页起不来。"""
    html = _html()
    assert "PREFS_ALLOWED" in html
    assert "PREFS_DEFAULT" in html
    assert "function loadPrefs()" in html


def test_font_change_keeps_scroll_position():
    """调字号必须保住阅读位置：字号一变正文高度就变，不同步回滚会「跳页」。"""
    html = _html()
    assert "function applyFont(keepPosition)" in html
    assert "applyFont(true)" in html, "手动调字号要传 keepPosition"
    assert "applyFont()" in html, "渲染章节时不要保留（此时位置由 scrollRatio 决定）"


def test_reader_settings_reuse_shared_stylesheet():
    """设置面板样式只写在共享 style.css 里，不在页面内联第二份。"""
    html = _html()
    assert ".setting-row{" not in html
    assert ".setting-row{" in _css()


# ---------- 运行时（Node；环境无 node 时跳过）----------------------------


def test_reader_render_runtime():
    """跑真实 reader.html 的渲染函数：页码不产生字符、按偏移插入、转义正确。"""
    node = _find_node()
    if not node:
        pytest.skip("环境没有 node，跳过阅读页渲染验证")

    proc = subprocess.run(
        [node, str(HARNESS), str(READER_HTML)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, f"阅读页渲染验证失败：\n{output}"
    assert "全部通过" in proc.stdout
