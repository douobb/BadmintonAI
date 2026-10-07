# BadmintonAI

BadmintonAI 讓使用者用自然語言分析羽球逐拍資料。Open WebUI 提供聊天介面，獨立的 Tool Server 讀取本機資料；需要計算或繪圖時，模型會撰寫 Python 程式，交由短生命週期 Docker 沙箱執行。

## 功能

- 依資料集摘要與欄位說明理解可用資料，再依問題分析完整快照。
- 對必要的名詞或計算口徑先查核；資訊不足且會改變結果時，向使用者澄清。
- 支援自訂 Python 分析與互動式 Plotly 圖表，圖表可直接顯示在聊天室。
- 管理員可從專案附帶的 100 題題庫選題執行評測、檢視原始對話及匯出報告。
- 可從聊天列表的「⋯ → 下載」下載完整對話的互動 HTML 或 PDF；評測工作台也可直接下載 PDF。

## 開始使用

需要 Git 與 Docker Desktop（含 Docker Compose）。專案已附羽球逐拍 CSV、欄位 metadata、100 題題庫及 Skill／Knowledge 原始文件；Compose 會建置 Tool Server 與修補版 Open WebUI，分析沙箱映像則從 GHCR 公開下載。

第一次安裝還需建立本機秘密檔，並在 Open WebUI 手動設定模型供應商、Tool Server、system prompt、Skill 與 Knowledge。請依照[首次設定指南](docs/first-setup.md)逐步完成。

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

- [首次設定指南](docs/first-setup.md)：說明 `.env`、沙箱映像、服務啟動，以及 Open WebUI 的個別設定。
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

HTML 匯出不會重新執行模型或分析。Open WebUI 原生聊天列表選單提供 PDF 與互動 HTML 下載；伺服器依登入者的匯出權限及對話擁有權讀取已儲存的完整目前分支。兩種輸出共用安全 HTML builder 與 Open WebUI 映像內的 Chromium；PDF 會等候字型和 Plotly 圖表完成。舊 JSON→手繪 PDF 腳本已移除，JSON 匯出與 JSON→HTML 工具仍保留。

## 安全與限制

此專案以可信任的單機開發為目標。Tool Server 可存取 Docker daemon，因此不應直接提供給不受信任的使用者或當作多租戶代碼執行服務。分析結果仍需依原始資料與問題口徑核對；流程完成不代表答案必然正確。

## 授權

本專案自行撰寫程式依 MIT 授權；授權全文位於儲存庫根目錄的 `LICENSE`。自訂 Open WebUI 映像以 v0.11.3 上游來源建置，並保留該版本隨附的授權與聲明檔。
