"""可由 Open WebUI 匯入的最小 OpenAPI Tool Server。"""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import logging
import os
import re
import tempfile
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
    SandboxInputFile,
    SandboxMaterializationError,
    SandboxOutputError,
    SandboxPolicyError,
    SandboxResult,
    SandboxTimeoutError,
    SandboxUnavailableError,
    sandbox_code_error_message,
)
from .chart_display import (
    OpenWebUIBridgeError,
    OpenWebUIBridgeNotConfigured,
    OpenWebUIChartBridge,
    OpenWebUIEventOutcomeUnknown,
    OpenWebUIIdentityMismatch,
)
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
from .result_store import (
    AnalysisResultExpired,
    AnalysisResultNotFound,
    AnalysisResultOutputError,
    AnalysisResultScope,
    AnalysisResultStore,
    AnalysisResultStoreUnavailable,
    StoredAnalysisResult,
)

ACCESS_LOGGER = logging.getLogger("uvicorn.error.badminton_ai")

SERVER_VERSION = "0.1.0"
MAX_ANALYSIS_CODE_CHARS = 64 * 1024
MAX_ARTIFACT_TEXT_PREVIEW_BYTES = 16 * 1024
MAX_ANALYSIS_TEXT_PREVIEW_BYTES = 4 * 1024
MAX_ANALYSIS_TEXT_PREVIEW_TOTAL_BYTES = 4 * 1024
_OPENWEBUI_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
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
            "預載名稱不限 import；pandas/numpy/matplotlib/plotly 已安裝；"
            "標準庫可 import，SciPy 未裝（Spearman corr 需）。澄清前可用 Python 探查／部分分析，"
            "未確認必要口徑不作結論。核對 df.columns 或 "
            "BADMINTON_SCHEMA_FILE；不猜欄名、不覆寫 df。"
            "可讀 BADMINTON_EVENTS_FILE、BADMINTON_MANIFEST_FILE、"
            "BADMINTON_METADATA_FILE。"
            "正式結果須在 BADMINTON_OUTPUT_DIR 保存至少一個檔案；短小 print-only 探查可回傳最多 4 KiB stdout，但沒有 result_id、不可繪圖。"
            "它是環境變數，不是 Python 名稱：out = Path(os.environ['BADMINTON_OUTPUT_DIR'])。"
            "小型 JSON、CSV、JSONL 產物會回傳有限文字預覽，不須指定檔名。"
            "此工具只分析並保存可重用的 JSON、CSV 或 JSONL；"
            "不要輸出圖表。互動圖稍後由 renderAnalysisChart 從已保存檔案產生，"
            "不要在後續繪圖時重新查詢原始資料或把完整資料塞入工具參數。"
        ),
    )


class RenderAnalysisRequest(BaseModel):
    """依同一 chat 的保存結果產生互動圖，不接觸原始資料 snapshot。"""

    model_config = ConfigDict(extra="forbid")

    result_id: Annotated[
        str,
        StringConstraints(pattern=r"^[a-f0-9]{48}$", min_length=48, max_length=48),
    ]
    code: str = Field(
        min_length=1,
        max_length=MAX_ANALYSIS_CODE_CHARS,
        description=(
            "在隔離 Docker 繪圖 sandbox 執行 Python；沒有 df、原始 events、metadata 或 "
            "resolve_player。唯讀使用預載 results_dir（包含此 result_id 的保存 CSV/JSON/JSONL）、"
            "pd、np、json、Path 與 output_dir；繪圖端可 import 已安裝的 Plotly。依本工具列出的檔名"
            "自行讀取與整理；不要假設未保存欄位或另查 snapshot。"
            "使用 Path(output_dir / 'plotly_charts.json') 在根目錄只寫該檔，格式恰為 "
            "schema_version='badminton-plotly/v1' 與 charts（1–4 個），每個 chart 含 title "
            "與 Plotly figure JSON（例如 json.loads(fig.to_json())）。不輸出 PNG、HTML 或其他檔案。"
        ),
    )


