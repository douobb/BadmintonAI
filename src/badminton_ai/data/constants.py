"""v2 羽球逐拍資料契約使用的固定欄位與類別集合。"""

from __future__ import annotations

import re

MVP_REQUIRED_COLUMNS = (
    "match_id",
    "set",
    "rally",
    "rally_id",
    "ball_round",
    "player",
    "opponent",
    "type",
    "getpoint_player",
    "win_reason",
    "lose_reason",
    "landing_area",
    "player_location_area",
)

APPROVED_SHOT_TYPES = frozenset(
    {
        "切球",
        "平球",
        "長球",
        "挑球",
        "接殺防守",
        "推撲球",
        "殺球",
        "發長球",
        "發短球",
        "網前球",
    }
)

# 正式球場網格只包含 1～32；33 是資料來源保留的「未定義落點」sentinel，
# 不得被視為任何正式場區或納入前／中／後場分類。
COURT_ZONE_CODES = frozenset(range(1, 33))
UNDEFINED_LANDING_AREA_CODE = 33
STRICT_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

METADATA_FILENAMES = (
    "actor_aliases.json",
    "column_definition.json",
    "event_semantic_registry.json",
    "court_place.txt",
)

REGISTRY_FIELD_REQUIRED_KEYS = (
    "column",
    "group",
    "role",
    "grain",
    "perspective",
    "null_policy",
    "reference_frame",
    "unit",
)

REGISTRY_ENUMS = {
    "role": frozenset(
        {
            "sequence_context",
            "partition_key",
            "order_key",
            "timestamp",
            "event_actor",
            "counterpart_actor",
            "score_snapshot",
            "event_type",
            "actor_attribute",
            "event_position",
            "event_trajectory",
            "actor_position",
            "counterpart_position",
            "actor_movement",
            "counterpart_movement",
            "sequence_winner",
            "terminal_reason",
            "source_auxiliary",
            "unused",
        }
    ),
    "grain": frozenset({"event", "terminal_event", "sequence_snapshot"}),
    "perspective": frozenset({"actor", "counterpart", "neutral", "dynamic_actor"}),
    "null_policy": frozenset({"required", "optional", "terminal_only", "always_null"}),
    "reference_frame": frozenset(
        {
            "sequence",
            "temporal",
            "actor",
            "counterpart",
            "court",
            "result",
            "source",
            "none",
        }
    ),
    "unit": frozenset(
        {
            "identifier",
            "count",
            "timestamp",
            "frame",
            "score",
            "category",
            "code",
            "coordinate",
            "distance",
            "binary",
            "reason",
            "unknown",
        }
    ),
}
