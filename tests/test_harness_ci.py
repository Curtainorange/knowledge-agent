"""CI 与提交钩子的覆盖性检查：门禁必须「一定会被触发」才算门禁。

本仓库此前的状态：pytest 写得很好、harness 也装了，但**没有任何东西保证它跑**——
`.github/` 不存在，`.git/hooks/` 里只有 sample，全靠人记得。今天这次改动就是例子：
8 条新检查如果没人跑，等于没写。

所以这里钉三件事：
1. `.github/workflows/ci.yml` 存在，且真的跑了「可触达面门禁 + pytest」；
2. workflow 在 push 与 pull_request 上都会触发，Python 版本与 `requires-python` 对齐；
3. `.githooks/pre-commit` 存在，且真的覆盖了这两件事。

不测「钩子有没有装上」——那是每台机器一次性的 `git config`，写进 AGENTS.md 交付步骤里，
由人执行；CI 里也不做提交，装不装无所谓。
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
HOOK = ROOT / ".githooks" / "pre-commit"


def test_workflow_exists_and_covers_both_gates() -> None:
    """workflow 必须同时跑可触达面门禁与全量测试——少一条就是半个门禁。"""
    assert WORKFLOW.exists(), "缺 CI 配置：.github/workflows/ci.yml"
    text = WORKFLOW.read_text(encoding="utf-8")
    for token in ("scripts/harness_check.py", "pytest"):
        assert token in text, f"CI 里没有跑 {token}"


def test_workflow_triggers_on_push_and_pr() -> None:
    """触发条件必须包含 push 与 pull_request；只配一个等于另一半场景没人管。"""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "push:" in text and "pull_request:" in text
    # 版本与 pyproject 的 requires-python（>=3.12）对齐，避免 CI 跑一个项目不支持的解释器
    assert "3.12" in text


def test_workflow_is_valid_yaml() -> None:
    """结构层面再验一次：YAML 不合法的话 GitHub 会直接不跑，而且报错藏在网页上。

    没有 pyyaml 的环境（如只装了运行时依赖的机器）跳过——GitHub 侧也会拒非法 workflow，
    这里只是想把错误提前到本地。
    """
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # YAML 1.1 把裸 `on:` 解析成布尔 True（经典坑），两种键名都接受
    triggers = data.get("on", data.get(True))
    assert isinstance(triggers, dict), f"触发条件解析异常：{triggers!r}"
    assert "push" in triggers and "pull_request" in triggers

    jobs = data["jobs"]
    runs = " ".join(
        step.get("run", "")
        for job in jobs.values()
        for step in job.get("steps", [])
    )
    assert "scripts/harness_check.py" in runs
    assert "pytest" in runs


def test_precommit_hook_covers_harness() -> None:
    """提交钩子必须跑同一套检查，否则「本地门禁」只在 CI 那一刻存在。"""
    assert HOOK.exists(), "缺提交钩子：.githooks/pre-commit（并 git config core.hooksPath .githooks）"
    text = HOOK.read_text(encoding="utf-8")
    assert "scripts/harness_check.py" in text
    assert "tests/test_harness_" in text, "钩子没跑 harness 自身的测试"


def test_precommit_hook_is_a_shell_script() -> None:
    """Git 用 sh 执行钩子；没有 shebang 的文件在某些环境下会被静默跳过。"""
    first_line = HOOK.read_text(encoding="utf-8").splitlines()[0]
    assert first_line.startswith("#!"), f"缺 shebang：{first_line!r}"
    assert "sh" in first_line


def test_precommit_hook_is_lf_only() -> None:
    """钩子必须是 LF 行尾。

    本机 `core.autocrlf=true`：不强制的话，`.githooks/pre-commit` 每次检出都会变成 CRLF，
    sh 会把 `\\r` 当成命令的一部分，报 `set: command not found` 之类——看起来像"钩子没生效"，
    实际是整个文件坏了。`.gitattributes` 里对钩子与 `*.sh` 钉了 `eol=lf`，这条检查盯着它别被删。
    """
    raw = HOOK.read_bytes()
    assert b"\r\n" not in raw, (
        "钩子被检出成 CRLF 了。检查 .gitattributes 里的 `.githooks/* text eol=lf`，"
        "然后 rm 该文件并 git checkout -- 重新检出。"
    )
