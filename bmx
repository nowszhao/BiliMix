#!/usr/bin/env bash
# BiliMix CLI 入口
# 用法: ./bmx <command> [options]
# 示例: ./bmx task submit --url https://x.com/ep.mp3 --wait
exec python3 "$(dirname "$0")/cli.py" "$@"
