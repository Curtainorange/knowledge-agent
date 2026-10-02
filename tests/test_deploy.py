"""部署入口测试：双启动脚本的绑定地址 + 手机接入手册有落点。

这些是「配置即代码」的结构断言——绑定地址写错不会报错，只会表现为
「手机上打不开」或「不该暴露的口子开着」，值得钉死。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_start_server_binds_lan_by_default():
    """默认启动脚本绑定 0.0.0.0——局域网/手机可达是阶段 1 的交付项。"""
    text = (ROOT / "scripts" / "start_server.bat").read_text(encoding="utf-8")
    assert "--host 0.0.0.0" in text


def test_localhost_only_variant_preserved():
    """保留仅本机版：不需要手机接入时能收回 127.0.0.1（攻击面最小）。"""
    text = (ROOT / "scripts" / "start_server_local.bat").read_text(encoding="utf-8")
    assert "--host 127.0.0.1" in text
    assert "--host 0.0.0.0" not in text


def test_readme_has_mobile_access_handbook():
    """手机接入手册四块齐全：局域网直连 / Tailscale / PWA / 安全边界。"""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "## 手机接入" in text
    assert "0.0.0.0" in text
    assert "tailscale serve" in text
    assert "安全上下文" in text      # PWA 安装的 HTTPS 前提说清楚
    assert "端口映射" in text        # 公网直暴露明确不做
