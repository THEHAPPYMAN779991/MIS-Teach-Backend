# MIS-Teach Backend

MIS-Teach 是一個研究型智慧學習平台。此後端提供帳號、教材與測驗管理、題型化批改、
AI 引導式補救教學、學習分析，以及 GraphRAG／ChromaDB／純 LLM 的比較介面。

目前工作區同時含研究資料與歷史版本。本目錄的公開準備範圍只包含可執行原始碼、設定
範例與文件；不包含教材、考題、學生資料、資料庫、向量索引或 API credential。

## 系統重點

- Flask REST API，搭配 Angular 前端。
- MongoDB 儲存結構化題目與提交紀錄；MySQL 儲存帳號、測驗歷程與教學進度。
- 支援單選、多選、文字、程式、繪圖與手寫公式等作答型態。
- Gemini / Vertex AI 進行解答生成、批改與引導式教學；LINE Bot 為選用整合。
- GraphRAG 從外部 ComputerScienceKG 服務取得 Neo4j Concept、Chunk 與直接先備證據。
- 保留 ChromaDB 及純 LLM 作為研究比較基線。

詳情請閱讀：

- [系統架構](../docs/backend/architecture.md)
- [資料來源與公開範圍](../docs/backend/data-sources.md)
- [公開展示版整理計畫](../docs/backend/public-release-plan.md)

## 前置需求

- Python 3.11 或相容版本
- Node.js 18.19+ 或 20.9+（前端）
- MySQL、MongoDB 與 Redis
- Neo4j（使用 GraphRAG 時需要）
- Google AI Studio API key 或已設定的 Vertex AI 環境
- 選用：外部 ComputerScienceKG GraphRAG API（預設 `http://localhost:8001`）

## 環境設定

所有本機秘密都放在 `api.env`，這個檔案已由 `.gitignore` 排除。先複製範例：

```powershell
Copy-Item .env.example api.env
```

```bash
cp .env.example api.env
```

再由你自己的服務帳號填入必要值，例如：

```ini
GEMINI_API_KEY=YOUR_OWN_API_KEY
MONGO_URI=YOUR_OWN_MONGODB_URI
NEO4J_URI=YOUR_OWN_NEO4J_URI
NEO4J_USERNAME=YOUR_OWN_NEO4J_USERNAME
NEO4J_PASSWORD=YOUR_OWN_NEO4J_PASSWORD
SQLALCHEMY_DATABASE_URI=YOUR_OWN_MYSQL_URI
REDIS_URL=YOUR_OWN_REDIS_URL
```

`YOUR_OWN_*` 僅是 README 範例，不能寫入 Python 程式邏輯。Gemini 的多 key 設定可
沿用 `GEMINI_API_KEYS`、`WU_API_KEYS`、`PAN_API_KEYS`；Vertex AI 則使用
`GEMINI_BACKEND=vertex`、`VERTEX_PROJECT` 與 `VERTEX_LOCATION`。

若要顯示外部 PDF 轉檔流程產生的題圖，還需在本機設定
`PDF_OUTPUT_JSON_DIR`、`PDF_TEST_IMAGE_DIR` 與 `PDF_TEMP_ASSETS_DIR`。公開展示時
請改為匿名、具授權的範例素材。

## 啟動後端

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

後端預設監聽 `http://127.0.0.1:5000`。啟動時會初始化既有資料表與部分本機資料庫
內容；請使用獨立的開發資料庫，不要連到正式研究資料庫。

## 啟動前端

前端位於相鄰的 `mis_teach_frontend-main` 資料夾：

```powershell
cd ..\mis_teach_frontend-main
npm ci
npm start
```

前端開發環境預設呼叫 `http://127.0.0.1:5000`。production build：

```powershell
npm run build
```

## GraphRAG 啟動順序

1. 準備 Neo4j 資料庫與具 embedding 的 `Concept`、`Chunk`、`PREREQUISITE_OF` 資料。
2. 在獨立的 ComputerScienceKG 專案啟動 GraphRAG API。
3. 於 `api.env` 設定 `GRAPHRAG_API_BASE`、Neo4j 連線與 Gemini/Vertex 設定。
4. 最後啟動本 Flask 後端與 Angular 前端。

本專案第一次補救教學會以題目與選項查找 5 個候選 Seed、每個 Seed 的前 3 個教材
Chunk，並選出最多 5 個距離 1 先備 Concept 與前 5 個先備 Chunk。後續對話重用首輪
快取證據，不會重新進行全域檢索。完整契約見[架構文件](../docs/backend/architecture.md)。

## 驗證

後端可先進行不連外的語法檢查：

```powershell
.\venv\Scripts\python.exe -m py_compile app.py config.py src\rag_sys\config.py
```

前端 production build：

```powershell
npm run build
```

## 安全與公開發布

- 不要 commit `api.env`、`.env*`、`security_key`、資料庫檔、日誌、教材、題庫或向量索引。
- `.env.example` 可 commit，但只能保留變數名稱與註解。
- 舊的 parent-repository 自動更新 workflow 已停用；本目錄不會因 push 而寫入其他 GitHub
  repository。
- 在建立公開 Git repository 前，請先依[公開展示版整理計畫](../docs/backend/public-release-plan.md)
  建立匿名範例資料與選定授權。

## 授權

尚未指定公開授權。請在發布前新增適合的 `LICENSE`，並確認所有範例題目、教材與圖片都
具有可公開使用的授權。
