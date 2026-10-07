# 首次設定指南

本指南供第一次安裝 BadmintonAI 的使用者使用。以下命令以 Windows PowerShell 為例，請從 clone 後的專案根目錄執行。

## 哪些已包含，哪些要自行設定

| 項目 | Clone／啟動後的狀態 |
| --- | --- |
| 羽球逐拍 CSV、欄位 metadata、100 題評測題庫 | 已包含在專案中，預設路徑已填好 |
| Open WebUI 與 Tool Server | `docker compose up -d --build` 時建置並啟動 |
| Python 分析沙箱映像 | 需先從 GHCR 拉取固定 digest；Compose 不會代為拉取或建置 |
| 模型供應商、API key、基礎模型 | 每個 Open WebUI 執行個體都要自行設定 |
| OpenAPI Tool Server 連線與 BadmintonAI 模型 | 每個 Open WebUI 執行個體都要自行建立 |
| System prompt、Skill、Knowledge | 原始文件在專案中，但不會自動匯入；需在 Open WebUI 手動設定 |
| 評測／聊天室圖表附件 API key | 選用；要使用這些功能時才設定在本機 `.env` |

CSV 不會在啟動時自動轉成 SQLite。Tool Server 會依設定讀取 CSV 或指定的 SQLite 檔案。

## 1. Clone 專案並建立 `.env`

```powershell
git clone https://github.com/douobb/BadmintonAI.git
Set-Location BadmintonAI
Copy-Item .env.example .env
```

範例檔已指向專案附帶的資料與題庫。使用預設資料時，通常只要建立秘密檔；只有改用自備資料或需避開連接埠衝突時，才修改下表設定。

| `.env` 設定 | 預設值 | 何時修改 |
| --- | --- | --- |
| `BADMINTON_AI_SOURCE_DATA_HOST_DIR` | `./data` | 改用其他資料資料夾時，填主機上的資料目錄 |
| `BADMINTON_AI_DATA_FILE` | `processed_new_3.csv` | 改用其他 CSV／SQLite 檔時，填相對於資料目錄的檔名 |
| `BADMINTON_AI_METADATA_DIR` | `metadata` | 欄位說明不在資料目錄的 `metadata` 子目錄時 |
| `BADMINTON_AI_SQLITE_TABLE` | `match_data` | SQLite 資料表名稱不是 `match_data` 時 |
| `BADMINTON_AI_EVALUATION_QUESTIONS_HOST_FILE` | `./evaluation/questions/評估問題_v2.txt` | 改用其他題庫檔時 |
| `OPEN_WEBUI_HOST_PORT` | `3000` | 主機的 3000 埠已被占用時 |
| `BADMINTON_AI_TOOL_SERVER_HOST_PORT` | `8000` | 主機的 8000 埠已被占用時 |

自備資料時，請確認 CSV／SQLite 檔案、欄位 metadata 與設定彼此相符。Windows 路徑建議使用絕對路徑及正斜線，例如 `C:/Badminton/data`。

## 2. 建立持久的 Open WebUI 秘密檔

`.env.example` 的 `WEBUI_SECRET_KEY_FILE` 預設值是 `.secrets/webui_secret_key`。這是相對於專案根目錄的主機端路徑；設定的是檔案位置，不是金鑰內容。Compose 會把該檔案掛載給 Open WebUI，並要求它在首次啟動前已存在。

要建立的結構如下。檔名是 `webui_secret_key`，**沒有副檔名**，不要存成 `webui_secret_key.txt`：

```text
專案根目錄\
└─ .secrets\
   └─ webui_secret_key
```

```powershell
New-Item -ItemType Directory -Path .secrets -Force | Out-Null
```

上面的命令只建立 `.secrets` 資料夾，不會建立金鑰檔。接著二選一：

**方式 A：使用瀏覽器的密碼管理器**

1. 使用瀏覽器內建密碼管理器（若有提供此功能），或其他密碼管理器的隨機密碼產生器，產生至少 32 個隨機字元。
2. 用文字編輯器建立檔案 `.secrets/webui_secret_key`，將產生的字串單獨放在檔案第一行後儲存。若使用記事本的「另存新檔」，「存檔類型」選「所有檔案」，檔名輸入 `webui_secret_key`，避免自動加上 `.txt`。

**方式 B：用 PowerShell 直接產生並寫入檔案**

在專案根目錄執行以下命令。它會用作業系統的密碼學亂數產生 32 個隨機位元組，轉成 64 個十六進位字元後直接寫入目標檔案，不會把金鑰印到畫面上；若檔案已存在則停止，不會覆寫：

