import os

# app.py loads api.env before importing this module. Keep this idempotent
# fallback so direct configuration imports use the same local environment file.
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'api.env'), override=False)
except ImportError:
    pass

class Config:
    # 基本配置
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_recycle': 3600,
        'pool_size': 5,
        'max_overflow': 10
    }

    # 安全配置
    def _get_security_key(self):
        """Read the Flask secret from environment or the ignored local file."""
        configured_key = os.getenv('FLASK_SECRET_KEY') or os.getenv('SECRET_KEY')
        if configured_key:
            return configured_key

        import os
        # 獲取當前文件所在目錄
        current_dir = os.path.dirname(os.path.abspath(__file__))
        security_key_path = os.path.join(current_dir, 'security_key')

        if os.path.exists(security_key_path):
            with open(security_key_path, 'r', encoding='utf-8') as f:
                return f.read().strip()

        raise RuntimeError(
            'FLASK_SECRET_KEY is not configured. Set it in api.env or create a local security_key file.'
        )

    @property
    def SECRET_KEY(self):
        return self._get_security_key()

    @property
    def SECURITY_PASSWORD_SALT(self):
        return self._get_security_key()

    # 郵件配置
    MAIL_SERVER = 'smtp.gmail.com'
    MAIL_PORT = 587
    MAIL_USE_TLS = True
    MAIL_USERNAME = os.getenv('MAIL_USERNAME')
    MAIL_PASSWORD = os.getenv('MAIL_PASSWORD')
    MAIL_DEFAULT_SENDER = os.getenv('MAIL_DEFAULT_SENDER')
    # 資料庫配置
    # MongoDB
    MONGO_URI = os.getenv('MONGO_URI')
    MONGO_DB_NAME = os.getenv('MONGO_DB_NAME', 'MIS_Teach')

    # Redis
    REDIS_URL = os.getenv('REDIS_URL')
    # Neo4j
    NEO4J_URI = os.getenv('NEO4J_URI')
    NEO4J_USERNAME = os.getenv('NEO4J_USERNAME')
    NEO4J_PASSWORD = os.getenv('NEO4J_PASSWORD')
    # JWT 配置
    JWT_SECRET_KEY = SECRET_KEY
    JWT_ACCESS_TOKEN_EXPIRES = 3600  # 1小時
    JWT_REFRESH_TOKEN_EXPIRES = 2592000  # 30天

    ROUTE_ROLE_MAPPING = {

    }

class DevelopmentConfig(Config):
    SQLALCHEMY_DATABASE_URI = os.getenv('SQLALCHEMY_DATABASE_URI')
    SQLALCHEMY_BINDS = {}

    API_BASE_URL = 'http://localhost:5000'
    # ngrok 前端網址：https://2244e984b70a.ngrok-free.app
    DOMAIN_NAME = 'https://2244e984b70a.ngrok-free.app'
    DEBUG = True

class ProductionConfig(Config):
    SQLALCHEMY_DATABASE_URI = (os.getenv('SQLALCHEMY_DATABASE_URI_PRODUCTION') or os.getenv('SQLALCHEMY_DATABASE_URI'))
    SQLALCHEMY_BINDS = {}

    API_BASE_URL = 'http://localhost:5000'
    DOMAIN_NAME = 'http://localhost:4200'
    DEBUG = True
