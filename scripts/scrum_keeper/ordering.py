"""課題の並べ方と、日曜までに入りきらん分の切り分け。"""
from __future__ import annotations

from .model import Item, PRIORITY_RANK

EPS = 1e-9  # 足し算の順で出る小数の誤差で、ちょうどの件が溢れ扱いにならんように


def order_key(item: Item):
    pr = PRIORITY_RANK.get(item.priority or "", len(PRIORITY_RANK))
    return (pr, item.repo, item.number)


def sort_plain(items: list[Item]) -> list[Item]:
    """Priority P0→P1→P2→P3→空、repo、番号の古い順。"""
    return sorted(items, key=order_key)


def sort_in_progress_first(items: list[Item]) -> list[Item]:
    return sorted(items, key=lambda i: (0 if i.is_in_progress else 1,) + order_key(i))


def overflow(ordered: list[Item], limit: float) -> list[Item]:
    """上から Story Point を足し、合計が limit を > で超えた最初の件から下を全部返す。

    ちょうど同じ合計は残る。下の小さい件を詰め直さん。limit が 0 以下なら全部。
    """
    if limit <= 0:
        return list(ordered)
    cum = 0.0
    for idx, it in enumerate(ordered):
        cum += it.est
        if cum > limit + EPS:
            return list(ordered[idx:])
    return []
