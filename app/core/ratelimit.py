"""失败限流（进程内实现）。

用途：给登录这类「可被离线爆破」的入口加成本。计数同时按**账号**与**来源 IP**
两个维度进行，任一维度超限即拒绝：

- 只按账号：攻击者每个账号试几次、广撒网，永远踩不到阈值
- 只按 IP：同一出口 IP（公司/学校 NAT）下的正常用户会被连带误伤

局限（有意为之）：计数存在进程内存里，多 worker / 多实例部署时各算各的、等效阈值放大。
单机 P0 够用；需要精确限流应换 Redis 之类的集中式存储。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class _Bucket:
    failures: deque[float] = field(default_factory=deque)
    locked_until: float = 0.0


class FailureThrottle:
    """滑动窗口失败计数 + 超限锁定。

    时间统一取 `time.monotonic()`：不受系统时间调整（NTP 校正、用户改钟）影响。
    """

    def __init__(
        self,
        max_attempts: int,
        window_seconds: int,
        lock_seconds: int,
        max_buckets: int = 10_000,
    ) -> None:
        self._max = max(1, max_attempts)
        self._window = max(1, window_seconds)
        self._lock_seconds = max(1, lock_seconds)
        self._max_buckets = max(1, max_buckets)
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}

    def retry_after(self, *keys: str) -> int:
        """返回还需等待的秒数；0 表示可以继续尝试。"""
        if not keys:
            return 0
        now = time.monotonic()
        with self._lock:
            self._sweep(now)
            wait = 0
            for key in keys:
                bucket = self._buckets.get(key)
                if bucket is not None and bucket.locked_until > now:
                    wait = max(wait, int(bucket.locked_until - now) + 1)
            return wait

    def record_failure(self, *keys: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._sweep(now)
            for key in keys:
                if key not in self._buckets and len(self._buckets) >= self._max_buckets:
                    # 内存保护：桶数量到顶后不再新增，宁可在极端情况下少计数也不无限增长
                    logger.warning("限流桶已达上限 %d，本次失败未计数（key=%s）", self._max_buckets, key)
                    continue
                bucket = self._buckets.setdefault(key, _Bucket())
                bucket.failures.append(now)
                while bucket.failures and now - bucket.failures[0] > self._window:
                    bucket.failures.popleft()
                if len(bucket.failures) >= self._max:
                    bucket.locked_until = now + self._lock_seconds
                    bucket.failures.clear()
                    logger.warning("失败次数达上限，锁定 %d 秒（key=%s）", self._lock_seconds, key)

    def reset(self, *keys: str) -> None:
        """成功后清空计数（避免正常用户被自己的历史失败拖累）。"""
        if not keys:
            return
        with self._lock:
            for key in keys:
                self._buckets.pop(key, None)

    def clear(self) -> None:
        """清空全部计数（仅供测试）。"""
        with self._lock:
            self._buckets.clear()

    def _sweep(self, now: float) -> None:
        """懒惰清理：丢弃窗口外的失败记录与已解封且无记录的桶，防止内存无界增长。"""
        for key in list(self._buckets.keys()):
            bucket = self._buckets[key]
            while bucket.failures and now - bucket.failures[0] > self._window:
                bucket.failures.popleft()
            if bucket.locked_until <= now and not bucket.failures:
                del self._buckets[key]
