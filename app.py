from flask import Flask, jsonify, request, Blueprint, send_from_directory
from flask_cors import CORS
import sys
import os as _os_for_env

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ============================================================
# 【必須在 import accessories 之前】載入 api.env 到環境變數
# 這樣 AI_PROVIDER / VERTEX_PROJECT / GEMINI_BACKEND 才能被讀到，
# 否則 init_ai / init_llm 的環境變數覆寫邏輯會失效。
# ============================================================
try:
    from dotenv import load_dotenv as _load_dotenv
    _env_path = _os_for_env.path.join(
        _os_for_env.path.dirname(_os_for_env.path.abspath(__file__)),
        "api.env",
    )
    if _os_for_env.path.exists(_env_path):
        _load_dotenv(_env_path, override=False)
        _provider = _os_for_env.getenv("AI_PROVIDER", "(未設定)")
        _vp = _os_for_env.getenv("VERTEX_PROJECT", "(未設定)")
        print(f"✅ api.env 已載入：AI_PROVIDER={_provider}, VERTEX_PROJECT={_vp}")
    else:
        print(f"⚠️ 找不到 api.env: {_env_path}")
except ImportError:
    print("⚠️ python-dotenv 未安裝，AI_PROVIDER 覆寫不會生效。請: pip install python-dotenv")

from accessories import sqldb, mail, redis_client, token_store, mongo, login_manager, init_mongo_data
from sqlalchemy import text
from config import Config, ProductionConfig, DevelopmentConfig
from src.login import login_bp
from src.register import register_bp
from src.dashboard import dashboard_bp
from src.quiz import quiz_bp, init_quiz_tables
from src.ai_quiz import ai_quiz_bp
from src.materials_api import materials_bp
from src.note import note_bp
import os
import redis, json ,time
from datetime import datetime
from flask_mail import Mail, Message
from accessories import mail, redis_client, send_calendar_notification
import threading
import schedule
from src.dashboard import init_calendar_tables
from neo4j.exceptions import ServiceUnavailable


from src.ai_teacher import ai_teacher_bp
# user_guide_api 已整合到 website_guide
from src.web_ai_assistant import web_ai_bp
from src.website_guide import guide_bp
from src.linebot import linebot_bp  # 新增 LINE Bot Blueprint
from src.learning_analytics import analytics_bp
from tool.insert_mongodb import initialize_mis_teach_db # 引入教材資料庫
from tool.init_neo4j_knowledge_graph import init_neo4j_knowledge_graph  # 引入Neo4j知識圖譜初始化
from accessories import init_neo4j  # 引入Neo4j驅動初始化
from tool.insert_test_school import check_and_insert_test_school  # 引入測試學校自動檢查
from src.news_api import news_api_bp  # 引入新聞 API Blueprint
from src.graphrag_proxy import graphrag_bp  # 引入 GraphRAG API Blueprint
from src.rag_backend_api import rag_backend_bp  # RAG backend 切換 API
from tool.init_news_table import init_news_table, migrate_news_data  # 引入新聞表初始化與資料遷移
from tool.rename_materials import rename_materials

# 定義 BASE_DIR 為 backend 資料夾的絕對路徑
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Initialize Flask app
# 明確禁用 Flask 的默認 static 文件處理，使用自定義路由
app = Flask(__name__, static_folder=None, static_url_path=None)

# Load configuration based on environment
cfg = Config()
productionCfg = ProductionConfig()
developmentCfg = DevelopmentConfig()
app.config.from_object(cfg)
if len(sys.argv) > 1:
    if sys.argv[-1] == 'production':
        app.config.from_object(productionCfg)
    else:
        app.config.from_object(developmentCfg)
else:
    app.config.from_object(developmentCfg)


domain_name_config = app.config.get('DOMAIN_NAME')

# 定義允許的來源（包含所有 ngrok instant endpoints 和 localhost）
def is_allowed_origin(origin):
    """檢查來源是否為允許的域名"""
    if not origin:
        return False
    # 允許 localhost（開發環境）
    if origin.startswith('http://localhost:') or origin.startswith('https://localhost:'):
        return True
    # 允許所有 .ngrok-free.app 和 .ngrok.io 域名（Docker Desktop instant endpoints）
    if origin.endswith('.ngrok-free.app') or origin.endswith('.ngrok.io'):
        return True
    # 也允許配置的特定域名
    if origin == app.config.get('DOMAIN_NAME'):
        return True
    return False

