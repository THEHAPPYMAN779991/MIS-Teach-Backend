from flask import Blueprint, request, jsonify, session
from flask_cors import cross_origin
from datetime import datetime
import logging
import jwt
from flask import current_app
from accessories import mongo, refresh_token

# 創建藍圖
user_guide_bp = Blueprint('user_guide', __name__)

# 設定日誌
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def get_user_email_from_token():
    """從 JWT token 獲取用戶 email"""
    try:
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token:
            return None

        decoded = jwt.decode(token, current_app.config['SECRET_KEY'], algorithms=['HS256'])
        return decoded.get('user')
    except:
        return None

@user_guide_bp.route('/api/user-guide/status', methods=['GET', 'OPTIONS'])
@cross_origin()
def get_user_guide_status():
    """
    獲取用戶導覽狀態
    """
    if request.method == 'OPTIONS':
        return '', 204

    try:
        # 優先從 JWT token 獲取用戶 email
        user_email = get_user_email_from_token()

        if not user_email:
            # 如果沒有 token，從 session 獲取用戶 ID
            user_id = session.get('user_id', 'anonymous_user')
            user_record = mongo.db.user.find_one({'user_id': user_id})
        else:
            # 使用 email 查詢用戶
            user_record = mongo.db.user.find_one({'email': user_email})

        if user_record:
            # 用戶存在，返回實際狀態
            status = {
                'user_id': str(user_record.get('_id', 'unknown')),
                'email': user_record.get('email', ''),
                'new_user': user_record.get('new_user', True),
                'guide_completed': user_record.get('guide_completed', False),
                'last_login': user_record.get('last_login', datetime.now().isoformat()),
                'guide_completion_date': user_record.get('guide_completion_date')
            }

            logger.info(f"用戶 {user_email or user_id} 導覽狀態: new_user={status['new_user']}, guide_completed={status['guide_completed']}")
        else:
            # 用戶不存在，返回預設狀態
            status = {
                'user_id': 'anonymous',
                'email': user_email or '',
                'new_user': True,
                'guide_completed': False,
                'last_login': datetime.now().isoformat()
            }

            logger.info(f"用戶不存在，返回預設狀態: {status}")

        return jsonify(status), 200

    except Exception as e:
        logger.error(f"獲取用戶導覽狀態失敗: {str(e)}")
        return jsonify({
            'token': None,
            'error': '獲取用戶狀態失敗',
            'message': str(e)
        }), 500

@user_guide_bp.route('/api/user-guide/mark-guided', methods=['POST', 'OPTIONS'])
@cross_origin()
def mark_user_as_guided():
    """
    標記用戶已完成導覽
    """
    if request.method == 'OPTIONS':
        return '', 204

    try:
        # 優先從 JWT token 獲取用戶 email
        user_email = get_user_email_from_token()

        if not user_email:
            # 如果沒有 token，從 session 獲取用戶 ID
            user_id = session.get('user_id', 'anonymous_user')
            query = {'user_id': user_id}
            identifier = user_id
        else:
            # 使用 email 查詢用戶
            query = {'email': user_email}
            identifier = user_email

        # 更新用戶記錄
        update_result = mongo.db.user.update_one(
            query,
            {
                '$set': {
                    'new_user': False,
                    'guide_completed': True,
                    'guide_completion_date': datetime.now().isoformat(),
                    'last_updated': datetime.now().isoformat()
                }
            }
        )

        if update_result.modified_count > 0:
            logger.info(f"用戶 {identifier} 已完成導覽")
            return jsonify({
                'token': refresh_token(token),
                'success': True,
                'message': '導覽狀態已更新',
                'user_identifier': identifier,
                'completion_date': datetime.now().isoformat()
            }), 200
        else:
            logger.warning(f"用戶 {identifier} 導覽狀態更新失敗 - 可能用戶不存在或狀態未變更")
            return jsonify({
                'success': False,
                'message': '導覽狀態更新失敗 - 用戶不存在或狀態未變更'
            }), 400

    except Exception as e:
        logger.error(f"標記用戶導覽完成失敗: {str(e)}")
        return jsonify({
            'token': None,
            'error': '標記導覽完成失敗',
            'message': str(e)
        }), 500

@user_guide_bp.route('/api/user-guide/reset', methods=['POST'])
@cross_origin()
def reset_user_guide_status():
    """
    重置用戶導覽狀態（用於測試）
    """
    try:
        # 從 session 獲取用戶 ID
        user_id = session.get('user_id', 'anonymous_user')

        # 連接 MongoDB
        users_collection = mongo.db.user

        # 重置用戶記錄
        update_result = users_collection.update_one(
            {'user_id': user_id},
            {
                '$set': {
                    'new_user': True,
                    'guide_completed': False,
                    'guide_completion_date': None,
                    'last_updated': datetime.now().isoformat(),
                    'reset_at': datetime.now().isoformat()
                }
            },
            upsert=True
        )

        if update_result.modified_count > 0 or update_result.upserted_id:
            logger.info(f"用戶 {user_id} 導覽狀態已重置")
            return jsonify({
                'token': refresh_token(token),
                'success': True,
                'message': '導覽狀態已重置為新用戶',
                'user_id': user_id,
                'reset_date': datetime.now().isoformat()
            }), 200
        else:
            logger.warning(f"用戶 {user_id} 導覽狀態重置失敗")
            return jsonify({
                'success': False,
                'message': '導覽狀態重置失敗'
            }), 400

    except Exception as e:
        logger.error(f"重置用戶導覽狀態失敗: {str(e)}")
        return jsonify({
            'token': None,
            'error': '重置導覽狀態失敗',
            'message': str(e)
        }), 500

@user_guide_bp.route('/api/user-guide/stats', methods=['GET'])
@cross_origin()
def get_guide_statistics():
    """
    獲取導覽統計數據
    """
    try:
        # 連接 MongoDB
        users_collection = mongo.db.user

        # 統計數據
        total_users = users_collection.count_documents({})
        new_users = users_collection.count_documents({'new_user': True})
        guided_users = users_collection.count_documents({'guide_completed': True})

        stats = {
            'total_users': total_users,
            'new_users': new_users,
            'guided_users': guided_users,
            'completion_rate': round((guided_users / total_users * 100), 2) if total_users > 0 else 0,
            'generated_at': datetime.now().isoformat()
        }

        logger.info(f"導覽統計數據: {stats}")
        return jsonify({'token': refresh_token(token), 'data': stats}), 200

    except Exception as e:
        logger.error(f"獲取導覽統計失敗: {str(e)}")
        return jsonify({
            'token': None,
            'error': '獲取統計數據失敗',
            'message': str(e)
        }), 500

# 測試端點
@user_guide_bp.route('/api/user-guide/test', methods=['GET'])
@cross_origin()
def test_user_guide_api():
    """
    測試 API 連接
    """
    try:
        user_id = session.get('user_id', 'test_user')
        return jsonify({
            'token': refresh_token(token),
            'success': True,
            'message': '用戶導覽 API 正常運作',
            'user_id': user_id,
            'timestamp': datetime.now().isoformat()
        }), 200
    except Exception as e:
        return jsonify({
            'token': None,
            'error': 'API 測試失敗',
            'message': str(e)
        }), 500
