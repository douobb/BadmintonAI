"""BadmintonAI v2 的唯讀結構化查詢邊界。"""

from .service import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    BadmintonQueryService,
    EventFilter,
    MatchSummary,
    QueryError,
    QueryPage,
    QueryValidationError,
    UnknownMatchError,
)

__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "MAX_PAGE_LIMIT",
    "BadmintonQueryService",
    "EventFilter",
    "MatchSummary",
    "QueryError",
    "QueryPage",
    "QueryValidationError",
    "UnknownMatchError",
]
