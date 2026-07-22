"""BiliMix Podcast API — Flask Blueprint

播客收藏、RSS 订阅管理、订阅单集、播客搜索/RSS 解析、订阅自动刷新。
从 web_app.py 提取而来。
"""
import sys
import os
import time
import threading

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from flask import Blueprint, jsonify, request

from core import config
from core.database import (
    get_favorites, add_favorite, remove_favorite, is_favorite,
    get_subscriptions, add_subscription, remove_subscription,
    upsert_episodes, get_episodes, update_episode_status,
    get_episode_stats, get_unread_counts_by_subscription,
    mark_all_episodes_read,
    get_recent_podcasts, add_search_keyword,
)
from services.podcast_service import search_podcasts_itunes, parse_rss_feed

podcast_bp = Blueprint('podcast_api', __name__, url_prefix='/api')

# ============================================================
# 订阅自动刷新 (module-level)
# ============================================================

_last_refresh_time: float = 0.0
_refresh_lock = threading.Lock()


def _refresh_all_subscriptions():
    """拉取所有订阅的最新 RSS，更新单集数据库。"""
    global _last_refresh_time
    with _refresh_lock:
        subs = get_subscriptions()
        if not subs:
            return

        total_new = 0
        for sub in subs:
            rss_url = sub.get("rss_url", "")
            title = sub.get("title", rss_url[:50])
            if not rss_url:
                continue
            try:
                result = parse_rss_feed(rss_url)
                episodes = result.get("episodes", [])
                if episodes:
                    new_count = upsert_episodes(rss_url, episodes)
                    total_new += new_count
            except Exception as e:
                print(f"[Refresh] 订阅刷新失败 [{title}]: {e}")

        _last_refresh_time = time.time()
        if total_new > 0:
            print(f"[Refresh] 刷新完成: {len(subs)} 个订阅, "
                  f"新增 {total_new} 集")


def _start_subscription_refresh_loop():
    """启动订阅自动刷新后台线程。"""
    interval_minutes = getattr(config, "SUBSCRIPTION_REFRESH_INTERVAL_MINUTES", 60)
    if interval_minutes <= 0:
        print("[Refresh] 订阅自动刷新已禁用 (interval=0)")
        return

    def _loop():
        time.sleep(30)
        while True:
            try:
                _refresh_all_subscriptions()
            except Exception as e:
                print(f"[Refresh] 刷新异常: {e}")
            time.sleep(interval_minutes * 60)

    t = threading.Thread(target=_loop, daemon=True, name="subscription-refresh")
    t.start()
    print(f"[Refresh] 订阅自动刷新已启动 (间隔 {interval_minutes} 分钟)")


_start_subscription_refresh_loop()


# ============================================================
# API 路由 — 播客收藏
# ============================================================

@podcast_bp.route("/favorites")
def api_get_favorites():
    return jsonify({"favorites": get_favorites()})


@podcast_bp.route("/favorites", methods=["POST"])
def api_add_favorite():
    data = request.get_json()
    if not data or "rss_url" not in data:
        return jsonify({"error": "请提供 rss_url"}), 400
    fav = add_favorite(title=data.get("title", ""), author=data.get("author", ""),
                       image=data.get("image", ""), rss_url=data["rss_url"])
    return jsonify({"ok": True, "favorite": fav})


@podcast_bp.route("/favorites", methods=["DELETE"])
def api_remove_favorite():
    data = request.get_json()
    if not data or "rss_url" not in data:
        return jsonify({"error": "请提供 rss_url"}), 400
    remove_favorite(data["rss_url"])
    return jsonify({"ok": True})


@podcast_bp.route("/favorites/check")
def api_check_favorite():
    rss_url = request.args.get("rss_url", "").strip()
    if not rss_url:
        return jsonify({"error": "请提供 rss_url"}), 400
    return jsonify({"is_favorite": is_favorite(rss_url)})


# ============================================================
# API 路由 — RSS 订阅管理
# ============================================================

@podcast_bp.route("/subscriptions")
def api_get_subscriptions():
    """获取全部 RSS 订阅"""
    return jsonify({
        "subscriptions": get_subscriptions(),
        "last_refresh": _last_refresh_time,
    })


@podcast_bp.route("/subscriptions", methods=["POST"])
def api_add_subscription():
    """添加 RSS 订阅"""
    data = request.get_json()
    if not data or "rss_url" not in data:
        return jsonify({"error": "请提供 rss_url"}), 400
    sub = add_subscription(
        title=data.get("title", ""),
        author=data.get("author", ""),
        image=data.get("image", ""),
        rss_url=data["rss_url"],
    )
    return jsonify({"ok": True, "subscription": sub})


@podcast_bp.route("/subscriptions", methods=["DELETE"])
def api_remove_subscription():
    """移除 RSS 订阅"""
    data = request.get_json()
    if not data or "rss_url" not in data:
        return jsonify({"error": "请提供 rss_url"}), 400
    remove_subscription(data["rss_url"])
    return jsonify({"ok": True})


@podcast_bp.route("/subscriptions/refresh", methods=["POST"])
def api_refresh_subscriptions():
    """手动刷新所有订阅"""
    import threading as _th
    _th.Thread(target=_refresh_all_subscriptions, daemon=True).start()
    return jsonify({"ok": True, "message": "刷新已开始"})


