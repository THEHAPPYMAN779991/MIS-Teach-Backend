from flask import Blueprint, jsonify, request
from accessories import mongo
from bson import ObjectId
from bson.json_util import dumps
from datetime import datetime
import jwt
from functools import wraps

# 建立 Blueprint
note_bp = Blueprint("note", __name__)

def convert_objectid(data):
    """
    遞迴處理 dict / list / ObjectId
    - 如果是 ObjectId，轉成字串
    - 如果是 list，遞迴處理每個元素
    - 如果是 dict，遞迴處理每個鍵值
    - 其他型別則直接回傳
    """
    if isinstance(data, ObjectId):
        return str(data)
    elif isinstance(data, list):
        return [convert_objectid(item) for item in data]
    elif isinstance(data, dict):
        return {k: convert_objectid(v) for k, v in data.items()}
    return data


def get_user_from_token():
    """
    從請求頭中獲取 JWT token 並解析用戶信息
    返回用戶 email 或 None
    """
    try:
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return None

        token = auth_header.split(' ')[1]

        # 這裡需要從 config 獲取 SECRET_KEY
        # 為了避免循環導入，我們直接從環境變數或 config 讀取
        import os
        from config import Config
        config = Config()
        secret_key = config.SECRET_KEY

        # 解析 JWT token
        payload = jwt.decode(token, secret_key, algorithms=['HS256'])
        return payload.get('user')  # 返回用戶 email
    except Exception as e:
        print(f"解析 token 失敗: {e}")
        return None


@note_bp.route('/highlights', methods=['GET'])
def get_highlights():
    """
    獲取指定教材的所有劃記
    GET /note/highlights?filename=xxx.md&user=xxx
    """
    try:
        filename = request.args.get('filename')
        user = request.args.get('user') or get_user_from_token()

        if not filename:
            return jsonify({"error": "缺少 filename 參數"}), 400

        if not user:
            return jsonify({"error": "無法識別用戶，請先登入"}), 401

        # 查詢劃記
        highlights = list(mongo.db.material_notes.find({
            "filename": filename,
            "user": user,
            "type": "highlight"
        }))

        # 轉換 ObjectId
        highlights = [convert_objectid(h) for h in highlights]

        return jsonify({"highlights": highlights})
    except Exception as e:
        print(f"獲取劃記失敗: {e}")
        return jsonify({"error": str(e)}), 500


@note_bp.route('/highlights', methods=['POST'])
def save_highlight():
    """
    儲存劃記
    POST /note/highlights
    {
        "filename": "xxx.md",
        "highlight_id": "highlight_xxx",
        "text": "劃記的文字",
        "color": "#ffff00",
        "user": "user@example.com"  # 可選，會從 token 獲取
    }
    """
    try:
        data = request.get_json()

        filename = data.get('filename')
        highlight_id = data.get('highlight_id')
        text = data.get('text')
        color = data.get('color', '#ffff00')
        user = data.get('user') or get_user_from_token()

        if not all([filename, highlight_id, text]):
            return jsonify({"error": "缺少必要參數"}), 400

        if not user:
            return jsonify({"error": "無法識別用戶，請先登入"}), 401

        # 檢查是否已存在
        existing = mongo.db.material_notes.find_one({
            "filename": filename,
            "highlight_id": highlight_id,
            "user": user,
            "type": "highlight"
        })

        highlight_data = {
            "filename": filename,
            "highlight_id": highlight_id,
            "text": text,
            "color": color,
            "user": user,
            "type": "highlight",
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow()
        }

        if existing:
            # 更新現有劃記
            mongo.db.material_notes.update_one(
                {"_id": existing["_id"]},
                {"$set": {
                    "text": text,
                    "color": color,
                    "updated_at": datetime.utcnow()
                }}
            )
            highlight_data["_id"] = str(existing["_id"])
        else:
            # 插入新劃記
            result = mongo.db.material_notes.insert_one(highlight_data)
            highlight_data["_id"] = str(result.inserted_id)

        return jsonify({"success": True, "highlight": convert_objectid(highlight_data)})
    except Exception as e:
        print(f"儲存劃記失敗: {e}")
        return jsonify({"error": str(e)}), 500


@note_bp.route('/highlights/<highlight_id>', methods=['DELETE'])
def delete_highlight(highlight_id):
    """
    刪除劃記
    DELETE /note/highlights/<highlight_id>?filename=xxx.md&user=xxx
    """
    try:
        filename = request.args.get('filename')
        user = request.args.get('user') or get_user_from_token()

        if not filename:
            return jsonify({"error": "缺少 filename 參數"}), 400

        if not user:
            return jsonify({"error": "無法識別用戶，請先登入"}), 401

        # 刪除劃記
        result = mongo.db.material_notes.delete_one({
            "filename": filename,
            "highlight_id": highlight_id,
            "user": user,
            "type": "highlight"
        })

        if result.deleted_count > 0:
            return jsonify({"success": True, "message": "劃記已刪除"})
        else:
            return jsonify({"error": "劃記不存在"}), 404
    except Exception as e:
        print(f"刪除劃記失敗: {e}")
        return jsonify({"error": str(e)}), 500


