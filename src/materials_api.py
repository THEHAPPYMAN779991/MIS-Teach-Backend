from flask import Blueprint, jsonify, Response
import os
from accessories import mongo
import markdown
from bson.json_util import dumps
import traceback
from bson import ObjectId

# 建立 Blueprint
materials_bp = Blueprint("materials", __name__)

# 教材存放目錄在 backend/materials/
BASE_DIR = os.path.dirname(os.path.dirname(__file__))  # backend/
MATERIALS_DIR = os.path.join(BASE_DIR, "data", "materials")

# 確保資料夾存在
os.makedirs(MATERIALS_DIR, exist_ok=True)



@materials_bp.route("/<filename>", methods=["GET"])
def get_material(filename):
    """
    回傳教材的原始 Markdown 內容（給 Angular 渲染）
    """
    # ✅ 自動補上 .md 副檔名
    if not filename.lower().endswith(".md"):
        filename += ".md"

    filepath = os.path.join(MATERIALS_DIR, filename)

    if not os.path.exists(filepath):
        return jsonify({"error": "File not found"}), 404

    # 讀取 Markdown
    with open(filepath, "r", encoding="utf-8") as f:
        md_content = f.read()

    return jsonify({
        "filename": filename,
        "content": md_content
    })



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


@materials_bp.route("/key_points", methods=["GET"])
def get_key_points():
    """
    從 mongodb exam 集合中抓取所有 key_points，去重後回傳
    """
    exams = mongo.db.exam.find({}, {"key_points": 1})
    key_points_set = set()
    for exam in exams:
        key_points = exam.get("key_points", [])
        if isinstance(key_points, list):
            for kp in key_points:
                key_points_set.add(kp)
        elif isinstance(key_points, str):
            key_points_set.add(key_points)
    return jsonify({"key_points": sorted(list(key_points_set))})


@materials_bp.route('/domain', methods=['GET'])
def get_domains():
    """
    GET /materials/domain
    從 MongoDB 抓取 domain 資料，並將所有 ObjectId 轉為字串
    """
    domains = list(mongo.db.domain.find())
    if not domains:
        return jsonify({"error": "No domain collection or data found"}), 404

    # 遞迴轉換 ObjectId
    domains = [convert_objectid(d) for d in domains]
    return jsonify(domains)


@materials_bp.route('/block', methods=['GET'])
def get_blocks():
    """
    GET /materials/block
    從 MongoDB 抓取 block 資料，並將所有 ObjectId 轉為字串
    """
    blocks = list(mongo.db.block.find())
    if not blocks:
        return jsonify({"error": "No block collection or data found"}), 404

    # 遞迴轉換 ObjectId
    blocks = [convert_objectid(b) for b in blocks]
    return jsonify(blocks)


@materials_bp.route('/micro_concept', methods=['GET'])
def get_micro_concepts():
    """
    GET /materials/micro_concept
    從 MongoDB 抓取 micro_concept 資料，並將所有 ObjectId 轉為字串
    """
    micro_concepts = list(mongo.db.micro_concept.find())
    if not micro_concepts:
        return jsonify({"error": "No micro_concept collection or data found"}), 404

    # 遞迴轉換 ObjectId
    micro_concepts = [convert_objectid(m) for m in micro_concepts]
    return jsonify(micro_concepts)