# Enable CORS - 手動處理，避免 Flask-CORS 函數參數的兼容性問題
# 使用 after_request 鉤子完全控制 CORS 頭的設置
@app.after_request
def handle_cors(response):
    """手動處理 CORS 頭，只允許通過檢查的來源"""
    origin = request.headers.get('Origin', '')

    # 只對通過檢查的來源設置 CORS 頭
    if is_allowed_origin(origin):
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Credentials'] = 'true'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS, PUT, DELETE, PATCH'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, ngrok-skip-browser-warning'

    # 處理 OPTIONS 預檢請求
    if request.method == 'OPTIONS':
        response.status_code = 200

    return response

# 初始化數據庫
sqldb.init_app(app)  # 啟用SQL數據庫
mail.init_app(app)
redis_client.init_app(app)
token_store.init_app(app)
mongo.init_app(app)
login_manager.init_app(app)
login_manager.login_view = '/login'


# Register blueprints
app.register_blueprint(login_bp, url_prefix='/login')
app.register_blueprint(register_bp, url_prefix='/register')
app.register_blueprint(dashboard_bp, url_prefix='/dashboard')
app.register_blueprint(quiz_bp, url_prefix='/quiz')
app.register_blueprint(ai_quiz_bp, url_prefix='/ai_quiz')
app.register_blueprint(ai_teacher_bp, url_prefix='/ai_teacher')
app.register_blueprint(web_ai_bp, url_prefix='/web-ai')
app.register_blueprint(guide_bp, url_prefix='/guide')  # 註冊導覽 Blueprint
app.register_blueprint(linebot_bp, url_prefix='/linebot') # 註冊 LINE Bot Blueprint
app.register_blueprint(materials_bp, url_prefix="/materials")
app.register_blueprint(note_bp, url_prefix="/note")  # 註冊筆記 API Blueprint
app.register_blueprint(analytics_bp, url_prefix='/api/learning-analytics')  # 註冊學習分析 API Blueprint
app.register_blueprint(news_api_bp) # 註冊新聞 API Blueprint
app.register_blueprint(graphrag_bp) # 註冊 GraphRAG API Blueprint
app.register_blueprint(rag_backend_bp) # RAG backend 切換 API

# 創建 PDF 裁切 asset 服務路由 (給前端 P2-PLOT1 之類的 image_url 用)
@app.route('/api/assets/<path:filename>')
def serve_pdf_asset(filename):
    """提供 PDF 裁切的 asset PNG 圖檔（vision propose 階段裁出來的）。

    來源：由本機環境變數指定的 PDF 轉檔輸出目錄。
    URL 範例：/api/assets/114台大_資訊管理導論_p2_vision_plot2.png
    """
    try:
        import os
        import mimetypes
        from flask import send_from_directory
        # quiz.py 的舊資料相容路徑只會帶檔名；不接受路徑片段，避免以
        # /api/assets 路由存取資產根目錄外的檔案。
        safe_filename = os.path.basename(str(filename or '').replace('\\', '/'))
        if not safe_filename:
            return jsonify({'error': 'invalid PDF asset filename'}), 400
        # PDF 裁切資產存放位置 — 跟轉檔流程的 temp_dir/assets 對應。
        # 路徑一律從本機環境變數讀取；公開程式碼不綁定任何個人目錄。
        # ★ testIMG/ 優先 — 你手動裁好的圖放這裡 ★
        # 找到就用 testIMG，找不到才退回自動裁的 temp_pdf_pages/assets
        output_root = _os_for_env.environ.get('PDF_OUTPUT_JSON_DIR', '')
        run_asset_dirs = []
        if os.path.isdir(output_root):
            try:
                # 相容兩種轉檔輸出結構：
                # - 舊版：run_xxx/temp_pdf_pages/assets/
                # - 新版：run_xxx/assets/
                # 新版 new_exam_output.json 的 crop_path 會指向後者；若只搜尋
                # 舊版路徑，前端雖取得 /api/assets/... URL，但最後必定回傳 404。
                run_dirs = [
                    asset_dir
                    for name in os.listdir(output_root)
                    if name.startswith('run_')
                    for asset_dir in (
                        os.path.join(output_root, name, 'assets'),
                        os.path.join(output_root, name, 'temp_pdf_pages', 'assets'),
                    )
                ]
                run_asset_dirs = sorted(
                    [d for d in run_dirs if os.path.isdir(d)],
                    key=lambda d: os.path.getmtime(d),
                    reverse=True,
                )
            except Exception:
                run_asset_dirs = []
        asset_dirs = [
            _os_for_env.environ.get('PDF_TEST_IMAGE_DIR', ''),  # ★ 手動裁切圖優先
            os.environ.get('PDF_ASSETS_DIR', ''),
            *run_asset_dirs,
            _os_for_env.environ.get('PDF_TEMP_ASSETS_DIR', ''),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'temp_pdf_pages', 'assets'),
        ]
        for d in asset_dirs:
            if not d:
                continue
            d = os.path.normpath(d)
            candidate = os.path.join(d, safe_filename)
            if os.path.exists(candidate):
                mime_type, _ = mimetypes.guess_type(safe_filename)
                if not mime_type:
                    mime_type = 'image/png'
                response = send_from_directory(d, safe_filename, mimetype=mime_type)
                origin = request.headers.get('Origin', '')
                if is_allowed_origin(origin):
                    response.headers['Access-Control-Allow-Origin'] = origin
                else:
                    response.headers['Access-Control-Allow-Origin'] = app.config.get('DOMAIN_NAME', '*')
                response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
                response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, ngrok-skip-browser-warning'
                response.headers['Content-Type'] = mime_type
                return response
            # 新版輸出會以 assets/<document_id>/<asset_file> 儲存，因此
            # crop_path 的 basename 不能只在 assets 根目錄查找。
            for current_dir, _, files in os.walk(d):
                if safe_filename not in files:
                    continue
                mime_type, _ = mimetypes.guess_type(safe_filename)
                if not mime_type:
                    mime_type = 'image/png'
                response = send_from_directory(current_dir, safe_filename, mimetype=mime_type)
                origin = request.headers.get('Origin', '')
                if is_allowed_origin(origin):
                    response.headers['Access-Control-Allow-Origin'] = origin
                else:
                    response.headers['Access-Control-Allow-Origin'] = app.config.get('DOMAIN_NAME', '*')
                response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
                response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, ngrok-skip-browser-warning'
                response.headers['Content-Type'] = mime_type
                return response
        print(f"PDF asset 找不到：{safe_filename}（試過：{asset_dirs}）")
        return jsonify({'error': 'PDF asset not found', 'filename': safe_filename}), 404
    except Exception as e:
        print(f"PDF asset 服務錯誤：{e}")
        return jsonify({'error': 'PDF asset service error'}), 500


