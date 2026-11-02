from __future__ import annotations
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Episode:
    number: int
    title: str
    bvid: str
    page: int = 1
    cid: int | None = None
    published: int | None = None
    duration: float | None = None

    @property
    def key(self) -> str:
        return f'{self.bvid}:p{self.page}'

    @property
    def url(self) -> str:
        return f'https://www.bilibili.com/video/{self.bvid}?p={self.page}'


@dataclass
class Collection:
    title: str
    kind: str  # single, multip, season, series
    episodes: list[Episode]
    selected_number: int
    order_note: str
    source_url: str
    warnings: list[str] = field(default_factory=list)
    # Ephemeral extraction for the initially selected episode; never persisted.
    initial_info: dict | None = field(default=None, repr=False)

    @property
    def is_collection(self) -> bool:
        return len(self.episodes) > 1


@dataclass(frozen=True)
class FormatChoice:
    selector: str
    height: int
    description: str


class BiliAlbumError(Exception):
    """Expected error that can be presented to the user."""


class Cancelled(BiliAlbumError):
    pass


class NetworkFailure(BiliAlbumError):
    """A request-stage failure, separate from successful metadata extraction."""

    def __init__(self, stage, category, message, *, status=None, retryable=True):
        super().__init__(message)
        self.stage = stage
        self.category = category
        self.status = status
        self.retryable = retryable
