#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
初始化新聞數據表
從 ithome_news.json 讀取數據並存入 MySQL
"""

import os
import sys
import json

# 添加 backend 目錄到 Python 路徑
backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from sqlalchemy import text
from flask import Flask
from config import DevelopmentConfig
from accessories import sqldb

# 創建 Flask 應用實例
app = Flask(__name__)
app.config.from_object(DevelopmentConfig)
sqldb.init_app(app)


def init_news_table():
    """初始化新聞數據表"""
    with app.app_context():
        try:
            with sqldb.engine.connect() as conn:
                # 創建新聞表
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS news (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        title TEXT NOT NULL,
                        summary TEXT,
                        href VARCHAR(500),
                        image VARCHAR(500),
                        date VARCHAR(50),
                        tags JSON,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        INDEX idx_date (date),
                        INDEX idx_created_at (created_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """))
                conn.commit()
                print("✅ 新聞數據表創建成功")
        except Exception as e:
            print(f"❌ 創建新聞數據表失敗: {e}")


def migrate_news_data():
    """從 JSON 文件遷移新聞數據到數據庫"""
    with app.app_context():
        try:
            json_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'ithome_news.json')

            if not os.path.exists(json_path):
                print(f"❌ JSON 文件不存在: {json_path}")
                return

            # 讀取 JSON 數據
            with open(json_path, 'r', encoding='utf-8') as f:
                news_data = json.load(f)

            print(f"📄 讀取到 {len(news_data)} 條新聞")

            with sqldb.engine.connect() as conn:
                # 檢查是否已有數據
                result = conn.execute(text("SELECT COUNT(*) as count FROM news"))
                count = result.fetchone()[0]

                if count > 0:
                    print(f"⚠️ 數據庫中已有 {count} 條新聞，跳過遷移")
                    return

                # 插入數據
                for idx, news in enumerate(news_data, 1):
                    try:
                        title = news.get('title', {})
                        title_text = title.get('text', '') if isinstance(title, dict) else str(title)
                        title_href = title.get('href', news.get('href', '')) if isinstance(title, dict) else news.get('href', '')

                        conn.execute(text("""
                            INSERT INTO news (title, summary, href, image, date, tags)
                            VALUES (:title, :summary, :href, :image, :date, :tags)
                        """), {
                            'title': title_text,
                            'summary': news.get('summary', ''),
                            'href': title_href if title_href else news.get('href', ''),
                            'image': news.get('image', ''),
                            'date': news.get('date', ''),
                            'tags': json.dumps(news.get('tags', []))
                        })

                        if idx % 50 == 0:
                            print(f"  └─ 已處理 {idx}/{len(news_data)} 條")

                    except Exception as e:
                        print(f"❌ 插入第 {idx} 條新聞失敗: {e}")
                        continue

                conn.commit()
                print(f"✅ 成功遷移 {idx} 條新聞到數據庫")

        except Exception as e:
            print(f"❌ 遷移新聞數據失敗: {e}")


if __name__ == '__main__':
    init_news_table()
    migrate_news_data()
