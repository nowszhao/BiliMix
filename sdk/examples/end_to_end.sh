#!/usr/bin/env bash
# 端到端示例：提交音频 → 等待完成 → 下载混合音频
# 用法: ./end_to_end.sh <audio_url> [output.mp3]
set -euo pipefail

URL="${1:?用法: $0 <audio_url> [output.mp3]}"
OUTPUT="${2:-mixed_$(date +%s).mp3}"

echo "==> 提交任务: $URL"
TID=$(bmx task submit --url "$URL" --mode smart_translate --wait \
      --field task_id)
echo "==> 任务完成: $TID"

echo "==> 下载混合音频 → $OUTPUT"
bmx audio download --task-id "$TID" --type mixed -o "$OUTPUT"

echo "==> 完成"
bmx task result "$TID" --field result
