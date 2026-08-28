# CLAUDE.md

## プロジェクト概要

Slack `#food` チャンネルへの食事投稿を Notion DB「食事記録」に取り込み、健康診断結果をふまえた食事アドバイス・評価を Slack に返すツール。GitHub Actions cron で同期 (JST 6:00-24:00 の毎時) を実行。アドバイス (`advise.yml`) の cron は無効化済みで手動実行のみ。詳細な仕様・DB構造・区分判定ルールは `README.md` を参照。

## 実行環境

依存管理・実行は `uv` を使う。ローカル実行時は下記環境変数を全て設定:

- 必須: `SLACK_BOT_TOKEN`, `NOTION_TOKEN`, `GOOGLE_API_KEY`, `NOTION_DATABASE_ID`
- 任意: `SLACK_CHANNEL` (既定 `food`), `LOOKBACK_HOURS` (既定 168), `GEMINI_MODEL` (既定 `gemini-flash-latest`), `ADVICE_LOOKBACK_DAYS` (既定 14), `HEALTH_CONTEXT_PAGE_ID` (既定は健康診断ページ)

## 設計上の重要な選択

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