class AnalysisFileResponse(BaseModel):
    """不把完整保存資料回傳給模型的簡短檔案描述。"""

    model_config = ConfigDict(extra="forbid")

    relative_path: str
    kind: str
    extension: str
    mime_type: str
    size_bytes: int
    text_preview: str | None = None
    preview_truncated: bool = False
    preview_offset_bytes: int | None = None
    next_offset_bytes: int | None = None
    has_more: bool | None = None


class ReadAnalysisResultResponse(BaseModel):
    """從同一 user/chat 讀取已保存檔案的有限文字預覽。"""

    model_config = ConfigDict(extra="forbid")

    result_id: str
    artifacts: list[AnalysisFileResponse]


class AnalysisResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_id: str | None = Field(
        default=None,
        description="有保存可重用 artifact 時提供；stdout-only 探查沒有 result_id，不可繪圖",
    )
    result_fingerprint: str | None = Field(
        default=None,
        description="Tool Server 對已保存資料檔內容計算的 SHA-256 識別；不包含資料本文",
    )
    analysis_runs_remaining: int | None = Field(
        default=None,
        ge=0,
        le=MAX_ANALYSIS_RUNS_PER_MESSAGE,
        description="此 assistant 訊息尚可執行的分析次數；只供回合收尾控制，不代表分析完整性",
    )
    job_id: str
    exit_code: int
    snapshot_id: str
    source: str
    row_count: int
    columns: list[str]
    artifacts: list[AnalysisFileResponse] = Field(
        description=(
            "relative_path 是可供繪圖使用的真實保存檔名；完整 CSV/JSON/JSONL 留在 user/chat 綁定的 result_id 中，不回傳 base64 或整份資料。"
            "小型 JSON、CSV、JSONL 可提供最多 4 KiB UTF-8 文字預覽；完整檔案仍留在保存區。"
            "單次回應內所有檔案共用 4 KiB 預覽額度；額度用完後後續預覽為 null 並標記截斷。"
            "大型內容只回傳截斷預覽與標記，不回傳 Base64 或完整資料。"
        )
    )
    stdout_preview: str | None = Field(
        default=None,
        description="只有沒有 artifact 的 stdout-only 探查會回傳，最多 4 KiB UTF-8，不能供繪圖重用",
    )
    stdout_preview_truncated: bool = False


class RenderAnalysisResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["embedded", "duplicate_suppressed"] = Field(
        description=(
            "embedded 表示圖已發布；duplicate_suppressed 表示同一 assistant 訊息已有圖，"
            "兩者都應停止重複呼叫並交付。"
        )
    )
    result_id: str = Field(description="本次發布或同訊息既有圖表所使用的分析結果識別碼")
    chart_count: int = Field(ge=1, le=4)


