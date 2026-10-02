"""从 web/assets/avatar-lg.png 生成 PWA 安装图标。

产物（web/assets/icons/）：
  icon-192.png          常规图标 192×192（purpose: any）
  icon-512.png          常规图标 512×512（purpose: any）
  icon-maskable-512.png 自适应图标 512×512（purpose: maskable）

maskable 版按 Android 规范留安全区：头像缩到画布 72%、铺品牌底色
（--accent #4f46e5），保证被圆形/圆角遮罩裁切后主体不缺角。

用法：python scripts/gen_pwa_icons.py（需要 Pillow；产物直接提交进仓库，
本脚本只在换头像时重跑，运行时不依赖 Pillow）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "web" / "assets" / "avatar-lg.png"
OUT_DIR = ROOT / "web" / "assets" / "icons"
BRAND_BG = (79, 70, 229)  # #4f46e5，与 style.css 的 --accent 同值
MASKABLE_SCALE = 0.72  # 主体占画布比例，落在遮罩安全区（80% 直径圆）内


def main() -> int:
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - 环境缺依赖时的明确提示
        print("需要 Pillow：pip install pillow", file=sys.stderr)
        return 1

    if not SRC.exists():
        print(f"缺少头像底图：{SRC}", file=sys.stderr)
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = Image.open(SRC).convert("RGBA")

    for size, name in ((192, "icon-192.png"), (512, "icon-512.png")):
        base.resize((size, size), Image.LANCZOS).save(OUT_DIR / name)
        print(f"生成 {OUT_DIR / name}")

    canvas = Image.new("RGBA", (512, 512), BRAND_BG + (255,))
    inner = int(512 * MASKABLE_SCALE)
    icon = base.resize((inner, inner), Image.LANCZOS)
    canvas.alpha_composite(icon, ((512 - inner) // 2, (512 - inner) // 2))
    canvas.save(OUT_DIR / "icon-maskable-512.png")
    print(f"生成 {OUT_DIR / 'icon-maskable-512.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
