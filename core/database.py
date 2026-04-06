"""
SQLite 数据库模块
统一管理任务索引、播客收藏、RSS 订阅、搜索历史等持久化数据。
替代之前的 tasks_index.json 方案，性能更好且支持并发读写。
"""
import json
import os
import sqlite3
import threading
from contextlib import contextmanager

from core import config

DB_PATH = config.DB_PATH

# 线程局部存储：每个线程使用独立连接
_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    """获取当前线程的数据库连接（懒创建）"""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(DB_PATH, timeout=10)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
    return _local.conn


@contextmanager
def get_db():
    """上下文管理器：获取连接并在结束时提交"""
    conn = _get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db():
    """初始化数据库表结构"""
    with get_db() as conn:
        conn.executescript("""
            -- 任务索引表（替代 tasks_index.json）
            CREATE TABLE IF NOT EXISTS tasks (
                task_id       TEXT PRIMARY KEY,
                url           TEXT DEFAULT '',
                title         TEXT DEFAULT '',
                difficulty    TEXT DEFAULT '',
                process_mode  TEXT DEFAULT 'word_replace',
                status        TEXT DEFAULT '',
                progress      INTEGER DEFAULT 0,
                message       TEXT DEFAULT '',
                created_at    TEXT DEFAULT '',
                basename      TEXT DEFAULT '',
                total_words   INTEGER DEFAULT 0,
                total_replacements INTEGER DEFAULT 0,
                original_duration  REAL DEFAULT 0,
                mixed_duration     REAL DEFAULT 0
            );

            -- 播客收藏表
            CREATE TABLE IF NOT EXISTS favorites (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                title      TEXT NOT NULL,
                author     TEXT DEFAULT '',
                image      TEXT DEFAULT '',
                rss_url    TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                UNIQUE(rss_url)
            );

            -- RSS 订阅管理表
            CREATE TABLE IF NOT EXISTS subscriptions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                title      TEXT NOT NULL,
                author     TEXT DEFAULT '',
                image      TEXT DEFAULT '',
                rss_url    TEXT NOT NULL UNIQUE,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            );

            -- 搜索历史表
            CREATE TABLE IF NOT EXISTS search_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword    TEXT NOT NULL UNIQUE,
                count      INTEGER DEFAULT 1,
                updated_at TEXT DEFAULT (datetime('now', 'localtime'))
            );

            -- 全局生词库表
            CREATE TABLE IF NOT EXISTS vocabulary (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                english         TEXT NOT NULL,
                chinese         TEXT DEFAULT '',
                type            TEXT DEFAULT 'word',
                frequency_level TEXT DEFAULT '',
                encounter_count INTEGER DEFAULT 1,
                first_seen_at   TEXT DEFAULT (datetime('now', 'localtime')),
                last_seen_at    TEXT DEFAULT (datetime('now', 'localtime')),
                source_tasks    TEXT DEFAULT '[]',
                context_sentence TEXT DEFAULT '',
                mastered        INTEGER DEFAULT 0,
                UNIQUE(english)
            );
        """)
        # 向已有的 tasks 表添加 title 列（兼容旧数据库）
        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN title TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # 列已存在，忽略


# ============================================================
# 数据迁移：从 tasks_index.json 导入已有数据
# ============================================================

