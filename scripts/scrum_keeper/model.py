"""GitHub Project の読み取り結果を入れる型と、JST の週の計算。

タイトルは持たない。ログと CSV に題名が漏れないようにするため。
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import re
from dataclasses import dataclass, field

JST = dt.timezone(dt.timedelta(hours=9), "JST")
DEFAULT_ESTIMATE = 1.5  # Estimate が空の時に足す時間
ITER_TITLE = "Sprint {}"
CAPACITY_RE = re.compile(r"scrum-keeper:capacity=(\d+(?:\.\d+)?)h count_from=(\d{4}-\d{2}-\d{2})")
BOARD_MARKER = "scrum-board: v1"
PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2}


def to_jst(now: dt.datetime) -> dt.datetime:
    if now.tzinfo is None:
        raise ValueError("naive datetime is not allowed")
    return now.astimezone(JST)


def parse_now(text: str | None) -> dt.datetime:
    if not text:
        return dt.datetime.now(JST)
    return to_jst(dt.datetime.fromisoformat(text.replace("Z", "+00:00")))


def monday_of(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def week_start(now: dt.datetime) -> dt.date:
    return monday_of(to_jst(now).date())


def days_to_sunday_inclusive(now: dt.datetime) -> int:
    """今日を含めて日曜までの日数。月曜 7、日曜 1。"""
    return 7 - to_jst(now).weekday()


def parse_ts(text: str | None) -> dt.datetime | None:
    if not text:
        return None
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


@dataclass
class Iteration:
    id: str
    title: str
    start: dt.date
    duration: int = 7

    @property
    def end(self) -> dt.date:
        return self.start + dt.timedelta(days=self.duration - 1)


@dataclass
class Item:
    item_id: str
    kind: str  # Issue / PullRequest / DraftIssue
    repo: str = ""
    number: int = 0
    node_id: str = ""
    state: str = ""  # OPEN / CLOSED
    updated_at: dt.datetime | None = None
    closed_at: dt.datetime | None = None
    parent: str | None = None  # "owner/repo#n"
    children: list[str] = field(default_factory=list)
    status: str | None = None
    iteration_id: str | None = None
    priority: str | None = None
    estimate: float | None = None
    end_date: dt.date | None = None
    wait: str | None = None
    carryover: float | None = None

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.number}"

    @property
    def is_open_issue(self) -> bool:
        return self.kind == "Issue" and self.state == "OPEN"

    @property
    def is_done(self) -> bool:
        return self.status == "Done"

    @property
    def est(self) -> float:
        return DEFAULT_ESTIMATE if self.estimate is None else float(self.estimate)

    @property
    def carry(self) -> int:
        return int(self.carryover or 0)


@dataclass
class Board:
    owner: str
    number: int
    project_id: str
    title: str = ""
    description: str = ""
    field_ids: dict[str, str] = field(default_factory=dict)  # 項目名 -> id
    status_options: dict[str, dict[str, str]] = field(default_factory=dict)  # 項目名 -> {選択肢名: id}
    iterations: list[Iteration] = field(default_factory=list)  # 終わった週も含む全部
    iteration_duration: int = 7
    iteration_start: dt.date | None = None  # 項目の設定が持つ最初の日付
    items: list[Item] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"{self.owner}/{self.number}"

    def iteration_by_id(self, iid: str | None) -> Iteration | None:
        for it in self.iterations:
            if it.id == iid:
                return it
        return None

    def iteration_by_start(self, start: dt.date) -> Iteration | None:
        for it in self.iterations:
            if it.start == start:
                return it
        return None

    def item_by_key(self, key: str) -> Item | None:
        for it in self.items:
            if it.kind != "DraftIssue" and it.key == key:
                return it
        return None

    def week_of(self, item: Item) -> dt.date | None:
        it = self.iteration_by_id(item.iteration_id)
        return it.start if it else None

    def copy(self) -> "Board":
        return dataclasses.replace(
            self,
            iterations=[dataclasses.replace(i) for i in self.iterations],
            items=[dataclasses.replace(i, children=list(i.children)) for i in self.items],
        )


def parse_capacity(description: str) -> tuple[float, dt.date] | None:
    m = CAPACITY_RE.search(description or "")
    if not m:
        return None
    return float(m.group(1)), dt.date.fromisoformat(m.group(2))
