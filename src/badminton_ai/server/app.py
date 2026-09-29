"""可由 Open WebUI 匯入的最小 OpenAPI Tool Server。"""

from __future__ import annotations

import base64
import binascii
import importlib.resources
import json
import logging
import os
import time
import uuid
from collections import OrderedDict
from dataclasses import asdict
from pathlib import Path
from threading import Lock
from typing import Annotated, Any, Literal

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..catalog import (
    BadmintonCatalogService,
    CatalogError,
    ColumnSummary,
    DatasetSummary,
    PlayerCoverageSummary,
)
from ..data import (
    AmbiguousPlayerAliasError,
    DataError,
    UnknownPlayerAliasError,
)
from ..query import (
    QueryValidationError,
    UnknownMatchError,
)
from ..sandbox import (
    SandboxArtifactError,
    SandboxCodeError,
    SandboxExecutionError,
    SandboxMaterializationError,
    SandboxOutputError,
    SandboxPolicyError,
    SandboxResult,
    SandboxTimeoutError,
    SandboxUnavailableError,
)
from .chart_display import (
    OpenWebUIBridgeError,
    OpenWebUIBridgeNotConfigured,
    OpenWebUIChartBridge,
    OpenWebUIEventOutcomeUnknown,
    OpenWebUIIdentityMismatch,
)
from .chart_spec import CHART_SPEC_FILE
from .composition import CompositionError, ToolServices, build_services
from .plotly_rich import (
    DEFAULT_PLOTLY_ASSET_URL,
    PLOTLY_ASSET_PATH,
    PLOTLY_CHARTS_FILE,
    PLOTLY_VERSION,
    PlotlySpecError,
    parse_plotly_charts_artifact,
    render_plotly_charts_html,
    validate_plotly_asset_url,
)

ACCESS_LOGGER = logging.getLogger("uvicorn.error.badminton_ai")

SERVER_VERSION = "0.1.0"
MAX_ANALYSIS_CODE_CHARS = 64 * 1024
MAX_ARTIFACT_TEXT_PREVIEW_BYTES = 16 * 1024
TEXT_ARTIFACT_EXTENSIONS = frozenset({".csv", ".json", ".jsonl"})
MAX_ANALYSIS_FAILURES_PER_MESSAGE = 4
MAX_ANALYSIS_RUNS_PER_MESSAGE = 12
MAX_TRACKED_ANALYSIS_MESSAGES = 2048


class ErrorResponse(BaseModel):
    """所有工具錯誤共用的安全回應。"""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: dict[str, Any] | None = None


