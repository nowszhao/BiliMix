"""Explicit service startup checks and runtime initialization."""

import os
import shutil
import sys
import threading
from core import config
from core.database import setup_database, load_tasks_index, save_task_to_index

_HW_MIN_CPU = 4  # 最低 CPU 核心数
_HW_MIN_MEM_GB = 8  # 最低内存 (GB)
_HW_MIN_DISK_GB = 15  # 最低磁盘可用空间 (GB)
_HW_REC_CPU = 8  # 推荐 CPU 核心数
_HW_REC_MEM_GB = 16  # 推荐内存 (GB)
_HW_REC_DISK_GB = 50  # 推荐磁盘可用空间 (GB)


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
            capture_output=True,
            text=True,
            timeout=15,
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
                import wave

                os.makedirs(os.path.dirname(_smoke_input), exist_ok=True)
                with wave.open(_smoke_input, "w") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(44100)
                    wf.writeframes(b"\x00\x00" * 44100)
            r = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "demucs",
                    "--two-stems",
                    "vocals",
                    "-n",
                    "htdemucs",
                    "-d",
                    "cpu",
                    "-o",
                    _smoke_outdir,
                    _smoke_input,
                ],
                capture_output=True,
                text=True,
                timeout=120,
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
            missing_hints.append("  - demucs 推理测试超时（>120s），模型加载可能卡住")
        except FileNotFoundError:
            missing_hints.append(f"  - demucs 推理测试失败：{sys.executable} 不可用")
        except Exception as e:
            missing_hints.append(f"  - demucs 推理测试异常：{str(e)[:200]}")
        finally:
            # 清理烟雾测试文件
            try:
                os.remove(_smoke_input) if "_smoke_input" in dir() and os.path.exists(
                    _smoke_input
                ) else None
                import shutil

                shutil.rmtree(
                    _smoke_outdir, ignore_errors=True
                ) if "_smoke_outdir" in dir() and os.path.exists(
                    _smoke_outdir
                ) else None
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
            print(
                f"  {sys.executable} -m pip install {' '.join(set(pip_pkgs_to_install))}"
            )
            print()
        print("完整依赖列表与说明请参考 README.md")
        print("=" * 60)
        sys.exit(1)


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
            return int(out) / (1024**3)
    except Exception:
        pass
    return -1.0


def _check_hardware_requirements():
    """启动时检测 CPU / 内存 / 磁盘，低于最低要求打印警告（不阻止启动）。"""
    cpu_count = os.cpu_count() or 1
    total_mem_gb = _get_total_memory_gb()
    disk_free_gb = -1.0
    disk_path = (
        os.path.join(config.BASE_DIR, "data")
        if os.path.isdir(os.path.join(config.BASE_DIR, "data"))
        else config.BASE_DIR
    )
    try:
        usage = shutil.disk_usage(disk_path)
        disk_free_gb = usage.free / (1024**3)
    except Exception:
        pass

    below_min = []
    if cpu_count < _HW_MIN_CPU:
        below_min.append(f"CPU 核心数: {cpu_count}（最低 {_HW_MIN_CPU}）")
    if total_mem_gb > 0 and total_mem_gb < _HW_MIN_MEM_GB:
        below_min.append(f"内存: {total_mem_gb:.1f} GB（最低 {_HW_MIN_MEM_GB} GB）")
    if disk_free_gb > 0 and disk_free_gb < _HW_MIN_DISK_GB:
        below_min.append(
            f"磁盘可用: {disk_free_gb:.1f} GB（最低 {_HW_MIN_DISK_GB} GB）"
        )

    # 打印当前硬件信息
    mem_str = f"{total_mem_gb:.1f} GB" if total_mem_gb > 0 else "未知"
    disk_str = f"{disk_free_gb:.1f} GB" if disk_free_gb > 0 else "未知"
    print(f"💻 硬件检测: CPU {cpu_count} 核 | 内存 {mem_str} | 磁盘可用 {disk_str}")
    print(
        f"   最低要求: CPU ≥ {_HW_MIN_CPU} 核 | 内存 ≥ {_HW_MIN_MEM_GB} GB | 磁盘 ≥ {_HW_MIN_DISK_GB} GB"
    )
    print(
        f"   推荐配置: CPU ≥ {_HW_REC_CPU} 核 | 内存 ≥ {_HW_REC_MEM_GB} GB | 磁盘 ≥ {_HW_REC_DISK_GB} GB"
    )

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
        print(
            "    → 建议：使用 translategemma:4b、减少 TTS Worker 数、使用 smaller WhisperX 模型"
        )
        print("=" * 60)
        print()


_startup_lock = threading.Lock()
_started = False


def start_runtime():
    """Initialize persistence and background refresh once in a single server process."""
    global _started
    with _startup_lock:
        if _started:
            return
        setup_database()
        index = load_tasks_index()
        for task_id, task in index.items():
            if task.get("status") in ("queued", "processing", "downloading"):
                save_task_to_index(
                    task_id,
                    {
                        **task,
                        "status": "error",
                        "message": "任务因服务重启而中断，请重试",
                    },
                )
        from services.podcast_api import _start_subscription_refresh_loop

        _start_subscription_refresh_loop()
        _started = True
