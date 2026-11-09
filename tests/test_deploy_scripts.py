"""部署脚本的护栏。

这些 .bat 只在服务器上跑，本机测试碰不到它们——而它们一旦写错，代价是停服。
所以把「踩过的坑」钉成断言，改脚本时由 CI 拦，而不是由线上 502 拦。
"""
from __future__ import annotations

from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def _text(name: str) -> str:
    return (DEPLOY / name).read_text(encoding="utf-8", errors="ignore")


def _lines(name: str) -> list[str]:
    return _text(name).splitlines()


def test_curl_probing_localhost_bypasses_any_proxy():
    """探测 127.0.0.1 的 curl 必须带 --noproxy。

    本机与不少企业服务器都设了 HTTP_PROXY。代理会把 http://127.0.0.1:8010/health
    接管掉并返回 502（实测过：8010 上根本没人监听，curl 却拿到 502）。
    watchdog 一旦这样误判，就会认为服务永远挂着，每 5 分钟重启一次，永不停歇。
    """
    for name in ("watchdog.bat", "update.bat"):
        for lineno, line in enumerate(_lines(name), 1):
            if "curl.exe" not in line:
                continue
            assert "--noproxy" in line, f"{name}:{lineno} 的 curl 没带 --noproxy，会被代理接管"


def test_update_pauses_the_watchdog_around_the_window():
    """update.bat 必须先停 watchdog、结束前再启用。

    停服到起服之间有个几十秒的窗口，watchdog 每 5 分钟一拍，正好探到 502 就会
    `schtasks /run` 用**旧代码**把服务拉起来。最坏结果不是失败，而是 update.bat
    最后探到 200、以为更新成功，实际跑的是 watchdog 拉起的旧进程。
    """
    text = _text("update.bat")
    assert "cognition-watchdog" in text, "update.bat 没有管 watchdog"
    assert "/disable" in text, "update.bat 没有在更新前暂停 watchdog"
    assert "/enable" in text, "update.bat 没有在更新后恢复 watchdog"


def test_watchdog_ends_the_task_before_running_it():
    """watchdog 必须先 /end 再 /run。

    进程崩了但计划任务状态仍是「正在运行」时，schtasks /run 会被拒绝（任务已在运行），
    于是它只能每 5 分钟往日志写一行 STILL DOWN，永远拉不起来——这是 schtasks 最经典的坑。
    另外不能用 `find "Running"` 判断状态：中文 Windows 的输出是「正在运行」，一匹配就落空。
    """
    lines = _lines("watchdog.bat")
    body = [ln for ln in lines if "schtasks" in ln]
    end_at = next((i for i, ln in enumerate(body) if "/end" in ln), None)
    run_at = next((i for i, ln in enumerate(body) if "/run /tn" in ln), None)
    assert end_at is not None, "watchdog.bat 没有 /end，卡在 Running 的任务永远起不来"
    assert run_at is not None, "watchdog.bat 没有 /run"
    assert end_at < run_at, "watchdog.bat 先 /run 后 /end，顺序反了"

    assert 'find /i "Running"' not in _text("watchdog.bat"), (
        "不要靠英文字样判断任务状态：中文 Windows 输出「正在运行」，匹配必落空"
    )


def test_bat_files_are_pure_ascii():
    """批处理文件必须只有 ASCII 字节。

    cmd.exe 按 OEM 代码页（中文 Windows 是 GBK）逐行解析 .bat。用 UTF-8 写的中文注释
    会被解成乱码字节，把行尾的 CR/LF 吞掉，注释片段被当成命令执行——实测过：注释里
    冒出「'8010' 不是内部或外部命令」，脚本行为完全不可预测。
    中文说明放 README.md（.md 是 UTF-8，没有这个问题）。
    """
    for path in sorted(DEPLOY.rglob("*.bat")):
        raw = path.read_bytes()
        offenders = [i for i, b in enumerate(raw) if b > 0x7F]
        assert not offenders, (
            f"{path.name} 含非 ASCII 字节（首个偏移 {offenders[0]}）："
            "批处理里不能写中文，注释也不行"
        )


def test_update_safety_check_uses_verbose_listing():
    """update.bat 的安全校验必须用 `schtasks /query /fo list /v`。

    不带 /v 的 list 只输出「任务名 / 下次运行时间 / 状态」，没有「要运行的任务」那行，
    于是 find 永远找不到项目路径，脚本永远判定「不是本项目」并在第一步退出——
    表现为「更新脚本自己跑不起来」，而且不给任何线索。
    """
    text = _text("update.bat")
    assert "/fo list /v" in text, "update.bat 的安全校验用了不带 /v 的 list，会永远误报"