class HealthResponse(BaseModel):
    """程序健康與資料可用狀態。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "degraded"]
    data_available: bool
    version: str


class DatasetSummaryResponse(BaseModel):
    """資料集描述性摘要，不回傳主機絕對路徑。"""

    model_config = ConfigDict(extra="forbid")

    source: str
    row_count: int
    column_count: int
    match_count: int
    set_count: int
    rally_count: int
    player_count: int
    snapshot_id: str
    snapshot_version: str | None


class ColumnSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None
    data_type: str | None
    role: str | None
    unit: str | None
    null_count: int
    blank_count: int
    json_null_count: int
    distinct_count: int


class ColumnCatalogResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    columns: list[ColumnSummaryResponse]
    empty: bool
    unknown_names: list[str] = Field(default_factory=list)
    available_names: list[str] = Field(default_factory=list)


class ClarificationRequest(BaseModel):
    """向使用者提出分析前必須回答的問題；不執行資料查詢。"""

    model_config = ConfigDict(extra="forbid")

    question: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)
    ]
    options: list[
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    ] = Field(default_factory=list, max_length=3)


class ClarificationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["awaiting_clarification"]
    question: str
    options: list[str]


class PlayerCoverageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    player: str
    match_count: int
    rally_count: int
    won_rallies: int
    lost_rallies: int
    event_count: int
    swing_event_count: int


class PlayerCatalogResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    players: list[PlayerCoverageResponse]
    empty: bool


class MatchSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    match_id: str
    players: list[str]
    sets: list[int]
    rally_count: int
    event_count: int


class MatchCatalogResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    matches: list[MatchSummaryResponse]
    empty: bool


class AnalysisRequest(BaseModel):
    """LLM 產生的 Python；實際執行只交給 Docker runner。"""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(
        min_length=1,
        max_length=MAX_ANALYSIS_CODE_CHARS,
        description=(
            "在隔離 Docker 沙箱執行 Python。已預載完整、未篩選的事件 df（保留來源列序、"
            "欄位順序、空字串與 null）、pd、np、plt、json、os、Path、resolve_player。"
            "球員別名先用 resolve_player('周天成') 轉為 df 中的正式名稱再篩選。"
            "必要口徑未確認前不要呼叫本工具；欄位探查先用 listColumnCatalog，"
            "不得以 Python 探查。正式分析時再核對 df.columns 或 "
            "BADMINTON_SCHEMA_FILE，不猜欄名或覆寫原始 df。"
            "可讀 BADMINTON_EVENTS_FILE、BADMINTON_MANIFEST_FILE、"
            "BADMINTON_METADATA_FILE、BADMINTON_SCHEMA_FILE。"
            "本工具不是 REPL；不要先以 print 探查再另呼叫分析。"
            "每次須在 BADMINTON_OUTPUT_DIR 輸出至少一個檔案，print 不算產物；"
            "它是環境變數，不是 Python 名稱：out = Path(os.environ['BADMINTON_OUTPUT_DIR'])。"
            "文字摘要建議用 summary.json。"
            "圖表可選且不受固定模板限制：聊天圖表只能用 Plotly，"
            "預載的 plt/Matplotlib 產生的 PNG 不會顯示於聊天。需要互動圖時同次輸出根目錄 "
            "plotly_charts.json，其最外層恰為 schema_version='badminton-plotly/v1' "
            "與 charts（1–4 個）；每個 chart 含 title 與 Plotly figure JSON，"
            "可用 figure=json.loads(fig.to_json())。不可交回 figure JSON 字串、HTML 或 PNG "
            "作聊天圖表；圖表與摘要須使用相同的已核對資料。"
            "篩選結果為零時不得放寬確認條件湊非零；只輸出摘要，不輸出圖表。"
            "圖已嵌入時直接描述，不要另寫 plotly_charts.json 的 Markdown 連結。"
        ),
    )


class ArtifactResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relative_path: str
    kind: str
    extension: str
    mime_type: str
    size_bytes: int
    content_base64: str | None = Field(
        default=None,
        description=(
            "一般文字 artifact 保留原始位元組 base64；plotly_charts.json 是伺服器消費的"
            " Rich UI 控制檔，不回傳給模型。PNG 不會附加或回傳 base64。"
        ),
    )
    chart_display_status: Literal[
        "not_applicable",
        "attached",
        "rendered_as_rich_ui",
        "invalid_png",
        "bridge_not_configured",
        "chat_context_missing",
        "identity_mismatch",
        "attachment_failed",
        "attachment_unknown",
    ] = Field(
        default="not_applicable",
        description=("保留舊版回應欄位供相容；新分析不附加 PNG。"),
    )
    text_preview: str | None = Field(
        default=None,
        description=(
            "僅 UTF-8 .json、.jsonl、.csv 產物提供，最多 16 KiB UTF-8；"
            "其他格式或無效 UTF-8 為 null。答案只能依可見內容作答；"
            "若 preview_truncated 為 true，請要求產生更精簡摘要，"
            "不得猜測預覽未顯示的類別、資料列或數值。"
        ),
    )
    preview_truncated: bool = Field(
        default=False,
        description=(
            "文字預覽是否超過 16 KiB 上限而被截斷。true 代表內容不完整；"
            "請要求更精簡摘要，不得推測被截斷部分的類別、資料列或數值。"
        ),
    )


class AnalysisResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    exit_code: int
    snapshot_id: str
    source: str
    row_count: int
    columns: list[str]
    rich_ui_status: Literal[
        "not_requested",
        "embedded",
        "duplicate_suppressed",
        "invalid_spec",
        "bridge_not_configured",
        "chat_context_missing",
        "identity_mismatch",
        "embed_failed",
        "embed_unknown",
    ] = Field(
        default="not_requested",
        description=(
            "Plotly Rich UI 狀態。embedded 代表通用 renderer 已持久附加；"
            "duplicate_suppressed 代表此訊息已有語意完全相同的圖，沿用既有圖；"
            "invalid_spec 時依 rich_ui_error 修正契約後再試；沒有 PNG 備援。"
        ),
    )
    rich_ui_error: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "Rich UI 驗證或嵌入失敗時的安全短訊息；成功或未要求 Rich UI 時為 null。"
            "不包含 chart spec 原文、HTML、圖表資料值或私密路徑。"
        ),
    )
    artifacts: list[ArtifactResponse] = Field(
        description=(
            "一般文字 artifact 保留原始 content_base64；plotly_charts.json 由伺服器消費"
            "且不列在回應內。JSON、JSONL、CSV 另提供有上限的 text_preview。"
            "PNG 不會附加或回傳；只有 rich_ui_status=embedded 或 duplicate_suppressed"
            " 才能聲稱圖表已顯示。回答數值只能依"
            "可見且未截斷的文字預覽；若已截斷，應要求更精簡摘要，不得猜測缺失類別或數值。"
        )
    )


ERROR_RESPONSES = {
    400: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    429: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
    502: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
    504: {"model": ErrorResponse},
}


def create_app(
    services: ToolServices | None = None,
    *,
    service_loader=build_services,
    chart_bridge: OpenWebUIChartBridge | None = None,
    plotly_asset_url: str | None = None,
) -> FastAPI:
    """建立 API app；測試可注入 fixture services，正式啟動使用 composition root。"""

    application = FastAPI(
        title="BadmintonAI Tool Server",
        version=SERVER_VERSION,
        description=(
            "提供唯讀資料目錄與受 Docker 隔離的自訂 Python 分析工具；"
            "不接受任意 SQL，API 程序不直接執行模型程式碼。"
        ),
    )
    if services is None:
        try:
            services = service_loader()
        except DataError:
            services = None
    application.state.tool_services = services
    application.state.chart_bridge = chart_bridge or OpenWebUIChartBridge(
        base_url=os.environ.get("BADMINTON_AI_OPEN_WEBUI_URL"),
        api_key=os.environ.get("BADMINTON_AI_OPEN_WEBUI_API_KEY"),
    )
    application.state.plotly_asset_url = validate_plotly_asset_url(
        plotly_asset_url
        or os.environ.get("BADMINTON_AI_PLOTLY_ASSET_URL")
        or DEFAULT_PLOTLY_ASSET_URL
    )
    application.state.analysis_runs = OrderedDict()
    application.state.analysis_failures = OrderedDict()
    application.state.analysis_attempts_lock = Lock()
    application.state.terminal_analysis_messages = set()
    application.state.uncertain_embeds = set()
    application.state.completed_embeds = set()

    @application.middleware("http")
    async def trace_request(request: Request, call_next):
        """只記錄請求中繼資料，不記錄提問、程式碼、身分或金鑰。"""

        request_id = uuid.uuid4().hex
        started = time.perf_counter()
        status_code = 500
        error_code = "internal_error"
        try:
            response = await call_next(request)
            status_code = response.status_code
            error_code = response.headers.get("X-Badminton-Error-Code", "")
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            route = request.scope.get("route")
            ACCESS_LOGGER.info(
                json.dumps(
                    {
                        "event": "http_request",
                        "request_id": request_id,
                        "method": request.method,
                        "route": getattr(route, "path", "<unmatched>"),
                        "status": status_code,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                        "error_code": error_code or None,
                    },
                    ensure_ascii=False,
                )
            )

    @application.exception_handler(RequestValidationError)
    async def request_validation_handler(
        _request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        fields = sorted(
            {
                str(location[-1])
                for error in exc.errors()
                if (location := error.get("loc"))
            }
        )
        return _error_response(
            422,
            "invalid_input",
            "請求內容不符合工具契約",
            {"fields": fields} if fields else None,
        )

    @application.exception_handler(DataError)
    async def data_error_handler(_request: Request, exc: DataError) -> JSONResponse:
        status, code, message = _map_domain_error(exc)
        return _error_response(status, code, message)

    @application.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        _request: Request,
        exc: StarletteHTTPException,
    ) -> JSONResponse:
        if exc.status_code == 404:
            return _error_response(404, "not_found", "找不到工具路徑")
        return _error_response(400, "invalid_input", "HTTP 請求不符合工具契約")

    @application.exception_handler(Exception)
    async def internal_error_handler(
        _request: Request,
        _exc: Exception,
    ) -> JSONResponse:
        return _error_response(500, "internal_error", "服務內部錯誤")

    @application.get(
        "/health",
        operation_id="healthCheck",
        summary="檢查 Tool Server 健康狀態",
        description="確認 API 程序可回應，並指出核准資料是否已載入。",
        response_model=HealthResponse,
    )
    async def health() -> HealthResponse:
        available = application.state.tool_services is not None
        return HealthResponse(
            status="ok" if available else "degraded",
            data_available=available,
            version=SERVER_VERSION,
        )

    @application.post(
        "/tools/request-clarification",
        operation_id="requestClarification",
        summary="要求使用者澄清必要的分析條件",
        description=(
            "只有缺少會實質改變答案、且沒有已核准預設的必要條件時才呼叫。"
            "question 是要向使用者顯示的簡短問題；options 可留空或提供最多三個選項。"
            "本工具只回報等待補答，不執行資料分析；呼叫後請直接呈現問題並停止本輪分析。"
        ),
        response_model=ClarificationResponse,
        responses=ERROR_RESPONSES,
    )
    async def request_clarification(
        request: ClarificationRequest,
    ) -> ClarificationResponse:
        return ClarificationResponse(
            status="awaiting_clarification",
            question=request.question,
            options=request.options,
        )

    @application.get(PLOTLY_ASSET_PATH, include_in_schema=False)
    async def plotly_asset() -> FileResponse:
        """提供與 server 依賴同版本、無 CDN 的 Plotly.js bundle。"""

        bundle = importlib.resources.files("plotly").joinpath(
            "package_data", "plotly.min.js"
        )
        return FileResponse(
            Path(str(bundle)),
            media_type="application/javascript",
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                "X-Content-Type-Options": "nosniff",
                "X-Plotly-Python-Version": PLOTLY_VERSION,
            },
        )

    @application.get(
        "/tools/dataset-summary",
        operation_id="getDatasetSummary",
        summary="取得資料集描述摘要",
        description="回傳資料來源檔名、列欄數、場次、局、回合與球員覆蓋量。",
        response_model=DatasetSummaryResponse,
        responses=ERROR_RESPONSES,
    )
    async def dataset_summary() -> DatasetSummaryResponse:
        catalog = _catalog(application)
        return _dataset_response(catalog.dataset_summary())

    @application.get(
        "/tools/columns",
        operation_id="listColumnCatalog",
        summary="列出欄位目錄",
        description=(
            "回傳欄位語意與覆蓋量。欄位已知時不必例行呼叫；只需特定欄位時用 "
            "names=player,type（逗號分隔），省略 names 才回傳全部。"
            "若有不存在欄名，仍回傳已找到欄位，unknown_names 列出未命中項，"
            "available_names 提供完整可用欄名；未命中不代表整個資料集缺少相關資訊。"
            "請從 available_names 選擇或查完整目錄，不要換個猜測欄名反覆呼叫。"
            "null_count 保留既有語意，計算 JSON null 與空白字串；"
            "blank_count 是其中的空白字串子集，json_null_count 是實際 JSON null 數；"
            "distinct_count 排除 JSON null 與空白字串。"
        ),
        response_model=ColumnCatalogResponse,
        responses=ERROR_RESPONSES,
    )
    async def columns(
        names: str | None = Query(
            default=None,
            min_length=1,
            max_length=512,
            description="逗號分隔的精確欄名；省略時回傳全部欄位。",
        ),
    ) -> ColumnCatalogResponse | JSONResponse:
        summaries = _catalog(application).describe_columns()
        unknown_names: list[str] = []
        available_names: list[str] = []
        if names is not None:
            requested = [name.strip() for name in names.split(",")]
            if any(not name for name in requested):
                return _error_response(422, "invalid_input", "names 含空值欄名")
            available_names = [item.name for item in summaries]
            available = set(available_names)
            unknown_names = list(
                dict.fromkeys(name for name in requested if name not in available)
            )
            selected = set(requested)
            summaries = [item for item in summaries if item.name in selected]
        values = [_column_response(item) for item in summaries]
        return ColumnCatalogResponse(
            columns=values,
            empty=not values,
            unknown_names=unknown_names,
            available_names=available_names if unknown_names else [],
        )

    @application.get(
        "/tools/players",
        operation_id="listPlayerCoverage",
        summary="列出球員資料覆蓋",
        description="回傳 canonical 球員的場次、事件與勝負回合數；勝負依完整回合終局得分者計數。",
        response_model=PlayerCatalogResponse,
        responses=ERROR_RESPONSES,
    )
    async def players() -> PlayerCatalogResponse:
        values = [
            _player_response(item) for item in _catalog(application).describe_players()
        ]
        return PlayerCatalogResponse(players=values, empty=not values)

    @application.get(
        "/tools/matches",
        operation_id="listMatchCoverage",
        summary="列出比賽資料覆蓋",
        description="回傳參賽者、局、回合與事件覆蓋的 deterministic 摘要。",
        response_model=MatchCatalogResponse,
        responses=ERROR_RESPONSES,
    )
    async def matches() -> MatchCatalogResponse:
        values = [
            _match_response(item) for item in _catalog(application).describe_matches()
        ]
        return MatchCatalogResponse(matches=values, empty=not values)

    @application.post(
        "/tools/analyze",
        operation_id="runPythonAnalysis",
        summary="在 Docker sandbox 執行自訂 Python 分析",
        description=(
            "將完整資料快照、metadata/schema 與程式交給 Docker 沙箱；"
            "API 程序不直接執行程式碼。輸入與圖表格式見 code 欄位說明。"
        ),
        response_model=AnalysisResponse,
        responses=ERROR_RESPONSES,
    )
    async def analyze(
        request: AnalysisRequest,
        http_request: Request,
    ) -> AnalysisResponse | JSONResponse:
        tool_services = _services(application)
        message_key = _analysis_message_key(http_request)
        if message_key is not None:
            with application.state.analysis_attempts_lock:
                if message_key in application.state.uncertain_embeds:
                    return _error_response(
                        409,
                        "chart_state_unknown",
                        "先前圖表嵌入狀態無法確認；請重新載入對話確認後再要求重試",
                    )
                if message_key in application.state.completed_embeds:
                    return _error_response(
                        409,
                        "chart_already_embedded",
                        "本則回答已有互動圖表；請沿用既有圖，不要重複執行分析",
                    )
                if message_key in application.state.terminal_analysis_messages:
                    return _error_response(
                        409,
                        "analysis_terminated",
                        "本則回答的分析已因基礎設施錯誤終止；請停止工具呼叫並說明分析未完成",
                    )
                failures = application.state.analysis_failures
                current_failures = failures.get(message_key, 0)
                if current_failures >= MAX_ANALYSIS_FAILURES_PER_MESSAGE:
                    return _error_response(
                        429,
                        "analysis_retry_limit",
                        "本則回答的連續分析失敗已達上限；請停止工具呼叫，僅回覆已驗證結果或說明無法完成",
                        {
                            "failed_attempts": current_failures,
                            "max_failed_attempts": MAX_ANALYSIS_FAILURES_PER_MESSAGE,
                            "terminal": True,
                        },
                    )
                runs = application.state.analysis_runs
                current_runs = runs.get(message_key, 0)
                if current_runs >= MAX_ANALYSIS_RUNS_PER_MESSAGE:
                    return _error_response(
                        429,
                        "analysis_message_limit",
                        "本則回答的分析執行次數已達上限；請停止工具呼叫並據實回覆",
                        {
                            "runs": current_runs,
                            "max_runs": MAX_ANALYSIS_RUNS_PER_MESSAGE,
                            "terminal": True,
                        },
                    )
                runs[message_key] = current_runs + 1
                runs.move_to_end(message_key)
                if len(runs) > MAX_TRACKED_ANALYSIS_MESSAGES:
                    old_key, _ = runs.popitem(last=False)
                    application.state.analysis_failures.pop(old_key, None)
                    application.state.terminal_analysis_messages.discard(old_key)
                    application.state.uncertain_embeds.discard(old_key)
                    application.state.completed_embeds.discard(old_key)
        try:
            result = tool_services.sandbox.run(tool_services.query, request.code)
        except DataError as exc:
            status, code, message = _map_domain_error(exc)
            if message_key is not None:
                with application.state.analysis_attempts_lock:
                    if status in {502, 503, 504}:
                        application.state.terminal_analysis_messages.add(message_key)
                    elif isinstance(exc, (SandboxCodeError, SandboxOutputError)):
                        failures = application.state.analysis_failures
                        failures[message_key] = failures.get(message_key, 0) + 1
                if status in {502, 503, 504}:
                    return _error_response(
                        status,
                        code,
                        message,
                        {"terminal": True},
                    )
            raise
        response = _analysis_response(
            result,
            chart_bridge=application.state.chart_bridge,
            chat_id=http_request.headers.get("X-OpenWebUI-Chat-Id"),
            message_id=http_request.headers.get("X-OpenWebUI-Message-Id"),
            user_id=http_request.headers.get("X-OpenWebUI-User-Id"),
            plotly_asset_url=application.state.plotly_asset_url,
        )
        if message_key is not None:
            with application.state.analysis_attempts_lock:
                if response.rich_ui_status == "embed_unknown":
                    application.state.uncertain_embeds.add(message_key)
                elif response.rich_ui_status in {"embedded", "duplicate_suppressed"}:
                    application.state.completed_embeds.add(message_key)
                if response.rich_ui_status == "invalid_spec":
                    failures = application.state.analysis_failures
                    failures[message_key] = failures.get(message_key, 0) + 1
                else:
                    application.state.analysis_failures.pop(message_key, None)
        return response

    return application


def _analysis_message_key(request: Request) -> tuple[str, str, str] | None:
    """只在 Open WebUI 提供完整訊息識別時限制同則回答的分析次數。"""

    user_id = request.headers.get("X-OpenWebUI-User-Id")
    chat_id = request.headers.get("X-OpenWebUI-Chat-Id")
    message_id = request.headers.get("X-OpenWebUI-Message-Id")
    if user_id and chat_id and message_id:
        return user_id, chat_id, message_id
    return None


def _services(application: FastAPI) -> ToolServices:
    services = getattr(application.state, "tool_services", None)
    if services is None:
        raise CompositionError("核准資料尚未載入，Tool Server 暫不可用")
    return services


def _catalog(application: FastAPI) -> BadmintonCatalogService:
    return _services(application).catalog


def _map_domain_error(exc: DataError) -> tuple[int, str, str]:
    if isinstance(exc, (QueryValidationError, UnknownPlayerAliasError)):
        return 400, "invalid_input", "查詢輸入不符合工具契約"
    if isinstance(exc, AmbiguousPlayerAliasError):
        return 400, "invalid_input", "球員 alias 具有歧義"
    if isinstance(exc, UnknownMatchError):
        return 404, "not_found", "找不到指定資料"
    if isinstance(exc, SandboxPolicyError):
        return 400, "invalid_input", "分析程式不符合 sandbox policy"
    if isinstance(exc, SandboxCodeError):
        return 422, "analysis_code_error", str(exc)
    if isinstance(exc, SandboxOutputError):
        if str(exc) == "sandbox 必須產生至少一個 artifact":
            return (
                422,
                "analysis_output_error",
                "分析沒有產生檔案；請在 BADMINTON_OUTPUT_DIR 寫入至少一個檔案（例如 summary.json），print 輸出不算產物",
            )
        return (
            422,
            "analysis_output_error",
            "分析程式輸出的檔案不符合契約；請核對產物位置、數量、格式與大小",
        )
    if isinstance(exc, SandboxUnavailableError):
        return 503, "sandbox_unavailable", "分析 sandbox 目前不可用"
    if isinstance(exc, SandboxTimeoutError):
        return 504, "sandbox_timeout", "分析執行超過時間上限"
    if isinstance(exc, (SandboxExecutionError, SandboxArtifactError)):
        return 502, "sandbox_execution_failure", "分析 sandbox 執行失敗"
    if isinstance(exc, SandboxMaterializationError):
        return 503, "data_unavailable", "完整資料 snapshot 無法準備"
    if isinstance(exc, CatalogError):
        return 503, "data_unavailable", "資料目錄目前不可用"
    if isinstance(exc, CompositionError):
        return 503, "data_unavailable", "核准資料目前不可用"
    return 503, "data_unavailable", "資料目前不可用"


def _error_response(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        headers={"X-Badminton-Error-Code": code},
        content=ErrorResponse(
            code=code,
            message=message,
            details=details,
        ).model_dump(),
    )


def _dataset_response(summary: DatasetSummary) -> DatasetSummaryResponse:
    return DatasetSummaryResponse(
        source=_safe_source_name(summary.source),
        row_count=summary.row_count,
        column_count=summary.column_count,
        match_count=summary.match_count,
        set_count=summary.set_count,
        rally_count=summary.rally_count,
        player_count=summary.player_count,
        snapshot_id=summary.snapshot_id,
        snapshot_version=summary.snapshot_version,
    )


def _column_response(summary: ColumnSummary) -> ColumnSummaryResponse:
    return ColumnSummaryResponse(**asdict(summary))


def _player_response(summary: PlayerCoverageSummary) -> PlayerCoverageResponse:
    return PlayerCoverageResponse(**asdict(summary))


def _match_response(summary: Any) -> MatchSummaryResponse:
    return MatchSummaryResponse(
        match_id=summary.match_id,
        players=list(summary.players),
        sets=list(summary.sets),
        rally_count=summary.rally_count,
        event_count=summary.event_count,
    )


def _analysis_response(
    result: SandboxResult,
    *,
    chart_bridge: OpenWebUIChartBridge | None = None,
    chat_id: str | None = None,
    message_id: str | None = None,
    user_id: str | None = None,
    plotly_asset_url: str = DEFAULT_PLOTLY_ASSET_URL,
) -> AnalysisResponse:
    manifest = result.manifest
    artifact_responses: list[ArtifactResponse] = []
    chart_spec_indices = [
        index
        for index, artifact in enumerate(result.artifacts)
        if artifact.relative_path == CHART_SPEC_FILE
    ]
    plotly_indices = [
        index
        for index, artifact in enumerate(result.artifacts)
        if artifact.relative_path == PLOTLY_CHARTS_FILE
    ]
    control_artifact_indices = set(chart_spec_indices) | set(plotly_indices)
    rich_ui_status: Literal[
        "not_requested",
        "embedded",
        "duplicate_suppressed",
        "invalid_spec",
        "bridge_not_configured",
        "chat_context_missing",
        "identity_mismatch",
        "embed_failed",
        "embed_unknown",
    ] = "not_requested"
    rich_ui_error: str | None = None

    rich_html: str | None = None
    if plotly_indices:
        if _summary_has_zero_events(result.artifacts):
            rich_ui_status = "invalid_spec"
            rich_ui_error = (
                "summary.json 的 event_count 為 0，不嵌入空圖；請核對球員別名與篩選條件"
            )
        elif len(plotly_indices) != 1:
            rich_ui_status = "invalid_spec"
            rich_ui_error = "分析輸出只能包含一個根目錄 plotly_charts.json"
        else:
            try:
                plotly_charts = parse_plotly_charts_artifact(
                    result.artifacts[plotly_indices[0]]
                )
                rich_html = render_plotly_charts_html(
                    plotly_charts,
                    asset_url=plotly_asset_url,
                )
            except PlotlySpecError as exc:
                rich_ui_status = "invalid_spec"
                rich_ui_error = str(exc)[:200]
            except ValueError:
                rich_ui_status = "invalid_spec"
                rich_ui_error = "Plotly 本機資產 URL 設定無效"

    # 舊 chart_spec 與 PNG 只供歷史對話保留檢視，不再作新圖的備援。
    if (
        rich_html is None
        and rich_ui_status != "invalid_spec"
        and (chart_spec_indices or any(_is_png_candidate(a) for a in result.artifacts))
    ):
        rich_ui_status = "invalid_spec"
        rich_ui_error = (
            "新圖表只接受 plotly_charts.json 互動圖；PNG 與 chart_spec 不會附加"
        )

    if rich_html is not None:
        rich_ui_status, rich_ui_error = _emit_rich_ui(
            rich_html,
            chart_bridge=chart_bridge,
            chat_id=chat_id,
            message_id=message_id,
            user_id=user_id,
        )

    for index, artifact in enumerate(result.artifacts):
        if index in control_artifact_indices or _is_png_candidate(artifact):
            # 控制檔與 PNG 不回傳給模型，也不寫入聊天附件。
            continue
        artifact_responses.append(_artifact_response(artifact))

    return AnalysisResponse(
        job_id=result.job_id,
        exit_code=result.exit_code,
        snapshot_id=manifest.snapshot_id,
        source=_safe_source_name(manifest.source),
        row_count=manifest.row_count,
        columns=list(manifest.columns),
        rich_ui_status=rich_ui_status,
        rich_ui_error=rich_ui_error,
        artifacts=artifact_responses,
    )


def _summary_has_zero_events(artifacts: tuple[Any, ...]) -> bool:
    """摘要明示無事件時擋下圖表，避免先嵌空圖後無法同則修正。"""

    for artifact in artifacts:
        if (
            artifact.relative_path != "summary.json"
            or artifact.size_bytes > MAX_ARTIFACT_TEXT_PREVIEW_BYTES
        ):
            continue
        try:
            raw = base64.b64decode(artifact.content_base64, validate=True)
            if len(raw) != artifact.size_bytes:
                continue
            summary = json.loads(raw)
        except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(summary, dict) and type(summary.get("event_count")) is int:
            return summary["event_count"] == 0
    return False


def _emit_rich_ui(
    html_content: str,
    *,
    chart_bridge: OpenWebUIChartBridge | None,
    chat_id: str | None,
    message_id: str | None,
    user_id: str | None,
) -> tuple[
    Literal[
        "embedded",
        "duplicate_suppressed",
        "bridge_not_configured",
        "chat_context_missing",
        "identity_mismatch",
        "embed_failed",
        "embed_unknown",
    ],
    str | None,
]:
    if chart_bridge is None or not chart_bridge.configured:
        return "bridge_not_configured", "Open WebUI 圖表橋接尚未設定"
    if not (chat_id and message_id and user_id):
        return "chat_context_missing", "缺少 Open WebUI 聊天識別資訊"
    try:
        result = chart_bridge.emit_chart_embed(
            html_content,
            chat_id=chat_id,
            message_id=message_id,
            user_id=user_id,
        )
    except OpenWebUIBridgeNotConfigured:
        return "bridge_not_configured", "Open WebUI 圖表橋接尚未設定"
    except OpenWebUIIdentityMismatch:
        return "identity_mismatch", "Open WebUI API key 與聊天使用者不一致"
    except OpenWebUIEventOutcomeUnknown:
        return "embed_unknown", "互動圖表附加狀態無法確認；請重新載入對話檢查"
    except OpenWebUIBridgeError:
        return "embed_failed", "Rich UI event 寫入失敗"
    return (
        ("duplicate_suppressed", None)
        if result == "duplicate_suppressed"
        else ("embedded", None)
    )


def _is_png_candidate(artifact: Any) -> bool:
    return (
        artifact.extension.casefold() == ".png"
        or artifact.mime_type == "image/png"
        or artifact.kind == "chart"
    )


def _artifact_response(
    artifact: Any,
    *,
    content_base64: str | None = None,
    chart_display_status: Literal[
        "not_applicable",
        "attached",
        "rendered_as_rich_ui",
        "invalid_png",
        "bridge_not_configured",
        "chat_context_missing",
        "identity_mismatch",
        "attachment_failed",
        "attachment_unknown",
    ] = "not_applicable",
) -> ArtifactResponse:
    """保留原始 artifact，並為支援的 UTF-8 文字格式附上 bounded preview。"""

    preview: str | None = None
    truncated = False
    if artifact.extension.lower() in TEXT_ARTIFACT_EXTENSIONS:
        try:
            content = base64.b64decode(artifact.content_base64, validate=True)
            if len(content) == artifact.size_bytes:
                decoded = content.decode("utf-8")
                truncated = len(content) > MAX_ARTIFACT_TEXT_PREVIEW_BYTES
                preview = (
                    content[:MAX_ARTIFACT_TEXT_PREVIEW_BYTES].decode(
                        "utf-8",
                        errors="ignore",
                    )
                    if truncated
                    else decoded
                )
        except (binascii.Error, UnicodeDecodeError):
            pass

    return ArtifactResponse(
        relative_path=artifact.relative_path,
        kind=artifact.kind,
        extension=artifact.extension,
        mime_type=artifact.mime_type,
        size_bytes=artifact.size_bytes,
        content_base64=(
            artifact.content_base64
            if content_base64 is None and not _is_png_candidate(artifact)
            else content_base64
        ),
        chart_display_status=chart_display_status,
        text_preview=preview,
        preview_truncated=truncated,
    )


def _safe_source_name(value: str) -> str:
    name = Path(value).name
    return name if name not in {"", ".", ".."} else "dataset"


def main() -> None:
    """啟動本機 uvicorn；不會在程序內執行分析程式碼。"""

    import uvicorn

    host = os.environ.get("BADMINTON_AI_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("BADMINTON_AI_PORT", "8000"))
    except ValueError as exc:
        raise SystemExit("BADMINTON_AI_PORT 必須是整數") from exc
    uvicorn.run(app, host=host, port=port, access_log=False)


app = create_app()


__all__ = [
    "AnalysisRequest",
    "AnalysisResponse",
    "ArtifactResponse",
    "ColumnCatalogResponse",
    "ColumnSummaryResponse",
    "DatasetSummaryResponse",
    "ErrorResponse",
    "HealthResponse",
    "MatchCatalogResponse",
    "MatchSummaryResponse",
    "PlayerCatalogResponse",
    "PlayerCoverageResponse",
    "app",
    "create_app",
    "main",
]
