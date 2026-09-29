"""BadmintonAI v2 的純 Python 唯讀資料層。"""

from .constants import (
    APPROVED_SHOT_TYPES,
    COURT_ZONE_CODES,
    MVP_REQUIRED_COLUMNS,
    REGISTRY_ENUMS,
    UNDEFINED_LANDING_AREA_CODE,
)
from .errors import (
    AmbiguousAliasError,
    AmbiguousPlayerAliasError,
    DataError,
    DataFormatError,
    DataLoadError,
    DatasetFormatError,
    DatasetLoadError,
    DataSourceError,
    InvalidIdentifierError,
    MetadataError,
    MetadataLoadError,
    MetadataValidationError,
    SchemaError,
    SchemaValidationError,
    UnknownAliasError,
    UnknownPlayerAliasError,
)
from .loaders import load_csv, load_sqlite, quote_identifier
from .metadata import load_metadata, validate_registry_covers_columns
from .models import DatasetSnapshot, MetadataSnapshot
from .schema import validate_snapshot

__all__ = [
    "APPROVED_SHOT_TYPES",
    "AmbiguousAliasError",
    "AmbiguousPlayerAliasError",
    "COURT_ZONE_CODES",
    "DataError",
    "DataFormatError",
    "DataLoadError",
    "DataSourceError",
    "DatasetFormatError",
    "DatasetLoadError",
    "DatasetSnapshot",
    "InvalidIdentifierError",
    "MVP_REQUIRED_COLUMNS",
    "REGISTRY_ENUMS",
    "UNDEFINED_LANDING_AREA_CODE",
    "MetadataError",
    "MetadataLoadError",
    "MetadataSnapshot",
    "MetadataValidationError",
    "SchemaError",
    "SchemaValidationError",
    "UnknownAliasError",
    "UnknownPlayerAliasError",
    "load_csv",
    "load_metadata",
    "load_sqlite",
    "quote_identifier",
    "validate_registry_covers_columns",
    "validate_snapshot",
]