def migrate_from_json():
    """将旧的 tasks_index.json 数据导入 SQLite（仅首次运行时执行）"""
    json_path = os.path.join(config.BASE_DIR, "tasks_index.json")
    if not os.path.exists(json_path):
        return

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            index = json.load(f)
    except Exception:
        return

    if not index:
        return

    with get_db() as conn:
        # 检查是否已经有任务数据（避免重复导入）
        row = conn.execute("SELECT COUNT(*) as cnt FROM tasks").fetchone()
        if row["cnt"] > 0:
            return

        for task_id, summary in index.items():
            conn.execute("""
                INSERT OR IGNORE INTO tasks
                (task_id, url, title, difficulty, process_mode, status, progress,
                 message, created_at, basename, total_words,
                 total_replacements, original_duration, mixed_duration)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                summary.get("task_id", task_id),
                summary.get("url", ""),
                summary.get("title", ""),
                summary.get("difficulty", ""),
                summary.get("process_mode", "word_replace"),
                summary.get("status", ""),
                summary.get("progress", 0),
                summary.get("message", ""),
                summary.get("created_at", ""),
                summary.get("basename", ""),
                summary.get("total_words", 0),
                summary.get("total_replacements", 0),
                summary.get("original_duration", 0),
                summary.get("mixed_duration", 0),
            ))

    # 迁移成功后，重命名旧文件作为备份
    backup_path = json_path + ".bak"
    try:
        os.rename(json_path, backup_path)
        print(f"[DB] 已将 tasks_index.json 迁移到 SQLite，旧文件备份为 {backup_path}")
    except Exception:
        pass


# ============================================================
# 任务索引 CRUD
# ============================================================

def load_tasks_index() -> dict:
    """加载全部任务索引（兼容旧接口）"""
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM tasks").fetchall()
    result = {}
    for row in rows:
        task_id = row["task_id"]
        result[task_id] = dict(row)
    return result


def save_task_to_index(task_id: str, summary: dict):
    """保存/更新单条任务索引"""
    with get_db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO tasks
            (task_id, url, title, difficulty, process_mode, status, progress,
             message, created_at, basename, total_words,
             total_replacements, original_duration, mixed_duration)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task_id,
            summary.get("url", ""),
            summary.get("title", ""),
            summary.get("difficulty", ""),
            summary.get("process_mode", "word_replace"),
            summary.get("status", ""),
            summary.get("progress", 0),
            summary.get("message", ""),
            summary.get("created_at", ""),
            summary.get("basename", ""),
            summary.get("total_words", 0),
            summary.get("total_replacements", 0),
            summary.get("original_duration", 0),
            summary.get("mixed_duration", 0),
        ))


def delete_task_from_index(task_id: str):
    """删除单条任务索引"""
    with get_db() as conn:
        conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))


def get_task_from_index(task_id: str) -> dict:
    """获取单条任务索引"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
    return dict(row) if row else {}


# ============================================================
# 播客收藏 CRUD
# ============================================================

def add_favorite(title: str, author: str, image: str, rss_url: str) -> dict:
    """添加收藏"""
    with get_db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO favorites (title, author, image, rss_url)
            VALUES (?, ?, ?, ?)
        """, (title, author, image, rss_url))
        row = conn.execute(
            "SELECT * FROM favorites WHERE rss_url = ?", (rss_url,)
        ).fetchone()
    return dict(row) if row else {}


def remove_favorite(rss_url: str):
    """移除收藏"""
    with get_db() as conn:
        conn.execute("DELETE FROM favorites WHERE rss_url = ?", (rss_url,))


def get_favorites() -> list:
    """获取全部收藏"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM favorites ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def is_favorite(rss_url: str) -> bool:
    """检查是否已收藏"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM favorites WHERE rss_url = ?", (rss_url,)
        ).fetchone()
    return row is not None


# ============================================================
# RSS 订阅 CRUD
# ============================================================

def add_subscription(title: str, author: str, image: str, rss_url: str) -> dict:
    """添加 RSS 订阅"""
    with get_db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO subscriptions (title, author, image, rss_url)
            VALUES (?, ?, ?, ?)
        """, (title, author, image, rss_url))
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE rss_url = ?", (rss_url,)
        ).fetchone()
    return dict(row) if row else {}


def remove_subscription(rss_url: str):
    """移除 RSS 订阅"""
    with get_db() as conn:
        conn.execute("DELETE FROM subscriptions WHERE rss_url = ?", (rss_url,))