```powershell
$keyPath = Join-Path (Get-Location) '.secrets\webui_secret_key'
if (Test-Path -LiteralPath $keyPath) { throw '金鑰檔已存在，為避免更換既有金鑰而停止。' }
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
try {
    $bytes = New-Object byte[] 32
    $rng.GetBytes($bytes)
    $key = -join ($bytes | ForEach-Object { $_.ToString('x2') })
    [System.IO.File]::WriteAllText($keyPath, $key)
}
finally {
    $rng.Dispose()
    Remove-Variable bytes, key, rng -ErrorAction SilentlyContinue
}
```

金鑰不是 Open WebUI 登入密碼或模型 API key。請保留同一份檔案供後續啟動使用；不要把金鑰貼到聊天，也不要提交 `.env` 或 `.secrets/`。如果這個檔案已存在且 Open WebUI 已使用它，請勿重新產生或覆寫，否則可能使既有登入工作階段失效，並影響以舊金鑰加密的資料。

## 3. 拉取固定版本的分析沙箱

```powershell
docker pull ghcr.io/douobb/badminton-ai-sandbox@sha256:ee9862501ceac5bf9064679b209ccb88801df753dc16fc14ecef76c386b9cc22
```

這是公開 GHCR 映像，不需要登入。程式以 digest 固定版本，避免不同安裝取得不一致的沙箱。映像目前以 `linux/amd64` 建置及驗證。

Compose 只建置 Tool Server 與修補版 Open WebUI；Python 分析時，Tool Server 會要求本機 Docker 使用這個預先拉取的映像建立短生命週期容器。若跳過此步驟，聊天頁仍可能開啟，但 Python 分析會因找不到映像而失敗。

## 4. 啟動服務並確認健康狀態

```powershell
docker compose config --quiet
docker compose up -d --build
docker compose ps
Invoke-RestMethod http://localhost:8000/health
```

