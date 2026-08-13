#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BiliMix CLI — 面向 AI Agent 的命令行接口

将 BiliMix Web App 的全部 REST API 包装为结构化、可脚本化的 CLI。
默认输出 JSON 到 stdout，便于 AI Agent / 管道 / jq 直接消费。

环境变量:
  BILIMIX_SERVER  服务地址 (默认 http://localhost:5000)
  BILIMIX_HOME    配置目录 (默认 ~/.bilimix)

退出码:
  0  成功
  1  API 错误 (HTTP 4xx/5xx)
  2  未认证 (401)
  3  请求超时
  4  网络错误 (无法连接)
  5  缺少依赖

示例:
  bmx auth login --username admin --password xxx
  bmx task submit --url https://x.com/ep.mp3 --mode sentence_translate --wait
  bmx task result <task_id> --field result.mixed_audio
  bmx audio download --task-id <id> --type mixed -o out.mp3
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    sys.stderr.write("错误: 缺少依赖 requests，请运行 pip install requests\n")
    sys.exit(5)


# ============================================================
# 常量
# ============================================================

DEFAULT_SERVER = "http://localhost:5000"
SESSION_DIR = Path(os.environ.get("BILIMIX_HOME", str(Path.home() / ".bilimix")))
SESSION_FILE = SESSION_DIR / "session.json"
CONFIG_FILE = SESSION_DIR / "config.json"

EXIT_OK = 0
EXIT_API_ERROR = 1
EXIT_AUTH = 2
EXIT_TIMEOUT = 3
EXIT_NETWORK = 4
EXIT_DEP = 5

# 视为「终态」的任务状态：达到后停止轮询
TERMINAL_STATUSES = {
    "completed",
    "error",
    "cancelled",
    "awaiting_confirmation",
    "awaiting_sentence_confirmation",
}


# ============================================================
# 异常与 HTTP 客户端
# ============================================================

class CLIError(Exception):
    """携带退出码的业务异常"""

    def __init__(self, message, code=EXIT_API_ERROR):
        super().__init__(message)
        self.message = message
        self.code = code


# ---- server 地址持久化 ----
# 优先级: 命令行 --server > 环境变量 BILIMIX_SERVER > 配置文件 > 默认值

def _load_saved_server():
    """从配置文件读取上次保存的 server 地址"""
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            return cfg.get("server")
    except Exception:
        pass
    return None


def _save_server(server):
    """将 server 地址保存到配置文件，后续命令自动复用"""
    try:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        cfg = {}
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception:
                cfg = {}
        cfg["server"] = server.rstrip("/")
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # 保存失败不阻塞，仅下次需重新指定


def _resolve_server(cli_server):
    """按优先级解析 server 地址: 命令行 > 环境变量 > 配置文件 > 默认"""
    if cli_server:
        _save_server(cli_server)
        return cli_server.rstrip("/")
    env_server = os.environ.get("BILIMIX_SERVER")
    if env_server:
        return env_server.rstrip("/")
    saved = _load_saved_server()
    if saved:
        return saved.rstrip("/")
    return DEFAULT_SERVER


class BiliMixClient:
    """BiliMix Web API 客户端，自动管理 session cookie"""

    def __init__(self, server, quiet=False):
        self.server = server.rstrip("/")
        self.quiet = quiet
        self.session = requests.Session()
        self._load_cookies()

    # ---- cookie 持久化 ----
    def _load_cookies(self):
        if SESSION_FILE.exists():
            try:
                with open(SESSION_FILE, "r", encoding="utf-8") as f:
                    cookies = json.load(f)
                for name, value in cookies.items():
                    self.session.cookies.set(name, value)
            except Exception:
                pass

    def _save_cookies(self):
        try:
            SESSION_DIR.mkdir(parents=True, exist_ok=True)
            cookies = {c.name: c.value for c in self.session.cookies}
            with open(SESSION_FILE, "w", encoding="utf-8") as f:
                json.dump(cookies, f)
        except Exception as e:
            if not self.quiet:
                sys.stderr.write(f"[warn] 无法保存 session: {e}\n")

    def _clear_cookies(self):
        self.session.cookies.clear()
        try:
            if SESSION_FILE.exists():
                SESSION_FILE.unlink()
        except Exception:
            pass

    # ---- 请求 ----
    def request(self, method, path, **kwargs):
        """发送请求，返回解析后的 JSON 或原始 Response（非 JSON 响应）"""
        url = path if path.startswith("http") else f"{self.server}{path}"
        timeout = kwargs.pop("timeout", 120)
        try:
            resp = self.session.request(method, url, timeout=timeout, **kwargs)
        except requests.exceptions.ConnectionError as e:
            raise CLIError(f"无法连接到服务器 {self.server}: {e}", EXIT_NETWORK)
        except requests.exceptions.Timeout as e:
            raise CLIError(f"请求超时: {e}", EXIT_TIMEOUT)

        if resp.status_code == 401:
            body = self._safe_json(resp)
            raise CLIError(
                body.get("error", "未登录") if isinstance(body, dict) else "未登录",
                EXIT_AUTH,
            )
        if resp.status_code >= 400:
            body = self._safe_json(resp)
            if isinstance(body, dict):
                msg = body.get("error", f"HTTP {resp.status_code}")
            else:
                msg = f"HTTP {resp.status_code}"
            raise CLIError(msg, EXIT_API_ERROR)

        self._save_cookies()

        if not resp.content:
            return {}
        content_type = resp.headers.get("Content-Type", "")
        if "application/json" in content_type:
            return resp.json()
        return resp  # 二进制响应

    @staticmethod
    def _safe_json(resp):
        try:
            return resp.json()
        except Exception:
            return {}

    def get(self, path, params=None, **kw):
        return self.request("GET", path, params=params, **kw)

    def post_json(self, path, body=None, **kw):
        return self.request("POST", path, json=body or {}, **kw)

    def delete_json(self, path, body=None, **kw):
        return self.request("DELETE", path, json=body or {}, **kw)

    def patch_json(self, path, body=None, **kw):
        return self.request("PATCH", path, json=body or {}, **kw)

    def get_raw(self, path, params=None, stream=False, **kw):
        """获取原始 Response（用于流式下载二进制文件）"""
        url = path if path.startswith("http") else f"{self.server}{path}"
        try:
            resp = self.session.get(url, params=params, stream=stream,
                                    timeout=600, **kw)
        except requests.exceptions.ConnectionError as e:
            raise CLIError(f"无法连接到服务器: {e}", EXIT_NETWORK)
        except requests.exceptions.Timeout as e:
            raise CLIError(f"请求超时: {e}", EXIT_TIMEOUT)
        if resp.status_code == 401:
            raise CLIError("未登录", EXIT_AUTH)
        if resp.status_code >= 400:
            raise CLIError(f"HTTP {resp.status_code}", EXIT_API_ERROR)
        return resp


# ============================================================
# 输出工具
# ============================================================

def _extract_field(obj, field):
    """按点号路径提取嵌套字段，支持数组索引"""
    value = obj
    for part in field.split("."):
        if value is None:
            return None
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list):
            try:
                value = value[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return value


def emit(obj, pretty=False, field=None):
    """输出 JSON / 字段提取 / 标量"""
    if field is not None:
        value = _extract_field(obj, field)
        if isinstance(value, (dict, list)):
            print(json.dumps(value, ensure_ascii=False, indent=2 if pretty else None))
        elif value is None:
            print("null")
        else:
            print(value)
        return
    if isinstance(obj, (dict, list)):
        print(json.dumps(obj, ensure_ascii=False, indent=2 if pretty else None))
    elif obj is None:
        print("null")
    else:
        print(obj)


def log_err(msg):
    sys.stderr.write(f"{msg}\n")


# ============================================================
# auth 命令
# ============================================================

def cmd_auth_login(args, client):
    body = {"username": args.username, "password": args.password}
    result = client.post_json("/api/login", body)
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


def cmd_auth_logout(args, client):
    try:
        client.post_json("/api/logout")
    except CLIError:
        pass
    client._clear_cookies()
    emit({"ok": True, "message": "已退出登录并清除本地 session"},
         pretty=args.pretty, field=args.field)
    return EXIT_OK


def cmd_auth_status(args, client):
    result = client.get("/api/auth/check")
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


# ============================================================
# task 命令
# ============================================================

def cmd_task_submit(args, client):
    body = {}
    task_type = getattr(args, "type", "audio")

    if task_type == "video":
        body["type"] = "video"
        if args.video_url:
            body["video_url"] = args.video_url
        elif args.server_path:
            body["local_path"] = args.server_path
        elif args.local_path:
            body["local_path"] = args.local_path
        else:
            raise CLIError("必须提供 --video-url、--server-path 或 --local-path")
        if hasattr(args, 'subtitle_mode') and args.subtitle_mode:
            body["subtitle_mode"] = args.subtitle_mode
        if hasattr(args, 'subtitle_font_size') and args.subtitle_font_size:
            body["subtitle_font_size"] = args.subtitle_font_size
    else:
        if args.local_path:
            body["local_path"] = args.local_path
        elif args.url:
            body["url"] = args.url
        else:
            raise CLIError("必须提供 --url 或 --local-path")

    # difficulty removed
    pass  # process_mode fixed to sentence_translate
    if args.title:
        body["title"] = args.title

    # subtitle_path: 外部双语字幕文件（ASS，|| 分隔英文和中文）。
    # 提供后跳过转录和翻译，直接用字幕生成配音音频/视频。
    if getattr(args, 'subtitle_path', None):
        body["subtitle_path"] = args.subtitle_path

    # ref_select_mode: 参考音频选取模式（声音克隆）
    if getattr(args, 'ref_select_mode', None):
        body["ref_select_mode"] = args.ref_select_mode

    # keep_bgm: 保留原音频背景音乐
    if getattr(args, 'keep_bgm', False):
        body["keep_bgm"] = True

    # duration: 预知时长
    if getattr(args, 'duration', None):
        body["duration"] = args.duration

    # skip_confirmation: 默认 None(不传，服务端用默认 True)
    if args.skip_confirm:
        body["skip_confirmation"] = True
    elif args.no_skip_confirm:
        body["skip_confirmation"] = False

    result = client.post_json("/api/submit", body)
    task_id = result.get("task_id")

    if args.wait and task_id:
        return _wait_task(client, task_id, args, until_terminal=True)

    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


def _wait_task(client, task_id, args, until_terminal=False, until_status=None):
    """轮询任务状态，进度打 stderr，最终结果打 stdout"""
    interval = getattr(args, "poll_interval", 2.0) or 2.0
    last_progress = -1
    while True:
        try:
            status = client.get(f"/api/task/{task_id}")
        except CLIError as e:
            log_err(f"[poll] 查询失败: {e.message}")
            time.sleep(interval)
            continue

        st = status.get("status", "")
        prog = status.get("progress", 0)
        msg = status.get("message", "")
        if prog != last_progress and not args.quiet:
            sys.stderr.write(f"\r[task] {prog:3d}% {st} — {msg}" + " " * 10)
            sys.stderr.flush()
            last_progress = prog

        if until_status and st == until_status:
            if not args.quiet:
                sys.stderr.write("\n")
            result = client.get(f"/api/task/{task_id}/result")
            emit(result, pretty=args.pretty, field=args.field)
            return EXIT_OK

        if until_terminal and st in TERMINAL_STATUSES:
            if not args.quiet:
                sys.stderr.write("\n")
            result = client.get(f"/api/task/{task_id}/result")
            emit(result, pretty=args.pretty, field=args.field)
            if st == "completed":
                return EXIT_OK
            if st in ("error", "cancelled"):
                return EXIT_API_ERROR
            # awaiting_* 视为正常暂停，退出 0 让 Agent 继续确认流程
            return EXIT_OK

        time.sleep(interval)


def cmd_task_wait(args, client):
    if args.until:
        return _wait_task(client, args.task_id, args, until_status=args.until)
    return _wait_task(client, args.task_id, args, until_terminal=True)


def cmd_task_list(args, client):
    params = {}
    if getattr(args, 'limit', None):
        params['limit'] = args.limit
    result = client.get("/api/tasks", params=params if params else None)
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


def cmd_task_status(args, client):
    result = client.get(f"/api/task/{args.task_id}")
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


def cmd_task_result(args, client):
    result = client.get(f"/api/task/{args.task_id}/result")
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


def cmd_task_cancel(args, client):
    result = client.post_json(f"/api/task/{args.task_id}/cancel")
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


def _load_json_arg(value, file_path, name):
    """从字符串或文件加载 JSON"""
    if file_path:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    if value:
        try:
            return json.loads(value)
        except json.JSONDecodeError as e:
            raise CLIError(f"无效的 {name} JSON: {e}")
    raise CLIError(f"必须提供 --{name} 或 --{name}-file")


def cmd_task_confirm_sentences(args, client):
    translations = _load_json_arg(args.translations,
                                  args.translations_file, "translations")
    body = {"translations": translations}
    if args.indices:
        try:
            body["translated_indices"] = json.loads(args.indices)
        except json.JSONDecodeError as e:
            raise CLIError(f"无效的 indices JSON: {e}")
    result = client.post_json(f"/api/task/{args.task_id}/confirm_sentences", body)
    if args.wait:
        return _wait_task(client, args.task_id, args, until_terminal=True)
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


def cmd_task_retry(args, client):
    result = client.post_json(f"/api/task/{args.task_id}/retry")
    if args.wait:
        return _wait_task(client, args.task_id, args, until_terminal=True)
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


def cmd_task_retry_synthesis(args, client):
    result = client.post_json(f"/api/task/{args.task_id}/retry-synthesis")
    if args.wait:
        return _wait_task(client, args.task_id, args, until_terminal=True)
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


def cmd_task_reorder(args, client):
    result = client.post_json(f"/api/task/{args.task_id}/reorder",
                              {"direction": args.direction})
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


def cmd_task_delete(args, client):
    result = client.delete_json(f"/api/task/{args.task_id}")
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


def cmd_task_redo(args, client):
    result = client.post_json(f"/api/task/{args.task_id}/redo")
    if args.wait and "task_id" in result:
        return _wait_task(client, result["task_id"], args, until_terminal=True)
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


# ============================================================
# audio 命令
# ============================================================

def _resolve_audio_path(client, task_id, audio_type):
    """从任务结果解析音频文件的 URL 路径"""
    result = client.get(f"/api/task/{task_id}/result")
    result_obj = result.get("result") or {}
    key = "mixed_audio" if audio_type == "mixed" else "original_audio"
    audio_path = result_obj.get(key)
    if not audio_path:
        raise CLIError(f"任务结果中未找到 {audio_type} 音频路径 "
                       f"(任务状态: {result.get('status')})")

    # audio_path 是服务端绝对路径，需转为 /api/audio/<relative>
    if audio_type == "original":
        # 原始音频通常在 downloads/ 下，取 basename 即可
        return f"/api/audio/{os.path.basename(audio_path)}"

    # mixed_audio 在 results/{basename}/ 下
    for marker in ("/data/results/", "\\data\\results\\"):
        if marker in audio_path:
            rel = audio_path.split(marker, 1)[1].replace("\\", "/")
            return f"/api/audio/{rel}"
    return f"/api/audio/{os.path.basename(audio_path)}"


def cmd_audio_upload(args, client):
    if not os.path.isfile(args.file):
        raise CLIError(f"文件不存在: {args.file}")
    with open(args.file, "rb") as f:
        files = {"file": (os.path.basename(args.file), f)}
        result = client.request("POST", "/api/upload", files=files)
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


def _download_file(client, path, output, label, quiet, params=None):
    """流式下载文件并显示进度，返回下载字节数"""
    resp = client.get_raw(path, params=params, stream=True)
    total = int(resp.headers.get("Content-Length", 0))
    downloaded = 0
    with open(output, "wb") as f:
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                if total and not quiet:
                    pct = int(downloaded * 100 / total)
                    sys.stderr.write(f"\r[{label}] {pct:3d}% "
                                     f"({downloaded // 1024}KB/{total // 1024}KB)")
                    sys.stderr.flush()
    if not quiet:
        sys.stderr.write(f"\n[{label}] 已保存: {output}\n")
    return downloaded


def cmd_audio_download(args, client):
    if args.path:
        path = args.path
    elif args.task_id:
        path = _resolve_audio_path(client, args.task_id, args.type)
    else:
        raise CLIError("必须提供 --path 或 --task-id")

    output = args.output or os.path.basename(path) or "audio.bin"
    downloaded = _download_file(client, path, output, label="audio", quiet=args.quiet)
    emit({"ok": True, "path": output, "size_bytes": downloaded},
         pretty=args.pretty, field=args.field)
    return EXIT_OK


def cmd_audio_url(args, client):
    if args.task_id:
        path = _resolve_audio_path(client, args.task_id, args.type)
    elif args.path:
        path = args.path
    else:
        raise CLIError("必须提供 --path 或 --task-id")
    url = f"{client.server}{path}"
    print(url)
    return EXIT_OK


# ============================================================
# video 命令
# ============================================================

def _resolve_video_path(client, task_id, mode):
    """从视频任务结果解析文件的 URL 路径

    Args:
        mode: "video" 解析 video_url, "srt" 解析 srt_url
    """
    result = client.get(f"/api/task/{task_id}/result")
    video_result = result.get("video_result")
    if not video_result:
        raise CLIError(f"任务结果中未找到视频结果（可能不是视频任务），"
                       f"任务状态: {result.get('status')}")

    key = "video_url" if mode == "video" else "srt_url"
    path = video_result.get(key)
    if not path:
        label = "视频" if mode == "video" else "字幕"
        raise CLIError(f"任务结果中未找到{label}路径")

    # video_url / srt_url 已是 /api/audio/... 格式，直接使用
    return path


def cmd_video_download(args, client):
    if args.path:
        path = args.path
    elif args.task_id:
        path = _resolve_video_path(client, args.task_id, mode="video")
    else:
        raise CLIError("必须提供 --path 或 --task-id")

    output = args.output or os.path.basename(path) or "dubbed.mp4"
    downloaded = _download_file(client, path, output, label="video", quiet=args.quiet)
    emit({"ok": True, "path": output, "size_bytes": downloaded},
         pretty=args.pretty, field=args.field)
    return EXIT_OK


def cmd_video_download_srt(args, client):
    if args.path:
        path = args.path
    elif args.task_id:
        path = _resolve_video_path(client, args.task_id, mode="srt")
    else:
        raise CLIError("必须提供 --path 或 --task-id")

    output = args.output or os.path.basename(path) or "subtitle.ass"
    downloaded = _download_file(client, path, output, label="srt", quiet=args.quiet)
    emit({"ok": True, "path": output, "size_bytes": downloaded},
         pretty=args.pretty, field=args.field)
    return EXIT_OK


# ============================================================
# subtitle 命令
# ============================================================

def cmd_subtitle_parse(args, client):
    """解析/校验服务端双语字幕文件（ASS 格式，|| 分隔英文和中文）"""
    result = client.post_json("/api/parse-subtitle",
                              {"subtitle_path": args.path})
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


# ============================================================
# file 命令
# ============================================================

def cmd_file_download(args, client):
    """通过 /api/download 以附件形式下载文件（支持自定义文件名）"""
    raw = args.path.lstrip("/")
    for prefix in ("api/download/", "api/audio/"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    params = {"name": args.name} if args.name else None
    output = args.output or os.path.basename(raw) or "download.bin"
    downloaded = _download_file(client, f"/api/download/{raw}", output,
                                label="download", quiet=args.quiet, params=params)
    emit({"ok": True, "path": output, "size_bytes": downloaded},
         pretty=args.pretty, field=args.field)
    return EXIT_OK


# ============================================================
# translate 命令
# ============================================================

def cmd_translate_word(args, client):
    body = {"english": args.english}
    if args.context:
        body["context_sentence"] = args.context
    result = client.post_json("/api/translate", body)
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


def cmd_translate_word_levels(args, client):
    if args.words:
        words = [w.strip() for w in args.words.split(",")]
    else:
        import json
        with open(args.words_file, "r", encoding="utf-8") as f:
            words = json.load(f)
    body = {"words": words}
    result = client.post_json("/api/word-levels", body)
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


# ============================================================
# podcast 命令
# ============================================================

def cmd_podcast_search(args, client):
    result = client.get("/api/podcast/search", params={"q": args.query})
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


def cmd_podcast_rss(args, client):
    result = client.get("/api/podcast/rss", params={"url": args.url})
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK



# ============================================================
# favorites 命令
# ============================================================

def _build_podcast_body(args):
    body = {"rss_url": args.rss_url}
    if getattr(args, "title", None):
        body["title"] = args.title
    if getattr(args, "author", None):
        body["author"] = args.author
    if getattr(args, "image", None):
        body["image"] = args.image
    return body


def cmd_favorites_list(args, client):
    result = client.get("/api/favorites")
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


def cmd_favorites_add(args, client):
    body = _build_podcast_body(args)
    result = client.post_json("/api/favorites", body)
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


def cmd_favorites_remove(args, client):
    result = client.delete_json("/api/favorites", {"rss_url": args.rss_url})
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


def cmd_favorites_check(args, client):
    result = client.get("/api/favorites/check", params={"rss_url": args.rss_url})
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


# ============================================================
# subscriptions 命令
# ============================================================

def cmd_subscriptions_list(args, client):
    result = client.get("/api/subscriptions")
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


def cmd_subscriptions_add(args, client):
    result = client.post_json("/api/subscriptions", _build_podcast_body(args))
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


def cmd_subscriptions_remove(args, client):
    result = client.delete_json("/api/subscriptions", {"rss_url": args.rss_url})
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


def cmd_subscriptions_refresh(args, client):
    result = client.post_json("/api/subscriptions/refresh")
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


# ============================================================
# episodes 命令
# ============================================================

def cmd_episodes_list(args, client):
    params = {"status": args.status, "time_range": args.time_range,
              "page": args.page, "page_size": args.page_size}
    if args.rss_url:
        params["rss_url"] = args.rss_url
    result = client.get("/api/episodes", params=params)
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


def cmd_episodes_stats(args, client):
    params = {"time_range": args.time_range}
    if args.rss_url:
        params["rss_url"] = args.rss_url
    result = client.get("/api/episodes/stats", params=params)
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


def cmd_episodes_update(args, client):
    result = client.patch_json(f"/api/episodes/{args.id}", {"status": args.status})
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


def cmd_episodes_mark_read(args, client):
    body = {"time_range": args.time_range}
    if args.rss_url:
        body["rss_url"] = args.rss_url
    result = client.post_json("/api/episodes/mark-all-read", body)
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


def cmd_episodes_refresh(args, client):
    result = client.post_json("/api/episodes/refresh")
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


def cmd_episodes_refresh_feed(args, client):
    from urllib.parse import quote
    result = client.post_json(f"/api/episodes/refresh/{quote(args.rss_url, safe='')}")
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


# ============================================================
# history 命令
# ============================================================

def cmd_history_suggestions(args, client):
    result = client.get("/api/search-history/suggestions", params={"q": args.q})
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


def cmd_history_add(args, client):
    result = client.post_json("/api/search-history", {"keyword": args.keyword})
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


def cmd_history_clear(args, client):
    result = client.delete_json("/api/search-history")
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


# ============================================================
# recent 命令
# ============================================================

def cmd_recent_list(args, client):
    result = client.get("/api/recent-podcasts")
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


# ============================================================
# config 命令
# ============================================================

def cmd_config_get(args, client):
    result = client.get("/api/config")
    if args.key:
        value = result.get(args.key) if isinstance(result, dict) else None
        emit(value, pretty=args.pretty, field=args.field)
    else:
        emit(result, pretty=True, field=args.field)
    return EXIT_OK


def cmd_config_set(args, client):
    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            body = json.load(f)
    else:
        if not args.pairs:
            raise CLIError("必须提供 KEY=VALUE 对或 --file")
        body = {}
        for pair in args.pairs:
            if "=" not in pair:
                raise CLIError(f"无效的键值对: {pair}，格式应为 KEY=VALUE")
            k, v = pair.split("=", 1)
            # 尝试解析为 JSON 类型 (数字/布尔/null)
            try:
                v_parsed = json.loads(v)
            except json.JSONDecodeError:
                v_parsed = v
            body[k] = v_parsed
    result = client.post_json("/api/config", body)
    emit(result, pretty=args.pretty, field=args.field)
    return EXIT_OK


# ============================================================
# api 命令
# ============================================================

def cmd_api_index(args, client):
    result = client.get("/api")
    emit(result, pretty=True, field=args.field)
    return EXIT_OK


# ============================================================
# argparse 构建
# ============================================================

def build_parser():
    # 公共选项：主 parser 和所有子命令都继承，使 --field/--pretty/--quiet/--server
    # 可出现在命令的任意位置（子命令前或后），方便 AI Agent 灵活拼接命令。
    # 使用 SUPPRESS 默认值：子命令 parser 不会覆盖主 parser 已设的值。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--server", default=argparse.SUPPRESS,
                        help="服务地址 (首次指定后会自动记住; "
                             "也支持 $BILIMIX_SERVER 环境变量)")
    common.add_argument("--pretty", action="store_true", default=argparse.SUPPRESS,
                        help="美化 JSON 输出")
    common.add_argument("-q", "--quiet", action="store_true",
                        default=argparse.SUPPRESS, help="抑制提示信息")
    common.add_argument("--field", default=argparse.SUPPRESS,
                        help="提取 JSON 嵌套字段，如 result.mixed_audio")

    parser = argparse.ArgumentParser(
        prog="bmx",
        description="BiliMix CLI — 面向 AI Agent 的命令行接口",
        epilog="环境变量: BILIMIX_SERVER (服务地址), BILIMIX_HOME (配置目录)\n"
               "示例: bmx task submit --url URL --wait",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )

    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    # 辅助函数：给每个子命令自动注入 common 选项
    def add_cmd(parent, name, **kw):
        kw.setdefault("parents", [common])
        return parent.add_parser(name, **kw)

    # ---- auth ----
    p_auth = add_cmd(sub, "auth", help="认证管理")
    auth_sub = p_auth.add_subparsers(dest="subcommand", required=True)

    p = add_cmd(auth_sub, "login", help="登录")
    p.add_argument("--username", required=True)
    p.add_argument("--password", required=True)
    p.set_defaults(func=cmd_auth_login)

    p = add_cmd(auth_sub, "logout", help="退出登录")
    p.set_defaults(func=cmd_auth_logout)

    p = add_cmd(auth_sub, "status", help="查询登录状态")
    p.set_defaults(func=cmd_auth_status)

    # ---- task ----
    p_task = add_cmd(sub, "task", help="任务管理")
    task_sub = p_task.add_subparsers(dest="subcommand", required=True)

    p = add_cmd(task_sub, "submit", help="提交音频/视频处理任务")
    p.add_argument("--type", choices=["audio", "video"], default="audio",
                   help="任务类型 (默认 audio)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--url", help="音频文件 URL")
    g.add_argument("--local-path", help="本地文件路径 (需先 audio upload)")
    g.add_argument("--video-url", help="YouTube 视频 URL (--type video)")
    g.add_argument("--server-path", help="服务端视频绝对路径 (--type video)")
    p.add_argument("--mode", choices=["sentence_translate"], help="处理模式（固定为句子翻译）")
    p.add_argument("--subtitle-mode", choices=["bilingual", "chinese_only", "none"],
                   default="bilingual", help="字幕模式 (--type video, 默认 bilingual)")
    p.add_argument("--subtitle-font-size", type=int, default=20,
                   help="字幕字号 (--type video, 默认 20)")
    g2 = p.add_mutually_exclusive_group()
    g2.add_argument("--skip-confirm", action="store_true",
                    help="跳过确认环节 (默认行为)")
    g2.add_argument("--no-skip-confirm", action="store_true",
                    help="要求人工确认")
    p.add_argument("--title", help="任务标题")
    p.add_argument("--keep-bgm", action="store_true", help="保留原音频/视频背景音乐")
    p.add_argument("--duration", help="预知时长 (如 01:23:45 或 142)")
    p.add_argument("--subtitle-path",
                   help="服务端外部双语字幕文件路径 (.ass, || 分隔英文和中文)，"
                        "提供后跳过转录和翻译，直接用字幕生成配音")
    p.add_argument("--ref-select-mode",
                   choices=["speaker_global", "speaker_local", "segment"],
                   help="参考音频选取模式 (声音克隆, 默认服务端配置)")
    p.add_argument("--wait", action="store_true", help="提交后阻塞等待完成")
    p.add_argument("--poll-interval", type=float, default=2.0,
                   help="轮询间隔秒数 (默认 2.0)")
    p.set_defaults(func=cmd_task_submit)

    p = add_cmd(task_sub, "list", help="任务列表")
    p.add_argument("--limit", type=int, help="最大返回条数")
    p.set_defaults(func=cmd_task_list)

    p = add_cmd(task_sub, "status", help="任务状态")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_task_status)

    p = add_cmd(task_sub, "result", help="任务完整结果")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_task_result)

    p = add_cmd(task_sub, "cancel", help="终止任务")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_task_cancel)

    p = add_cmd(task_sub, "confirm-sentences", help="确认句子翻译并继续")
    p.add_argument("task_id")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--translations", help="翻译映射 JSON 字符串")
    g.add_argument("--translations-file", help="翻译映射 JSON 文件")
    p.add_argument("--indices", help="translated_indices JSON 数组")
    p.add_argument("--wait", action="store_true", help="确认后等待完成")
    p.add_argument("--poll-interval", type=float, default=2.0)
    p.set_defaults(func=cmd_task_confirm_sentences)

    p = add_cmd(task_sub, "retry", help="断点续传重试")
    p.add_argument("task_id")
    p.add_argument("--wait", action="store_true", help="重试后等待完成")
    p.add_argument("--poll-interval", type=float, default=2.0)
    p.set_defaults(func=cmd_task_retry)

    p = add_cmd(task_sub, "retry-synthesis", help="重试 TTS 合成")
    p.add_argument("task_id")
    p.add_argument("--wait", action="store_true", help="重试后等待完成")
    p.add_argument("--poll-interval", type=float, default=2.0)
    p.set_defaults(func=cmd_task_retry_synthesis)

    p = add_cmd(task_sub, "reorder", help="调整排队中任务的顺序")
    p.add_argument("task_id")
    p.add_argument("--direction", choices=["up", "down"], required=True,
                   help="上移/下移")
    p.set_defaults(func=cmd_task_reorder)

    p = add_cmd(task_sub, "delete", help="删除任务及其文件")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_task_delete)

    p = add_cmd(task_sub, "redo", help="完整重做（清除产物，从头执行）")
    p.add_argument("task_id")
    p.add_argument("--wait", action="store_true", help="重做后等待完成")
    p.add_argument("--poll-interval", type=float, default=2.0)
    p.set_defaults(func=cmd_task_redo)

    p = add_cmd(task_sub, "wait", help="等待任务完成")
    p.add_argument("task_id")
    p.add_argument("--until", help="等待到指定状态 "
                                   "(如 awaiting_confirmation)")
    p.add_argument("--poll-interval", type=float, default=2.0)
    p.set_defaults(func=cmd_task_wait)

    # ---- audio ----
    p_audio = add_cmd(sub, "audio", help="音频文件")
    audio_sub = p_audio.add_subparsers(dest="subcommand", required=True)

    p = add_cmd(audio_sub, "upload", help="上传本地音频/视频/字幕文件")
    p.add_argument("file", help="本地音频/视频/字幕文件路径 (.ass/.srt)")
    p.set_defaults(func=cmd_audio_upload)

    p = add_cmd(audio_sub, "download", help="下载音频")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--task-id", help="通过任务 ID 解析音频路径")
    g.add_argument("--path", help="直接指定 URL 路径 "
                                  "(如 basename/basename_mixed.mp3)")
    p.add_argument("--type", choices=["mixed", "original"], default="mixed",
                   help="音频类型 (默认 mixed)")
    p.add_argument("-o", "--output", help="输出文件名")
    p.set_defaults(func=cmd_audio_download)

    p = add_cmd(audio_sub, "url", help="获取音频下载 URL")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--task-id")
    g.add_argument("--path")
    p.add_argument("--type", choices=["mixed", "original"], default="mixed")
    p.set_defaults(func=cmd_audio_url)

    # ---- video ----
    p_vid = add_cmd(sub, "video", help="视频文件")
    vid_sub = p_vid.add_subparsers(dest="subcommand", required=True)

    p = add_cmd(vid_sub, "download", help="下载配音视频")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--task-id", help="通过任务 ID 解析视频路径")
    g.add_argument("--path", help="直接指定 URL 路径 (如 basename/basename_dubbed.mp4)")
    p.add_argument("-o", "--output", help="输出文件名")
    p.set_defaults(func=cmd_video_download)

    p = add_cmd(vid_sub, "download-srt", help="下载字幕文件")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--task-id", help="通过任务 ID 解析 SRT 路径")
    g.add_argument("--path", help="直接指定 SRT URL 路径")
    p.add_argument("-o", "--output", help="输出文件名")
    p.set_defaults(func=cmd_video_download_srt)

    # ---- subtitle ----
    p_sub = add_cmd(sub, "subtitle", help="字幕文件")
    sub_sub = p_sub.add_subparsers(dest="subcommand", required=True)

    p = add_cmd(sub_sub, "parse", help="解析/校验服务端双语字幕文件 (ASS)")
    p.add_argument("path", help="服务端字幕文件路径 (upload 后返回的 local_path)")
    p.set_defaults(func=cmd_subtitle_parse)

    # ---- file ----
    p_file = add_cmd(sub, "file", help="通用文件下载")
    file_sub = p_file.add_subparsers(dest="subcommand", required=True)

    p = add_cmd(file_sub, "download", help="以附件形式下载结果/下载目录中的文件")
    p.add_argument("path", help="文件路径 (如 basename/basename_dubbed.mp4)")
    p.add_argument("-o", "--output", help="输出文件名")
    p.add_argument("--name", help="服务端下载文件名 (Content-Disposition)")
    p.set_defaults(func=cmd_file_download)

    # ---- translate ----
    p_tr = add_cmd(sub, "translate", help="翻译工具")
    tr_sub = p_tr.add_subparsers(dest="subcommand", required=True)

    p = add_cmd(tr_sub, "word", help="翻译单词/短语")
    p.add_argument("english", help="待翻译的英文")
    p.add_argument("--context", help="上下文句子")
    p.set_defaults(func=cmd_translate_word)

    p = add_cmd(tr_sub, "word-levels", help="查询 BNC/COCA 词频等级")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--words", help="单词列表，逗号分隔")
    g.add_argument("--words-file", help="单词 JSON 数组文件")
    p.set_defaults(func=cmd_translate_word_levels)

    # ---- podcast ----
    p_pc = add_cmd(sub, "podcast", help="播客搜索")
    pc_sub = p_pc.add_subparsers(dest="subcommand", required=True)

    p = add_cmd(pc_sub, "search", help="搜索播客")
    p.add_argument("query")
    p.set_defaults(func=cmd_podcast_search)

    p = add_cmd(pc_sub, "rss", help="解析 RSS Feed")
    p.add_argument("url")
    p.set_defaults(func=cmd_podcast_rss)

    # ---- favorites ----
    p_f = add_cmd(sub, "favorites", help="播客收藏")
    f_sub = p_f.add_subparsers(dest="subcommand", required=True)

    p = add_cmd(f_sub, "list", help="收藏列表")
    p.set_defaults(func=cmd_favorites_list)

    p = add_cmd(f_sub, "add", help="添加收藏")
    p.add_argument("--rss-url", required=True)
    p.add_argument("--title")
    p.add_argument("--author")
    p.add_argument("--image")
    p.set_defaults(func=cmd_favorites_add)

    p = add_cmd(f_sub, "remove", help="移除收藏")
    p.add_argument("--rss-url", required=True)
    p.set_defaults(func=cmd_favorites_remove)

    p = add_cmd(f_sub, "check", help="检查是否已收藏")
    p.add_argument("--rss-url", required=True)
    p.set_defaults(func=cmd_favorites_check)

    # ---- subscriptions ----
    p_s = add_cmd(sub, "subscriptions", help="RSS 订阅")
    s_sub = p_s.add_subparsers(dest="subcommand", required=True)

    p = add_cmd(s_sub, "list", help="订阅列表")
    p.set_defaults(func=cmd_subscriptions_list)

    p = add_cmd(s_sub, "add", help="添加订阅")
    p.add_argument("--rss-url", required=True)
    p.add_argument("--title")
    p.add_argument("--author")
    p.add_argument("--image")
    p.set_defaults(func=cmd_subscriptions_add)

    p = add_cmd(s_sub, "remove", help="移除订阅")
    p.add_argument("--rss-url", required=True)
    p.set_defaults(func=cmd_subscriptions_remove)

    p = add_cmd(s_sub, "refresh", help="手动刷新所有订阅")
    p.set_defaults(func=cmd_subscriptions_refresh)

    # ---- episodes ----
    p_ep = add_cmd(sub, "episodes", help="订阅单集")
    ep_sub = p_ep.add_subparsers(dest="subcommand", required=True)

    p = add_cmd(ep_sub, "list", help="单集列表")
    p.add_argument("--status", default="all",
                   choices=["all", "unread", "read", "transcribed", "dismissed"])
    p.add_argument("--time-range", default="all",
                   choices=["today", "week", "month", "all"])
    p.add_argument("--rss-url", default="")
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--page-size", type=int, default=100)
    p.set_defaults(func=cmd_episodes_list)

    p = add_cmd(ep_sub, "stats", help="单集统计")
    p.add_argument("--time-range", default="all",
                   choices=["today", "week", "month", "all"])
    p.add_argument("--rss-url", default="")
    p.set_defaults(func=cmd_episodes_stats)

    p = add_cmd(ep_sub, "update", help="更新单集状态")
    p.add_argument("id", type=int)
    p.add_argument("--status", required=True,
                   choices=["unread", "read", "transcribed", "dismissed"])
    p.set_defaults(func=cmd_episodes_update)

    p = add_cmd(ep_sub, "mark-read", help="批量已读")
    p.add_argument("--rss-url", default="")
    p.add_argument("--time-range", default="all",
                   choices=["today", "week", "month", "all"])
    p.set_defaults(func=cmd_episodes_mark_read)

    p = add_cmd(ep_sub, "refresh", help="刷新所有订阅源")
    p.set_defaults(func=cmd_episodes_refresh)

    p = add_cmd(ep_sub, "refresh-feed", help="刷新单个订阅源")
    p.add_argument("rss_url")
    p.set_defaults(func=cmd_episodes_refresh_feed)

    # ---- history ----
    p_h = add_cmd(sub, "history", help="搜索历史")
    h_sub = p_h.add_subparsers(dest="subcommand", required=True)

    p = add_cmd(h_sub, "suggestions", help="搜索建议")
    p.add_argument("q")
    p.set_defaults(func=cmd_history_suggestions)

    p = add_cmd(h_sub, "add", help="记录搜索关键词")
    p.add_argument("keyword")
    p.set_defaults(func=cmd_history_add)

    p = add_cmd(h_sub, "clear", help="清空搜索历史")
    p.set_defaults(func=cmd_history_clear)

    # ---- recent ----
    p_r = add_cmd(sub, "recent", help="最近播客")
    r_sub = p_r.add_subparsers(dest="subcommand", required=True)

    p = add_cmd(r_sub, "list", help="最近播客列表")
    p.set_defaults(func=cmd_recent_list)

    # ---- config ----
    p_c = add_cmd(sub, "config", help="系统配置")
    c_sub = p_c.add_subparsers(dest="subcommand", required=True)

    p = add_cmd(c_sub, "get", help="读取配置")
    p.add_argument("--key", help="仅取指定键")
    p.set_defaults(func=cmd_config_get)

    p = add_cmd(c_sub, "set", help="修改配置")
    p.add_argument("pairs", nargs="*", help="KEY=VALUE 对，可多个")
    p.add_argument("--file", help="从 JSON 文件批量读取")
    p.set_defaults(func=cmd_config_set)

    # ---- api ----
    p = add_cmd(sub, "api", help="API 元信息")
    p.set_defaults(func=cmd_api_index)

    return parser


# ============================================================
# 入口
# ============================================================

def main():
    parser = build_parser()
    args = parser.parse_args()

    # SUPPRESS 模式下全局选项属性可能不存在，这里统一补默认值
    args.server = getattr(args, "server", None)
    args.pretty = getattr(args, "pretty", False)
    args.quiet = getattr(args, "quiet", False)
    args.field = getattr(args, "field", None)

    # server 解析优先级: 命令行 > 环境变量 > 配置文件 > 默认
    # 首次通过 --server 指定后，地址自动保存到 ~/.bilimix/config.json，
    # 后续命令无需再传 --server
    server = _resolve_server(args.server)
    client = BiliMixClient(server, quiet=args.quiet)

    try:
        code = args.func(args, client)
    except CLIError as e:
        log_err(f"错误: {e.message}")
        return e.code
    except FileNotFoundError as e:
        log_err(f"文件不存在: {e}")
        return EXIT_API_ERROR
    except KeyboardInterrupt:
        log_err("\n已中断")
        return 130
    except Exception as e:
        log_err(f"未预期错误: {e}")
        return EXIT_API_ERROR
    return code or EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
