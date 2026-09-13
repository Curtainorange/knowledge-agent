"""重新生成提示词指纹清单（tests/golden/prompts.json）。

用法：改了 app/llm/prompts.py 里某个提示词的文本后——
1. 先把对应 PromptSpec 的 version 手动 bump（v1 → v2）；
2. 运行本脚本：`python scripts/regenerate_prompts_fingerprints.py`；
3. 提交 tests/golden/prompts.json 的变更。

tests/test_golden.py 会用 sha256 拦住「改了文本却没 bump 版本/没更新指纹」的脱节。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.llm.prompts import ALL_PROMPTS

MANIFEST = Path(__file__).resolve().parents[1] / "tests" / "golden" / "prompts.json"


def main() -> None:
    data = {
        p.name: {"version": p.version, "sha256": hashlib.sha256(p.text.encode("utf-8")).hexdigest()}
        for p in ALL_PROMPTS
    }
    MANIFEST.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已更新 {MANIFEST}")
    for name, entry in data.items():
        print(f"  {name}: {entry['version']} {entry['sha256'][:12]}…")


if __name__ == "__main__":
    main()
