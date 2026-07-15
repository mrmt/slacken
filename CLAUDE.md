# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## プロジェクト概要

Slack `#food` チャンネルへの食事投稿を Notion DB「食事記録」に取り込むツール。GitHub Actions cron (1時間おき) で `slacken/main.py` を実行する。詳細な仕様・DB構造・区分判定ルールは `README.md` を参照。

## コマンド

依存管理・実行は `uv` を使う。

- `uv sync` — 依存インストール (CI は `--frozen`)
- `uv run slacken` — 同期スクリプト実行 (`slacken/main.py:main`)
- ローカル実行時は下記環境変数を全て設定:
  - `SLACK_BOT_TOKEN`, `NOTION_TOKEN`, `GOOGLE_API_KEY`, `NOTION_DATABASE_ID`
  - 任意: `SLACK_CHANNEL` (既定 `food`), `LOOKBACK_HOURS` (既定 168), `GEMINI_MODEL` (既定 `gemini-flash-latest`)

テスト・lint 設定は未整備。

## アーキテクチャ

シングルファイル (`slacken/main.py`)。処理は上から下に読める1パス構成:

1. `resolve_channel_id` — チャンネル名→ID (private含む、ページング対応)
2. `conversations_history` を `LOOKBACK_HOURS` 遡って取得、ページングで全件走査
3. `process_message` で各メッセージ処理:
   - `classify_meal` — 本文キーワード優先→時間帯フォールバック
   - `extract_date` — 本文の `M/D` or `M月D日` 指定→なければ投稿日
   - 画像添付は Slack Bot Token 付きでDL、`analyze_image` (Gemini vision) で内容抽出
   - `notion_upsert` — permalink をキーに既存レコード検索→PATCH or POST

### 設計上の重要な選択

- **Upsert キーは Slack permalink** (`Slack投稿元` プロパティ)。編集された投稿にも追随する
- **Gemini 呼び出しは指数バックオフでリトライ** (429/500/503、最大8回・最大60秒待機)。1件の一時エラーで同期全体が落ちないよう `process_message` レベルでも例外catch
- **cron は1時間おき**。要件は「4時間おきで可」だが編集追随のため短めに設定 (README記載)
- **JST固定** (`Asia/Tokyo`)。時間帯による区分判定・日付計算は全てJSTで行う
- **依存は Notion 公式SDKを使わず requests 直叩き**。Notionプロパティ名は日本語 (「名前」「日付」「区分」「内容」「写真」「Slack投稿元」「Slack最終編集」「取込元」)
- **Notion手動編集の保護**: `Slack最終編集` に Slack 側の最終編集ts を保存し、次回同期時に一致すれば PATCH スキップ
- **画像は Notion File Upload API で `写真` プロパティに添付**。画像バイトは Gemini 解析と Notion アップロードで共用 (二重DL回避)
- **本文の日付表現除去**: 「昨日」「7/14」等は区分・日付判定に使用後、Gemini で自然に除去して `内容` に格納

## GitHub Actions

`.github/workflows/sync.yml`。Secrets は `SLACK_BOT_TOKEN` / `NOTION_TOKEN` / `GOOGLE_API_KEY`、Variables は `NOTION_DATABASE_ID` / `SLACK_CHANNEL`。手動実行時のみ `lookback_hours` を上書き可能 (workflow_dispatch)。
