"""課題の並べ方と、日曜までに入りきらん分の切り分け。

何を先にやるかは盤の手で並べた順（Item.position）で決める。Priority は目安で、並べ順には使わん。
"""
from __future__ import annotations

from .model import Item

EPS = 1e-9  # 足し算の順で出る小数の誤差で、ちょうどの件が溢れ扱いにならんように


def order_key(item: Item):
    """盤の上から。同じ盤の中では position は重ならんので、これだけで順が決まる。"""
    return (item.position,)


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
