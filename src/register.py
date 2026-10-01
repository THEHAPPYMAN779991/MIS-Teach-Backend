from werkzeug.security import generate_password_hash
from flask_mail import Message
from flask import jsonify, request, redirect, url_for, Blueprint, current_app
import uuid
from accessories import mail, redis_client, mongo, save_json_to_mongo
from bson.objectid import ObjectId
register_bp = Blueprint('register', __name__)

@register_bp.route('/register_user', methods=['POST', 'OPTIONS'])
def register():
    if request.method == 'OPTIONS':
        return '', 204
    def send_verification_email(email, verification_link):
        msg = Message("Please verify your email", recipients=[email])
        msg.body = f"Click the link to verify your email: {verification_link}"
        mail.send(msg)
    data = request.get_json()
    name = data.get("name")
    email = data.get("email")
    password = data.get("password")

    if not all([name, email, password]):
        return jsonify({"error": "Missing required fields."}), 400

    if mongo.db.user.find_one({"email": email}):
        return jsonify({"error": "Email already exists."}), 409
    token = str(uuid.uuid4())
    redis_client.hset(token, mapping={
       'email': email,
       'name': name,
       'password':generate_password_hash(password)})
    redis_client.expire(token, 3600)
    verification_link = url_for('register.verify_email', token=token, _external=True)

    send_verification_email(email, verification_link)

    return jsonify({"message": "Verification email sent."}), 200

@register_bp.route('/verify/<token>', methods=['GET'])
def verify_email(token):
    user_data = redis_client.hgetall(token)

    if user_data and not mongo.db.user.find_one({"email": user_data.get(b'email').decode('utf-8')}):
        name = user_data.get(b'name').decode('utf-8')
        password = user_data.get(b'password').decode('utf-8')
        email = user_data.get(b'email').decode('utf-8')

        mongo.db.user.insert_one({
            "name": name,
            "password": password,
            "email": email,
            'new_user': True
        })
        redis_client.delete(token)
        return jsonify({"message": "Email verified successfully."}), 200