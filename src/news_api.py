#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新聞 API - 提供新聞查詢和分頁功能
"""

from flask import Blueprint, request, jsonify
from flask_cors import cross_origin
from sqlalchemy import text
from accessories import sqldb
import json

news_api_bp = Blueprint('news_api', __name__)


@news_api_bp.route('/api/news', methods=['GET', 'OPTIONS'])
@cross_origin()
def get_news():
    """獲取新聞列表（支援分頁）"""
    try:
        # 獲取查詢參數
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 12))
        search = request.args.get('search', '')

        # 計算偏移量
        offset = (page - 1) * per_page

        # 構建查詢語句
        where_clause = ""
        params = {'limit': per_page, 'offset': offset}

        if search:
            where_clause = "WHERE title LIKE :search"
            params['search'] = f'%{search}%'

        # 查詢總數
        with sqldb.engine.connect() as conn:
            count_query = f"SELECT COUNT(*) as total FROM news {where_clause}"
            count_result = conn.execute(text(count_query), params if not search else {'search': f'%{search}%'})
            total_count = count_result.fetchone()[0]

            # 查詢數據
            data_query = f"""
                SELECT id, title, summary, href, image, date, tags, created_at
                FROM news
                {where_clause}
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
            """

            result = conn.execute(text(data_query), params)
            news_list = []

            for row in result:
                # 解析 tags JSON
                tags = []
                if row[6]:
                    try:
                        tags = json.loads(row[6]) if isinstance(row[6], str) else row[6]
                    except:
                        tags = []

                news_list.append({
                    'id': row[0],
                    'title': row[1],
                    'summary': row[2],
                    'href': row[3],
                    'image': row[4],
                    'date': row[5],
                    'tags': tags,
                    'created_at': row[7].isoformat() if row[7] else None
                })

        # 計算總頁數
        total_pages = (total_count + per_page - 1) // per_page

        return jsonify({
            'data': news_list,
            'pagination': {
                'current_page': page,
                'per_page': per_page,
                'total': total_count,
                'total_pages': total_pages,
                'has_next': page < total_pages,
                'has_prev': page > 1
            }
        }), 200

    except Exception as e:
        print(f"❌ 獲取新聞失敗: {e}")
        return jsonify({'error': str(e)}), 500


@news_api_bp.route('/api/news/<int:news_id>', methods=['GET', 'OPTIONS'])
@cross_origin()
def get_news_detail(news_id):
    """獲取單條新聞詳情"""
    try:
        with sqldb.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT id, title, summary, href, image, date, tags, created_at
                FROM news
                WHERE id = :news_id
            """), {'news_id': news_id})

            row = result.fetchone()

            if not row:
                return jsonify({'error': '新聞不存在'}), 404

            # 解析 tags JSON
            tags = []
            if row[6]:
                try:
                    tags = json.loads(row[6]) if isinstance(row[6], str) else row[6]
                except:
                    tags = []

            news_item = {
                'id': row[0],
                'title': row[1],
                'summary': row[2],
                'href': row[3],
                'image': row[4],
                'date': row[5],
                'tags': tags,
                'created_at': row[7].isoformat() if row[7] else None
            }

            return jsonify(news_item), 200

    except Exception as e:
        print(f"❌ 獲取新聞詳情失敗: {e}")
        return jsonify({'error': str(e)}), 500


@news_api_bp.route('/api/news/stats', methods=['GET', 'OPTIONS'])
@cross_origin()
def get_news_stats():
    """獲取新聞統計信息"""
    try:
        with sqldb.engine.connect() as conn:
            # 總數
            total_result = conn.execute(text("SELECT COUNT(*) as total FROM news"))
            total_count = total_result.fetchone()[0]

            # 最新日期
            latest_result = conn.execute(text("SELECT MAX(date) as latest_date FROM news"))
            latest_date = latest_result.fetchone()[0]

            return jsonify({
                'total': total_count,
                'latest_date': latest_date
            }), 200

    except Exception as e:
        print(f"❌ 獲取新聞統計失敗: {e}")
        return jsonify({'error': str(e)}), 500
