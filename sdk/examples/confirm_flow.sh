#!/usr/bin/env bash
# 需人工确认翻译的流程示例（sentence_translate 模式）
# 用法: ./confirm_flow.sh <audio_url>
set -euo pipefail

URL="${1:?用法: $0 <audio_url>}"

echo "==> 1. 提交任务（要求确认翻译）"
TID=$(bmx task submit --url "$URL" --no-skip-confirm | jq -r .task_id)
echo "    task_id = $TID"

echo "==> 2. 等待任务暂停在句子确认状态"
bmx task wait "$TID" --until awaiting_sentence_confirmation -q

echo "==> 3. 导出 AI 翻译结果到文件"
bmx task result "$TID" --field translations > "/tmp/translations_${TID}.json"
echo "    翻译已保存到 /tmp/translations_${TID}.json"
echo "    可手动编辑该文件：修改译文、删除不需要翻译的句子"

echo "==> 4. 确认翻译并继续处理"
read -p "按回车确认继续（或先编辑 translations 文件）..."
bmx task confirm-sentences "$TID" --translations-file "/tmp/translations_${TID}.json" --wait -q

echo "==> 5. 下载结果"
bmx audio download --task-id "$TID" --type mixed -o "output_${TID}.mp3"
echo "==> 完成"