@app.route('/output_json/<path:filename>')
def serve_output_json_asset(filename):
    """提供每次轉檔 run_* 資料夾內的 PDF 裁切圖片。"""
    try:
        import mimetypes

        output_roots = [
            _os_for_env.environ.get('PDF_OUTPUT_JSON_DIR', ''),
            _os_for_env.path.abspath(_os_for_env.path.join(BASE_DIR, '..', 'output_json')),
            _os_for_env.path.abspath(_os_for_env.path.join(_os_for_env.getcwd(), 'output_json')),
        ]

        normalized_filename = filename.replace('\\', '/').lstrip('/')
        for root in output_roots:
            if not root:
                continue
            root_abs = _os_for_env.path.abspath(_os_for_env.path.normpath(root))
            candidate = _os_for_env.path.abspath(
                _os_for_env.path.normpath(_os_for_env.path.join(root_abs, normalized_filename))
            )
            try:
                if _os_for_env.path.commonpath([root_abs, candidate]) != root_abs:
                    continue
            except ValueError:
                continue
            if _os_for_env.path.exists(candidate):
                mime_type, _ = mimetypes.guess_type(candidate)
                if not mime_type:
                    mime_type = 'image/png'
                response = send_from_directory(root_abs, normalized_filename, mimetype=mime_type)
                origin = request.headers.get('Origin', '')
                if is_allowed_origin(origin):
                    response.headers['Access-Control-Allow-Origin'] = origin
                else:
                    response.headers['Access-Control-Allow-Origin'] = app.config.get('DOMAIN_NAME', '*')
                response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
                response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, ngrok-skip-browser-warning'
                response.headers['Content-Type'] = mime_type
                return response

        print(f"output_json asset 找不到：{filename}（試過：{output_roots}）")
        return jsonify({'error': 'output_json asset not found', 'filename': filename}), 404
    except Exception as e:
        print(f"output_json asset 服務錯誤：{e}")
        return jsonify({'error': 'output_json asset service error'}), 500