ERROR_RESPONSES = {
    400: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    429: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
    502: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
    504: {"model": ErrorResponse},
    410: {"model": ErrorResponse},
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
    result_store = services.analysis_results if services is not None else None
    if result_store is None:
        result_store_tempdir = tempfile.TemporaryDirectory(
            prefix="badminton-ai-analysis-results-"
        )
        application.state.result_store_tempdir = result_store_tempdir
        result_store = AnalysisResultStore(result_store_tempdir.name)
    application.state.analysis_result_store = result_store
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
            "缺漏必要口徑會改變答案且無核准預設才呼叫。question 共用說明範圍、球種與分母，"
            "options 用白話短句呈現不同定義及自行定義，合起來須可直接分析；最多三項。"
            "最貼題且資料支持者先列，僅首項標建議；無合理 proxy 則說明限制。"
            "可先以 Python 探查或做不依賴該口徑的部分分析，但不得把未確認定義當最終結論。"
            "明確口徑下零樣本直接交付，不為改口徑湊樣本澄清。本工具只回報等待補答，不分析資料。"
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
        summary="在 Docker sandbox 分析並保存可重用資料",
        description=(
            "將完整資料快照、metadata/schema 與程式交給 Docker 沙箱，將 JSON/CSV 類產物"
            "保存於同一 user/chat 可存取的短期結果空間；回傳 result_id 及實際檔名，不嵌入圖表。"
            "僅探查且沒有檔案時，可回傳最多 4 KiB stdout preview，但不建立 result_id，不能繪圖。"
            "互動圖需另呼叫 renderAnalysisChart。API 程序不直接執行程式碼。"
        ),
        response_model=AnalysisResponse,
        responses=ERROR_RESPONSES,
    )
    async def analyze(
        request: AnalysisRequest,
        http_request: Request,
    ) -> AnalysisResponse | JSONResponse:
        tool_services = _services(application)
        scope = _analysis_result_scope(http_request)
        if scope is None:
            return _error_response(
                400,
                "chat_context_missing",
                "缺少有效 Open WebUI 使用者與聊天室識別資訊",
            )
        message_key = _analysis_message_key(http_request)
        analysis_runs_remaining: int | None = None
        if message_key is not None:
            with application.state.analysis_attempts_lock:
                if message_key in application.state.terminal_analysis_messages:
                    return _error_response(
                        409,
                        "analysis_terminated",
                        "本則回答的分析已因基礎設施錯誤終止；請停止工具呼叫並說明分析未完成",
                        {
                            "terminal": True,
                            "analysis_runs_remaining": max(
                                0,
                                MAX_ANALYSIS_RUNS_PER_MESSAGE
                                - application.state.analysis_runs.get(message_key, 0),
                            ),
                        },
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
                            "analysis_runs_remaining": max(
                                0,
                                MAX_ANALYSIS_RUNS_PER_MESSAGE
                                - application.state.analysis_runs.get(message_key, 0),
                            ),
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
                            "analysis_runs_remaining": 0,
                        },
                    )
                runs[message_key] = current_runs + 1
                analysis_runs_remaining = max(
                    0,
                    MAX_ANALYSIS_RUNS_PER_MESSAGE - runs[message_key],
                )
                runs.move_to_end(message_key)
                if len(runs) > MAX_TRACKED_ANALYSIS_MESSAGES:
                    old_key, _ = runs.popitem(last=False)
                    application.state.analysis_failures.pop(old_key, None)
                    application.state.terminal_analysis_messages.discard(old_key)
        try:
            result = tool_services.sandbox.run(tool_services.query, request.code)
            if not result.artifacts:
                preview, _ = _bounded_stdout_preview(result)
                if preview is None:
                    raise SandboxOutputError("sandbox 必須產生至少一個 artifact")
        except DataError as exc:
            status, code, message = _map_domain_error(exc)
            remaining_details = (
                {"analysis_runs_remaining": analysis_runs_remaining}
                if analysis_runs_remaining is not None
                else {}
            )
            if message_key is not None:
                with application.state.analysis_attempts_lock:
                    if status in {502, 503, 504}:
                        application.state.terminal_analysis_messages.add(message_key)
                    elif isinstance(
                        exc,
                        (
                            SandboxCodeError,
                            SandboxOutputError,
                            AnalysisResultOutputError,
                        ),
                    ):
                        failures = application.state.analysis_failures
                        failures[message_key] = failures.get(message_key, 0) + 1
                if status in {502, 503, 504}:
                    remaining_details["terminal"] = True
                    return _error_response(
                        status,
                        code,
                        message,
                        remaining_details,
                    )
            return _error_response(
                status,
                code,
                message,
                remaining_details or None,
            )
        stored_result: StoredAnalysisResult | None = None
        if result.artifacts:
            try:
                stored_result = application.state.analysis_result_store.save(
                    scope=scope,
                    snapshot_id=result.manifest.snapshot_id,
                    artifacts=result.artifacts,
                )
            except DataError as exc:
                status, code, message = _map_domain_error(exc)
                remaining_details = (
                    {"analysis_runs_remaining": analysis_runs_remaining}
                    if analysis_runs_remaining is not None
                    else {}
                )
                if message_key is not None:
                    with application.state.analysis_attempts_lock:
                        if status in {502, 503, 504}:
                            application.state.terminal_analysis_messages.add(
                                message_key
                            )
                        elif isinstance(exc, AnalysisResultOutputError):
                            failures = application.state.analysis_failures
                            failures[message_key] = failures.get(message_key, 0) + 1
                if status in {502, 503, 504}:
                    remaining_details["terminal"] = True
                return _error_response(
                    status,
                    code,
                    message,
                    remaining_details or None,
                )
        response = _analysis_response(
            result,
            stored_result,
            analysis_runs_remaining=analysis_runs_remaining,
        )
        if message_key is not None and stored_result is not None:
            with application.state.analysis_attempts_lock:
                application.state.analysis_failures.pop(message_key, None)
        return response

    @application.get(
        "/tools/analysis-result",
        operation_id="readAnalysisResult",
        summary="唯讀查看已保存分析結果",
        description=(
            "依同一 user/chat 的 result_id 讀取已保存檔名與最多 4 KiB UTF-8 預覽，"
            "可用 relative_path 精確選取單一檔案，offset_bytes 預設 0；有 has_more 時以 next_offset_bytes 讀下一段。"
            "位置均為 UTF-8 bytes，片段不代表完整 JSON；"
            "此工具不執行 Python、不查詢原始 snapshot、不繪圖，也不扣分析額度。"
        ),
        response_model=ReadAnalysisResultResponse,
        responses=ERROR_RESPONSES,
    )
    async def read_analysis_result(
        http_request: Request,
        result_id: str = Query(
            ...,
            min_length=48,
            max_length=48,
            pattern=r"^[a-f0-9]{48}$",
            description="先前 runPythonAnalysis 回傳的 opaque result_id。",
        ),
        relative_path: str | None = Query(
            default=None,
            min_length=1,
            max_length=512,
            description="可選，精確保存檔名；不接受路徑模式或路徑正規化。",
        ),
        offset_bytes: str | None = Query(
            default=None,
            max_length=20,
            pattern=r"^(0|[1-9][0-9]*)$",
            description="僅指定 relative_path 時可用，UTF-8 byte 起點；使用上段 next_offset_bytes，不是字元索引。",
        ),
    ) -> ReadAnalysisResultResponse | JSONResponse:
        scope = _analysis_result_scope(http_request)
        if scope is None:
            return _error_response(
                400,
                "chat_context_missing",
                "缺少有效 Open WebUI 使用者與聊天室識別資訊",
            )
        store: AnalysisResultStore = http_request.app.state.analysis_result_store
        try:
            stored = store.get(result_id, scope=scope)
        except DataError as exc:
            status, code, message = _map_domain_error(exc)
            return _error_response(status, code, message)

        files = stored.files
        if relative_path is not None:
            files = tuple(item for item in files if item.relative_path == relative_path)
            if not files:
                return _error_response(
                    404,
                    "analysis_result_file_not_found",
                    "找不到此保存結果中的指定檔案",
                )
        if relative_path is None and offset_bytes is not None:
            return _error_response(
                400, "analysis_result_offset_invalid", "分段讀取需指定保存檔名"
            )
        if relative_path is not None:
            try:
                artifacts = [_analysis_file_segment(files[0], int(offset_bytes or "0"))]
            except ValueError:
                return _error_response(
                    400,
                    "analysis_result_offset_invalid",
                    "讀取位置超界、非 UTF-8 邊界或保存文字編碼無效",
                )
        else:
            artifacts = _analysis_file_responses(files)
        return ReadAnalysisResultResponse(
            result_id=stored.result_id,
            artifacts=artifacts,
        )

    @application.post(
        "/tools/render-chart",
        operation_id="renderAnalysisChart",
        summary="從保存的分析結果繪製並嵌入互動圖",
        description=(
            "以 result_id 讀取同一 user/chat 的短期保存 JSON/CSV，將唯讀檔案交給 Docker 繪圖模式；"
            "不查詢原始資料 snapshot。繪圖 sandbox 僅輸出 Plotly JSON，由 Tool Server 驗證後"
            "透過既有 Rich UI 寫回本 assistant 訊息；失敗可只修正繪圖程式。"
            "embedded 與 duplicate_suppressed 都代表同訊息圖表已發布；duplicate_suppressed 時勿再呼叫 render，直接交付。"
        ),
        response_model=RenderAnalysisResponse,
        responses=ERROR_RESPONSES,
    )
    async def render_analysis_chart(
        request: RenderAnalysisRequest,
        http_request: Request,
    ) -> RenderAnalysisResponse | JSONResponse:
        services = _services(application)
        scope = _analysis_result_scope(http_request)
        message_id = http_request.headers.get("X-OpenWebUI-Message-Id")
        if (
            scope is None
            or not isinstance(message_id, str)
            or _OPENWEBUI_ID_PATTERN.fullmatch(message_id) is None
        ):
            return _error_response(
                400,
                "chat_context_missing",
                "繪圖需要有效 Open WebUI 使用者、聊天室與 assistant 訊息識別資訊",
            )
        store: AnalysisResultStore = application.state.analysis_result_store
        try:
            stored = store.get(request.result_id, scope=scope)
            claim = store.begin_render(
                request.result_id,
                scope=scope,
                message_id=message_id,
            )
        except DataError as exc:
            status, code, message = _map_domain_error(exc)
            return _error_response(status, code, message)

        if claim.status == "completed":
            if claim.result_id is None:
                return _error_response(
                    409,
                    "chart_state_unknown",
                    "既有圖表使用的分析結果無法確認；請先查看原對話，避免重複發布",
                    {"terminal": True},
                )
            return RenderAnalysisResponse(
                status="duplicate_suppressed",
                result_id=claim.result_id,
                chart_count=max(1, claim.chart_count or 1),
            )
        if claim.status in {"unknown", "terminal", "busy", "limit"}:
            status, code, message = {
                "unknown": (
                    409,
                    "chart_state_unknown",
                    "先前繪圖或嵌入狀態無法確認；請先檢查原對話，不要盲目重送",
                ),
                "terminal": (
                    409,
                    "render_terminated",
                    "本則訊息的繪圖已因基礎設施錯誤終止；請停止重試並說明未完成",
                ),
                "busy": (409, "render_in_progress", "本則訊息已有繪圖正在執行"),
                "limit": (
                    429,
                    "render_retry_limit",
                    "本則訊息的繪圖修正次數已達上限；請停止重試",
                ),
            }[claim.status]
            return _error_response(
                status,
                code,
                message,
                {"terminal": claim.status != "busy", "attempts": claim.attempts},
            )

        try:
            rendered = services.sandbox.run_render(
                tuple(
                    SandboxInputFile(item.relative_path, item.content)
                    for item in stored.files
                ),
                snapshot_id=stored.snapshot_id,
                code=request.code,
            )
        except DataError as exc:
            status, code, message = _map_render_error(exc)
            try:
                store.finish_render(
                    scope=scope,
                    message_id=message_id,
                    status="terminal" if status in {502, 503, 504} else "failed",
                )
            except DataError:
                pass
            return _error_response(
                status,
                code,
                message,
                {
                    "terminal": status in {502, 503, 504},
                    "attempt": claim.attempts,
                    "max_attempts": MAX_ANALYSIS_FAILURES_PER_MESSAGE,
                    **(
                        {
                            "available_files": [
                                item.relative_path for item in stored.files
                            ]
                        }
                        if isinstance(exc, SandboxCodeError)
                        and exc.hint == "missing_file"
                        else {}
                    ),
                },
            )

        chart_artifacts = [
            item
            for item in rendered.artifacts
            if item.relative_path == PLOTLY_CHARTS_FILE
        ]
        if len(chart_artifacts) != 1 or len(rendered.artifacts) != 1:
            return _finish_render_error(
                store,
                scope=scope,
                message_id=message_id,
                status_code=422,
                code="render_output_error",
                message="繪圖只可輸出根目錄 plotly_charts.json，不可輸出其他檔案",
                attempt=claim.attempts,
                result_id=request.result_id,
            )
        try:
            charts = parse_plotly_charts_artifact(chart_artifacts[0])
            if _stored_json_reports_zero_events(stored):
                raise PlotlySpecError("來源摘要表示沒有符合條件的事件")
            rich_html = render_plotly_charts_html(
                charts,
                asset_url=application.state.plotly_asset_url,
            )
        except (PlotlySpecError, ValueError) as exc:
            detail = (
                str(exc)[:200]
                if isinstance(exc, PlotlySpecError)
                else "Plotly 本機資產 URL 設定無效"
            )
            return _finish_render_error(
                store,
                scope=scope,
                message_id=message_id,
                status_code=422,
                code="render_invalid_spec",
                message=f"Plotly 圖表格式無效：{detail}",
                attempt=claim.attempts,
                result_id=request.result_id,
            )

        rich_status, rich_error = _emit_rich_ui(
            rich_html,
            chart_bridge=application.state.chart_bridge,
            chat_id=scope.chat_id,
            message_id=message_id,
            user_id=scope.user_id,
        )
        if rich_status in {"embedded", "duplicate_suppressed"}:
            try:
                store.finish_render(
                    scope=scope,
                    message_id=message_id,
                    status="completed",
                    chart_count=len(charts.charts),
                )
            except DataError:
                return _error_response(
                    503,
                    "render_state_unavailable",
                    "圖表已送出但保存完成狀態失敗；請檢查原對話，不要盲目重送",
                    {"terminal": True},
                )
            return RenderAnalysisResponse(
                status=rich_status,
                result_id=request.result_id,
                chart_count=len(charts.charts),
            )
        if rich_status == "embed_unknown":
            terminal_state = "unknown"
            status_code, error_code = 409, "chart_state_unknown"
        elif rich_status == "bridge_not_configured":
            terminal_state = "terminal"
            status_code, error_code = 503, "chart_bridge_unavailable"
        elif rich_status == "identity_mismatch":
            terminal_state = "terminal"
            status_code, error_code = 403, "chart_identity_mismatch"
        else:
            terminal_state = "terminal"
            status_code, error_code = 502, "chart_embed_failed"
        try:
            store.finish_render(
                scope=scope,
                message_id=message_id,
                status=terminal_state,
            )
        except DataError:
            terminal_state = "unknown"
        return _error_response(
            status_code,
            error_code,
            rich_error or "Rich UI 圖表嵌入失敗",
            {"terminal": terminal_state in {"unknown", "terminal"}},
        )

    return application


