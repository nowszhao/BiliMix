"""
BiliMix Web App - Flask 后端
提供 REST API：提交音频URL、查询进度、获取结果、音频服务
支持：任务终止、历史任务列表、历史任务删除（含文件清理）

重构后的路由层：核心逻辑已分别提取到：
  - task_manager.py: 任务状态管理、持久化、恢复
  - podcast_service.py: 播客搜索、RSS 解析
  - config_manager.py: 配置读取/保存
"""
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
import traceback
import urllib.request

# 确保项目根目录在 sys.path 中，支持 python services/web_app.py 直接启动
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from flask import Flask, jsonify, request, send_file, send_from_directory, session, redirect

from core import config
from core.task_manager import (
    tasks, tasks_lock, cancel_flags, task_subprocesses,
    load_tasks_index,
    save_task_result_to_disk, restore_task_from_disk,
    update_task, get_task, is_cancelled,
)
from core.database import (
    setup_database, delete_task_from_index, save_task_to_index,
    load_tasks_index, get_db,
    update_episode_status_by_task,
    get_subscriptions, add_subscription, remove_subscription,  # still used by refresh in pipeline
)
from services.shared import (
    _queue_condition, _queue_waiters, _run_with_retry,
    _cleanup_intermediate_files, _kill_task_subprocesses,
    _probe_audio_duration, download_audio, generate_task_id,
    _try_resolve_local_url, _make_audio_url,
    _load_step_timing_from_disk, _load_video_result_from_disk,
    _get_queue_position,
)

# ── Blueprint imports (extracted modules) ──
from services.auth import auth_bp
from services.podcast_api import podcast_bp
from services.tools_api import tools_bp
from services.media_api import media_bp
from services.task_query import task_query_bp

from pipeline.step1_transcribe import transcribe, extract_full_text
# step2_identify_difficult_words removed (word_replace mode deleted)
from pipeline.step3_tts_confucius import synthesize_sentences_with_confucius_tts
# step4_audio_editor removed (word_replace mode deleted)
from pipeline.step2b_translate_sentences import (
    select_sentences_to_translate,
    translate_sentences,
)
from pipeline.step4b_sentence_mixer import mix_sentence_audio, build_segments_with_mixed_time
from pipeline.step_vocal_separation import separate_vocals
from pipeline.step0_video_prepare import prepare_video
from pipeline.step5_video_assemble import generate_bilingual_srt, assemble_video, _probe_video_size
from pipeline.ref_audio_utils import (
    extract_ref_audio_for_segments,
    extract_ref_audio_speaker_local,
    extract_ref_audio_speaker_global,
)

# Flask 静态文件目录使用绝对路径（web_app.py 已移至 services/ 子目录）
_web_dir = os.path.join(config.BASE_DIR, "web")
app = Flask(__name__, static_folder=_web_dir, static_url_path="")
app.secret_key = getattr(config, "SECRET_KEY", "bilimix-secret-key-change-me")

# 参考音频提取策略 dispatch map
_REF_EXTRACTORS = {
    "speaker_global": extract_ref_audio_speaker_global,
    "speaker_local": extract_ref_audio_speaker_local,
    "segment": extract_ref_audio_for_segments,
}

# ── Register extracted Blueprints ──
app.register_blueprint(auth_bp)
app.register_blueprint(podcast_bp)
app.register_blueprint(tools_bp)
app.register_blueprint(media_bp)
app.register_blueprint(task_query_bp)