# 創建靜態文件服務路由 (用於題目圖片)
@app.route('/static/images/<path:filename>')
def serve_static_image(filename):
    """提供靜態圖片文件服務（題目圖片）"""
    try:
        import os
        import mimetypes
        from flask import send_from_directory

        # 圖片文件位於 backend/src/picture 目錄
        # 使用絕對路徑，確保路徑正確
        base_dir = os.path.dirname(os.path.abspath(__file__))
        image_dir = os.path.join(base_dir, 'src', 'picture')
        image_path = os.path.join(image_dir, filename)

        # 確定 MIME 類型
        mime_type, _ = mimetypes.guess_type(filename)
        if not mime_type:
            # 根據副檔名設定預設 MIME 類型
            ext = os.path.splitext(filename)[1].lower()
            mime_map = {
                '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg',
                '.png': 'image/png',
                '.gif': 'image/gif',
                '.webp': 'image/webp',
                '.svg': 'image/svg+xml'
            }
            mime_type = mime_map.get(ext, 'image/jpeg')

        if os.path.exists(image_path):
            response = send_from_directory(image_dir, filename, mimetype=mime_type)
            # 設置 CORS 頭 - 動態允許 ngrok 域名
            origin = request.headers.get('Origin', '')
            if is_allowed_origin(origin):
                response.headers['Access-Control-Allow-Origin'] = origin
            else:
                response.headers['Access-Control-Allow-Origin'] = app.config.get('DOMAIN_NAME', '*')
            response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, ngrok-skip-browser-warning'
            response.headers['Content-Type'] = mime_type
            return response
        else:
            return jsonify({'error': 'Image not found'}), 404

    except Exception as e:
        print(f"靜態圖片服務錯誤: {e}")
        return jsonify({'error': 'Image service error'}), 500

