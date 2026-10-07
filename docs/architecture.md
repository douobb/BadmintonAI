# 架構與問答流程

本文說明 BadmintonAI 的使用者提問如何經過 Open WebUI、資料工具與 Python 分析，再回到聊天室。適合一般使用者理解服務流程，也提供維護者查閱主要元件與限制。這是目前實作說明，不代表每個答案都已通過人工驗證。

## 一次提問的流程

1. **提出問題**：在 Open WebUI 選擇 BadmintonAI，輸入羽球資料問題。同一對話中的前文可作為後續問題的脈絡。
2. **確認問題與資料口徑**：模型可查看欄位說明、球員或比賽摘要，以及已配置的 Skill（操作規則）和 Knowledge（參考資料）。若缺少會實質改變結果的定義，且沒有可採用的核准定義或合理預設，模型會先向使用者確認。
3. **執行分析**：需要計算、篩選或比較時，模型呼叫 Tool Server。分析沙箱使用完整資料快照，將需要重用的 JSON、CSV 或 JSONL 保存為短期結果，並只回傳 `result_id`、檔名與有限摘要；無保存檔案的 print-only 探查僅回傳受限 stdout 預覽且 `result_id=null`，不可供繪圖重用。
4. **呈現圖表**：圖表有助理解時，模型另呼叫 `renderAnalysisChart`。繪圖沙箱只讀取該 `result_id` 已保存的資料，不會重新查詢原始 snapshot；Tool Server 驗證 Plotly JSON 後，透過既有 Open WebUI Rich UI 發布圖表。

這不是每題都固定呼叫多個工具的流程。問題與口徑已清楚時，模型可以直接分析；不需要計算的問題也不必執行 Python。

## 元件分工

| 元件 | 用途 | 使用者可見內容 |
| --- | --- | --- |
| Open WebUI | 對話、模型選擇、Skills、Knowledge 與可選的網頁搜尋 | 聊天訊息、澄清、圖表與匯出入口 |
| Tool Server | 載入資料快照，提供摘要工具，驗證分析結果與圖表 | 分析結果、文字摘要與圖表產物 |
| Python 沙箱 | 按需啟動的短生命週期容器，預先提供完整 `df` 與常用 Python 套件 | 不直接提供獨立介面 |

Compose 常駐 `open-webui` 與 `tool-server` 兩個服務。Python 沙箱在分析或繪圖時才啟動，不是第三個常駐服務。Open WebUI 帳號與對話資料、評測工作台資料、短期分析結果分別保存在 Docker volumes；結果快取保存 24 小時、最多 512 MiB／256 筆，已發布圖表及匯出不依賴該暫存。

## 資料從哪裡來

專案附有目前使用的逐拍 CSV `data/processed_new_3.csv` 與對應 metadata `data/metadata/`；`.env` 預設指向這些檔案。管理者也可改設其他 CSV 或 SQLite 資料。Tool Server 以唯讀方式載入資料快照；資料不會透過聊天上傳，專案也沒有資料集管理介面。

摘要與唯讀查詢工具可協助模型了解資料集欄位、球員與比賽範圍。分析時，Python 沙箱會把完整資料快照載入 DataFrame；繪圖時，隔離的 render 模式只提供先前保存的檔案，不提供原始 DataFrame、events 或 metadata。若繪圖所需欄位／逐點值未保存，須先以分析工具明確保存，不能由 renderer 暗中重建。

模型設定、Tool Server 連線、Skill 與 Knowledge 需在 Open WebUI 配置。`skills/` 與 `knowledge/` 目錄是來源文件，啟動 Compose 不會自動同步其內容。

## 圖表如何顯示

`runPythonAnalysis` 只保存可重用分析資料，不嵌入圖表。適合圖表呈現時，`renderAnalysisChart` 從已保存檔案自由產生 1–4 張 Plotly 圖；Tool Server 驗證後，透過 Open WebUI Rich UI 呈現在原對話中。多類別比較、趨勢與空間分布通常適合圖表；單一數值通常以文字回答。繪圖失敗可只修 render 程式，不重算統計；已發布圖表與歷史對話匯出不依賴結果快取。

聊天室圖表不是 PNG。PNG 可以作為一般分析檔案，但圖表格式錯誤或未附加時不會自動改成圖片。聊天列表原生「⋯ → 下載」會依 chat ID 從 Open WebUI 讀取已儲存完整對話分支，使用共用 HTML builder 產生互動 HTML 或 PDF；伺服器依 Open WebUI 的 `chat.export` 群組／預設權限與對話擁有權授權，不以管理員 API key 授權任意 chat ID。PDF 以 Open WebUI 映像內的 Chromium 將相同可信任 HTML 轉成固定淺色頁面，等候字型及 Plotly ready 標記；圖表失敗或逾時會回傳錯誤，不產生缺圖 PDF。瀏覽器 renderer 不允許外部網路、下載或未驗證聊天 HTML 執行。

## 評測工作台

管理員可在 Open WebUI 的 `/badmintonai/evaluation` 工作台，從專案附帶的 `evaluation/questions/評估問題_v2.txt`（100 題）選擇題目、開始或續跑評測，並在需要澄清時補答。每題會建立對應的 Open WebUI 對話，工作台另保存執行進度與人工註記。詳情可檢視完整原對話；評測可匯出 JSON、HTML 報告與直接 PDF，原有瀏覽器手動列印頁亦保留。工作台 API 維持管理員限制。

「流程完成」只表示評測步驟結束，不代表模型回答、統計結果或圖表都正確。工作台供管理員驗證模型流程，不是一般使用者的資料管理功能。

## 安全邊界與限制

本專案以可信任的單機開發為目標。Python 程式在有資源限制、唯讀根檔案系統及無網路的短生命週期容器中執行；Tool Server 仍可存取 Docker daemon，因此這些措施不等於可安全執行不受信任使用者程式碼，也不構成多租戶安全保證。

需要即時賽事、排名或賽程等公開資訊時，可在 Open WebUI 啟用網頁搜尋。搜尋只用於公開時效資訊，不能替代本機資料的統計計算或專有名詞定義。
