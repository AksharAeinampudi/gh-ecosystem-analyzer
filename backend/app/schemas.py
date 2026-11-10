"""API response models. They mirror SPEC §5; the frontend's api.ts mirrors these."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel

Dimension = Literal["language", "topic"]


class Health(BaseModel):
    status: Literal["ok"]


class WeightsOut(BaseModel):
    stars: float
    forks: float
    pushes: float


class MetaOut(BaseModel):
    earliest_day: date
    latest_day: date
    windows: list[int]
    tracked_repos: int
    weights: WeightsOut
    push_cap: int
    floor: float
    min_repos: int
    max_top_repo_share: float


class WindowInfo(BaseModel):
    """Fields shared by every windowed response. Ranges are [first day, last day]."""

    dimension: Dimension
    window: int
    as_of: date
    current: tuple[date, date]
    previous: tuple[date, date]


class LeaderboardItem(BaseModel):
    rank: int
    name: str
    momentum: float
    share_current: float
    share_previous: float
    activity_current: float
    activity_previous: float
    active_repos: int
    top_repo_share: float
    concentrated: bool


class LeaderboardOut(WindowInfo):
    items: list[LeaderboardItem]


class TimeseriesPointOut(BaseModel):
    day: date
    stars: int
    forks: int
    pushes: int
    pr_events: int
    issue_events: int
    activity: float
    share: float


class TimeseriesOut(WindowInfo):
    name: str
    points: list[TimeseriesPointOut]


class TopRepoOut(BaseModel):
    full_name: str
    activity_current: float
    activity_previous: float
    delta: float
    stars_total: int | None
    url: str


class TopReposOut(WindowInfo):
    name: str
    items: list[TopRepoOut]