@note_bp.route('/notes', methods=['GET'])
def get_notes():
    """
    獲取指定教材的所有筆記
    GET /note/notes?filename=xxx.md&user=xxx
    """
    try:
        filename = request.args.get('filename')
        user = request.args.get('user') or get_user_from_token()

        if not filename:
            return jsonify({"error": "缺少 filename 參數"}), 400

        if not user:
            return jsonify({"error": "無法識別用戶，請先登入"}), 401

        # 查詢筆記
        notes = list(mongo.db.material_notes.find({
            "filename": filename,
            "user": user,
            "type": "note"
        }).sort("created_at", -1))

        # 轉換 ObjectId
        notes = [convert_objectid(n) for n in notes]

        return jsonify({"notes": notes})
    except Exception as e:
        print(f"獲取筆記失敗: {e}")
        return jsonify({"error": str(e)}), 500


@note_bp.route('/notes', methods=['POST'])
def create_note():
    """
    建立新筆記
    POST /note/notes
    {
        "filename": "xxx.md",
        "highlight_id": "highlight_xxx",  # 可選，關聯的劃記 ID
        "text": "筆記內容",
        "title": "筆記標題",  # 可選
        "user": "user@example.com"  # 可選，會從 token 獲取
    }
    """
    try:
        data = request.get_json()

        filename = data.get('filename')
        text = data.get('text', '')
        title = data.get('title', '')
        highlight_id = data.get('highlight_id')
        user = data.get('user') or get_user_from_token()

        if not filename:
            return jsonify({"error": "缺少必要參數"}), 400

        if not user:
            return jsonify({"error": "無法識別用戶，請先登入"}), 401

        note_data = {
            "filename": filename,
            "text": text,
            "title": title or f"筆記 {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}",
            "highlight_id": highlight_id,
            "user": user,
            "type": "note",
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow()
        }

        result = mongo.db.material_notes.insert_one(note_data)
        note_data["_id"] = str(result.inserted_id)

        return jsonify({"success": True, "note": convert_objectid(note_data)})
    except Exception as e:
        print(f"建立筆記失敗: {e}")
        return jsonify({"error": str(e)}), 500


@note_bp.route('/notes/<note_id>', methods=['PUT'])
def update_note(note_id):
    """
    更新筆記
    PUT /note/notes/<note_id>
    {
        "text": "更新的筆記內容",
        "title": "更新的標題"  # 可選
    }
    """
    try:
        data = request.get_json()
        user = get_user_from_token()

        if not user:
            return jsonify({"error": "無法識別用戶，請先登入"}), 401

        # 檢查筆記是否存在且屬於該用戶
        note = mongo.db.material_notes.find_one({
            "_id": ObjectId(note_id),
            "user": user,
            "type": "note"
        })

        if not note:
            return jsonify({"error": "筆記不存在或無權限"}), 404

        update_data = {
            "updated_at": datetime.utcnow()
        }

        if "text" in data:
            update_data["text"] = data["text"]

        if "title" in data:
            update_data["title"] = data["title"]

        # 更新筆記
        mongo.db.material_notes.update_one(
            {"_id": ObjectId(note_id)},
            {"$set": update_data}
        )

        # 獲取更新後的筆記
        updated_note = mongo.db.material_notes.find_one({"_id": ObjectId(note_id)})

        return jsonify({"success": True, "note": convert_objectid(updated_note)})
    except Exception as e:
        print(f"更新筆記失敗: {e}")
        return jsonify({"error": str(e)}), 500


@note_bp.route('/notes/<note_id>', methods=['DELETE'])
def delete_note(note_id):
    """
    刪除筆記
    DELETE /note/notes/<note_id>
    """
    try:
        user = get_user_from_token()

        if not user:
            return jsonify({"error": "無法識別用戶，請先登入"}), 401

        # 檢查筆記是否存在且屬於該用戶
        note = mongo.db.material_notes.find_one({
            "_id": ObjectId(note_id),
            "user": user,
            "type": "note"
        })

        if not note:
            return jsonify({"error": "筆記不存在或無權限"}), 404

        # 刪除筆記
        result = mongo.db.material_notes.delete_one({"_id": ObjectId(note_id)})

        if result.deleted_count > 0:
            return jsonify({"success": True, "message": "筆記已刪除"})
        else:
            return jsonify({"error": "刪除失敗"}), 500
    except Exception as e:
        print(f"刪除筆記失敗: {e}")
        return jsonify({"error": str(e)}), 500


@note_bp.route('/highlights/clear', methods=['POST'])
def clear_all_highlights():
    """
    清除指定教材的所有劃記
    POST /note/highlights/clear
    {
        "filename": "xxx.md",
        "user": "user@example.com"  # 可選，會從 token 獲取
    }
    """
    try:
        data = request.get_json()
        filename = data.get('filename')
        user = data.get('user') or get_user_from_token()

        if not filename:
            return jsonify({"error": "缺少 filename 參數"}), 400

        if not user:
            return jsonify({"error": "無法識別用戶，請先登入"}), 401

        # 刪除所有劃記
        result = mongo.db.material_notes.delete_many({
            "filename": filename,
            "user": user,
            "type": "highlight"
        })

        return jsonify({
            "success": True,
            "message": f"已清除 {result.deleted_count} 個劃記"
        })
    except Exception as e:
        print(f"清除劃記失敗: {e}")
        return jsonify({"error": str(e)}), 500
