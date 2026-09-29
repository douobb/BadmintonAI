"""資料載入、schema 與 metadata 的穩定領域例外。"""

from __future__ import annotations

from collections.abc import Hashable
from typing import Any


class DataError(Exception):
    """所有 v2 資料層錯誤的基底類別。"""

    def __init__(
        self,
        message: str,
        *,
        row_number: int | None = None,
        rally_key: tuple[Hashable, ...] | None = None,
    ) -> None:
        super().__init__(message)
        self.row_number = row_number
        self.rally_key = rally_key


class DataLoadError(DataError):
    """資料來源無法讀取或格式不符合 loader 基本要求。"""


class DataSourceError(DataLoadError):
    """資料來源不存在、不是檔案或無法以唯讀方式開啟。"""


class DataFormatError(DataLoadError):
    """資料檔案的編碼、欄列結構或名稱格式錯誤。"""


class SchemaValidationError(DataError):
    """逐拍資料違反 canonical schema 規則。"""


class MetadataError(DataError):
    """metadata 無法讀取或不符合 metadata 契約。"""


class MetadataLoadError(MetadataError):
    """metadata 檔案不存在、編碼錯誤或 JSON 無法解析。"""


class MetadataValidationError(MetadataError):
    """metadata 根結構、欄位或語意屬性驗證失敗。"""


class UnknownPlayerAliasError(MetadataValidationError):
    """查詢到未收錄的球員 alias。"""


class AmbiguousPlayerAliasError(MetadataValidationError):
    """球員 alias 對應多個 canonical 球員。"""


class InvalidIdentifierError(DataLoadError):
    """SQLite table 名稱不是允許的嚴格 identifier。"""


# 提供較短且語意相近的公開名稱，讓呼叫端不必依賴實作檔名。
DatasetLoadError = DataLoadError
DatasetFormatError = DataFormatError
SchemaError = SchemaValidationError
MetadataValidation = MetadataValidationError
UnknownAliasError = UnknownPlayerAliasError
AmbiguousAliasError = AmbiguousPlayerAliasError


def format_value(value: Any) -> str:
    """將錯誤訊息中的值轉成不含完整內部狀態的簡短表示。"""

    return repr(value)
