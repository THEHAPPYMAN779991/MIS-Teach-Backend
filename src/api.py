from flask import request,url_for, Blueprint, jsonify, current_app, send_from_directory, abort
import jwt
from accessories import mongo
from datetime import datetime, timedelta
from functools import wraps
import os
import time

def verify_token(token):
    """验证JWT token并返回用户信息"""
    try:
        decoded_token = jwt.decode(token, current_app.config['SECRET_KEY'], algorithms=["HS256"])
        # 檢查token是否過期 - 現在使用Unix時間戳
        exp = decoded_token.get('exp')
        if exp and time.time() < exp:
            # 返回用户邮箱
            user_email = decoded_token.get('user')
            if user_email:
                return user_email
            else:
                return None
        return None
    except jwt.ExpiredSignatureError:
        print("❌ Token已過期")
        return None
    except jwt.InvalidTokenError as e:
        print(f"❌ Token無效: {e}")
        return None

def get_user_info(token, key):
    decoded_token = jwt.decode(token, current_app.config['SECRET_KEY'], algorithms=["HS256"])

    user = mongo.db.user.find_one({"email": decoded_token['user']})
    if not user:
        print(f"❌ 找不到用戶: {decoded_token['user']}")
        raise ValueError("User not found")

    return user[key]
