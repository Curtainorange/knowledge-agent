"""获取本地 embedding 模型（BGE 中文 ONNX）。

背景：部分网络环境下 `huggingface.co` / `hf-mirror.com` 不可达（DNS 能解析但 TCP 连不上），
fastembed 因此无法自动下载模型，录入时只能降级为关键词召回。本脚本从可达的
ModelScope 镜像拉取同一模型到本地目录，供 app 直接加载（见 embedding 的 local 后端）。

特点：下载一次即可长期离线使用，不再依赖任何模型源。

用法：
    python scripts/fetch_embedding_model.py
    python scripts/fetch_embedding_model.py --dir D:\\models\\bge-small-zh-v1.5
    python scripts/fetch_embedding_model.py --float32     # 拉取完整精度版（约 90MB，默认取量化版）
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.request

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DIR = os.path.join(PROJECT_ROOT, "models", "bge-small-zh-v1.5")

REPO = "onnx-community/bge-small-zh-v1.5-ONNX"
BASE_URL = f"https://www.modelscope.cn/api/v1/models/{REPO}/repo"

COMMON_FILES = (
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
)
# 该仓库的 ONNX 采用「外部数据」格式：.onnx 只存计算图（几百 KB），
# 真正的权重在同名 .onnx_data 里，两者缺一不可。
QUANTIZED_WEIGHT_FILES = ("onnx/model_quantized.onnx", "onnx/model_quantized.onnx_data")
FLOAT32_WEIGHT_FILES = ("onnx/model.onnx", "onnx/model.onnx_data")


def download(remote_path: str, dest_root: str) -> bool:
    dest = os.path.join(dest_root, remote_path.replace("/", os.sep))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        print(f"  跳过（已存在）：{remote_path}（{os.path.getsize(dest) / 1024 / 1024:.1f} MB）")
        return True

    url = f"{BASE_URL}?Revision=master&FilePath={remote_path}"
    print(f"  下载 {remote_path} …", end=" ", flush=True)
    started = time.time()
    try:
        with urllib.request.urlopen(url, timeout=180) as resp, open(dest, "wb") as fh:
            received = 0
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                fh.write(chunk)
                received += len(chunk)
    except Exception as exc:
        print(f"失败：{exc}")
        if os.path.exists(dest):
            os.remove(dest)
        return False
    print(f"完成（{received / 1024 / 1024:.1f} MB，{time.time() - started:.1f}s）")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="从 ModelScope 拉取 BGE ONNX 模型到本地")
    parser.add_argument("--dir", default=DEFAULT_DIR, help="目标目录")
    parser.add_argument("--float32", action="store_true", help="拉取完整精度权重（默认量化版）")
    args = parser.parse_args()

    print(f"模型来源：ModelScope / {REPO}")
    print(f"目标目录：{args.dir}")
    print(f"权重版本：{'float32（约 90MB）' if args.float32 else 'quantized（约 23MB，推荐）'}")

    files = list(COMMON_FILES)
    files.extend(FLOAT32_WEIGHT_FILES if args.float32 else QUANTIZED_WEIGHT_FILES)

    for remote_path in files:
        if not download(remote_path, args.dir):
            print("下载中断，请检查网络后重试（脚本支持断点续传：已完成的文件会跳过）。")
            return 1

    print("模型已就绪。在 .env 中设置 EMBEDDING_BACKEND=local 即可启用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
