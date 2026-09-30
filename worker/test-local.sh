#!/usr/bin/env bash
# ローカル wrangler dev を叩くための Slack 署名済みリクエスト送信ヘルパ
#
# 前提: wrangler dev が http://127.0.0.1:8787 で起動していること
#       .dev.vars の SLACK_SIGNING_SECRET と一致させる
#
# 使い方:
#   ./test-local.sh challenge   # url_verification challenge
#   ./test-local.sh message     # 通常メッセージ (dispatch 発火試行、GHダミー鍵ならFail)
#   ./test-local.sh bot         # bot_message (フィルタでスキップ)
#   ./test-local.sh retry       # retry ヘッダ付き (通常メッセージと同様に dispatch 発火試行)
#   ./test-local.sh badsig      # 不正な署名 (401)
#   ./test-local.sh notmsg      # メッセージ以外 (スキップ)
#   ./test-local.sh wrongch     # 対象外チャンネル (スキップ)

set -eu

URL="${URL:-http://127.0.0.1:8787}"
SECRET="${SLACK_SIGNING_SECRET:-test_signing_secret}"
TS="$(date +%s)"

sign() {
  local body="$1"
  local base="v0:${TS}:${body}"
  local sig
  sig=$(printf '%s' "$base" | openssl dgst -sha256 -hmac "$SECRET" -hex | awk '{print $NF}')
  echo "v0=${sig}"
}

send() {
  local body="$1"
  local extra_headers="${2:-}"
  local sig
  sig=$(sign "$body")
  echo "--- request ---"
  echo "$body"
  echo "--- response ---"
  # shellcheck disable=SC2086
  curl -sS -i "$URL" \
    -X POST \
    -H 'content-type: application/json' \
    -H "x-slack-request-timestamp: ${TS}" \
    -H "x-slack-signature: ${sig}" \
    ${extra_headers} \
    --data "$body"
  echo
  echo
}

case "${1:-}" in
  challenge)
    send '{"type":"url_verification","challenge":"abc123"}'
    ;;
  message)
    send '{"type":"event_callback","event":{"type":"message","channel":"'"${SLACK_CHANNEL_ID:-C_TEST}"'","ts":"1731234567.000100","text":"ラーメン食べた","user":"U123"}}'
    ;;
  bot)
    send '{"type":"event_callback","event":{"type":"message","subtype":"bot_message","channel":"C_TEST","ts":"1.0","bot_id":"B1"}}'
    ;;
  retry)
    # 再送も無視せず処理する。ログに processing slack retry が出て dispatch 経路へ進めば正常
    send '{"type":"event_callback","event":{"type":"message","channel":"'"${SLACK_CHANNEL_ID:-C_TEST}"'","ts":"1731234567.000200","text":"再送されたラーメン","user":"U123"}}' "-H x-slack-retry-num:1 -H x-slack-retry-reason:http_timeout"
    ;;
  badsig)
    TS_OK="$TS"
    curl -sS -i "$URL" -X POST \
      -H 'content-type: application/json' \
      -H "x-slack-request-timestamp: $TS_OK" \
      -H "x-slack-signature: v0=deadbeef" \
      --data '{"type":"url_verification","challenge":"x"}'
    echo
    ;;
  notmsg)
    send '{"type":"event_callback","event":{"type":"reaction_added","channel":"C_TEST","user":"U1","item":{}}}'
    ;;
  wrongch)
    send '{"type":"event_callback","event":{"type":"message","channel":"C_WRONG","ts":"1.0","text":"x","user":"U1"}}'
    ;;
  *)
    echo "usage: $0 {challenge|message|bot|retry|badsig|notmsg|wrongch}" >&2
    exit 1
    ;;
esac
