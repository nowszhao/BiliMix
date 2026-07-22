"""Auth Blueprint - login, logout, session check."""
import sys, os
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from flask import (
    Blueprint, current_app, jsonify, request, session, redirect, send_from_directory
)
from core import config

auth_bp = Blueprint('auth', __name__)


@auth_bp.before_app_request
def check_auth():
    """Global request hook: redirect to login if not authenticated."""
    if not getattr(config, "AUTH_ENABLED", True):
        return None

    allowed_paths = ["/login", "/api/login", "/api/auth/check"]
    if request.path in allowed_paths:
        return None

    if request.path == "/login.html":
        return None

    if session.get("logged_in"):
        return None

    if request.path.startswith("/api/"):
        return jsonify({"error": "未登录", "code": "AUTH_REQUIRED"}), 401

    static_exts = (".css", ".js", ".svg", ".png", ".ico", ".woff", ".woff2", ".ttf")
    if any(request.path.endswith(ext) for ext in static_exts):
        return None

    return redirect("/login")


@auth_bp.route("/login")
def login_page():
    """Login page."""
    if session.get("logged_in") and getattr(config, "AUTH_ENABLED", True):
        return redirect("/")

    # _web_dir from config module
    _web_dir = getattr(config, "WEB_DIR", os.path.join(_project_root, "web"))
    return send_from_directory(_web_dir, "login.html")


@auth_bp.route("/api/login", methods=["POST"])
def api_login():
    """Login API endpoint."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "请提供登录信息"}), 400

    username = data.get("username", "").strip()
    password = data.get("password", "").strip()

    expected_user = getattr(config, "AUTH_USERNAME", "admin")
    expected_pass = getattr(config, "AUTH_PASSWORD", "bilimix2024")

    if username == expected_user and password == expected_pass:
        session["logged_in"] = True
        session["username"] = username
        return jsonify({"ok": True, "message": "登录成功"})
    else:
        return jsonify({"error": "用户名或密码错误"}), 401


@auth_bp.route("/api/logout", methods=["POST"])
def api_logout():
    """Logout endpoint."""
    session.clear()
    return jsonify({"ok": True, "message": "已退出登录"})


@auth_bp.route("/api/auth/check")
def api_auth_check():
    """Check authentication status."""
    auth_enabled = getattr(config, "AUTH_ENABLED", True)
    if not auth_enabled:
        return jsonify({"authenticated": True, "auth_enabled": False})
    return jsonify({
        "authenticated": bool(session.get("logged_in")),
        "auth_enabled": True,
        "username": session.get("username", ""),
    })
