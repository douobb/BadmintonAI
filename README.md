# BadmintonAI

BadmintonAI 讓使用者用自然語言分析羽球逐拍資料。Open WebUI 提供聊天介面，獨立的 Tool Server 讀取本機資料；需要計算或繪圖時，模型會撰寫 Python 程式，交由短生命週期 Docker 沙箱執行。

## 功能

- 依資料集摘要與欄位說明理解可用資料，再依問題分析完整快照。
- 對必要的名詞或計算口徑先查核；資訊不足且會改變結果時，向使用者澄清。
- 支援自訂 Python 分析與互動式 Plotly 圖表，圖表可直接顯示在聊天室。
- 管理員可從專案附帶的 100 題題庫選題執行評測、檢視原始對話及匯出報告。
- 可將已匯出的 Open WebUI 對話轉成離線 HTML；另有靜態 PDF 匯出工具。

## 開始使用

### 需求

- Docker Desktop（含 Docker Compose）
- 專案附一份羽球逐拍 CSV 與欄位 metadata；也可改用自備 CSV 或 SQLite。
- 本機備妥 `src/badminton_ai/sandbox/service.py` 指定的分析沙箱映像

### 首次設定

1. 將 `.env.example` 複製為 `.env`。附帶資料的路徑已預先設定，一般情況不需要修改：

   | 設定 | 預設值 | 何時需要修改 |
   | --- | --- | --- |
   | `BADMINTON_AI_SOURCE_DATA_HOST_DIR` | `./data` | 改用其他資料目錄時 |
   | `BADMINTON_AI_DATA_FILE` | `processed_new_3.csv` | 改用其他 CSV／SQLite 檔案時；填相對於資料目錄的檔名 |
   | `BADMINTON_AI_SQLITE_TABLE` | `match_data` | SQLite 檔案使用不同資料表名稱時 |
   | `BADMINTON_AI_METADATA_DIR` | `metadata` | 欄位 metadata 不在資料目錄下的 `metadata` 資料夾時 |
   | `BADMINTON_AI_EVALUATION_QUESTIONS_HOST_FILE` | `./evaluation/questions/評估問題_v2.txt` | 改用其他評測題目檔時 |

   其他埠號與 runtime 路徑保留範例值即可，一般不需要修改。

2. `WEBUI_SECRET_KEY_FILE` 預設為 `.secrets/webui_secret_key`。請在該路徑建立秘密檔，放入密碼管理器產生的隨機字串，並在每次啟動時沿用同一檔案。這是秘密檔的路徑，不是要填入 `.env` 的秘密內容。
3. 若要使用評測工作台或讓分析圖表以附件形式保存到聊天室，請在本機 `.env` 加入 `BADMINTON_AI_OPEN_WEBUI_API_KEY=`。請使用實際操作工作台及發問的同一 Open WebUI 帳號所建立的 key。未設定時，基本文字聊天仍可使用，但評測工作台與聊天室圖表附件不可用。
4. 模型供應商的 API key（例如 OpenAI 相容服務的 key）是在 Open WebUI 管理介面設定，不是填入這個專案的 `.env`。
5. 在專案根目錄建置並啟動服務：

```powershell
docker compose config --quiet
docker compose up -d --build
docker compose ps
Invoke-RestMethod http://localhost:8000/health
```

Compose 會建置 Tool Server 與修補版 Open WebUI，不會建置分析沙箱映像。沙箱映像必須先存在於同一個 Docker 環境，且須符合程式指定的固定版本；若本機尚未備妥，請由維護者依 `sandbox/Dockerfile` 與固定版本設定建置。否則聊天介面可能開啟，但 Python 分析無法執行。

首次開啟 [http://localhost:3000/](http://localhost:3000/) 後，建立管理員帳號並在 Open WebUI 設定模型與 Tool Server。容器內 Tool Server 的 OpenAPI 位址為 `http://tool-server:8000/openapi.json`。請在聊天模型選單選擇 BadmintonAI。

### 日常啟動

先開啟 Docker Desktop，等候引擎啟動；在專案根目錄執行：

```powershell
docker compose up -d
docker compose ps
```

接著開啟 [http://localhost:3000/](http://localhost:3000/)。需要停止服務時執行 `docker compose stop`。

## 提問範例

- 周天成最常使用的三種球種是什麼？
- 比較周天成在各場比賽的得分球種分布。
- 繪製周天成殺球的落點熱區圖。

分析範圍、球種定義或資料限制若會影響結果，系統可能先查詢或向你確認。單一數值通常以文字回答；多類別比較、趨勢或空間分布會優先使用互動圖表。

## 文件

- [架構與問答流程](docs/architecture.md)：說明資料如何進入分析、圖表如何呈現，以及系統限制。
- [與舊版比較](docs/legacy-comparison.md)：整理介面、資料處理及分析方式的差異。

## 開發與匯出

Python 需求為 3.10 以上。安裝開發依賴並執行測試：

```powershell
python -m pip install -e ".[dev]"
python -m pytest -q
```

已匯出的聊天 JSON 可轉成單一離線 HTML；預設淺色，也支援深色或跟隨系統主題：

```powershell
python scripts/export_chat_html.py conversation.json conversation.html --theme light
```

HTML 匯出不會重新執行模型或分析。PDF 使用獨立的靜態匯出工具。

## 安全與限制

此專案以可信任的單機開發為目標。Tool Server 可存取 Docker daemon，因此不應直接提供給不受信任的使用者或當作多租戶代碼執行服務。分析結果仍需依原始資料與問題口徑核對；流程完成不代表答案必然正確。

## 授權

程式碼依 MIT 授權；授權全文位於儲存庫根目錄的 `LICENSE`。
