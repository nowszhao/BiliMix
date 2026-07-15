#!/usr/bin/env bash
# ============================================================
# BiliMix 一键安装脚本（macOS + Linux）
#
# 用法：
#   ./setup.sh
#
# 功能：
#   - 安装 Miniconda（如未安装）
#   - 创建 bilimix conda env（Python 3.10）
#   - 安装系统依赖（ffmpeg / ollama）
#   - 安装 Python 依赖（requirements.txt + whisperx + requirements-tts.txt）
#   - clone Confucius4-TTS-CPU 到 ../Confucius4-TTS-CPU
#   - 从模板生成 core/config_local.py（已存在则跳过）
#
# 幂等：可重复运行，已存在的步骤自动跳过
# ============================================================
set -euo pipefail

# ---------- 颜色 ----------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m'
info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()   { echo -e "${RED}[ERR]${NC} $*"; }

# ---------- 路径 ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
CONFUCIUS_DIR="$SCRIPT_DIR/../Confucius4-TTS-CPU"
CONFIG_LOCAL="$SCRIPT_DIR/core/config_local.py"
CONFIG_EXAMPLE="$SCRIPT_DIR/core/config_local.example.py"

# ---------- 平台检测 ----------
OS="$(uname -s)"
ARCH="$(uname -m)"
info "平台: $OS / $ARCH"

# ---------- 1. Conda 引导 ----------
install_miniconda() {
    local installer_url=""
    case "$OS/$ARCH" in
        Darwin/arm64)  installer_url="https://repo.anaconda.com/miniconda/Miniconda3-latest-MacOSX-arm64.sh" ;;
        Darwin/x86_64) installer_url="https://repo.anaconda.com/miniconda/Miniconda3-latest-MacOSX-x86_64.sh" ;;
        Linux/x86_64)  installer_url="https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh" ;;
        Linux/aarch64) installer_url="https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-aarch64.sh" ;;
        *) err "不支持的平台: $OS/$ARCH"; exit 1 ;;
    esac
    local installer_file="/tmp/miniconda-installer.sh"
    info "下载 Miniconda: $installer_url"
    curl -fsSL "$installer_url" -o "$installer_file"
    info "安装 Miniconda 到 $HOME/miniconda3 ..."
    bash "$installer_file" -b -p "$HOME/miniconda3"
    rm -f "$installer_file"
    ok "Miniconda 安装完成"
}

if ! command -v conda &>/dev/null && ! type conda &>/dev/null; then
    if [ -x "$HOME/miniconda3/bin/conda" ]; then
        info "找到已安装的 Miniconda: $HOME/miniconda3"
    else
        warn "未检测到 conda，开始安装 Miniconda"
        install_miniconda
    fi
fi

# 定位 conda.sh（conda 可能是 shell 函数而非可执行文件）
if [ -x "$HOME/miniconda3/bin/conda" ]; then
    CONDA_BASE="$("$HOME/miniconda3/bin/conda" info --base)"
elif command -v conda &>/dev/null; then
    CONDA_BASE="$(conda info --base 2>/dev/null)"
else
    # conda 是 shell 函数，通过 CONDA_EXE 定位
    if [ -n "$CONDA_EXE" ] && [ -x "$CONDA_EXE" ]; then
        CONDA_BASE="$("$CONDA_EXE" info --base)"
    else
        err "无法定位 conda，请手动运行 'conda init' 后重开终端，再运行 ./setup.sh"
        exit 1
    fi
fi

# 激活 conda
if [ -z "$CONDA_BASE" ] || [ ! -f "$CONDA_BASE/etc/profile.d/conda.sh" ]; then
    err "无法定位 conda.sh，请手动运行 'conda init' 后重开终端，再运行 ./setup.sh"
    exit 1
fi
source "$CONDA_BASE/etc/profile.d/conda.sh"
ok "conda 已激活: $CONDA_BASE"

# ---------- 1b. 网络配置 ----------
# 注意：macOS LibreSSL 与部分中文镜像存在 SSL 兼容问题
# ① 清理 conda channels（防止残留的历史配置）
conda config --remove-key channels 2>/dev/null || true
conda config --set ssl_verify true 2>/dev/null || true

export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_DEFAULT_TIMEOUT=120