# 創建課程圖片服務路由
@app.route('/static/<path:filename>')
def serve_course_image(filename):
    """提供課程圖片文件服務"""
    try:
        import os
        import mimetypes
        from flask import send_from_directory, Response

        # 課程圖片文件位於 backend/data/courses_picture 目錄
        # 使用絕對路徑，確保路徑正確
        base_dir = os.path.dirname(os.path.abspath(__file__))
        course_image_dir = os.path.join(base_dir, 'data', 'courses_picture')
        image_path = os.path.join(course_image_dir, filename)

        # 確定 MIME 類型
        mime_type, _ = mimetypes.guess_type(filename)
        if not mime_type:
            # 根據副檔名設定預設 MIME 類型
            ext = os.path.splitext(filename)[1].lower()
            mime_map = {
                '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg',
                '.png': 'image/png',
                '.gif': 'image/gif',
                '.webp': 'image/webp',
                '.svg': 'image/svg+xml'
            }
            mime_type = mime_map.get(ext, 'image/jpeg')

        if os.path.exists(image_path):
            response = send_from_directory(course_image_dir, filename, mimetype=mime_type)
            # 設置 CORS 頭 - 動態允許 ngrok 域名
            origin = request.headers.get('Origin', '')
            if is_allowed_origin(origin):
                response.headers['Access-Control-Allow-Origin'] = origin
            else:
                response.headers['Access-Control-Allow-Origin'] = app.config.get('DOMAIN_NAME', '*')
            response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, ngrok-skip-browser-warning'
            response.headers['Content-Type'] = mime_type
            return response
        else:
            # 如果課程圖片不存在，嘗試從題目圖片目錄查找
            base_dir = os.path.dirname(os.path.abspath(__file__))
            question_image_dir = os.path.join(base_dir, 'src', 'picture')
            question_image_path = os.path.join(question_image_dir, filename)
            if os.path.exists(question_image_path):
                response = send_from_directory(question_image_dir, filename, mimetype=mime_type)
                origin = request.headers.get('Origin', '')
                if is_allowed_origin(origin):
                    response.headers['Access-Control-Allow-Origin'] = origin
                else:
                    response.headers['Access-Control-Allow-Origin'] = app.config.get('DOMAIN_NAME', '*')
                response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
                response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, ngrok-skip-browser-warning'
                response.headers['Content-Type'] = mime_type
                return response
            return jsonify({'error': 'Image not found', 'filename': filename}), 404

    except Exception as e:
        print(f"❌ 課程圖片服務錯誤: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Image service error', 'message': str(e)}), 500

def check_calendar_notifications():
    """檢查 Redis 中的行事曆通知並發送郵件"""
    try:
        # 獲取當前時間
        current_time = datetime.now()
        current_time_str = current_time.strftime('%Y-%m-%d %H:%M')

        # 從 Redis List 獲取所有通知
        notifications = redis_client.lrange('event_notification', 0, -1)
        notifications_to_send = []

        for notification_data in notifications:
            try:
                notification = json.loads(notification_data)
                notify_time_str = notification.get('notify_time')

                if notify_time_str:
                    # 檢查是否到了通知時間（允許 5 分鐘誤差）
                    notify_time = datetime.strptime(notify_time_str, '%Y-%m-%d %H:%M')
                    time_diff = abs((notify_time - current_time).total_seconds())

                    if time_diff <= 300:  # 5 分鐘內
                        notifications_to_send.append({
                            'notification_data': notification_data,
                            'event_id': notification.get('event_id'),
                            'notification': notification
                        })
            except Exception as e:
                print(f"處理通知時發生錯誤: {e}")
                continue

        # 發送通知
        for item in notifications_to_send:
            try:
                notification = item['notification']
                student_email = notification.get('student_email')
                user_id = notification.get('user_id')
                event_title = notification.get('event_title') or notification.get('title')
                event_content = notification.get('event_content') or notification.get('content', '')
                event_date = notification.get('event_date', '')

                if student_email and event_title:
                    # 發送郵件通知
                    mail_success = False
                    with app.app_context():
                        mail_success = send_calendar_notification(
                            student_email=student_email,
                            event_title=event_title,
                            event_content=event_content,
                            event_date=event_date
                        )

                    # 發送 LINE Bot 通知
                    line_success = False
                    if user_id:
                        line_success = send_line_calendar_notification(
                            user_id=user_id,
                            event_title=event_title,
                            event_content=event_content,
                            event_date=event_date
                        )

                    if mail_success or line_success:
                        # 發送成功後從 Redis List 移除
                        redis_client.lrem('event_notification', 1, item['notification_data'])
                        print(f"✅ 通知已發送並從 Redis List 移除: event_id {item['event_id']}")
                    else:
                        print(f"❌ 通知發送失敗: event_id {item['event_id']}")

            except Exception as e:
                print(f"發送通知時發生錯誤: {e}")
                continue

    except Exception as e:
        print(f"檢查行事曆通知時發生錯誤: {e}")

def send_line_calendar_notification(user_id: str, event_title: str, event_content: str, event_date: str) -> bool:
    """發送 LINE Bot 行事曆通知"""
    try:
        from src.linebot import line_bot_api, PushMessageRequest, TextMessage

        # 格式化事件日期
        try:
            event_datetime = datetime.fromisoformat(event_date.replace('Z', '+00:00'))
            formatted_date = event_datetime.strftime('%Y年%m月%d日 %H:%M')
        except:
            formatted_date = event_date

        # 創建通知訊息
        notification_text = f"""🔔 行事曆提醒

📅 事件：{event_title}
⏰ 時間：{formatted_date}
{f'📝 內容：{event_content}' if event_content else ''}
"""

        # 發送 LINE 訊息
        line_bot_api.push_message(
            PushMessageRequest(
                to=user_id,
                messages=[TextMessage(text=notification_text)]
            )
        )

        print(f"✅ LINE 行事曆通知已發送給用戶 {user_id}")
        return True

    except Exception as e:
        print(f"❌ 發送 LINE 行事曆通知失敗: {e}")
        return False

def run_scheduler():
    """運行背景排程器"""
    schedule.every(1).minutes.do(check_calendar_notifications)
    while True:
        schedule.run_pending()
        time.sleep(60)

# 初始化數據庫表格
with app.app_context():
    sqldb.create_all()
    init_quiz_tables()
    init_calendar_tables()

    # ✨ 建 tutoring_progress 表（給 AI 教學對話進度持久化用）
    try:
        from src.tutoring_progress import init_tutoring_progress_table
        init_tutoring_progress_table()
    except Exception as e:
        print(f"⚠️ init_tutoring_progress_table 失敗: {e}")
    init_news_table()  # 初始化新聞表
    migrate_news_data()  # 自動遷移 ithome_news.json 到資料庫（若尚未導入）
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    # 初始化MongoDB數據
    init_mongo_data()
    initialize_mis_teach_db()
    rename_materials()
    # 自動檢查並插入測試學校資料
    check_and_insert_test_school()

    # 初始化Neo4j（如果服務未運行則跳過）
    try:
        init_neo4j()  # 初始化Neo4j驅動
        # 已停用：避免 app.py 啟動時重建 MIS 舊 Neo4j 圖譜，導致 GraphRAG Concept 資料消失
        # init_neo4j_knowledge_graph()
        print("✓ Neo4j 已連線；已略過 MIS 舊知識圖譜初始化")
    except ServiceUnavailable as e:
        print(f"⚠️ Neo4j 服務未運行，跳過 MIS 舊知識圖譜初始化：{e}")
    except Exception as e:
        print(f"⚠️ Neo4j 初始化時出現錯誤，跳過 MIS 舊知識圖譜初始化：{e}")

if __name__ == '__main__':
    app.run(debug=False, use_reloader=False)
