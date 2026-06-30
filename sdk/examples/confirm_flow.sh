#!/usr/bin/env bash
# 需人工确认的流程示例
# 用法: ./confirm_flow.sh <audio_url>
set -euo pipefail

URL="${1:?用法: $0 <audio_url>}"

echo "==> 1. 提交任务（要求确认）"
TID=$(bmx task submit --url "$URL" --no-skip-confirm \
      --mode smart_translate | jq -r .task_id)
echo "    task_id = $TID"

echo "==> 2. 等待任务暂停在确认状态"
bmx task wait "$TID" --until awaiting_confirmation -q

echo "==> 3. 导出 AI 识别的生词到文件"
bmx task result "$TID" --field difficult_words > "/tmp/words_${TID}.json"
echo "    生词已保存到 /tmp/words_${TID}.json"
echo "    可手动编辑该文件：删除误标词、补充新生词"

echo "==> 4. 确认生词并继续处理"
read -p "按回车确认继续（或先编辑 words 文件）..."
bmx task confirm "$TID" --words-file "/tmp/words_${TID}.json" --wait -q

echo "==> 5. 下载结果"
bmx audio download --task-id "$TID" --type mixed -o "output_${TID}.mp3"
echo "==> 完成"