# ② pip 镜像自动检测
_choose_pip_index() {
    local ts_code ts_time py_code py_time
    ts_time=$(curl -s -o /dev/null -w "%{time_total}" --max-time 5 https://pypi.tuna.tsinghua.edu.cn/simple/pip/ 2>/dev/null || echo "99")
    ts_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 https://pypi.tuna.tsinghua.edu.cn/simple/pip/ 2>/dev/null || echo "000")
    py_time=$(curl -s -o /dev/null -w "%{time_total}" --max-time 5 https://pypi.org/simple/pip/ 2>/dev/null || echo "99")
    py_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 https://pypi.org/simple/pip/ 2>/dev/null || echo "000")

    if [ "$ts_code" = "200" ] && awk "BEGIN{exit !($ts_time < $py_time * 3)}"; then
        mkdir -p "$HOME/.pip"
        cat > "$HOME/.pip/pip.conf" <<'PIPEOF'
[global]
index-url = https://pypi.tuna.tsinghua.edu.cn/simple
trusted-host = pypi.tuna.tsinghua.edu.cn
timeout = 120
retries = 5
PIPEOF
        ok "pip → 清华镜像（快于官方 ${py_time}s vs ${ts_time}s）"
    else
        rm -f "$HOME/.pip/pip.conf" 2>/dev/null
        ok "pip → 官方 PyPI（清华不可达）"
    fi
}
_choose_pip_index

# ---------- 2. 创建 bilimix env ----------
# 先清理损坏的包缓存（macOS 上 conda 包缓存损坏会导致 [Errno 35]）
conda clean -a -y 2>&1 | tail -2

if conda env list | grep -q "^bilimix\b"; then
    info "conda env 'bilimix' 已存在，跳过创建"
else
    info "创建 conda env 'bilimix' (Python 3.10)"
    conda create -y -n bilimix python=3.10
fi

conda activate bilimix
ENV_PYTHON="$(which python)"
ENV_BIN="$(dirname "$ENV_PYTHON")"
ok "激活 bilimix env: $ENV_PYTHON"

# 确保用 env 内的 pip（防止 PATH 中其他 pip 覆盖）
_pip() { "$ENV_PYTHON" -m pip "$@"; }

# ---------- 3. 系统依赖 ----------
# HuggingFace 镜像（中国大陆访问 huggingface.co 常被墙）
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
export HF_HUB_ENABLE_HF_TRANSFER=0

if [ "$OS" = "Darwin" ]; then
    if ! command -v brew &>/dev/null; then
        warn "未检测到 Homebrew，请先安装：https://brew.sh"
        warn "安装后重新运行 ./setup.sh"
        exit 1
    fi
    command -v ffmpeg &>/dev/null || brew install ffmpeg
    command -v ollama &>/dev/null || brew install ollama
else
    # Linux
    if ! command -v ffmpeg &>/dev/null; then
        info "安装 ffmpeg ..."
        sudo apt-get update && sudo apt-get install -y ffmpeg
    fi
    if ! command -v ollama &>/dev/null; then
        info "安装 Ollama ..."
        curl -fsSL https://ollama.com/install.sh | sh
    fi
fi
ok "系统依赖就绪（ffmpeg / ollama）"

# ---------- 验证 ffmpeg 带 libx264 编码器 ----------
if ! ffmpeg -encoders 2>/dev/null | grep -q libx264; then
    warn "ffmpeg 缺少 libx264 视频编码器（视频组装可能超时或失败）"
    if [ "$OS_TYPE" = "macos" ]; then
        info "macOS: brew install ffmpeg 默认包含 libx264，无需额外操作"
    else
        info "Linux: 请安装带 libx264 的 ffmpeg — conda install -c conda-forge ffmpeg"
    fi
fi

# ---------- 4. 主 Python 依赖 ----------
info "安装主 Python 依赖 (requirements.txt) ..."
_pip install -r requirements.txt
ok "主依赖安装完成"

# ---------- 5. WhisperX ----------
info "安装 WhisperX ..."
_pip install whisperx
ok "WhisperX 安装完成"

# ---------- 6. torch 对齐 ----------
info "对齐 torch/torchaudio 版本到 2.7.0（与 Confucius4-TTS-CPU 对齐）..."
if [ "$OS" = "Linux" ] && [ "$ARCH" = "x86_64" ]; then
    _pip install --upgrade torch==2.7.0 torchaudio==2.7.0 --index-url https://download.pytorch.org/whl/cpu
else
    _pip install --upgrade torch==2.7.0 torchaudio==2.7.0
fi
ok "torch 版本对齐完成"

# ---------- 7. TTS 依赖 ----------
info "安装 TTS 依赖 (requirements-tts.txt) ..."
_pip install -r requirements-tts.txt
ok "TTS 依赖安装完成"

# ---------- 8. clone Confucius4-TTS-CPU ----------
if [ -d "$CONFUCIUS_DIR/.git" ]; then
    info "Confucius4-TTS-CPU 已存在，跳过 clone"
else
    info "clone Confucius4-TTS-CPU 到 $CONFUCIUS_DIR ..."
    git clone https://github.com/nowszhao/Confucius4-TTS-CPU.git "$CONFUCIUS_DIR"
fi
ok "Confucius4-TTS-CPU 就绪"

# ---------- 9. 脚手架 core/config_local.py ----------
if [ -f "$CONFIG_LOCAL" ]; then
    warn "core/config_local.py 已存在，跳过生成（如需重置请先删除该文件）"
else
    if [ ! -f "$CONFIG_EXAMPLE" ]; then
        err "模板文件不存在: $CONFIG_EXAMPLE"
        exit 1
    fi
    info "从模板生成 core/config_local.py ..."
    cp "$CONFIG_EXAMPLE" "$CONFIG_LOCAL"
    # 替换占位符
    CONFUCIUS_ABS="$(cd "$CONFUCIUS_DIR" && pwd)"
    sed -i.bak "s|__BILIMIX_PYTHON__|$ENV_PYTHON|g" "$CONFIG_LOCAL"
    sed -i.bak "s|__CONFUCIUS_ROOT__|$CONFUCIUS_ABS|g" "$CONFIG_LOCAL"
    rm -f "$CONFIG_LOCAL.bak"
    ok "core/config_local.py 生成完成"
fi

# ---------- 10. 验证 ----------
info "验证关键依赖可 import ..."
python -c "import flask, torch, torchaudio, demucs, soundfile, transformers, pydub" || {
    err "依赖验证失败，请检查上方日志"
    exit 1
}
ok "依赖验证通过"

# ---------- 10b. 持久化环境变量到 shell rc（便于后续启动）----------
persist_env_to_shell() {
    local rc_file=""
    case "$SHELL" in
        */zsh)  rc_file="$HOME/.zshrc" ;;
        */bash) rc_file="$HOME/.bashrc" ;;
        *) rc_file="$HOME/.profile" ;;
    esac
    [ -z "$rc_file" ] && return
    touch "$rc_file"

    # HuggingFace 镜像（中国大陆必需）
    if ! grep -q "HF_ENDPOINT" "$rc_file" 2>/dev/null; then
        echo '' >> "$rc_file"
        echo '# BiliMix 环境变量' >> "$rc_file"
        echo 'export HF_ENDPOINT=https://hf-mirror.com' >> "$rc_file"
        echo "已写入 HF_ENDPOINT 到 $rc_file"
    fi
}
if [ -n "${PIP_INDEX_URL:-}" ]; then
    info "持久化镜像配置到 shell rc ..."
    persist_env_to_shell
fi

# ---------- 11. 后续步骤提示 ----------
echo ""
echo -e "${GREEN}============================================================${NC}"
echo -e "${GREEN}✅ BiliMix 安装完成！${NC}"
echo -e "${GREEN}============================================================${NC}"
echo ""
echo "后续步骤："
echo ""
echo "1. 启动 Ollama 服务（如未运行）："
echo "   ollama serve                  # 或 brew services start ollama"
echo ""
echo "2. 拉取翻译模型（首次需要）："
echo "   ollama pull translategemma:4b"
echo ""
echo "3. 激活 conda 环境并启动 BiliMix："
echo "   conda activate bilimix"
echo "   python services/web_app.py 5555"
echo ""
echo "4. 浏览器访问："
echo "   http://localhost:5555"
echo ""
echo "提示："
echo "  - TTS 模型首次运行会自动下载 ~3GB（HuggingFace）"
echo "  - 重复运行 ./setup.sh 可修复环境问题（幂等）"
echo "  - 如需修改配置，编辑 core/config_local.py"
echo ""