def _analysis_result_scope(request: Request) -> AnalysisResultScope | None:
    user_id = request.headers.get("X-OpenWebUI-User-Id")
    chat_id = request.headers.get("X-OpenWebUI-Chat-Id")
    if (
        not isinstance(user_id, str)
        or _OPENWEBUI_ID_PATTERN.fullmatch(user_id) is None
        or not isinstance(chat_id, str)
        or _OPENWEBUI_ID_PATTERN.fullmatch(chat_id) is None
    ):
        return None
    try:
        return AnalysisResultScope(user_id=user_id, chat_id=chat_id)
    except ValueError:
        return None


def _map_render_error(exc: DataError) -> tuple[int, str, str]:
    if isinstance(exc, SandboxCodeError):
        return 422, "render_code_error", sandbox_code_error_message(exc)
    if isinstance(exc, SandboxOutputError):
        return (
            422,
            "render_output_error",
            "繪圖程式輸出不符合契約；只輸出根目錄 plotly_charts.json",
        )
    status, code, message = _map_domain_error(exc)
    return status, f"render_{code}", message


def _finish_render_error(
    store: AnalysisResultStore,
    *,
    scope: AnalysisResultScope,
    message_id: str,
    status_code: int,
    code: str,
    message: str,
    attempt: int,
    result_id: str,
) -> JSONResponse:
    try:
        store.finish_render(
            scope=scope,
            message_id=message_id,
            status="failed",
        )
    except DataError:
        return _error_response(
            503,
            "render_state_unavailable",
            "繪圖狀態無法保存；請先檢查原對話再處理",
            {"terminal": True},
        )
    return _error_response(
        status_code,
        code,
        message,
        {
            "terminal": False,
            "attempt": attempt,
            "max_attempts": MAX_ANALYSIS_FAILURES_PER_MESSAGE,
            "result_id": result_id,
        },
    )