def get_subscriptions() -> list:
    """获取全部 RSS 订阅"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM subscriptions ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# ============================================================
# 搜索历史 CRUD
# ============================================================

def add_search_keyword(keyword: str):
    """记录搜索关键词"""
    keyword = keyword.strip()
    if not keyword:
        return
    with get_db() as conn:
        conn.execute("""
            INSERT INTO search_history (keyword, count, updated_at)
            VALUES (?, 1, datetime('now', 'localtime'))
            ON CONFLICT(keyword) DO UPDATE SET
                count = count + 1,
                updated_at = datetime('now', 'localtime')
        """, (keyword,))


def get_search_suggestions(prefix: str, limit: int = 10) -> list:
    """根据前缀获取搜索建议"""
    prefix = prefix.strip()
    if not prefix:
        # 返回最近使用的关键词
        with get_db() as conn:
            rows = conn.execute(
                "SELECT keyword FROM search_history ORDER BY updated_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [r["keyword"] for r in rows]
    with get_db() as conn:
        rows = conn.execute(
            "SELECT keyword FROM search_history WHERE keyword LIKE ? ORDER BY count DESC, updated_at DESC LIMIT ?",
            (prefix + "%", limit)
        ).fetchall()
    return [r["keyword"] for r in rows]


def clear_search_history():
    """清空搜索历史"""
    with get_db() as conn:
        conn.execute("DELETE FROM search_history")


# ============================================================
# 最近使用的播客（从任务历史中提取）
# ============================================================

def get_recent_podcasts(limit: int = 10) -> list:
    """
    从任务历史中提取最近使用过的播客源 URL 的域名分组。
    返回格式: [{url_host, last_url, last_used, task_count}]
    """
    with get_db() as conn:
        rows = conn.execute("""
            SELECT url, created_at FROM tasks
            WHERE url != '' AND status IN ('completed', 'processing', 'downloading',
                'awaiting_confirmation', 'awaiting_sentence_confirmation')
            ORDER BY created_at DESC
        """).fetchall()

    # 按域名分组
    from urllib.parse import urlparse
    host_map = {}
    for row in rows:
        url = row["url"]
        try:
            parsed = urlparse(url)
            host = parsed.netloc or parsed.hostname or "unknown"
        except Exception:
            host = "unknown"

        if host not in host_map:
            host_map[host] = {
                "url_host": host,
                "last_url": url,
                "last_used": row["created_at"],
                "task_count": 0,
            }
        host_map[host]["task_count"] += 1

    result = sorted(host_map.values(), key=lambda x: x["last_used"], reverse=True)
    return result[:limit]


# ============================================================
# 全局生词库 CRUD
# ============================================================

def add_or_update_vocabulary(words: list, task_id: str = "", task_title: str = ""):
    """
    批量添加/更新生词到全局生词库。
    words: [{"english": "...", "chinese": "...", "type": "word|phrase|idiom|collocation"}]
    如果生词已存在，更新 encounter_count、last_seen_at、source_tasks。
    """
    from core.word_frequency import get_word_level_str

    with get_db() as conn:
        for w in words:
            english = w.get("english", "").strip()
            if not english:
                continue
            chinese = w.get("chinese", "").strip()
            word_type = w.get("type", "word")
            freq_level = get_word_level_str(english) or ""

            # 查找是否已存在
            row = conn.execute(
                "SELECT id, encounter_count, source_tasks FROM vocabulary WHERE english = ?",
                (english,)
            ).fetchone()

            if row:
                # 已存在：更新 encounter_count 和 source_tasks
                existing_sources = json.loads(row["source_tasks"] or "[]")
                new_source = {"task_id": task_id, "title": task_title}
                # 避免重复添加同一任务
                if not any(s.get("task_id") == task_id for s in existing_sources):
                    existing_sources.append(new_source)
                conn.execute("""
                    UPDATE vocabulary
                    SET encounter_count = encounter_count + 1,
                        last_seen_at = datetime('now', 'localtime'),
                        source_tasks = ?,
                        chinese = CASE WHEN chinese = '' THEN ? ELSE chinese END
                    WHERE id = ?
                """, (json.dumps(existing_sources, ensure_ascii=False),
                      chinese, row["id"]))
            else:
                # 新词：插入
                source_tasks = json.dumps(
                    [{"task_id": task_id, "title": task_title}] if task_id else [],
                    ensure_ascii=False
                )
                conn.execute("""
                    INSERT INTO vocabulary
                    (english, chinese, type, frequency_level, encounter_count,
                     source_tasks, context_sentence, mastered)
                    VALUES (?, ?, ?, ?, 1, ?, '', 0)
                """, (english, chinese, word_type, freq_level, source_tasks))


def get_vocabulary(sort_by: str = "last_seen_at", sort_order: str = "desc",
                   filter_mastered: str = "all", filter_type: str = "",
                   filter_freq: str = "", search: str = "",
                   page: int = 1, page_size: int = 50) -> dict:
    """
    获取生词库列表，支持排序、筛选、搜索、分页。
    sort_by: last_seen_at | encounter_count | frequency_level | english
    filter_mastered: all | unmastered | mastered
    filter_type: word | phrase | idiom | collocation | ""(全部)
    filter_freq: "1k" | "3k" | ... | ""(全部)
    search: 搜索关键词（匹配 english 或 chinese）
    """
    allowed_sort = {"last_seen_at", "encounter_count", "frequency_level", "english", "first_seen_at"}
    if sort_by not in allowed_sort:
        sort_by = "last_seen_at"
    allowed_order = {"asc", "desc"}
    if sort_order.lower() not in allowed_order:
        sort_order = "desc"

    conditions = []
    params = []

    if filter_mastered == "unmastered":
        conditions.append("mastered = 0")
    elif filter_mastered == "mastered":
        conditions.append("mastered = 1")

    if filter_type:
        conditions.append("type = ?")
        params.append(filter_type)

    if filter_freq:
        conditions.append("frequency_level = ?")
        params.append(filter_freq)

    if search:
        conditions.append("(english LIKE ? OR chinese LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    with get_db() as conn:
        # 总数
        count_row = conn.execute(
            f"SELECT COUNT(*) as cnt FROM vocabulary WHERE {where_clause}", params
        ).fetchone()
        total = count_row["cnt"]

        # 分页数据
        offset = (page - 1) * page_size
        rows = conn.execute(
            f"""SELECT * FROM vocabulary WHERE {where_clause}
                ORDER BY {sort_by} {sort_order}
                LIMIT ? OFFSET ?""",
            params + [page_size, offset]
        ).fetchall()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "words": [dict(r) for r in rows],
    }


def toggle_vocabulary_mastered(vocab_id: int) -> dict:
    """切换生词的掌握状态"""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM vocabulary WHERE id = ?", (vocab_id,)).fetchone()
        if not row:
            return {}
        new_mastered = 0 if row["mastered"] else 1
        conn.execute("UPDATE vocabulary SET mastered = ? WHERE id = ?",
                     (new_mastered, vocab_id))
        row = conn.execute("SELECT * FROM vocabulary WHERE id = ?", (vocab_id,)).fetchone()
    return dict(row) if row else {}


def delete_vocabulary(vocab_id: int):
    """删除单个生词"""
    with get_db() as conn:
        conn.execute("DELETE FROM vocabulary WHERE id = ?", (vocab_id,))


def get_vocabulary_stats() -> dict:
    """获取生词库统计信息"""
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) as cnt FROM vocabulary").fetchone()["cnt"]
        mastered = conn.execute(
            "SELECT COUNT(*) as cnt FROM vocabulary WHERE mastered = 1"
        ).fetchone()["cnt"]
        unmastered = total - mastered

        # 按类型统计
        type_rows = conn.execute(
            "SELECT type, COUNT(*) as cnt FROM vocabulary GROUP BY type"
        ).fetchall()
        by_type = {r["type"]: r["cnt"] for r in type_rows}

        # 按频率等级统计
        freq_rows = conn.execute(
            "SELECT frequency_level, COUNT(*) as cnt FROM vocabulary "
            "WHERE frequency_level != '' GROUP BY frequency_level"
        ).fetchall()
        by_freq = {r["frequency_level"]: r["cnt"] for r in freq_rows}

        # 最近7天新增数
        recent = conn.execute(
            "SELECT COUNT(*) as cnt FROM vocabulary "
            "WHERE first_seen_at >= datetime('now', '-7 days', 'localtime')"
        ).fetchone()["cnt"]

    return {
        "total": total,
        "mastered": mastered,
        "unmastered": unmastered,
        "by_type": by_type,
        "by_frequency": by_freq,
        "recent_7days": recent,
    }


# ============================================================
# 初始化入口
# ============================================================

def setup_database():
    """数据库初始化总入口：建表 + 迁移旧数据"""
    init_db()
    migrate_from_json()
    task_count = len(load_tasks_index())
    fav_count = len(get_favorites())
    sub_count = len(get_subscriptions())
    vocab_stats = get_vocabulary_stats()
    print(f"[DB] SQLite 数据库就绪: {task_count} 条任务, {fav_count} 个收藏, "
          f"{sub_count} 个订阅, {vocab_stats['total']} 个生词")