健康檢查應回報 `status` 為 `ok` 且資料可用。接著開啟 [http://localhost:3000/](http://localhost:3000/)，完成第一個管理員帳號的建立並登入。

## 5. 在 Open WebUI 設定模型與 Tool Server

這些設定儲存在 Open WebUI 執行個體，不在 `.env` 或 Docker image 中；換一個 Open WebUI 資料卷或新安裝時需重新設定。

1. **設定模型供應商**：到管理設定中的 Connections，新增所使用的模型供應商連線，填入供應商提供的 API URL、API key，並確認至少有一個可用的基礎模型。這把模型供應商 API key 存在 Open WebUI，不要填入專案 `.env`。
2. **連接 Tool Server**：在 Connections 新增 OpenAPI Server，URL 填 `http://tool-server:8000/openapi.json`。這是 Compose 網路內的服務名稱；不要在容器連線設定中改成 `localhost:8000`。儲存後確認 Open WebUI 已讀取工具清單。
   若既有 OpenAPI 連線設有 `function_name_filter_list`，須將 `renderAnalysisChart` 明確加入允許清單並重新載入該模型工具；新增 OpenAPI route 不會自動修改既有 allowlist。其他既有工具名稱請保留。
3. **匯入 Skill**：到 Workspace > Skills，使用匯入功能選擇 `skills/badminton-analysis.md`。該 Markdown 沒有 YAML frontmatter，因此匯入後需在介面補上名稱與描述並儲存。
4. **建立 Knowledge**：到 Workspace > Knowledge 建立一個羽球分析知識庫，分別上傳：
   - `knowledge/badminton-terminology.md`
   - `knowledge/event-terms-and-shot-types.md`
   - `knowledge/court-zones.md`

   等待文件處理完成。`knowledge/court-zones.json` 是程式使用的機器可讀檔，不是一般聊天知識文件的必要上傳項目。
5. **建立 BadmintonAI 模型**：到 Workspace > Models 建立模型，名稱可用 `BadmintonAI`，模型 ID 必須是 `badmintonai`（評測工作台使用此 ID）。選擇剛設定的基礎模型，將 `prompts/badmintonai-system.md` 的內容貼入系統提示詞，並綁定剛建立的 Skill、Knowledge，以及 Tool Server 提供的必要工具。基礎模型須支援可靠的工具呼叫，否則模型可能不會正確使用資料分析工具。

獨立繪圖工具需要新版 sandbox bootstrap。公開 pinned GHCR digest 尚未包含此版本；啟用 renderer 前，請在專案根目錄建置本機固定 tag，並在私有 `.env` 設定 `BADMINTON_AI_SANDBOX_IMAGE=badmintonai-sandbox:analysis-render-20261003`：

```powershell
docker build -f sandbox/Dockerfile -t badmintonai-sandbox:analysis-render-20261003 .
docker compose up -d tool-server
```

這項本機 override 只影響 Compose 啟動的 Tool Server，不會發布 GHCR image；公開 digest 需另行建置、驗證與發布。

Open WebUI 的操作名稱會隨版本略有不同；可參考官方文件：[Models](https://docs.openwebui.com/features/workspace/models/)、[Skills](https://docs.openwebui.com/features/workspace/skills/)、[Knowledge](https://docs.openwebui.com/features/workspace/knowledge/) 與 [OpenAPI Servers](https://docs.openwebui.com/features/extensibility/plugin/tools/openapi-servers/)。

重要：`skills/`、`knowledge/`、`prompts/` 是可供管理員匯入或複製的來源文件。Compose 不會替你建立模型、匯入內容或綁定工具；請確認模型已綁定三者，否則對話不會自動使用它們。

## 6. 選用：啟用評測工作台、聊天室圖表附件與對話匯出

同站 API 由 Event Function 註冊。先匯入並啟用 Event Function，工作台及原生聊天列表下載選單才會有對應 API。聊天匯出不需管理員 API key；該 key 只用於評測工作台和圖表附件持久化。

1. 管理員到 Workspace > Functions 匯入並啟用 `openwebui_functions/evaluation_workbench_event.py` Event Function。它會註冊管理員評測工作台及同站聊天匯出 API。
2. **只有要使用評測工作台或將聊天室圖表附件透過 API 存回對話時**，才需設定 Open WebUI API key：
   1. 登入 Open WebUI，從實際操作評測工作台及圖表附件功能的帳號建立 API key（帳號設定中的 API key 管理）。
   2. 將 key 放入本機 `.env`，取消註解並填入真實值：

      ```dotenv
      BADMINTON_AI_OPEN_WEBUI_API_KEY=在此填入你建立的API金鑰
      ```

   3. 儲存 `.env` 後，在專案根目錄執行 `docker compose up -d`，讓服務載入新環境變數。
3. 若已設定 API key，管理員可開啟 [評測工作台](http://localhost:3000/badmintonai/evaluation)。
4. 聊天匯出不需額外匯入 Action。建立或開啟已儲存的聊天後，在聊天列表該對話的「⋯ → 下載」選擇 PDF 或互動 HTML；選單以對話 ID 下載完整已儲存分支，並依登入者的 Open WebUI 匯出權限與對話擁有權授權，不會更動原訊息或圖表附件。尚未儲存的對話不提供匯出。

聊天與評測 PDF 共用 Open WebUI 映像內的 Chromium renderer；Dockerfile 會安裝 Playwright Chromium 與 Noto CJK 字型，Tool Server 不會安裝這些 PDF 依賴。更新程式碼後以 `docker compose up -d --build open-webui` 重建該服務，再測試一段含多輪對話及圖表的聊天與評測報告。若圖表未完成渲染或產生逾時，下載 API 會回報錯誤，不會提供缺圖的 PDF。

## 7. 驗證安裝

- 在 Open WebUI 模型選單選擇 `BadmintonAI`，詢問一個簡單的資料摘要問題，確認模型有使用 Tool Server 回答。
- 再試一個需要分類比較或空間分布的問題，確認 Python 分析可以執行，互動圖表能呈現在聊天室。
- 若已設定 API key，開啟評測工作台，預覽題庫並只選少量題目做首次測試。
- 在一個已儲存、含多輪內容或 Plotly 圖表的聊天中，從聊天列表的「⋯ → 下載」分別下載 PDF 與互動 HTML，確認下載完整對話、PDF 有中文字與完整圖表、HTML 可互動，且原訊息的圖表仍保留。新環境可先用自己的測試對話，不需重新執行評測或模型。

## 日常啟動與停止

Docker Desktop 啟動後，在專案根目錄執行：

```powershell
docker compose up -d
```

開啟 [http://localhost:3000/](http://localhost:3000/) 使用。暫時停止容器可執行 `docker compose stop`；要移除容器與網路可執行 `docker compose down`。不要加上 `-v`，除非確定要刪除 Open WebUI 對話與評測紀錄所在的持久卷。
