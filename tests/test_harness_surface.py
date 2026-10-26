"""Harness 层自检：模型可触达面必须有清单，且清单与代码保持一致。

背景（三次失败提交的共同点）：
- `e9886ba` 前端 `!target` 提前 return + 后端 `target_id` 非空，两处防御叠加使按钮从不可能生效；
- `4ce1605` 召回阈值与真实 ONNX 分数不匹配，Mock 全绿也发现不了；
- `ad523df` 功能上线后被用户要求整体回滚。

三次都不是业务逻辑写错，而是「外壳」缺位：没有一处能回答「系统现在能碰什么、不能碰什么」。
本文件把这件事钉成可执行的检查——**清单不是文档，是产物**：

1. `scripts/harness_check.py` 必须存在且退出码 0（一条能跑的命令）；
2. `docs/harness/模型可触达面清单.md` 的自动段必须与当前代码扫描结果逐字一致
   （代码里多出一个出网主机 / 一个密钥 / 一个传 tools 的调用点，这条立刻红）。

不测业务行为，只测边界。业务回归仍在各自的 test_*.py 里。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "harness_check.py"
DOC = ROOT / "docs" / "harness" / "模型可触达面清单.md"

BEGIN = "<!-- harness:auto-begin -->"
END = "<!-- harness:auto-end -->"


def _run(*args: str) -> subprocess.CompletedProcess:
    """跑传感器脚本。强制 UTF-8，避免 Windows 默认 cp936 把中文输出打成乱码。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def _doc_auto_section() -> str:
    """取出清单文档里的自动生成段（两个锚点之间的内容）。"""
    text = DOC.read_text(encoding="utf-8")
    start = text.index(BEGIN) + len(BEGIN)
    end = text.index(END)
    return text[start:end].strip()


def test_sensor_script_passes() -> None:
    """传感器必须存在并通过——这是「一条能跑的命令」的落地判据。"""
    assert SCRIPT.exists(), "缺传感器：scripts/harness_check.py 不存在"
    proc = _run()
    assert proc.returncode == 0, (
        "传感器报告漂移（白名单与代码不一致）：\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    # 输出必须点名每一条边界，否则「通过」没有信息量
    for keyword in ("工具", "文件", "网络", "密钥"):
        assert keyword in proc.stdout, f"传感器输出里没有「{keyword}」这一节"


def test_inventory_doc_in_sync() -> None:
    """清单文档的自动段必须等于当前扫描结果：文档过期即失败。"""
    assert DOC.exists(), f"缺清单：{DOC.relative_to(ROOT)}"
    proc = _run("--audit")
    assert proc.returncode == 0, proc.stderr
    expected = proc.stdout.strip()
    actual = _doc_auto_section()
    assert actual == expected, (
        "清单与代码不一致（文档过期）。跑 `python scripts/harness_check.py --write-doc` 重新生成后提交。\n"
        f"--- 文档里是 ---\n{actual[:800]}\n--- 实际扫描 ---\n{expected[:800]}"
    )


def test_agents_md_carries_harness_rules() -> None:
    """指令层也要有护栏：AGENTS.md 里的硬约定被静默删掉，这条会红。

    为什么值得单独一条：三次失败提交里有两次是「流程上没人提醒」——阈值改真机参数、
    前后端契约改动，都能过测试。约定写进 AGENTS.md 还不够，还得有人在 CI 里盯着它别被删。
    """
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    required = {
        "传感器命令": "scripts/harness_check.py",
        "可触达面": "模型可触达面",
        "先红后绿": "先写一条会失败的检查",
        "改动力度": "5 个文件",
    }
    missing = [name for name, token in required.items() if token not in text]
    assert not missing, f"AGENTS.md 缺少 harness 约定：{missing}"
