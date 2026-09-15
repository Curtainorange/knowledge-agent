"""前端会话逻辑测试：静默续期 / 令牌过期跳登录 / 退出按钮可用性。

为什么单独有这一层：这些行为只在**运行时**才暴露，用「页面里有没有某个
字符串」的 HTML 断言覆盖不到。这里用 Node 跑真实的 `web/assets/app.js`
（stub 掉 fetch / localStorage / location），把真实发生过的 bug 固化成断言。

背景（2026-09-15 线上 bug）：读完书退出来，页面变成空壳——列表空、
用户名空、退出按钮点了没反应。根因是会话探针 `/api/v1/auth/me` 的 401
被「auth 路径不跳转」规则误伤：既不跳登录也不报错，`requireAuth` 静默
返回 null，页面 `boot()` 直接结束。三个症状同源。
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "web" / "assets" / "app.js"
HARNESS = Path(__file__).resolve().parent / "js" / "session_behavior.js"


def _app_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _no_auth_redirect_paths() -> list[str]:
    """取出 app.js 里「401 不跳登录页」的白名单。"""
    js = _app_js()
    start = js.index("NO_AUTH_REDIRECT_PATHS = [")
    end = js.index("];", start)
    return re.findall(r"'([^']+)'", js[start:end])


# ---------- 源码层不变量（不需要 node，永远执行）-------------------------


def test_session_probe_401_must_relogin():
    """会话探针 /api/v1/auth/me 的 401 必须触发重新登录。

    这是本次 bug 的正中央：把整个 `/api/v1/auth/` 前缀都排除在跳转之外，
    会让「令牌过期」既跳不了登录、也报不出错，页面直接烂在空壳状态。
    所以这里必须是**白名单**（只排除真正「凭证本身不对」的端点）。
    """
    paths = _no_auth_redirect_paths()
    assert "/api/v1/auth/me" not in paths, (
        "会话探针的 401 就是「登录已过期」，必须跳登录页——"
        "不能出现在不跳转白名单里"
    )
    # 这些端点的 401 表示「你给的凭证不对」，跳登录会把「密码错误」误报成「登录过期」
    assert "/api/v1/auth/login" in paths
    assert "/api/v1/auth/register" in paths
    assert "/api/v1/auth/password" in paths
    assert "/api/v1/auth/account" in paths


def test_frontend_actually_uses_refresh_token():
    """refresh token 必须真的被用上。

    此前前端把 refresh token 存进 localStorage 却从不使用——access token
    只有 2 小时，读一本书动辄一两小时，过期必然发生，用户就会看到空壳页。
    """
    js = _app_js()
    assert "'cc_refresh_token'" in js, "refresh token 的存储键不见了"
    assert "/api/v1/auth/refresh" in js, "没有调用刷新端点"
    assert "tryRefresh" in js, "没有续期逻辑"


def test_topbar_bound_before_auth_check():
    """顶栏（含退出按钮）必须在会话校验**之前**绑定。

    否则一旦校验失败，退出按钮就没有监听器——用户既看不到数据、也退不出去，
    正是用户报的「点击退出登陆也没用」。
    """
    js = _app_js()
    body = js[js.index("async function requireAuth()"):]
    body = body[: body.index("return me;")]
    assert body.index("renderTopbar()") < body.index("api('GET', '/api/v1/auth/me')"), (
        "renderTopbar() 要先于会话探针调用"
    )


def test_logout_binding_is_idempotent():
    """退出按钮的绑定要幂等：requireAuth 每次调用都会渲染顶栏。"""
    assert "_topbarBound" in _app_js()


# ---------- 运行时行为（Node；环境无 node 时跳过）------------------------


def test_frontend_session_behavior_runtime():
    """跑真实的 app.js：静默续期、过期跳登录、退出可用、顶栏幂等。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("环境没有 node，跳过前端运行时行为验证")

    proc = subprocess.run(
        [node, str(HARNESS), str(APP_JS)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, f"前端会话行为验证失败：\n{output}"
    assert "全部通过" in proc.stdout