# ============================================================
# 启动时依赖检测：缺失关键依赖直接退出，避免运行时静默降级
# ============================================================
def _check_required_dependencies():
    """启动时检查所有依赖（Python 包 + CLI 工具），缺失任何一项 sys.exit(1)。"""
    import importlib
    import shutil as _shutil
    import subprocess

    missing_hints = []
    pip_pkgs_to_install = []

    # ---- 1. Python 包依赖（import 检查）----
    # {模块名: pip 包名}
    py_required = [
        ("flask", "flask"),
        ("pydub", "pydub"),
        ("requests", "requests"),
        ("torch", "torch"),
        ("torchaudio", "torchaudio"),
        ("soundfile", "soundfile"),
    ]
    for mod, pkg in py_required:
        try:
            importlib.import_module(mod)
        except ImportError:
            missing_hints.append(f"  - Python 模块 {mod} (pip install {pkg})")
            pip_pkgs_to_install.append(pkg)

    # ---- 2. demucs 子进程可用性 ----
    # demucs 是通过 `python -m demucs` 调用，必须确保 sys.executable 环境内可用
    try:
        r = subprocess.run(
            [sys.executable, "-m", "demucs", "--help"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            missing_hints.append(
                f"  - demucs CLI 不可用（{sys.executable} -m demucs 失败）\n"
                f"    stderr: {r.stderr.strip()[:200]}"
            )
            pip_pkgs_to_install.append("demucs")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        missing_hints.append(
            f"  - demucs CLI 超时或无法启动（{sys.executable} -m demucs）"
        )
        pip_pkgs_to_install.append("demucs")

    # 2b. demucs 实际推理能力检查（烟雾测试）
    # --help 通过不代表模型权重已下载，用最小输入做一次快速推理验证
    if not missing_hints:
        try:
            _smoke_input = os.path.join(config.DATA_DIR, "_demucs_smoke.wav")
            _smoke_outdir = os.path.join(config.DATA_DIR, "_demucs_smoke_out")
            if not os.path.exists(_smoke_input):
                # 生成 1 秒静音 WAV 作为烟雾测试输入
                import struct, wave
                os.makedirs(os.path.dirname(_smoke_input), exist_ok=True)
                with wave.open(_smoke_input, "w") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(44100)
                    wf.writeframes(b"\x00\x00" * 44100)
            r = subprocess.run(
                [sys.executable, "-m", "demucs",
                 "--two-stems", "vocals", "-n", "htdemucs",
                 "-d", "cpu", "-o", _smoke_outdir, _smoke_input],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode != 0:
                err = r.stderr.strip()[-500:] if r.stderr else ""
                missing_hints.append(
                    f"  - demucs 推理测试失败（模型权重可能未下载或 OOM）\n"
                    f"    stderr: {err}"
                )
                pip_pkgs_to_install.append("demucs")
            else:
                # 清理烟雾测试产物
                import shutil
                shutil.rmtree(_smoke_outdir, ignore_errors=True)
        except subprocess.TimeoutExpired:
            missing_hints.append(
                "  - demucs 推理测试超时（>120s），模型加载可能卡住"
            )
        except FileNotFoundError:
            missing_hints.append(
                f"  - demucs 推理测试失败：{sys.executable} 不可用"
            )
        except Exception as e:
            missing_hints.append(
                f"  - demucs 推理测试异常：{str(e)[:200]}"
            )
        finally:
            # 清理烟雾测试文件
            try:
                os.remove(_smoke_input) if '_smoke_input' in dir() and os.path.exists(_smoke_input) else None
                import shutil
                shutil.rmtree(_smoke_outdir, ignore_errors=True) if '_smoke_outdir' in dir() and os.path.exists(_smoke_outdir) else None
            except Exception:
                pass

    # ---- 3. CLI 工具（PATH 中的可执行文件）----
    cli_tools = [
        ("ffmpeg", "音视频转码/字幕烧录/人声分离", "brew install ffmpeg"),
        ("yt-dlp", "YouTube 视频下载", "pip install yt-dlp"),
    ]
    for bin_name, desc, install_hint in cli_tools:
        if not _shutil.which(bin_name):
            missing_hints.append(
                f"  - {bin_name}（{desc}）不在 PATH\n    安装：{install_hint}"
            )

    # ---- 4. WhisperX 二进制路径检查 ----
    whisperx_bin = getattr(config, "WHISPERX_BIN", "")
    if whisperx_bin:
        bin_cmd = whisperx_bin if isinstance(whisperx_bin, list) else [whisperx_bin]
        if bin_cmd and not _shutil.which(bin_cmd[0]) and not os.path.isfile(bin_cmd[0]):
            missing_hints.append(
                f"  - WhisperX 二进制不存在: {bin_cmd[0]}\n"
                f"    配置项 WHISPERX_BIN={whisperx_bin}\n"
                f"    安装：pip install whisperx （会安装 whisperx 命令行）"
            )
    else:
        missing_hints.append(
            "  - WhisperX 二进制未配置（config.WHISPERX_BIN）\n"
            "    安装：pip install whisperx"
        )

    # ---- 4b. Confucius4-TTS root & python ----
    confucius_root = getattr(config, "CONFUCIUS4_TTS_ROOT", "")
    if confucius_root:
        if not os.path.isdir(confucius_root):
            missing_hints.append(
                f"  - Confucius4-TTS 目录不存在: {confucius_root}\n"
                f"    配置项 CONFUCIUS4_TTS_ROOT\n"
                f"    安装：git clone https://github.com/nowszhao/Confucius4-TTS-CPU.git ../Confucius4-TTS-CPU"
            )
    else:
        _default_root = os.path.join(config.BASE_DIR, "..", "Confucius4-TTS-CPU")
        if not os.path.isdir(_default_root):
            missing_hints.append(
                "  - Confucius4-TTS 目录未配置（CONFUCIUS4_TTS_ROOT 为空且默认路径不存在）\n"
                "    默认路径: " + os.path.abspath(_default_root) + "\n"
                "    安装：git clone https://github.com/nowszhao/Confucius4-TTS-CPU.git ../Confucius4-TTS-CPU"
            )

    tts_python = getattr(config, "CONFUCIUS4_TTS_PYTHON", "")
    tts_python_bin = tts_python or sys.executable
    if not os.path.isfile(tts_python_bin):
        missing_hints.append(
            f"  - TTS Python 解释器不存在: {tts_python_bin}\n"
            f"    配置项 CONFUCIUS4_TTS_PYTHON（留空则使用当前解释器 sys.executable）"
        )

    worker_script = os.path.join(config.BASE_DIR, "workers", "confucius_tts_worker.py")
    if not os.path.isfile(worker_script):
        missing_hints.append(f"  - TTS worker 脚本缺失: {worker_script}")

    # ---- 5. Ollama 服务（LLM 翻译依赖）----
    ollama_url = getattr(config, "OLLAMA_BASE_URL", "http://localhost:11434")
    try:
        import urllib.request as _urllib_req
        _urllib_req.urlopen(f"{ollama_url}/api/tags", timeout=3)
    except Exception as e:
        missing_hints.append(
            f"  - Ollama 服务不可达 ({ollama_url})\n"
            f"    错误：{str(e)[:100]}\n"
            f"    启动：ollama serve"
        )

    if missing_hints:
        print("=" * 60)
        print("❌ 启动失败：缺少以下依赖：")
        print()
        for h in missing_hints:
            print(h)
        print()
        if pip_pkgs_to_install:
            print("安装缺失的 Python 包：")
            print(f"  {sys.executable} -m pip install {' '.join(set(pip_pkgs_to_install))}")
            print()
        print("完整依赖列表与说明请参考 README.md")
        print("=" * 60)
        sys.exit(1)


# ============================================================
# 启动时硬件资源检测：CPU / 内存 / 磁盘低于最低要求则给出明确警告
# ============================================================
_HW_MIN_CPU = 4           # 最低 CPU 核心数
_HW_MIN_MEM_GB = 8        # 最低内存 (GB)
_HW_MIN_DISK_GB = 15      # 最低磁盘可用空间 (GB)
_HW_REC_CPU = 8           # 推荐 CPU 核心数
_HW_REC_MEM_GB = 16       # 推荐内存 (GB)
_HW_REC_DISK_GB = 50      # 推荐磁盘可用空间 (GB)


def _get_total_memory_gb() -> float:
    """跨平台获取系统总内存（GB）。"""
    import platform
    try:
        if platform.system() == "Linux":
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        # 格式: "MemTotal:       16384000 kB"
                        kb = int(line.split()[1])
                        return kb / (1024 * 1024)
        elif platform.system() == "Darwin":
            import subprocess
            out = subprocess.check_output(
                ["sysctl", "-n", "hw.memsize"], text=True, timeout=5
            ).strip()
            return int(out) / (1024 ** 3)
    except Exception:
        pass
    return -1.0


def _check_hardware_requirements():
    """启动时检测 CPU / 内存 / 磁盘，低于最低要求打印警告（不阻止启动）。"""
    cpu_count = os.cpu_count() or 1
    total_mem_gb = _get_total_memory_gb()
    disk_free_gb = -1.0
    disk_path = os.path.join(config.BASE_DIR, "data") if os.path.isdir(
        os.path.join(config.BASE_DIR, "data")
    ) else config.BASE_DIR
    try:
        usage = shutil.disk_usage(disk_path)
        disk_free_gb = usage.free / (1024 ** 3)
    except Exception:
        pass

    below_min = []
    if cpu_count < _HW_MIN_CPU:
        below_min.append(f"CPU 核心数: {cpu_count}（最低 {_HW_MIN_CPU}）")
    if total_mem_gb > 0 and total_mem_gb < _HW_MIN_MEM_GB:
        below_min.append(f"内存: {total_mem_gb:.1f} GB（最低 {_HW_MIN_MEM_GB} GB）")
    if disk_free_gb > 0 and disk_free_gb < _HW_MIN_DISK_GB:
        below_min.append(f"磁盘可用: {disk_free_gb:.1f} GB（最低 {_HW_MIN_DISK_GB} GB）")

    # 打印当前硬件信息
    mem_str = f"{total_mem_gb:.1f} GB" if total_mem_gb > 0 else "未知"
    disk_str = f"{disk_free_gb:.1f} GB" if disk_free_gb > 0 else "未知"
    print(f"💻 硬件检测: CPU {cpu_count} 核 | 内存 {mem_str} | 磁盘可用 {disk_str}")
    print(f"   最低要求: CPU ≥ {_HW_MIN_CPU} 核 | 内存 ≥ {_HW_MIN_MEM_GB} GB | 磁盘 ≥ {_HW_MIN_DISK_GB} GB")
    print(f"   推荐配置: CPU ≥ {_HW_REC_CPU} 核 | 内存 ≥ {_HW_REC_MEM_GB} GB | 磁盘 ≥ {_HW_REC_DISK_GB} GB")

    if below_min:
        print()
        print("=" * 60)
        print("⚠️  硬件资源低于最低要求，运行可能出现问题：")
        for item in below_min:
            print(f"  - {item}")
        print()
        print("   仍可尝试启动，但以下环节可能因资源不足而失败：")
        print("    • Ollama 翻译模型 (translategemma:4b 约需 3-4 GB 内存)")
        print("    • Confucius4-TTS 合成 (每个 Worker 约需 2-4 GB 内存)")
        print("    • WhisperX 转录 (large 模型约需 3-4 GB 内存)")
        print("    → 建议：使用 translategemma:4b、减少 TTS Worker 数、使用 smaller WhisperX 模型")
        print("=" * 60)
        print()


_check_required_dependencies()
_check_hardware_requirements()


# ============================================================
# 订阅自动刷新（后台线程）
# ============================================================
_last_refresh_time: float = 0.0  # 上次刷新时间戳
_refresh_lock = threading.Lock()  # 防止并发刷新


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
        # 首次延迟 30s，等数据库和服务就绪
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


# 全局任务队列：保证同一时间只有一个任务在运行，后续任务按提交顺序排队（FIFO）


# ============================================================
# 核心处理流程（在后台线程中执行）
# ============================================================


# ============================================================
# 处理模式：word_replace（生词替换）
# ============================================================

def process_audio_sentence_mode(task_id: str, audio_path: str):
    """句子翻译模式前半段：转录 → 翻译 → 暂停等待确认"""
    try:
        basename = os.path.splitext(os.path.basename(audio_path))[0]
        # 追加 task_id 短前缀，确保同一文件/URL 被多次处理时 result_dir 唯一
        basename = f"{basename}_{task_id[:8]}"
        result_dir = os.path.join(config.RESULT_DIR, basename)
        os.makedirs(result_dir, exist_ok=True)
        update_task(task_id, _basename=basename, _audio_path=audio_path)

        # ---- Step 0: 人声/背景音分离（始终执行）----
        # 无论 keep_bgm 为何值，都需要分离纯人声用于 TTS 参考音频。
        # keep_bgm 仅控制最终混音时是否叠加 BGM 轨。
        task_pre = get_task(task_id) or {}
        keep_bgm = task_pre.get("keep_bgm", False)
        transcribe_audio_path = audio_path
        vocals_path = ""
        bgm_path = None

        update_task(task_id, status="processing", step="separate",
                    progress=3, message="正在分离人声与背景音...")
        sep_cache_dir = os.path.join(result_dir, "vocal_separation")
        sep_result = separate_vocals(audio_path, sep_cache_dir)
        if sep_result.get("ok"):
            vocals_path = sep_result.get("vocals_path", "")
            bgm_path = sep_result.get("no_vocals_path", "")
            if vocals_path and os.path.exists(vocals_path):
                transcribe_audio_path = vocals_path
                print(f"[VocalSep] 分离成功，转录将使用纯人声音频: {vocals_path}")
            update_task(task_id, _vocals_path=vocals_path)
            if keep_bgm and bgm_path:
                update_task(task_id, _bgm_path=bgm_path)
        else:
            print(f"[VocalSep] 分离失败，转录使用原始音频: {sep_result.get('error')}")

        # ---- Step 1 & 2: 转录 + 翻译（可被外部字幕跳过） ----
        if is_cancelled(task_id):
            raise InterruptedError("任务已被用户终止")

        # 检查是否有外部字幕文件
        subtitle_path = task_pre.get("_subtitle_path", "")
        external_subtitle_used = False

        if subtitle_path and os.path.isfile(subtitle_path):
            print(f"[Subtitle] 检测到外部字幕文件: {subtitle_path}")
            from pipeline.subtitle_parser import parse_ass_subtitle
            parsed_segments, parsed_translations, parsed_indices = (
                parse_ass_subtitle(subtitle_path))

            if parsed_segments and parsed_translations:
                print(f"[Subtitle] 解析成功: {len(parsed_segments)} 段, "
                      f"{len(parsed_indices)} 条双语，跳过转录和翻译")
                segments = parsed_segments
                translations = parsed_translations
                translated_indices = parsed_indices
                full_text = " ".join(s["text"] for s in segments)
                external_subtitle_used = True
                serialized_segments = [{"text": s.get("text", "").strip(),
                                        "start": s.get("start", 0),
                                        "end": s.get("end", 0),
                                        "speaker": s.get("speaker", "")} for s in segments]
                update_task(task_id, status="processing", step="synthesize",
                            progress=30,
                            message="Step 3/4: 使用外部字幕，跳过转录和翻译...",
                            segments=serialized_segments,
                            translations=translations,
                            translated_indices=translated_indices,
                            transcription_text=full_text)
                save_task_result_to_disk(result_dir, {
                    "task_id": task_id, "status": "processing",
                    "process_mode": "sentence_translate",
                    "transcription_text": full_text,
                    "segments": serialized_segments,
                    "translations": translations,
                    "translated_indices": translated_indices,
                })
            else:
                print(f"[Subtitle] 外部字幕解析失败，回退到正常转录流程")

        if not external_subtitle_used:
            # ---- Step 1: 转录 ----
            update_task(task_id, status="processing", step="transcribe",
                        progress=5, message="Step 1/4: 正在转录音频...")

            transcription = transcribe(transcribe_audio_path, output_dir=result_dir)
            full_text = extract_full_text(transcription)
            segments = transcription.get("segments", [])

        if not external_subtitle_used:
            if is_cancelled(task_id):
                raise InterruptedError("任务已被用户终止")

            serialized_segments = [{"text": s.get("text", "").strip(),
                                    "start": s.get("start", 0),
                                    "end": s.get("end", 0),
                                    "speaker": s.get("speaker", "")} for s in segments]
            update_task(task_id, progress=20,
                        message=f"转录完成: {len(segments)} 个句子",
                        transcription_text=full_text, segments=serialized_segments)

            # ---- Step 2: 选择并翻译句子 ----
            if is_cancelled(task_id):
                raise InterruptedError("任务已被用户终止")
            update_task(task_id, step="translate", progress=25,
                        message="Step 2/4: 正在翻译句子...")

            ratio = getattr(config, "SENTENCE_CN_RATIO", 1.0)
            translated_indices = select_sentences_to_translate(serialized_segments, ratio)

            if not translated_indices:
                update_task(task_id, status="completed", progress=100,
                            step="done", message="没有需要翻译的句子。")
                return

            update_task(task_id, progress=30,
                        message=f"将翻译 {len(translated_indices)}/{len(segments)} 个句子 "
                                f"(比例: {ratio*100:.0f}%)")

            def _translate_progress(batch_idx, total_batches):
                task_cur = get_task(task_id)
                if task_cur and task_cur.get("step") != "translate":
                    return
                pct = 30 + int((batch_idx / max(total_batches, 1)) * 25)
                update_task(task_id, step="translate", progress=pct,
                            message=f"Step 2/4: 翻译批次 ({batch_idx+1}/{total_batches})")

            def _cancel_check():
                return is_cancelled(task_id)

            def _translate_checkpoint(batch_idx, trans):
                """每批翻译完成后更新内存断点 + 落盘到 task_result.json"""
                update_task(task_id, _checkpoint_translate_batch=batch_idx,
                            _checkpoint_translations=trans)
                save_task_result_to_disk(result_dir, {
                    "task_id": task_id, "status": "processing",
                    "process_mode": "sentence_translate",
                    "_checkpoint_translate_batch": batch_idx,
                    "_checkpoint_translations": trans,
                })

            _task = get_task(task_id)
            resume_tl_batch = int(_task.get("_checkpoint_translate_batch", 0))
            resume_tl_trans = _task.get("_checkpoint_translations", None)
            if resume_tl_trans:
                resume_tl_trans = {int(k): v for k, v in resume_tl_trans.items()}

            translations = _run_with_retry(
                translate_sentences,
                serialized_segments, translated_indices,
                cancel_check=_cancel_check, progress_cb=_translate_progress,
                resume_batch=resume_tl_batch,
                existing_translations=resume_tl_trans,
                checkpoint_cb=_translate_checkpoint,
                name="句子翻译")

            update_task(task_id, _checkpoint_translate_batch=0, _checkpoint_translations=None)

            if is_cancelled(task_id):
                raise InterruptedError("任务已被用户终止")
            update_task(task_id, progress=55,
                        message=f"翻译完成: {len(translations)}/{len(translated_indices)} 个句子")

            # 翻译结果立刻落盘，kill 后断点续传能跳过翻译步骤
            save_task_result_to_disk(result_dir, {
                "task_id": task_id, "status": "processing",
                "process_mode": "sentence_translate",
                "transcription_text": full_text,
                "segments": serialized_segments,
                "translations": translations,
                "translated_indices": translated_indices,
            })

        # ---- 确认翻译环节 ----
        task = get_task(task_id)
        skip_confirm = task.get("skip_confirmation",
                                getattr(config, "SKIP_CONFIRMATION", True))

        if skip_confirm:
            print(f"[Sentence] 任务 {task_id[:8]}... 跳过确认，自动继续处理")
            update_task(task_id, translations=translations,
                        translated_indices=translated_indices,
                        _raw_segments=segments,
                        status="processing", step="synthesize", progress=58,
                        message=f"自动确认 {len(translations)} 个翻译，继续处理...")
            continue_after_sentence_confirmation(task_id)
        else:
            update_task(task_id, status="awaiting_sentence_confirmation",
                        step="confirm_sentence", progress=55,
                        message=f"已翻译 {len(translations)} 个句子，请确认后继续",
                        translations=translations,
                        translated_indices=translated_indices,
                        _raw_segments=segments)
            save_task_result_to_disk(result_dir, {
                "task_id": task_id,
                "status": "awaiting_sentence_confirmation",
                "process_mode": "sentence_translate",
                "transcription_text": full_text,
                "segments": serialized_segments,
                "translations": translations,
                "translated_indices": translated_indices,
            })
            print(f"[Sentence] 任务 {task_id[:8]}... 暂停等待用户确认翻译")

    except InterruptedError:
        update_task(task_id, status="cancelled", message="任务已被终止")
    except Exception as e:
        traceback.print_exc()
        task = get_task(task_id) or {}
        update_task(task_id, status="error", message=f"处理出错: {str(e)}",
                    _failed_step=task.get("step", "translate"))
    finally:
        task_subprocesses.pop(task_id, None)


# ============================================================
# 句子翻译/智能翻译的共享后半段
# ============================================================

def _ensure_vocals_for_tts(task_id: str) -> str:
    """
    确保存在纯人声音频用于 TTS 参考音频提取。

    无论 keep_bgm 为何值、无论任务是否续跑，TTS 声音克隆都需要
    纯人声参考音频，否则原始音频中的 BGM 会被 Confucius4-TTS
    的声音克隆学进去。

    若已有 _vocals_path 则直接复用；否则执行 demucs 分离。

    Returns:
        str: vocals.wav 路径（纯人声），失败时返回原始 audio_path
    """
    task = get_task(task_id)
    if not task:
        return ""

    audio_path = task.get("_audio_path", "")
    if not audio_path:
        return ""

    # 已有缓存直接复用
    vocals_path = task.get("_vocals_path", "")
    if vocals_path and os.path.exists(vocals_path):
        return vocals_path

    # 执行 demucs 分离
    basename = task.get("_basename", "")
    result_dir = os.path.join(config.RESULT_DIR, basename)
    sep_cache_dir = os.path.join(result_dir, "vocal_separation")

    print(f"[VocalSep] keep_bgm={task.get('keep_bgm', False)}，"
          f"vocals_path 缺失，执行 demucs 分离...")
    sep_result = separate_vocals(audio_path, sep_cache_dir)
    if sep_result.get("ok"):
        vocals_path = sep_result.get("vocals_path", "")
        update_task(task_id, _vocals_path=vocals_path)
        print(f"[VocalSep] 分离成功: vocals={vocals_path}")
        # 如果 keep_bgm=True，同时保存 bgm_path 用于最终混音
        if task.get("keep_bgm", False):
            bgm_path = sep_result.get("no_vocals_path", "")
            if bgm_path:
                update_task(task_id, _bgm_path=bgm_path)
        return vocals_path

    print(f"[VocalSep] 分离失败: {sep_result.get('error', '未知错误')}，"
          f"回退到原始音频（可能含 BGM）")
    return audio_path


def _build_confucius_ref_map(task_id: str, segments: list,
                              translated_indices: list) -> dict:
    """
    为 Confucius4-TTS 构建参考音频映射。

    先从 _ensure_vocals_for_tts 获取纯人声，再调用参考音频提取。
    """
    task = get_task(task_id)
    if not task:
        return {}

    voice_clone = getattr(config, "SENTENCE_TTS_VOICE_CLONE", True)
    if not voice_clone:
        return {}

    ref_audio_source = _ensure_vocals_for_tts(task_id)
    if not ref_audio_source:
        return {}

    basename = task.get("_basename", "")
    result_dir = os.path.join(config.RESULT_DIR, basename)
    confucius_ref_dir = os.path.join(result_dir, "tts_confucius_cache", "ref_audio")
    os.makedirs(confucius_ref_dir, exist_ok=True)

    pseudo_replacements = [
        {"segment_index": idx}
        for idx in translated_indices if idx < len(segments)
    ]

    ref_mode = task.get("_ref_select_mode", "") or getattr(config, "REF_SELECT_MODE", "speaker_local")
    extractor = _REF_EXTRACTORS.get(ref_mode, extract_ref_audio_for_segments)
    ref_map, _, _ = extractor(
        ref_audio_source, segments, pseudo_replacements,
        confucius_ref_dir)


    print(f"[Confucius] 提取了 {len(ref_map)} 个参考音频 (mode={ref_mode})")
    return ref_map


def continue_after_sentence_confirmation(task_id: str):
    """句子翻译模式后半段：TTS 合成 → 音频组装 → 完成"""
    try:
        task = get_task(task_id)
        if not task:
            return

        audio_path = task.get("_audio_path", "")
        basename = task.get("_basename", "")
        translations = task.get("translations", {})
        translated_indices = task.get("translated_indices", [])
        # 优先使用完整 _raw_segments（含 speaker 等字段，声音克隆需 speaker 做说话人匹配）；
        # _raw_segments 缺失时（如断点续传从磁盘恢复）回退到精简 segments
        segments = task.get("_raw_segments") or task.get("segments", [])

        full_text = task.get("transcription_text", "")
        result_dir = os.path.join(config.RESULT_DIR, basename)
        os.makedirs(result_dir, exist_ok=True)

        # 确保 TTS 参考音频使用纯人声（不受 keep_bgm 开关影响）
        ref_audio_source = _ensure_vocals_for_tts(task_id) or audio_path

        translations = {int(k): v for k, v in translations.items()}
        translated_indices = sorted([idx for idx in translated_indices
                                     if idx in translations])

        # 翻译已就绪，落盘以便 kill 后断点续传跳过翻译步骤
        mode = "sentence_translate"
        save_task_result_to_disk(result_dir, {
            "task_id": task_id, "status": "processing",
            "process_mode": "sentence_translate",
            "transcription_text": full_text,
            "segments": segments,
            "difficult_words": task.get("difficult_words", []),
            "translations": translations,
            "translated_indices": translated_indices,
        })

        if not translations:
            update_task(task_id, status="completed", progress=100,
                        step="done", message="没有翻译内容，已完成。")
            return

        if task_id not in cancel_flags:
            cancel_flags[task_id] = threading.Event()

        # ---- Step 3: TTS 合成 ----
        if is_cancelled(task_id):
            raise InterruptedError("任务已被用户终止")

        voice_clone = getattr(config, "SENTENCE_TTS_VOICE_CLONE", True)
        skip_tts = getattr(config, "SKIP_TTS", False)

        update_task(task_id, status="processing", step="synthesize", progress=60,
                    message="Step 3/4: 合成中文语音..." if not skip_tts else "跳过 TTS，使用原声")

        tts_audio_map = {}

        if skip_tts:
            print(f"[TTS] SKIP_TTS=True，跳过语音合成")
        else:
            # Confucius4-TTS-CPU: 零样本多语言声音克隆
            confucius_cache_dir = os.path.join(result_dir, "tts_confucius_cache")
            confucius_ref_dir = os.path.join(confucius_cache_dir, "ref_audio")
            os.makedirs(confucius_ref_dir, exist_ok=True)

            pseudo_replacements = [
                {"segment_index": idx}
                for idx in translated_indices if idx < len(segments)
            ]
            confucius_ref_map = {}
            if voice_clone and pseudo_replacements:
                ref_mode = task.get("_ref_select_mode", "") or getattr(config, "REF_SELECT_MODE", "speaker_local")
                extractor = _REF_EXTRACTORS.get(ref_mode, extract_ref_audio_for_segments)
                confucius_ref_map, confucius_ref_source_map, _confucius_ref_text_map = extractor(
                    ref_audio_source, segments, pseudo_replacements,
                    confucius_ref_dir)
                print(f"[Confucius] 提取了 {len(confucius_ref_map)} 个参考音频 (mode={ref_mode})")

            def _confucius_progress(current, total):
                # 防止 TTS 完成后回调仍然修改 message 导致 step/message 不同步
                task_cur = get_task(task_id)
                if task_cur and task_cur.get("step") != "synthesize":
                    return
                pct = 60 + int((current / max(total, 1)) * 20)
                update_task(task_id, step="synthesize", progress=pct,
                            message=f"Step 3/4: Confucius4-TTS 句子合成 ({current}/{total})",
                            _tts_completed_count=current)

            def _confucius_cancel():
                return is_cancelled(task_id)

            tts_audio_map = synthesize_sentences_with_confucius_tts(
                segments, translated_indices, translations,
                audio_path, confucius_cache_dir,
                ref_audio_map=confucius_ref_map if voice_clone else {},
                cancel_check=_confucius_cancel, progress_cb=_confucius_progress,
                task_id=task_id)

        if is_cancelled(task_id):
            raise InterruptedError("任务已被用户终止")

        if len(translated_indices) > 0 and not tts_audio_map:
            if getattr(config, "SKIP_TTS", False):
                print(f"[TTS] SKIP_TTS=True，跳过 TTS，使用原始音频继续")
            else:
                raise RuntimeError(
                    f"TTS 合成完全失败：{len(translated_indices)} 个句子未生成任何音频，"
                    f"请检查 TTS 引擎配置和环境依赖")

        update_task(task_id, progress=80,
                    message=f"语音合成完成: {len(tts_audio_map)} 条中文语音")

        # ---- Step 4: 中英交替音频组装 ----
        update_task(task_id, step="merge", progress=82,
                    message="Step 4/4: 组装中英交替音频...")

        output_audio_path = os.path.join(
            result_dir, f"{basename}_sentence.{config.OUTPUT_FORMAT}")

        # 背景音乐保留：优先复用 Step0 提前分离好的伴奏轨（_bgm_path），
        # 避免重复调用 demucs；仅当提前分离未执行/未成功时才在此处补做一次
        bgm_path = task.get("_bgm_path", "") or None
        keep_bgm = task.get("keep_bgm", False)
        if keep_bgm and not bgm_path and audio_path and os.path.exists(audio_path):
            update_task(task_id, message="正在分离人声与背景音...")
            sep_cache_dir = os.path.join(result_dir, "vocal_separation")
            sep_result = separate_vocals(audio_path, sep_cache_dir)
            if sep_result.get("ok"):
                bgm_path = sep_result.get("no_vocals_path", "")
                print(f"[VocalSep] 背景音分离成功: {bgm_path}")
            else:
                print(f"[VocalSep] 分离失败，跳过背景音保留: {sep_result.get('error')}")
        elif bgm_path:
            print(f"[VocalSep] 复用 Step0 提前分离的背景音轨: {bgm_path}")

        mix_result = mix_sentence_audio(
            audio_path=audio_path, segments=segments,
            translated_indices=translated_indices,
            translations=translations,
            tts_audio_map=tts_audio_map,
            output_path=output_audio_path,
            bgm_path=bgm_path)

        if is_cancelled(task_id):
            raise InterruptedError("任务已被用户终止")

        # ---- 完成 ----
        result_data = {
            "basename": basename,
            "original_audio": audio_path,
            "mixed_audio": output_audio_path,
            "original_duration": mix_result["original_duration"],
            "mixed_duration": mix_result["mixed_duration"],
            "total_segments": mix_result["total_segments"],
            "translated_segments": mix_result["translated_segments"],
            "process_mode": task.get("process_mode", "sentence_translate"),
        }

        # 构建混合时间轴的 segments（start/end 替换为混合音频上的位置）
        segments_mixed = build_segments_with_mixed_time(
            segments, translations, mix_result["time_mapping"])

        sentence_pairs = []
        for seg_idx in translated_indices:
            if seg_idx in translations and seg_idx < len(segments_mixed):
                sentence_pairs.append({
                    "index": seg_idx,
                    "english": segments_mixed[seg_idx].get("text", "").strip(),
                    "chinese": translations[seg_idx],
                    "start": segments_mixed[seg_idx].get("start", 0),
                    "end": segments_mixed[seg_idx].get("end", 0),
                })

        mode = "sentence_translate"
        is_video = task.get("type") == "video"

        update_task(task_id,
                    status="processing" if is_video else "completed",
                    step="assemble" if is_video else "done",
                    progress=95 if is_video else 100,
                    message="音频合成完成，正在组装视频..." if is_video else "全部完成！",
                    result=result_data,
                    sentence_pairs=sentence_pairs,
                    time_mapping=mix_result["time_mapping"],
                    segments_mixed=segments_mixed)

        # 回写关联单集状态为已转录
        try:
            update_episode_status_by_task(task_id, "transcribed")
        except Exception:
            print(f"[WARN] 更新单集 transcribed 状态失败（非关键）")

        save_task_result_to_disk(result_dir, {
            "task_id": task_id, "status": "completed",
            "process_mode": "sentence_translate",
            "transcription_text": full_text,
            "segments": segments,
            "segments_mixed": segments_mixed,
            "difficult_words": task.get("difficult_words", []),
            "translations": translations,
            "translated_indices": translated_indices,
            "sentence_pairs": sentence_pairs,
            "result": result_data,
            "time_mapping": mix_result["time_mapping"],
            # 持久化 tts_audio_map 避免重启丢失
            "tts_audio_map": tts_audio_map,
            # 步骤耗时
            "_step_timing": task.get("_step_timing", []),
            # 视频任务：保存原始视频路径，用于「原始」标签页播放
            "_video_path": task.get("_video_path", ""),
            "_subtitle_mode": task.get("_subtitle_mode", "bilingual"),
            "_subtitle_font_size": task.get("_subtitle_font_size"),
        })

        _cleanup_intermediate_files(result_dir)

        try:
            dw = task.get("difficult_words", [])
            if dw:
                task_title = task.get("title", "") or basename
                print(f"[Vocab] 已保存 {len(dw)} 个生词到全局生词库 (任务 {task_id[:8]}...)")
        except Exception as ve:
            print(f"[Vocab] 保存生词库失败: {ve}")

    except InterruptedError:
        update_task(task_id, status="cancelled", message="任务已被终止")
    except Exception as e:
        traceback.print_exc()
        task = get_task(task_id) or {}
        update_task(task_id, status="error", message=f"处理出错: {str(e)}",
                    _failed_step=task.get("step", "synthesize"))
    finally:
        cancel_flags.pop(task_id, None)
        task_subprocesses.pop(task_id, None)


# ============================================================
# 视频后处理（Module-level，同时被正常提交流程和断点续传复用）
# ============================================================

def _run_video_post_process(task_id):
    """视频任务后处理：生成双语字幕 + 组装配音视频。

    从任务 state 读取 result + _video_path，执行 Step 5 并更新状态。
    返回 True/False 表示是否成功。
    """
    try:
        task = get_task(task_id)
        if not task:
            update_task(task_id, status="error", message="任务不存在，视频后处理失败")
            return False
        result = (task.get("result") or {})
        if not result:
            print(f"[Video] 无混音结果，跳过后处理")
            update_task(task_id, status="error", message="无混音结果，视频后处理失败")
            return False

        mode = task.get("_subtitle_mode", "bilingual")
        sub_font_size = task.get("_subtitle_font_size", None)
        if sub_font_size is not None and sub_font_size <= 0:
            sub_font_size = None  # -1/0 表示自动计算
        basename = task.get("_basename", "")
        video_path = task.get("_video_path", "")
        mixed_audio = result.get("mixed_audio", "")
        result_dir = os.path.join(config.RESULT_DIR, basename)

        if not video_path or not os.path.isfile(video_path):
            print(f"[Video] 视频文件缺失: {video_path}")
            update_task(task_id, status="error", message=f"视频文件缺失")
            return False
        if not mixed_audio or not os.path.isfile(mixed_audio):
            print(f"[Video] 混音文件缺失: {mixed_audio}")
            update_task(task_id, status="error", message=f"混音文件缺失")
            return False

        # Step 5a: 生成双语 ASS 字幕
        update_task(task_id, step="subtitle", progress=93,
                    message="正在生成字幕...")
        segments = task.get("segments", [])
        translations = task.get("translations", {})
        time_mapping = task.get("time_mapping", [])
        srt_path = os.path.join(result_dir, f"{basename}.ass")
        _, video_h = _probe_video_size(video_path)
        srt_result = generate_bilingual_srt(
            segments, translations, time_mapping, srt_path,
            subtitle_mode=mode, video_height=video_h,
            subtitle_font_size=sub_font_size)
        if not srt_result:
            print(f"[Video] 字幕生成失败")
            update_task(task_id, status="error", message="字幕生成失败")
            return False

        # Step 5b: 组装视频
        update_task(task_id, step="assemble", progress=96,
                    message="正在合成视频（可能需要几分钟）...")
        output_video = os.path.join(result_dir, f"{basename}_dubbed.mp4")
        if os.path.exists(output_video):
            try:
                os.remove(output_video)
            except Exception:
                pass
        assembled = assemble_video(
            video_path, mixed_audio, srt_path, output_video,
            time_mapping=time_mapping, segments=segments,
            translations=translations, subtitle_mode=mode,
            subtitle_font_size=sub_font_size)

        if not assembled or not os.path.exists(assembled):
            update_task(task_id, status="error",
                        message="视频组装失败，请检查 ffmpeg 和磁盘空间")
            return False

        # 存储视频结果
        video_result = {
            "video_url": f"/api/audio/{basename}/{basename}_dubbed.mp4",
            "srt_url": f"/api/audio/{basename}/{basename}.ass",
            "video_path": assembled,
            "srt_path": srt_path,
        }
        update_task(task_id, status="completed", step="done", progress=100,
                    message="视频配音完成！",
                    video_result=video_result)

        # 落盘确保重启不丢
        try:
            existing = {}
            disk_path = os.path.join(result_dir, "task_result.json")
            if os.path.exists(disk_path):
                try:
                    with open(disk_path, "r") as f:
                        existing = json.load(f)
                except Exception:
                    pass
            existing.update({
                "video_result": video_result,
                "_video_path": video_path,
                "_subtitle_mode": mode,
                "_subtitle_font_size": sub_font_size,
            })
            save_task_result_to_disk(result_dir, existing)
        except Exception as e:
            print(f"[Video] 落盘失败: {e}")

        return True

    except InterruptedError:
        update_task(task_id, status="cancelled", message="任务已被终止")
        return False
    except Exception as e:
        traceback.print_exc()
        update_task(task_id, status="error",
                    message=f"视频后处理出错: {str(e)}")
        return False


@app.route("/api/parse-subtitle", methods=["POST"])
def parse_subtitle():
    """解析上传的双语字幕文件（ASS 格式，|| 分隔英文和中文）"""
    data = request.get_json() or {}
    subtitle_path = data.get("subtitle_path", "").strip()
    if not subtitle_path or not os.path.isfile(subtitle_path):
        return jsonify({"ok": False, "error": "文件不存在"}), 400

    from pipeline.subtitle_parser import parse_ass_subtitle
    segments, translations, translated_indices = parse_ass_subtitle(subtitle_path)

    bilingual_count = len(translated_indices)
    if not segments:
        return jsonify({"ok": False, "error": "未能解析出任何字幕内容"})

    return jsonify({
        "ok": True,
        "count": len(segments),
        "bilingual_count": bilingual_count,
        "segments_preview": segments[:5],
    })


@app.route("/api/submit", methods=["POST"])
def submit_task():
    """提交音频/视频处理任务"""
    data = request.get_json()
    if not data:
        return jsonify({"error": "请提供数据"}), 400

    task_type = data.get("type", "audio")  # "audio" | "video"
    local_path = data.get("local_path", "").strip()
    audio_url = data.get("url", "").strip()
    video_url = data.get("video_url", "").strip()

    # 视频任务
    if task_type == "video":
        if not local_path and not video_url:
            return jsonify({"error": "请提供视频URL或上传本地视频文件"}), 400
        if local_path:
            if not os.path.isfile(local_path):
                return jsonify({"error": f"文件不存在: {local_path}"}), 400
            source = f"file://{local_path}"
        else:
            if not video_url.startswith(("http://", "https://")):
                return jsonify({"error": "请提供有效的HTTP/HTTPS URL"}), 400
            source = video_url

        subtitle_mode = data.get("subtitle_mode", "bilingual")
        subtitle_font_size = data.get("subtitle_font_size", -1)
    else:
        # 音频任务（兼容旧版无 type 字段）
        if not local_path and not audio_url:
            return jsonify({"error": "请提供音频URL或上传本地文件"}), 400
        if local_path:
            if not os.path.isfile(local_path):
                return jsonify({"error": f"文件不存在: {local_path}"}), 400
            source = f"file://{local_path}"
        elif not audio_url.startswith(("http://", "https://")):
            return jsonify({"error": "请提供有效的HTTP/HTTPS URL"}), 400
        else:
            source = audio_url
        subtitle_mode = "bilingual"
        subtitle_font_size = 20

    # Always use sentence_translate mode with 100% translation
    process_mode = "video" if task_type == "video" else "sentence_translate"
    print(f"[Submit] process_mode={process_mode!r} (task_type={task_type!r})")
    skip_confirmation = data.get("skip_confirmation",
                                 getattr(config, "SKIP_CONFIRMATION", True))
    title = data.get("title", "").strip()
    duration_str = data.get("duration", "").strip()

    # 解析预知时长：支持 "HH:MM:SS" / "MM:SS" / 纯秒数
    pre_duration = 0.0
    if duration_str:
        try:
            parts = [float(p) for p in duration_str.replace(",","").split(":")]
            if len(parts) == 3:
                pre_duration = parts[0] * 3600 + parts[1] * 60 + parts[2]
            elif len(parts) == 2:
                pre_duration = parts[0] * 60 + parts[1]
            else:
                pre_duration = float(duration_str)
        except ValueError:
            pass

    task_id = generate_task_id(source + str(time.time()))

    cancel_flags[task_id] = threading.Event()

    created_at = time.strftime("%Y-%m-%d %H:%M:%S")
    with tasks_lock:
        tasks[task_id] = {
            "task_id": task_id,
            "url": source,
            "title": title,
            "process_mode": process_mode,
            "type": task_type,
            "skip_confirmation": skip_confirmation,
            "status": "downloading",
            "step": "download",
            "progress": 0,
            "message": "任务已创建，准备下载..." if task_type != "video" else "准备下载视频...",
            "created_at": created_at,
            "original_duration": pre_duration,
            "transcription_text": "",
            "segments": [],
            "difficult_words": [],
            "replacements": [],
            "translations": {},
            "translated_indices": [],
            "result": None,
            "video_result": None,
            "_basename": "",
            "_audio_path": "",
            "_video_path": "",
            "_subtitle_mode": subtitle_mode,
            "_subtitle_font_size": subtitle_font_size,
            "keep_bgm": bool(data.get("keep_bgm", False)),
            "_ref_select_mode": data.get("ref_select_mode", ""),
            "_subtitle_path": data.get("subtitle_path", ""),
        }

    # 任务创建后立即持久化到 SQLite，避免进程中途退出后历史记录丢失
    try:
        save_task_to_index(task_id, {
            "task_id": task_id,
            "url": source,
            "title": title,
            "process_mode": process_mode,
            "status": "downloading",
            "progress": 0,
            "message": "任务已创建",
            "created_at": created_at,
            "basename": "",
            "total_words": 0,
            "total_replacements": 0,
            "original_duration": pre_duration,
            "mixed_duration": 0,
        })
    except Exception as e:
        print(f"[WARN] 任务创建时持久化失败: {e}")

    def worker():
        # 排队等待：同一时间只允许一个任务运行，按提交顺序 FIFO
        update_task(task_id, status="queued",
                    message="排队中，请稍候…")
        print(f"[Queue] 任务 {task_id[:8]}... 排队等待")

        with _queue_condition:
            _queue_waiters.append(task_id)

            # 等待直到成为队首（FIFO 公平排队）
            while _queue_waiters[0] != task_id:
                if is_cancelled(task_id) or not get_task(task_id):
                    print(f"[Queue] 任务 {task_id[:8]}... 排队中被取消，退出")
                    _queue_waiters.remove(task_id)
                    _queue_condition.notify_all()
                    return
                _queue_condition.wait(timeout=1.0)

        try:
            # 成为队首后再次确认任务未被取消/删除
            if is_cancelled(task_id) or not get_task(task_id):
                print(f"[Queue] 任务 {task_id[:8]}... 获取执行权后检测到已取消，退出")
                return
            update_task(task_id, status="processing",
                        message="开始处理…")
            _run_worker(task_id, source)
        finally:
            # 执行完成（或异常退出），从队列移除并通知下一个等待者
            with _queue_condition:
                if task_id in _queue_waiters:
                    _queue_waiters.remove(task_id)
                _queue_condition.notify_all()

    def _run_worker(task_id, source_url):
        # ---- 视频任务：先下载/提取视频 ----
        if task_type == "video":
            is_local = source_url.startswith("file://")
            if is_local:
                local_path = source_url[len("file://"):]
                if not os.path.isfile(local_path):
                    update_task(task_id, status="error",
                                message=f"文件不存在: {local_path}")
                    return
                video_source = local_path
            else:
                video_source = source_url

            # 视频缓存目录
            video_cache_dir = os.path.join(config.DOWNLOAD_DIR, "video_cache")
            os.makedirs(video_cache_dir, exist_ok=True)

            update_task(task_id, step="download", progress=1,
                        message="正在下载/提取视频...")
            prep = prepare_video(video_source, video_cache_dir, title=title)
            if not prep.get("ok"):
                update_task(task_id, status="error",
                            message=f"视频准备失败: {prep.get('error', '未知错误')}")
                return

            video_path = prep["video_path"]
            audio_file = prep["audio_path"]
            real_title = prep.get("title", title) or os.path.basename(video_path)
            update_task(task_id, _video_path=video_path,
                        _basename=os.path.splitext(os.path.basename(audio_file))[0],
                        _audio_path=audio_file,
                        title=real_title,
                        progress=5,
                        message=f"视频就绪: {os.path.basename(video_path)}")
            # 探测从视频中提取的音频时长
            try:
                duration = _probe_audio_duration(audio_file)
                if duration > 0:
                    update_task(task_id, original_duration=duration)
            except Exception:
                pass

            # 更新任务标题到数据库
            try:
                save_task_to_index(task_id, {
                    "task_id": task_id, "url": source_url,
                    "title": real_title, "process_mode": "video",
                    "status": "processing", "progress": 5,
                    "message": "视频准备完成",
                })
            except Exception:
                pass

            # 重新从 task 读取更新后的值
            current_task = get_task(task_id)
            if not current_task:
                return
            audio_path_for_pipeline = audio_file
        else:
            audio_path_for_pipeline = _prepare_audio(task_id, source_url)
            if not audio_path_for_pipeline:
                return

        # 执行核心管道（转录 → 翻译 → TTS → 混音）
        process_audio_sentence_mode(task_id, audio_path_for_pipeline)

        # ---- 视频任务：追加字幕和组装 ----
        if task_type == "video":
            _run_video_post_process(task_id)

    def _prepare_audio(task_id, source_url):
        """准备音频文件（下载或使用本地），返回音频路径。"""
        is_file_url = source_url.startswith("file://")
        if is_file_url:
            local_path = source_url[len("file://"):]
            if os.path.isfile(local_path):
                local_file = local_path
            else:
                update_task(task_id, status="error",
                            message=f"文件不存在: {local_path}")
                return None
        else:
            local_file = _try_resolve_local_url(source_url)

        if local_file:
            basename = os.path.splitext(os.path.basename(local_file))[0]
            audio_path = local_file
            update_task(task_id, _basename=basename, _audio_path=audio_path)
            size_mb = os.path.getsize(local_file) / (1024 * 1024)
            update_task(task_id, progress=5,
                        message=f"使用本地音频文件 ({size_mb:.1f} MB)")
            # 探测本地音频时长并立即更新到任务，便于任务列表展示
            try:
                duration = _probe_audio_duration(local_file)
                if duration > 0:
                    update_task(task_id, original_duration=duration)
            except Exception:
                pass
        else:
            basename = hashlib.md5(source_url.encode()).hexdigest()
            ext = os.path.splitext(source_url.split("?")[0])[-1] or ".mp3"
            if ext not in (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"):
                ext = ".mp3"
            audio_path = os.path.join(config.DOWNLOAD_DIR, f"{basename}{ext}")
            update_task(task_id, _basename=basename, _audio_path=audio_path)

            if os.path.exists(audio_path):
                update_task(task_id, progress=5, message="文件已存在，跳过下载")
            else:
                if not download_audio(source_url, audio_path, task_id):
                    return None
        return audio_path

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    return jsonify({"task_id": task_id, "message": "任务已提交"})


@app.route("/api/task/<task_id>/cancel", methods=["POST"])
def cancel_task(task_id):
    """终止正在运行的任务"""
    task = get_task(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404

    status = task.get("status", "")
    if status in ("completed", "error", "cancelled"):
        return jsonify({"error": f"任务已是 {status} 状态，无需终止"}), 400

    event = cancel_flags.get(task_id)
    if event:
        event.set()

    proc = task_subprocesses.get(task_id)
    if proc:
        procs = proc if isinstance(proc, list) else [proc]
        for p in procs:
            if p and p.poll() is None:
                try:
                    p.terminate()
                    p.wait(timeout=5)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass

    update_task(task_id, status="cancelled", message="任务已被终止")

    # 通知排队中的任务：当前任务被取消后，下一个可以开始执行
    with _queue_condition:
        if task_id in _queue_waiters:
            _queue_waiters.remove(task_id)
        _queue_condition.notify_all()

    return jsonify({"message": "任务终止请求已发送"})


@app.route("/api/task/<task_id>/reorder", methods=["POST"])
def reorder_queue(task_id):
    """调整排队中任务在队列中的顺序（上移/下移）"""
    data = request.get_json() or {}
    direction = data.get("direction", "up")

    with _queue_condition:
        try:
            idx = _queue_waiters.index(task_id)
        except ValueError:
            return jsonify({"error": "任务不在队列中"}), 400

        if direction == "up" and idx > 0:
            _queue_waiters[idx], _queue_waiters[idx - 1] = (
                _queue_waiters[idx - 1], _queue_waiters[idx]
            )
        elif direction == "down" and idx < len(_queue_waiters) - 1:
            _queue_waiters[idx], _queue_waiters[idx + 1] = (
                _queue_waiters[idx + 1], _queue_waiters[idx]
            )
        else:
            return jsonify({"ok": True, "message": "已在边界"})

        _queue_condition.notify_all()

    # 持久化队列顺序
    try:
        with get_db() as conn:
            for i, tid in enumerate(_queue_waiters):
                conn.execute(
                    "UPDATE tasks SET queue_order = ? WHERE task_id = ?",
                    (i, tid),
                )
    except Exception:
        pass  # 持久化失败不影响核心功能

    return jsonify({"ok": True})


@app.route("/api/task/<task_id>/confirm_sentences", methods=["POST"])
def confirm_sentences(task_id):
    """用户确认翻译后，继续执行句子翻译模式的后续流程"""
    task = get_task(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    if task.get("status") != "awaiting_sentence_confirmation":
        return jsonify({"error": f"任务状态为 {task.get('status')}，不在等待句子确认状态"}), 400

    data = request.get_json()
    if not data or "translations" not in data:
        return jsonify({"error": "请提供 translations"}), 400

    confirmed_translations = {int(k): v for k, v in data["translations"].items()}
    confirmed_indices = data.get("translated_indices", [])
    if confirmed_indices:
        confirmed_indices = [int(i) for i in confirmed_indices]
    else:
        confirmed_indices = sorted(confirmed_translations.keys())

    update_task(task_id, translations=confirmed_translations,
                translated_indices=confirmed_indices,
                status="processing", step="synthesize", progress=58,
                message=f"用户确认 {len(confirmed_translations)} 个翻译，继续处理...")

    thread = threading.Thread(target=continue_after_sentence_confirmation,
                              args=(task_id,), daemon=True)
    thread.start()
    return jsonify({"message": f"已确认 {len(confirmed_translations)} 个翻译，继续处理"})


@app.route("/api/task/<task_id>/redo", methods=["POST"])
def redo_task(task_id):
    """完整重做：清空所有处理结果，仅保留源文件，从头跑 pipeline"""
    task = get_task(task_id)
    if not task:
        task = restore_task_from_disk(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404

    source_url = task.get("url", "")
    task_type = task.get("type", "audio")
    if not source_url:
        return jsonify({"error": "任务缺少源 URL，无法重做"}), 400

    basename = task.get("_basename", "")

    # ---- 1. 清理磁盘结果目录（保留源文件） ----
    if basename:
        result_dir = os.path.join(config.RESULT_DIR, basename)
        if os.path.isdir(result_dir):
            import shutil
            # 需要保留的：原始下载的音频/视频（由 _audio_path/_video_path 指向）
            # 删除所有生成物：TTS cache、人声分离缓存、sentence mp3、字幕、结果 JSON
            keep_paths = set()
            audio_path = task.get("_audio_path", "")
            video_path = task.get("_video_path", "")
            for p in (audio_path, video_path):
                if p and os.path.exists(p):
                    keep_paths.add(os.path.realpath(p))

            for name in os.listdir(result_dir):
                full = os.path.join(result_dir, name)
                real = os.path.realpath(full)
                if real not in keep_paths:
                    try:
                        if os.path.isdir(full):
                            shutil.rmtree(full)
                        else:
                            os.remove(full)
                    except Exception:
                        pass

    # ---- 2. 清空内存 task 中的所有处理状态 ----
    reset_fields = {
        "status": "queued",
        "step": "download",
        "progress": 0,
        "message": "正在重做...",
        "original_duration": 0,
        "transcription_text": "",
        "segments": [],
        "difficult_words": [],
        "replacements": [],
        "translations": {},
        "translated_indices": [],
        "sentence_pairs": [],
        "time_mapping": [],
        "segments_mixed": [],
        "tts_audio_map": {},
        "result": None,
        "video_result": None,
    }

    # 清理所有 checkpoint 和内部键
    checkpoint_keys = ["_checkpoint_translate_batch", "_checkpoint_translations",
                       "_checkpoint_tts_idx", "_bgm_path", "_vocals_path",
                       "_saved_result_path"]
    with tasks_lock:
        t = tasks.get(task_id, {})
        for k, v in reset_fields.items():
            t[k] = v
        for k in checkpoint_keys:
            t.pop(k, None)
        tasks[task_id] = t

    # ---- 3. 更新 SQLite index ----
    try:
        save_task_to_index(task_id, {
            "task_id": task_id,
            "url": source_url,
            "title": task.get("title", ""),
            "process_mode": "sentence_translate",
            "type": task_type,
            "status": "queued",
            "progress": 0,
            "message": "正在重做...",
            "created_at": task.get("created_at", ""),
            "basename": basename,
            "total_words": 0,
            "total_replacements": 0,
            "keep_bgm": task.get("keep_bgm", False),
        })
    except Exception:
        pass

    # ---- 4. 启动重做线程（走队列，同一时间只允许一个任务运行）----
    def _redo_worker():
        update_task(task_id, status="queued", progress=0,
                    message="排队中，请稍候…")
        with _queue_condition:
            _queue_waiters.append(task_id)
            while _queue_waiters[0] != task_id:
                _queue_condition.wait(timeout=1.0)

        try:
            if is_cancelled(task_id) or not get_task(task_id):
                return
            update_task(task_id, status="processing", step="download",
                        progress=0, message="正在重做...")
            if task_type == "video":
                _do_redo_video_task(task_id, source_url)
            else:
                _do_redo_audio_task(task_id, source_url)
        finally:
            with _queue_condition:
                if task_id in _queue_waiters:
                    _queue_waiters.remove(task_id)
                _queue_condition.notify_all()
            if isinstance(task_subprocesses.get(task_id), list):
                task_subprocesses.pop(task_id, None)

    cancel_flags[task_id] = threading.Event()
    thread = threading.Thread(target=_redo_worker, daemon=True)
    thread.start()
    return jsonify({"message": "重做已启动", "task_id": task_id})


def _do_redo_audio_task(task_id, source_url):
    """重做音频任务"""
    task = get_task(task_id)
    audio_path = task.get("_audio_path", "")
    if not audio_path or not os.path.isfile(audio_path):
        # 重新下载
        if source_url.startswith("file://"):
            audio_path = source_url[len("file://"):]
        elif source_url.startswith("http"):
            update_task(task_id, status="processing", step="download", progress=5)
            from urllib.request import Request, urlopen
            save_dir = config.DOWNLOAD_DIR
            os.makedirs(save_dir, exist_ok=True)
            ext = ".mp3"
            audio_path = os.path.join(save_dir, f"{task_id[:8]}_redo{ext}")
            try:
                req = Request(source_url, headers={"User-Agent": "BiliMix/1.0"})
                with urlopen(req, timeout=300) as resp, open(audio_path, "wb") as f:
                    f.write(resp.read())
            except Exception as e:
                update_task(task_id, status="error", message=f"重做下载失败: {e}")
                return
        else:
            update_task(task_id, status="error", message="无法获取音频源")
            return
        update_task(task_id, _audio_path=audio_path)
    process_audio_sentence_mode(task_id, audio_path)


def _do_redo_video_task(task_id, source_url):
    """重做视频任务"""
    task = get_task(task_id)
    _video_path = task.get("_video_path", "")
    # 视频文件如果不在了，重新走 step0
    if not _video_path or not os.path.isfile(_video_path):
        if source_url.startswith("file://"):
            _video_path = source_url[len("file://"):]
        else:
            # YouTube
            video_cache_dir = os.path.join(config.DOWNLOAD_DIR, "video_cache")
            os.makedirs(video_cache_dir, exist_ok=True)
            from pipeline.step0_video_prepare import prepare_video
            prep = prepare_video(source_url, video_cache_dir, title=task.get("title", ""))
            if not prep.get("ok"):
                update_task(task_id, status="error", message=f"重做下载失败: {prep.get('error','')}")
                return
            _video_path = prep["video_path"]
        update_task(task_id, _video_path=_video_path)
    # 重做时始终重新从视频提取音频，避免复用旧的 _audio_path
    from pipeline.step0_video_prepare import _extract_audio
    cache_dir = os.path.join(config.DOWNLOAD_DIR, "video_cache")
    os.makedirs(cache_dir, exist_ok=True)
    audio_file = os.path.join(cache_dir, f"audio_redo_{task_id[:8]}.wav")
    if not _extract_audio(_video_path, audio_file):
        update_task(task_id, status="error", message="音频提取失败")
        return
    update_task(task_id, _audio_path=audio_file)
    process_audio_sentence_mode(task_id, audio_file)
    # 视频后处理
    task = get_task(task_id)
    if task and task.get("status") != "error":
        if task.get("translations") and task.get("translated_indices"):
            try:
                _run_video_post_process(task_id)
            except Exception as e:
                print(f"[Redo] 视频后处理失败: {e}")


@app.route("/api/task/<task_id>/retry", methods=["POST"])
def retry_task(task_id):
    """通用断点续传：检查已有数据，跳过已完成的步骤"""
    task = get_task(task_id)
    if not task:
        task = restore_task_from_disk(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404

    if task.get("status") != "error":
        return jsonify({"error": f"任务状态为 {task.get('status')}，仅 error 状态可重试"}), 400

    mode = "sentence_translate"
    audio_path = task.get("_audio_path", "")
    if not audio_path:
        source_url = task.get("url", "")
        # 优先从原始 URL 找回文件路径
        if source_url.startswith("file://"):
            candidate = source_url[len("file://"):]
            if os.path.isfile(candidate):
                audio_path = candidate
                update_task(task_id, _audio_path=audio_path)
        if not audio_path:
            basename_check = task.get("_basename", "")
            if basename_check:
                for ext in (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".mp4", ".mkv", ".mov"):
                    p = os.path.join(config.DOWNLOAD_DIR, f"{basename_check}{ext}")
                    if os.path.exists(p):
                        audio_path = p
                        update_task(task_id, _audio_path=audio_path)
                        break
                if not audio_path:
                    # 模糊匹配：去掉 basename 中的 task_id 后缀后再找
                    base_no_taskid = basename_check.rsplit("_", 1)[0]
                    for name in sorted(os.listdir(config.DOWNLOAD_DIR)):
                        if name.startswith(base_no_taskid):
                            p = os.path.join(config.DOWNLOAD_DIR, name)
                            if os.path.isfile(p):
                                audio_path = p
                                update_task(task_id, _audio_path=audio_path)
                                break
        if not audio_path and source_url and not source_url.startswith("file://"):
            # 重新下载
            update_task(task_id, status="processing", step="download", progress=0,
                        message="重新下载音频...")
            from urllib.request import Request, urlopen
            ext = os.path.splitext(source_url.split("?")[0])[-1] or ".mp3"
            if ext not in (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"):
                ext = ".mp3"
            audio_path = os.path.join(config.DOWNLOAD_DIR, f"{task_id[:16]}.retry{ext}")
            try:
                req = Request(source_url, headers={"User-Agent": "BiliMix/1.0"})
                with urlopen(req, timeout=300) as resp, open(audio_path, "wb") as f:
                    f.write(resp.read())
                update_task(task_id, _audio_path=audio_path)
            except Exception as e:
                return jsonify({"error": f"重新下载失败: {e}"}), 400
        if not audio_path:
            return jsonify({"error": "任务缺少音频路径，无法重试"}), 400

    # 尝试从 task_result.json 加载断点数据（内存恢复可能缺失）
    basename = task.get("_basename", "")
    if basename:
        result_dir = os.path.join(config.RESULT_DIR, basename)
        checkpoint_path = os.path.join(result_dir, "task_result.json")
        if os.path.exists(checkpoint_path):
            try:
                with open(checkpoint_path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                # 恢复关键数据到内存 task
                for key in ("segments", "translations", "translated_indices",
                            "sentence_pairs", "tts_audio_map",
                            "_checkpoint_translate_batch",
                            "_checkpoint_translations",
                            "_checkpoint_tts_idx"):
                    if key in saved and not task.get(key):
                        task[key] = saved[key]
                if saved.get("_checkpoint_translations"):
                    task["_checkpoint_translations"] = {
                        int(k): v for k, v
                        in saved["_checkpoint_translations"].items()
                    }
                update_task(task_id, **{k: task[k] for k in task
                                        if k.startswith("_checkpoint_")})
            except Exception as e:
                print(f"[Retry] 加载断点数据失败: {e}")

    # 判断已有数据，确定从哪步恢复
    segments = task.get("segments", [])
    translations = task.get("translations", {})
    translated_indices = task.get("translated_indices", [])
    difficult_words = task.get("difficult_words", [])

    if True:  # always sentence_translate

        if translations and translated_indices and segments:
            # 翻译已完成，直接从 TTS 合成恢复（只补缺失的 TTS 文件）
            msg = f"断点续传 — 跳过转录+翻译，从 TTS 恢复 ({len(translated_indices)} 句)"
            print(f"[Retry] {msg}")
            _retry_target = lambda: _synthesis_resume(task_id)
        elif segments:
            # 转录已完成，从翻译步骤恢复
            msg = f"断点续传 — 跳过转录，从翻译恢复 ({len(segments)} 句)"
            print(f"[Retry] {msg}")
            update_task(task_id, _failed_step="", progress=20, message=msg)

            def _segments_retry():
                process_audio_sentence_mode(task_id, audio_path)
                # 视频任务：process_audio_sentence_mode 处理完音频后，追加视频组装
                task = get_task(task_id)
                vp = task.get("_video_path", "")
                if vp and os.path.isfile(vp):
                    if task.get("translations") and task.get("translated_indices"):
                        print(f"[Retry] 视频任务，从翻译恢复后追加视频组装...")
                        _run_video_post_process(task_id)

            _retry_target = _segments_retry
        else:
            # 从头开始
            update_task(task_id, _failed_step="", progress=0,
                        message="断点续传 — 从头开始…")

            def _from_scratch_inner():
                process_audio_sentence_mode(task_id, audio_path)
                task = get_task(task_id)
                vp = task.get("_video_path", "")
                if vp and os.path.isfile(vp):
                    if task.get("translations") and task.get("translated_indices"):
                        _run_video_post_process(task_id)

            _retry_target = _from_scratch_inner
    else:
        # 从头开始（if True 的 else 和 外层 else 共享同一逻辑）
        update_task(task_id, _failed_step="", progress=0,
                    message="断点续传 — 从头开始…")

        def _from_scratch_retry():
            process_audio_sentence_mode(task_id, audio_path)
            task = get_task(task_id)
            vp = task.get("_video_path", "")
            if vp and os.path.isfile(vp):
                if task.get("translations") and task.get("translated_indices"):
                    _run_video_post_process(task_id)

        _retry_target = _from_scratch_retry

    def _retry_worker():
        update_task(task_id, status="queued", progress=0,
                    message="排队中，请稍候…")
        with _queue_condition:
            _queue_waiters.append(task_id)
            while _queue_waiters[0] != task_id:
                _queue_condition.wait(timeout=1.0)

        try:
            if is_cancelled(task_id) or not get_task(task_id):
                return
            update_task(task_id, status="processing",
                        message="开始处理…")
            _retry_target()
        finally:
            with _queue_condition:
                if task_id in _queue_waiters:
                    _queue_waiters.remove(task_id)
                _queue_condition.notify_all()
            if isinstance(task_subprocesses.get(task_id), list):
                task_subprocesses.pop(task_id, None)

    cancel_flags[task_id] = threading.Event()
    thread = threading.Thread(target=_retry_worker, daemon=True)
    thread.start()
    return jsonify({"message": "已开始断点续传"})


def _synthesis_resume(task_id):
    """断点续传：跳过转录+翻译，只补充合成缺失的 TTS 文件并重新混合"""
    task = get_task(task_id)
    if not task: return
    segments = task.get("segments", [])
    translated_indices = task.get("translated_indices", [])
    translations = task.get("translations", {})
    translations = {int(k): v for k, v in translations.items()}
    audio_path = task.get("_audio_path", "")
    basename = task.get("_basename", "")
    result_dir = os.path.join(config.RESULT_DIR, basename)

    existing_tts = task.get("tts_audio_map", {}) or {}
    missing_indices = [idx for idx in translated_indices if idx not in existing_tts]

    if not missing_indices:
        tts_audio_map = existing_tts
        print(f"[SynthesisResume] 全部 {len(tts_audio_map)} 条 TTS 已就绪，直接混合")
    else:
        missing_translations = {k: translations[k] for k in missing_indices if k in translations}
        confucius_cache_dir = os.path.join(result_dir, "tts_confucius_cache")
        os.makedirs(confucius_cache_dir, exist_ok=True)

        def _progress(current, total):
            task_cur = get_task(task_id)
            if task_cur and task_cur.get("step") != "synthesize":
                return
            update_task(task_id, step="synthesize", progress=60 + int((current / max(total, 1)) * 20),
                        message=f"补充合成 TTS ({current}/{total})")

        def _cancel(): return is_cancelled(task_id)

        update_task(task_id, status="processing", step="synthesize",
                    progress=60, message=f"补充合成 {len(missing_indices)} 条 TTS...")
        try:
            confucius_ref_map = _build_confucius_ref_map(
                task_id, segments, missing_indices)
            new_tts = synthesize_sentences_with_confucius_tts(
                segments, missing_indices, missing_translations,
                audio_path, confucius_cache_dir,
                ref_audio_map=confucius_ref_map,
                cancel_check=_cancel, progress_cb=_progress, task_id=task_id)
        except InterruptedError:
            update_task(task_id, status="cancelled", message="重试已被取消")
            return
        tts_audio_map = dict(existing_tts)
        tts_audio_map.update(new_tts)
        update_task(task_id, tts_audio_map=tts_audio_map)

    update_task(task_id, status="processing", step="merge", progress=82,
                message="重新组装音频...")
    output_path = os.path.join(result_dir, f"{basename}_sentence.{config.OUTPUT_FORMAT}")
    bgm_path = task.get("_bgm_path", "") or None
    if task.get("keep_bgm", False) and not bgm_path and audio_path and os.path.exists(audio_path):
        sep_cache_dir = os.path.join(result_dir, "vocal_separation")
        sep_result = separate_vocals(audio_path, sep_cache_dir)
        if sep_result.get("ok"):
            bgm_path = sep_result.get("no_vocals_path", "")
    mix_result = mix_sentence_audio(audio_path, segments,
                                    translated_indices, translations,
                                    tts_audio_map, output_path,
                                    bgm_path=bgm_path)
    # 更新 time_mapping（mix_sentence_audio 返回的是权威数据，不使用 task 副本中可能过期的值）
    time_mapping = mix_result.get("time_mapping", [])

    # 如果是视频任务，继��执行视频组装（Step 5）
    video_path = task.get("_video_path", "")
    video_result_data = None
    if video_path and os.path.isfile(video_path):
        try:
            mode = task.get("_subtitle_mode", "bilingual")
            sub_font_size = task.get("_subtitle_font_size", None)
            if sub_font_size is not None and sub_font_size <= 0:
                sub_font_size = None  # -1/0 表示自动计算
            srt_path = os.path.join(result_dir, f"{basename}.ass")
            _, video_h = _probe_video_size(video_path)

            update_task(task_id, step="subtitle", progress=93,
                        message="断点续传: 正在生成字幕...")
            srt_result = generate_bilingual_srt(
                segments, translations, time_mapping, srt_path,
                subtitle_mode=mode, video_height=video_h,
                subtitle_font_size=sub_font_size)
            if srt_result:
                update_task(task_id, step="assemble", progress=96,
                            message="断点续传: 正在合成视频...")
                output_video = os.path.join(result_dir, f"{basename}_dubbed.mp4")
                if os.path.exists(output_video):
                    try:
                        os.remove(output_video)
                    except Exception:
                        pass
                assembled = assemble_video(
                    video_path, output_path, srt_path, output_video,
                    time_mapping=time_mapping, segments=segments,
                    translations=translations, subtitle_mode=mode,
                    subtitle_font_size=sub_font_size)
                if assembled and os.path.exists(assembled):
                    video_result_data = {
                        "video_url": f"/api/audio/{basename}/{basename}_dubbed.mp4",
                        "srt_url": f"/api/audio/{basename}/{basename}.ass",
                        "video_path": assembled,
                        "srt_path": srt_path,
                    }
                    print(f"[SynthesisResume] 视频组装完成: {assembled}")
                else:
                    print(f"[SynthesisResume] 视频组装失败，仅音频已就绪")
            else:
                print(f"[SynthesisResume] 字幕生成失败，跳过视频组装")
        except Exception as e:
            traceback.print_exc()
            print(f"[SynthesisResume] 视频组装异常: {e}")

    # 完成
    result_data = {
        "basename": basename,
        "original_audio": audio_path,
        "mixed_audio": output_path,
        "original_duration": mix_result["original_duration"],
        "mixed_duration": mix_result["mixed_duration"],
        "total_segments": len(segments),
        "translated_segments": len(translated_indices),
    }
    update_task(task_id, status="completed", progress=100,
                step="done", message="断点续传完成!",
                result=result_data, sentence_pairs=task.get("sentence_pairs", []),
                video_result=video_result_data)
    disk_data = {
        **result_data, "task_id": task_id,
        "status": "completed", "segments": segments,
        "translations": translations,
        "translated_indices": translated_indices,
        "process_mode": "sentence_translate",
        "tts_audio_map": task.get("tts_audio_map", {}),
        "_step_timing": task.get("_step_timing", []),
        "_subtitle_mode": task.get("_subtitle_mode", "bilingual"),
        "_subtitle_font_size": task.get("_subtitle_font_size"),
    }
    if video_result_data:
        disk_data["video_result"] = video_result_data
        disk_data["_video_path"] = video_path
    save_task_result_to_disk(result_dir, disk_data)
    save_task_to_index(task_id, {"status": "completed", "progress": 100,
                       "step": "done", "message": "断点续传完成!"})


@app.route("/api/task/<task_id>/retry-synthesis", methods=["POST"])
def retry_sentence_synthesis(task_id):
    """TTS 合成失败后手动重试"""
    task = get_task(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404

    status = task.get("status", "")
    if status not in ("error",):
        return jsonify({"error": f"任务状态为 {status}，仅 error 状态可重试合成"}), 400

    segments = task.get("segments", [])
    translated_indices = task.get("translated_indices", [])
    translations = task.get("translations", {})
    translations = {int(k): v for k, v in translations.items()}
    audio_path = task.get("_audio_path", "")
    basename = task.get("_basename", "")

    if not segments or not translated_indices or not translations:
        return jsonify({"error": "任务数据不完整，无法重试"}), 400

    # 找出已有 TTS 和缺失 TTS 的 segment
    existing_tts = task.get("tts_audio_map", {}) or {}
    missing_indices = [idx for idx in translated_indices
                       if idx not in existing_tts]

    if not missing_indices:
        # 全部已有 TTS，可直接混合
        tts_audio_map = existing_tts
    else:
        # 只合成缺失的部分
        result_dir = os.path.join(config.RESULT_DIR, basename)
        confucius_cache_dir = os.path.join(result_dir, "tts_confucius_cache")
        os.makedirs(confucius_cache_dir, exist_ok=True)

        missing_translations = {k: translations[k] for k in missing_indices
                                if k in translations}

        update_task(task_id, status="processing", step="retry_synthesis",
                    progress=60, message=f"重新合成 {len(missing_indices)} 条 TTS...")

        def _retry_progress(current, total):
            task_cur = get_task(task_id)
            if task_cur and task_cur.get("step") != "retry_synthesis":
                return
            pct = 60 + int((current / max(total, 1)) * 20)
            update_task(task_id, step="retry_synthesis", progress=pct,
                        message=f"TTS 重试合成 ({current}/{total})")

        def _retry_cancel():
            return is_cancelled(task_id)

        try:
            new_tts = synthesize_sentences_with_confucius_tts(
                segments, missing_indices, missing_translations,
                audio_path, confucius_cache_dir,
                ref_audio_map={},
                cancel_check=_retry_cancel, progress_cb=_retry_progress,
                task_id=task_id)
        except InterruptedError:
            update_task(task_id, status="cancelled", message="重试已被取消")
            return jsonify({"message": "重试已取消"})

        # 合并新旧 TTS
        tts_audio_map = dict(existing_tts)
        tts_audio_map.update(new_tts)
        update_task(task_id, tts_audio_map=tts_audio_map)

    # 执行音频混合
    update_task(task_id, step="retry_merge", progress=82,
                message="重新混合音频...")

    output_audio_path = os.path.join(
        config.RESULT_DIR, basename,
        f"{basename}_sentence.{config.OUTPUT_FORMAT}")

    bgm_path = task.get("_bgm_path", "") or None
    if task.get("keep_bgm", False) and not bgm_path and audio_path and os.path.exists(audio_path):
        result_dir_bgm = os.path.join(config.RESULT_DIR, basename)
        sep_cache_dir = os.path.join(result_dir_bgm, "vocal_separation")
        sep_result = separate_vocals(audio_path, sep_cache_dir)
        if sep_result.get("ok"):
            bgm_path = sep_result.get("no_vocals_path", "")

    mix_result = mix_sentence_audio(
        audio_path=audio_path, segments=segments,
        translated_indices=translated_indices,
        translations=translations,
        tts_audio_map=tts_audio_map,
        output_path=output_audio_path,
        bgm_path=bgm_path)

    # 如果是视频任务，执行视频组装（与 _synthesis_resume 保持一致）
    result_dir = os.path.join(config.RESULT_DIR, basename)
    time_mapping = mix_result.get("time_mapping", [])
    video_path = task.get("_video_path", "")
    video_result_data = None
    if video_path and os.path.isfile(video_path):
        try:
            mode = task.get("_subtitle_mode", "bilingual")
            sub_font_size = task.get("_subtitle_font_size", None)
            if sub_font_size is not None and sub_font_size <= 0:
                sub_font_size = None  # -1/0 表示自动计算
            srt_path = os.path.join(result_dir, f"{basename}.ass")
            _, video_h = _probe_video_size(video_path)

            update_task(task_id, step="subtitle", progress=93,
                        message="重试: 正在生成字幕...")
            srt_result = generate_bilingual_srt(
                segments, translations, time_mapping, srt_path,
                subtitle_mode=mode, video_height=video_h,
                subtitle_font_size=sub_font_size)
            if srt_result:
                update_task(task_id, step="assemble", progress=96,
                            message="重试: 正在合成视频...")
                output_video = os.path.join(result_dir, f"{basename}_dubbed.mp4")
                if os.path.exists(output_video):
                    try:
                        os.remove(output_video)
                    except Exception:
                        pass
                assembled = assemble_video(
                    video_path, output_audio_path, srt_path, output_video,
                    time_mapping=time_mapping, segments=segments,
                    translations=translations, subtitle_mode=mode,
                    subtitle_font_size=sub_font_size)
                if assembled and os.path.exists(assembled):
                    video_result_data = {
                        "video_url": f"/api/audio/{basename}/{basename}_dubbed.mp4",
                        "srt_url": f"/api/audio/{basename}/{basename}.ass",
                        "video_path": assembled,
                        "srt_path": srt_path,
                    }
                    print(f"[RetrySynthesis] 视频组装完成: {assembled}")
                else:
                    print(f"[RetrySynthesis] 视频组装失败，仅音频已就绪")
            else:
                print(f"[RetrySynthesis] 字幕生成失败，跳过视频组装")
        except Exception as e:
            traceback.print_exc()
            print(f"[RetrySynthesis] 视频组装异常: {e}")

    # 构建完整结果
    full_text = task.get("transcription_text", "")
    result_data = {
        "basename": basename,
        "original_audio": audio_path,
        "mixed_audio": output_audio_path,
        "original_duration": mix_result["original_duration"],
        "mixed_duration": mix_result["mixed_duration"],
        "total_segments": mix_result["total_segments"],
        "translated_segments": mix_result["translated_segments"],
        "process_mode": task.get("process_mode", "sentence_translate"),
    }

    # 构建混合时间轴的 segments
    segments_mixed = build_segments_with_mixed_time(
        segments, translations, mix_result["time_mapping"])

    sentence_pairs = []
    for seg_idx in translated_indices:
        if seg_idx in translations and seg_idx < len(segments_mixed):
            sentence_pairs.append({
                "index": seg_idx,
                "english": segments_mixed[seg_idx].get("text", "").strip(),
                "chinese": translations[seg_idx],
                "start": segments_mixed[seg_idx].get("start", 0),
                "end": segments_mixed[seg_idx].get("end", 0),
            })

    update_task(task_id, status="completed", step="done", progress=100,
                message="全部完成！", result=result_data,
                sentence_pairs=sentence_pairs,
                time_mapping=mix_result["time_mapping"],
                segments_mixed=segments_mixed,
                tts_audio_map=tts_audio_map,
                video_result=video_result_data)

    disk_data = {
        "task_id": task_id, "status": "completed",
        "process_mode": task.get("process_mode", "sentence_translate"),
        "transcription_text": full_text,
        "segments": segments,
        "segments_mixed": segments_mixed,
        "translations": translations,
        "translated_indices": translated_indices,
        "sentence_pairs": sentence_pairs,
        "result": result_data,
        "time_mapping": mix_result["time_mapping"],
        "tts_audio_map": tts_audio_map,
        "_step_timing": task.get("_step_timing", []),
        "_video_path": task.get("_video_path", ""),
        "_subtitle_mode": task.get("_subtitle_mode", "bilingual"),
        "_subtitle_font_size": task.get("_subtitle_font_size"),
    }
    if video_result_data:
        disk_data["video_result"] = video_result_data
    save_task_result_to_disk(result_dir, disk_data)

    _cleanup_intermediate_files(result_dir)

    return jsonify({"message": "重试完成", "result": result_data})


# ============================================================
# API 路由 — 任务查询与管理
# ============================================================


# ============================================================
# API 索引 — 暴露全部接口供 Agent 调用
# ============================================================


# ============================================================
# 启动
# ============================================================

if __name__ == "__main__":
    os.makedirs(_web_dir, exist_ok=True)
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)

    # 抑制 Flask 轮询类 API 的请求日志（如 /api/task/<id> 每 1.5s 轮询一次）
    import logging
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.WARNING)

    # 初始化 SQLite 数据库（建表 + 迁移旧 JSON 数据）
    setup_database()
    _index = load_tasks_index()
    # 服务重启时将进行中的任务标记为中断，保留在历史记录中
    orphaned = 0
    for _tid, _t in _index.items():
        if _t.get("status") in ("queued", "processing", "downloading"):
            save_task_to_index(_tid, {
                **{k: v for k, v in _t.items()},
                "status": "error",
                "message": "任务因服务重启而中断，请重试",
                "progress": _t.get("progress", 0),
            })
            orphaned += 1
    if orphaned:
        print(f"🧹 已标记 {orphaned} 个中断任务（服务重启导致）")
    # 支持命令行传入端口号，默认 5000
    _port = int(sys.argv[1]) if len(sys.argv) > 1 else 5000

    print(f"📋 已加载 {len(_index)} 条历史任务记录")
    print("=" * 50)
    print("🎧 BiliMix Web App")
    print(f"📡 http://localhost:{_port}")
    print("=" * 50)
    app.run(host="0.0.0.0", port=_port, debug=False, threaded=True)