# ============================================================
# API 路由 — 订阅单集 (Episodes)
# ============================================================

def _normalize_episodes_for_insert(episodes, rss_url):
    """将 parse_rss_feed 返回的 episode 列表标准化为 upsert_episodes 需要的格式"""
    for i, ep in enumerate(episodes):
        ep["audio_url"] = ep.get("enclosureUrl", "")
        ep["published_at"] = ep.get("datePublished", "")
        if not ep.get("guid"):
            ep["guid"] = ep.get("enclosureUrl") or f"{rss_url}#{i}"


def _refresh_single_feed(rss_url):
    """刷新单个 RSS Feed，返回 (new_count, error_dict)"""
    feed_data = parse_rss_feed(rss_url, max_episodes=50)
    if "error" in feed_data:
        return None, {"rss_url": rss_url, "error": feed_data["error"]}
    episodes = feed_data.get("episodes", [])
    _normalize_episodes_for_insert(episodes, rss_url)
    return upsert_episodes(rss_url, episodes), None


@podcast_bp.route("/episodes")
def api_get_episodes():
    """获取订阅单集列表，支持筛选"""
    status_filter = request.args.get("status", "all")
    rss_url = request.args.get("rss_url", "")
    time_range = request.args.get("time_range", "week")
    page = int(request.args.get("page", 1))
    page_size = int(request.args.get("page_size", 100))
    result = get_episodes(status_filter=status_filter, rss_url=rss_url,
                          time_range=time_range, page=page, page_size=page_size)
    return jsonify(result)


@podcast_bp.route("/episodes/stats")
def api_episode_stats():
    """获取单集状态统计"""
    rss_url = request.args.get("rss_url", "")
    time_range = request.args.get("time_range", "all")
    stats = get_episode_stats(rss_url=rss_url, time_range=time_range)
    subs = get_unread_counts_by_subscription(time_range=time_range)
    return jsonify({"stats": stats, "subscriptions": subs})


@podcast_bp.route("/episodes/<int:episode_id>", methods=["PATCH"])
def api_update_episode(episode_id):
    """更新单集状态"""
    data = request.get_json()
    status = data.get("status", "")
    task_id = data.get("task_id", "")
    if status not in ("unread", "read", "transcribed", "dismissed"):
        return jsonify({"error": "无效的状态"}), 400
    ep = update_episode_status(episode_id, status, task_id)
    if not ep:
        return jsonify({"error": "单集不存在"}), 404
    return jsonify({"ok": True, "episode": ep})


@podcast_bp.route("/episodes/mark-all-read", methods=["POST"])
def api_mark_all_episodes_read():
    """批量将未读单集标记为已读"""
    data = request.get_json(silent=True) or {}
    rss_url = data.get("rss_url", "")
    time_range = data.get("time_range", "all")
    affected = mark_all_episodes_read(rss_url=rss_url, time_range=time_range)
    return jsonify({"ok": True, "affected": affected})


@podcast_bp.route("/episodes/refresh", methods=["POST"])
def api_refresh_episodes():
    """手动刷新所有订阅源，并发拉取最新单集"""
    subs = get_subscriptions()
    total_new = 0
    errors = []
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _refresh_one(sub):
        rss_url = sub["rss_url"]
        count, err = _refresh_single_feed(rss_url)
        return count, err

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_refresh_one, sub): sub for sub in subs}
        for future in as_completed(futures):
            count, err = future.result()
            if count is None:
                errors.append(err)
            else:
                total_new += count

    return jsonify({
        "ok": True,
        "refreshed": len(subs),
        "new_episodes": total_new,
        "errors": errors,
    })


@podcast_bp.route("/episodes/refresh/<path:rss_url>", methods=["POST"])
def api_refresh_single_feed(rss_url):
    """刷新单个订阅源，用于新订阅时即时拉取单集"""
    from urllib.parse import unquote as _unquote
    rss_url = _unquote(rss_url)
    count, err = _refresh_single_feed(rss_url)
    if count is None:
        return jsonify({"ok": False, "error": err.get("error", "刷新失败")}), 502
    return jsonify({"ok": True, "new_episodes": count})


# ============================================================
# API 路由 — 最近使用的播客
# ============================================================

@podcast_bp.route("/recent-podcasts")
def api_recent_podcasts():
    """获取最近使用的播客源"""
    podcasts = get_recent_podcasts()
    return jsonify({"podcasts": podcasts})


# ============================================================
# API 路由 — 播客搜索
# ============================================================

@podcast_bp.route("/podcast/search")
def podcast_search():
    """搜索播客"""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "请提供搜索关键词 q"}), 400
    add_search_keyword(q)
    result = search_podcasts_itunes(q)
    if "error" in result:
        return jsonify({"error": result["error"]}), 500
    return jsonify(result)


@podcast_bp.route("/podcast/rss")
def podcast_rss():
    """通过 RSS Feed URL 解析播客单集列表"""
    feed_url = request.args.get("url", "").strip()
    if not feed_url:
        return jsonify({"error": "请提供 RSS Feed URL"}), 400
    if not feed_url.startswith(("http://", "https://")):
        return jsonify({"error": "请提供有效的 HTTP/HTTPS URL"}), 400
    result = parse_rss_feed(feed_url)
    if "error" in result:
        return jsonify({"error": result["error"]}), 500
    return jsonify(result)
