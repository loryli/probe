"""场次多样性：考察目标不变，取题与考察顺序按场次区分。

同一份计划反复练习时，若顺序完全确定，每场都会从同一条证据、同一道题问起。
这里用场次 ID 的哈希派生种子：同一场次内顺序稳定可复现（cursor 依赖它），
不同场次顺序不同；跨场次再把历史问过的排到后面。
"""

import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import TypeVar

T = TypeVar("T")

PRIORITY_ORDER = ("high", "medium", "low")


def session_seed(session_id: str) -> int:
    """由场次 ID 派生种子，不需要额外存储；进程重启后仍能得到同一顺序。"""
    return int.from_bytes(hashlib.sha256(session_id.encode("utf-8")).digest()[:8], "big")


def _rank(seed: int, value: str) -> str:
    return hashlib.sha256(f"{seed}\x1f{value}".encode("utf-8")).hexdigest()


def shuffled(items: Iterable[T], *, seed: int, key: Callable[[T], str]) -> list[T]:
    """按 (seed, 元素标识) 的哈希排序：同场次稳定、不同场次不同，且与进程状态无关。"""
    return sorted(items, key=lambda item: _rank(seed, key(item)))


def grouped_by_priority(
    items: Iterable[T],
    *,
    seed: int,
    key: Callable[[T], str],
    priority: Callable[[T], str],
    order: Sequence[str] = PRIORITY_ORDER,
) -> list[T]:
    """优先级分层不变，同层内按场次打乱：先问什么仍然听计划的。"""
    buckets: dict[str, list[T]] = {level: [] for level in order}
    for item in items:
        buckets.setdefault(priority(item), []).append(item)
    result: list[T] = []
    for level in order:
        result.extend(shuffled(buckets.pop(level, []), seed=seed, key=key))
    for bucket in buckets.values():
        result.extend(shuffled(bucket, seed=seed, key=key))
    return result


def prefer_unseen(
    items: Sequence[T], *, usage: Mapping[str, int], key: Callable[[T], str],
) -> list[T]:
    """历史场次问过的排后面；次数相同时保持传入顺序（稳定排序）。"""
    return sorted(items, key=lambda item: usage.get(key(item), 0))
