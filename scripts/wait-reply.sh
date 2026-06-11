#!/usr/bin/env bash
# wait-reply.sh — 轮询「代码查询」群，等机器人回复指定消息，回复齐了就退出。
#
# 用法: wait-reply.sh <chat_id> <message_id> [timeout_sec=900] [interval_sec=20]
#
# 设计给 AI agent 用：以后台任务方式运行，进程退出即代表"查询结束"，
# agent 会被 harness 自动唤醒，无需人工盯群。
#
# 退出码: 0 = 回复完整(回复内容已打印到 stdout)  2 = 超时  3 = 参数/依赖错误
set -euo pipefail

CHAT_ID="${1:?usage: wait-reply.sh <chat_id> <message_id> [timeout] [interval]}"
MSG_ID="${2:?missing message_id}"
TIMEOUT="${3:-900}"
INTERVAL="${4:-20}"

command -v lark-cli >/dev/null || { echo "lark-cli not installed" >&2; exit 3; }
command -v jq >/dev/null || { echo "jq not installed" >&2; exit 3; }

deadline=$(( $(date +%s) + TIMEOUT ))

while [ "$(date +%s)" -lt "$deadline" ]; do
  # 取群最近消息，筛出"回复我们那条提问"的机器人消息（sender_type == app）
  replies=$(lark-cli im +chat-messages-list --chat-id "$CHAT_ID" --json 2>/dev/null \
    | jq --arg mid "$MSG_ID" \
        '[.data.messages[]? | select(.reply_to == $mid and .sender.sender_type == "app")]' \
    || echo '[]')

  status=$(jq -r '
    def part: .content | capture("\\((?<k>\\d+)/(?<n>\\d+)\\)\\s*$") // null;
    if length == 0 then "waiting"
    elif ([.[] | part] | all(. == null)) then "complete"        # 单条回复，无分片标记
    else
      ([.[] | part | select(. != null) | .n | tonumber] | max) as $N
      | if ([.[] | part | select(. != null)] | length) >= $N
        then "complete" else "partial" end                      # (k/N) 分片要集齐 N 片
    end' <<<"$replies")

  if [ "$status" = "complete" ]; then
    # 按 message_position 升序输出全部回复内容
    jq -r 'sort_by(.message_position | tonumber) | .[].content' <<<"$replies"
    exit 0
  fi
  sleep "$INTERVAL"
done

echo "timeout: no complete reply within ${TIMEOUT}s" >&2
exit 2
