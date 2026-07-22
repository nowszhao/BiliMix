"""BiliMix Tools API — Flask Blueprint

翻译工具、词频等级查询、搜索历史管理。
从 web_app.py 提取而来。
"""
import sys
import os

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from flask import Blueprint, jsonify, request

from core.database import (
    add_search_keyword, get_search_suggestions, clear_search_history,
)

tools_bp = Blueprint('tools_api', __name__, url_prefix='/api')


# ============================================================
# 翻译 & 词频工具
# ============================================================

def _translate_word(english, context_sentence=None):
    """简化的翻译函数：调用 LLM 翻译单个英文词/短语"""
    try:
        from core.llm_utils import call_ollama
        prompt = f'Translate the following English word/phrase to Chinese. Return only the Chinese translation, nothing else.\n\nEnglish: {english}'
        if context_sentence:
            prompt += f'\nContext: {context_sentence}'
        result = call_ollama(prompt, raw=True)
        return {"english": english, "chinese": (result or "").strip()}
    except Exception as e:
        return {"english": english, "chinese": "", "error": str(e)}


def _batch_word_levels(words):
    """批量查询词频等级（回退到简单实现）"""
    return {"levels": {}, "level_nums": {}}


@tools_bp.route("/translate", methods=["POST"])
def api_translate():
    """翻译单个英文词/短语为中文"""
    data = request.get_json()
    if not data or "english" not in data:
        return jsonify({"error": "请提供 english 字段"}), 400
    result = _translate_word(data["english"], data.get("context_sentence"))
    return jsonify(result)


@tools_bp.route("/word-levels", methods=["POST"])
def api_word_levels():
    """查询单词的 BNC/COCA 词频等级"""
    data = request.get_json()
    words = data.get("words", []) if data else []
    if not words:
        return jsonify({"error": "请提供 words 列表"}), 400
    result = _batch_word_levels(words)
    return jsonify(result)


# ============================================================
# API 路由 — 搜索历史
# ============================================================

@tools_bp.route("/search-history/suggestions")
def api_search_suggestions():
    """获取搜索建议"""
    prefix = request.args.get("q", "").strip()
    suggestions = get_search_suggestions(prefix)
    return jsonify({"suggestions": suggestions})


@tools_bp.route("/search-history", methods=["POST"])
def api_add_search_history():
    """记录搜索关键词"""
    data = request.get_json()
    if data and "keyword" in data:
        add_search_keyword(data["keyword"])
    return jsonify({"ok": True})


@tools_bp.route("/search-history", methods=["DELETE"])
def api_clear_search_history():
    """清空搜索历史"""
    clear_search_history()
    return jsonify({"ok": True})
