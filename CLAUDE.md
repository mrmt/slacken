# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## プロジェクト概要

Slack `#food` チャンネルへの食事投稿を Notion DB「食事記録」に取り込み、健康診断結果をふまえた食事アドバイス・評価を Slack に返すツール。GitHub Actions cron で同期 (JST 6:00-24:00 の毎時) とアドバイス (JST 6/12/17 時) を実行。詳細な仕様・DB構造・区分判定ルールは `README.md` を参照。

## コマンド

依存管理・実行は `uv` を使う。

- `uv sync` — 依存インストール (CI は `--frozen`)
- `uv run slacken` — 同期スクリプト実行 (`slacken/main.py:main`)
- `uv run slacken-advise --meal <朝食|昼食|夕食>` — 定時アドバイス投稿 (`slacken/advisor.py:main`)
- `uv run pytest` — テスト実行 (純粋関数のみ、ネットワークなし)
- ローカル実行時は下記環境変数を全て設定:
  - `SLACK_BOT_TOKEN`, `NOTION_TOKEN`, `GOOGLE_API_KEY`, `NOTION_DATABASE_ID`
  - 任意: `SLACK_CHANNEL` (既定 `food`), `LOOKBACK_HOURS` (既定 168), `GEMINI_MODEL` (既定 `gemini-flash-latest`), `ADVICE_LOOKBACK_DAYS` (既定 14), `HEALTH_CONTEXT_PAGE_ID` (既定は健康診断ページ)

lint 設定は未整備。テストは `tests/test_main.py` に純粋関数の pytest。

## アーキテクチャ

2モジュール構成:

**`slacken/main.py`** — Slack → Notion 同期 + 新規時の個別評価投稿

1. `resolve_channel_id` — チャンネル名→ID (private含む、ページング対応)
2. `conversations_history` を `LOOKBACK_HOURS` 遡って取得、ページングで全件走査
3. `process_message` で各メッセージ処理:
   - `classify_meal` / `extract_date` — 生テキストで区分・日付を判定
   - `strip_date_via_gemini` — Notion保存本文からは日付表現を Gemini で除去
   - 画像添付は Slack Bot Token 付きでDL、`analyze_image` (Gemini vision) で内容抽出 + `notion_upload_file` で `写真` プロパティに添付
   - `notion_upsert` — permalink キーで検索、`Slack最終編集` ts 一致ならスキップ、それ以外は PATCH or POST し `created`/`updated`/`skipped` を返す
   - `evaluate_and_post` — `created` 時のみ、直近食事+健康診断コンテキストを Gemini に投入して評価コメントを生成し、元Slack投稿にスレッド返信

**`slacken/advisor.py`** — 定時アドバイス投稿 + 共有ヘルパ

- `fetch_recent_records(days)` — Notion DB を日付フィルタで検索し全件取得 (main.py からも import して評価コメントで再利用)
- `fetch_page_text(page_id)` — Notion Blocks API を再帰走査してページを平文化 (健康診断コンテキスト取得)
- `generate_advice` — Gemini でアドバイス生成
- `post_to_slack` — `chat.postMessage` で `#food` に投稿

### 設計上の重要な選択

- **Upsert キーは Slack permalink** (`Slack投稿元` プロパティ)。編集された投稿にも追随する
- **Gemini 呼び出しは指数バックオフでリトライ** (429/500/503、最大8回・最大60秒待機)。1件の一時エラーで同期全体が落ちないよう `process_message` レベルでも例外catch
- **cron は JST 6:00-24:00 の毎時** (深夜1-5時はスキップ、19回/日)。要件は「4時間おきで可」だが編集追随のため活動時間帯は1時間間隔 (README記載)
- **JST固定** (`Asia/Tokyo`)。時間帯による区分判定・日付計算は全てJSTで行う
- **依存は Notion 公式SDKを使わず requests 直叩き**。Notionプロパティ名は日本語 (「名前」「日付」「区分」「内容」「写真」「Slack投稿元」「Slack最終編集」「取込元」)
- **Notion手動編集の保護**: `Slack最終編集` に Slack 側の最終編集ts を保存し、次回同期時に一致すれば PATCH スキップ
- **画像は Notion File Upload API で `写真` プロパティに添付**。画像バイトは Gemini 解析と Notion アップロードで共用 (二重DL回避)
- **本文の日付表現除去**: 「昨日」「7/14」等は区分・日付判定に使用後、Gemini で自然に除去して `内容` に格納
- **アドバイス/評価は健康診断ページを常に背景注入**: `HEALTH_CONTEXT_PAGE_ID` の Notion ページを Blocks API で走査し、プロンプト先頭に埋め込む。ページを Integration に共有必須
- **新規食事の個別評価はスレッド返信**: `#food` を汚さないよう元投稿への `thread_ts` 付き返信で投稿

## GitHub Actions

- `.github/workflows/sync.yml` — 同期 (毎時cron + workflow_dispatch)
- `.github/workflows/advise.yml` — 定時アドバイス (3本のcronで `github.event.schedule` を meal に対応付け + workflow_dispatch)
- `.github/workflows/test.yml` — push/PR で pytest

Secrets は `SLACK_BOT_TOKEN` / `NOTION_TOKEN` / `GOOGLE_API_KEY`、Variables は `NOTION_DATABASE_ID` / `SLACK_CHANNEL`。手動実行時のみ `lookback_hours` を上書き可能 (sync workflow_dispatch)。