def _stored_json_reports_zero_events(result: StoredAnalysisResult) -> bool:
    for artifact in result.files:
        if (
            artifact.extension != ".json"
            or artifact.size_bytes > MAX_ARTIFACT_TEXT_PREVIEW_BYTES
        ):
            continue
        try:
            summary = json.loads(artifact.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(summary, dict) and type(summary.get("event_count")) is int:
            return summary["event_count"] == 0
    return False


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
    if isinstance(exc, AnalysisResultExpired):
        return 410, "analysis_result_expired", "保存分析結果已逾期，請重新執行分析"
    if isinstance(exc, AnalysisResultNotFound):
        return 404, "analysis_result_not_found", "找不到此聊天室可使用的分析結果"
    if isinstance(exc, AnalysisResultOutputError):
        return 422, "analysis_result_output_error", str(exc)
    if isinstance(exc, AnalysisResultStoreUnavailable):
        return 503, "analysis_result_store_unavailable", "分析結果暫存目前無法使用"
    if isinstance(exc, (QueryValidationError, UnknownPlayerAliasError)):
        return 400, "invalid_input", "查詢輸入不符合工具契約"
    if isinstance(exc, AmbiguousPlayerAliasError):
        return 400, "invalid_input", "球員 alias 具有歧義"
    if isinstance(exc, UnknownMatchError):
        return 404, "not_found", "找不到指定資料"
    if isinstance(exc, SandboxPolicyError):
        return 400, "invalid_input", "分析程式不符合 sandbox policy"
    if isinstance(exc, SandboxCodeError):
        return 422, "analysis_code_error", sandbox_code_error_message(exc)
    if isinstance(exc, SandboxOutputError):
        if str(exc) == "sandbox 必須產生至少一個 artifact":
            return (
                422,
                "analysis_output_error",
                "分析沒有產生可重用檔案；正式結果請在 BADMINTON_OUTPUT_DIR 保存 JSON、CSV 或 JSONL，stdout-only 探查不能供繪圖",
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
    stored: StoredAnalysisResult | None,
    *,
    analysis_runs_remaining: int | None = None,
) -> AnalysisResponse:
    manifest = result.manifest
    if stored is None:
        preview, truncated = _bounded_stdout_preview(result)
        return AnalysisResponse(
            result_id=None,
            result_fingerprint=None,
            analysis_runs_remaining=analysis_runs_remaining,
            job_id=result.job_id,
            exit_code=result.exit_code,
            snapshot_id=manifest.snapshot_id,
            source=_safe_source_name(manifest.source),
            row_count=manifest.row_count,
            columns=list(manifest.columns),
            artifacts=[],
            stdout_preview=preview,
            stdout_preview_truncated=truncated,
        )

    return AnalysisResponse(
        result_id=stored.result_id,
        result_fingerprint=_saved_result_fingerprint(stored),
        analysis_runs_remaining=analysis_runs_remaining,
        job_id=result.job_id,
        exit_code=result.exit_code,
        snapshot_id=manifest.snapshot_id,
        source=_safe_source_name(manifest.source),
        row_count=manifest.row_count,
        columns=list(manifest.columns),
        artifacts=_analysis_file_responses(stored.files),
        stdout_preview=None,
        stdout_preview_truncated=False,
    )


def _analysis_file_segment(stored_file: Any, offset_bytes: int) -> AnalysisFileResponse:
    """以 byte 游標切 UTF-8 完整字元；下一位置只前進實際回傳的 bytes。"""

    content = stored_file.content
    if not 0 <= offset_bytes <= len(content):
        raise ValueError("無效位置")
    content.decode("utf-8", errors="strict")
    content[:offset_bytes].decode("utf-8", errors="strict")
    end = min(len(content), offset_bytes + MAX_ANALYSIS_TEXT_PREVIEW_TOTAL_BYTES)
    # 已確認全文合法，只需避開段尾被切開的多 byte 字元。
    preview = content[offset_bytes:end].decode("utf-8", errors="ignore")
    next_offset = offset_bytes + len(preview.encode("utf-8"))
    return AnalysisFileResponse(
        relative_path=stored_file.relative_path,
        kind=stored_file.kind,
        extension=stored_file.extension,
        mime_type=stored_file.mime_type,
        size_bytes=stored_file.size_bytes,
        text_preview=preview,
        preview_truncated=offset_bytes > 0 or next_offset < len(content),
        preview_offset_bytes=offset_bytes,
        next_offset_bytes=next_offset,
        has_more=next_offset < len(content),
    )


def _analysis_file_responses(
    stored_files: tuple[Any, ...],
) -> list[AnalysisFileResponse]:
    """依原分析契約建立實際檔名與共享 4 KiB UTF-8 預覽。"""

    previewable_indexes = sorted(
        (
            index
            for index, stored_file in enumerate(stored_files)
            if stored_file.extension in {".json", ".csv", ".jsonl"}
        ),
        key=lambda index: (stored_files[index].size_bytes, index),
    )
    previews: dict[int, tuple[str | None, bool]] = {}
    remaining_preview_bytes = MAX_ANALYSIS_TEXT_PREVIEW_TOTAL_BYTES
    for index in previewable_indexes:
        stored_file = stored_files[index]
        if remaining_preview_bytes <= 0:
            previews[index] = (None, True)
            continue
        try:
            content = stored_file.content
            preview_limit = min(
                MAX_ANALYSIS_TEXT_PREVIEW_BYTES,
                remaining_preview_bytes,
            )
            truncated = len(content) > preview_limit
            preview = content[:preview_limit].decode(
                "utf-8",
                errors="ignore" if truncated else "strict",
            )
            remaining_preview_bytes -= len(preview.encode("utf-8"))
            previews[index] = (preview, truncated)
        except UnicodeDecodeError:
            previews[index] = (None, len(stored_file.content) > preview_limit)

    file_responses: list[AnalysisFileResponse] = []
    for index, stored_file in enumerate(stored_files):
        preview, truncated = previews.get(index, (None, False))
        file_responses.append(
            AnalysisFileResponse(
                relative_path=stored_file.relative_path,
                kind=stored_file.kind,
                extension=stored_file.extension,
                mime_type=stored_file.mime_type,
                size_bytes=stored_file.size_bytes,
                text_preview=preview,
                preview_truncated=truncated,
            )
        )
    return file_responses


def _saved_result_fingerprint(stored: StoredAnalysisResult) -> str | None:
    """依保存檔案的精確內容建 fingerprint，不納入檔名或回應包裝。"""

    file_digests = sorted(
        (len(item.content), hashlib.sha256(item.content).digest())
        for item in stored.files
        if item.extension in {".json", ".csv", ".jsonl"}
    )
    if not file_digests:
        return None
    digest = hashlib.sha256(b"badmintonai-saved-result-v1\0")
    for size_bytes, file_digest in file_digests:
        digest.update(size_bytes.to_bytes(8, "big"))
        digest.update(file_digest)
    return digest.hexdigest()


def _bounded_stdout_preview(result: SandboxResult) -> tuple[str | None, bool]:
    """以 UTF-8 位元組再次限制 stdout-only 探查回應。"""

    value = result.stdout_preview
    if not isinstance(value, str):
        return None, False
    content = value.encode("utf-8", errors="replace")
    truncated = bool(result.stdout_preview_truncated)
    if len(content) > MAX_ANALYSIS_TEXT_PREVIEW_TOTAL_BYTES:
        content = content[:MAX_ANALYSIS_TEXT_PREVIEW_TOTAL_BYTES]
        truncated = True
    preview = content.decode("utf-8", errors="ignore")
    if not preview.strip():
        return None, truncated
    return preview, truncated


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
    "AnalysisFileResponse",
    "ReadAnalysisResultResponse",
    "RenderAnalysisRequest",
    "RenderAnalysisResponse",
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
