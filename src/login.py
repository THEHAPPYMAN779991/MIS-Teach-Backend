from flask import request, jsonify, Blueprint, current_app
from werkzeug.security import check_password_hash
from datetime import datetime, timedelta
import jwt
from accessories import mongo
from flask_login import logout_user
import time

login_bp = Blueprint('login', __name__)

@login_bp.route('/login_user', methods=['POST', 'OPTIONS'])
def login():
    if request.method == 'OPTIONS':
        return '', 204
    data = request.json
    email = data.get('email')
    password = data.get('password')
    user_data = mongo.db.user.find_one({"email": email})
    if user_data and check_password_hash(user_data['password'], password):
        # 修復JWT token生成 - 使用標準Unix時間戳
        exp_time = datetime.now() + timedelta(hours=3)
        token = jwt.encode({
            'user': user_data['email'],
            'exp': int(exp_time.timestamp())  # 使用Unix時間戳而不是字符串
        }, current_app.config['SECRET_KEY'], algorithm='HS256')

        # 獲取用戶的 new_user 狀態
        new_user = user_data.get('new_user', True)
        guide_completed = user_data.get('guide_completed', False)

        # 返回 token 和導覽狀態
        return jsonify({
            'token': token,
            'new_user': new_user,
            'guide_completed': guide_completed,
            'guide_info': {
                'new_user': new_user,
                'guide_completed': guide_completed
            }
        }), 200
    else:
        return jsonify({'message': '用戶名或密碼不正確'}), 401



@login_bp.route('/logout',  methods=['OPTIONS','POST'])
def logout():
    logout_user()
    return jsonify({'message': 'success logged out'}), 200
