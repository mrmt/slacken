# slacken

Slack `#food` チャンネルへの食事投稿を Notion DB に自動取り込みし、健康診断結果をふまえた食事アドバイス・評価を Slack に返すツール。

## 概要

- 対象: Slack ワークスペース `shakuji.slack.com` の `#food` チャンネル
- 蓄積先: Notion DB「食事記録」 (Diet Teamspace / 「21世紀の体重異常者」配下)
- 実行方式:
  - **同期** (`slacken`): GitHub Actions cron (JST 6:00-24:00 のみ毎時、深夜帯はスキップ)
  - **アドバイス** (`slacken-advise`): GitHub Actions cron (JST 6:00 朝食 / 12:00 昼食 / 17:00 夕食)

## Notion DB 構造

| プロパティ | 型 | 用途 |
|---|---|---|
| 名前 | Title | 例「2026-07-14 昼食」自動生成 |
| 日付 | Date | 投稿の日付指定 or 投稿時刻 |
| 区分 | Select | 朝食 / 昼食 / 夕食 / 間食 |
| 内容 | Rich Text | テキスト投稿 (日付表現除去済) or 画像解析結果 |
| 写真 | Files | Slack添付画像 (Notion File Upload API 経由) |
| Slack投稿元 | URL | 元メッセージのpermalink (Upsert判定キー) |
| Slack最終編集 | Rich Text | Slackメッセージの最終編集ts (差分同期・Notion手動編集保護に使用) |
| 取込元 | Select | テキスト / 画像解析 / テキスト+画像 |

DB URL: <https://app.notion.com/p/b12824e541a7443bbd804ed58dd13f12>

## 取込ロジック

### 区分判定

投稿本文のキーワードを優先、なければ投稿時刻で判定。

- **朝食**: 「朝飯」「朝は」「朝食」等 / 時間帯 4:00-10:00
- **昼食**: 「昼飯」「昼は」「ランチ」等 / 時間帯 10:00-15:00
- **夕食**: 「晩飯」「晩は」「夜は」「ディナー」等 / 時間帯 17:00-22:00
- **間食**: 上記以外の時間帯

### 日付判定

以下の優先順で判定:

1. 本文の日付指定 `M/D` or `M月D日` (未来日は前年扱い)
2. 相対日付キーワード: 「今日/本日/今朝/今晩/今夜/けさ」= 当日、「昨日/きのう/ゆうべ/夕べ/昨夜/昨晩」= 前日、「一昨日/おととい/おとつい」= 2日前
3. 投稿タイムスタンプ (JST)

### 内容抽出

- テキスト投稿: 本文をそのまま格納。ただし日付指定・相対日付表現は Gemini で自然に除去し、食べたものの記述だけを残す
- 画像投稿: Gemini (vision) で解析
  - メニュー/注文票の写真 → OCRテキスト
  - 食事そのものの写真 → 内容の推定 (箇条書き)
  - 画像バイトは Notion File Upload API で `写真` プロパティにも添付
- 両方あれば併記

### Upsert / 差分同期

- Slack投稿の permalink を `Slack投稿元` に格納し、これをキーに新規/更新判定
- Slackメッセージの最終編集ts (`edited.ts` or 投稿ts) を `Slack最終編集` に保存
- 次回同期時、保存済 ts が Slack 側と一致すれば PATCH をスキップ → Slack 未編集の間は Notion 側の手動編集 (区分・内容・写真等) を保護
- Slack で再編集された投稿は追随して上書き
- 新規追加時 (`created`) は元の Slack 投稿にスレッド返信で個別評価コメントを自動投稿

## アドバイス / 評価

### 定時アドバイス (`slacken-advise`)

- JST 6:00 (朝食) / 12:00 (昼食) / 17:00 (夕食) に自動実行、`#food` に投稿
- 直近 `ADVICE_LOOKBACK_DAYS` 日 (既定14日) の食事記録 + 健康診断コンテキストを Gemini に投入
- これから食べる該当区分への提案 (300文字以内、メニュー案含む)
- `workflow_dispatch` で任意区分を手動 trigger 可能

### 新規食事の個別評価

- 同期スクリプトが新規レコードを作成した際に、Gemini で当該食事を評価し、元の Slack 投稿へスレッド返信 (250文字以内)
- 更新 (`updated`) / スキップ (`skipped`) 時は投稿しない

### 健康診断コンテキスト

- Notion ページ「健康診断結果の情報整理」を Blocks API で走査し、平文化してプロンプトの背景に注入
- ページ ID は環境変数 `HEALTH_CONTEXT_PAGE_ID` で上書き可 (既定は当該ページ)
- 該当ページを Notion Integration に共有しておく必要あり

## 同期方式

GitHub Actions cron で `conversations.history` を叩き、前回同期以降の差分を処理する。実行時間帯は JST 6:00-24:00 の毎時 (深夜 1:00-5:00 はスキップ、19回/日)。

要件は「4時間おきでOK」だが、編集追随の観点で活動時間帯は1時間間隔を採用。

## 必要な認証情報 (GitHub Secrets / Variables)

- `SLACK_BOT_TOKEN` (Secret) — `#food` の読み書き権限 (`channels:history`, `files:read`, `chat:write`)
- `NOTION_TOKEN` (Secret) — 食事記録DB・健康診断結果ページへの読み書き権限
- `GOOGLE_API_KEY` (Secret) — Gemini (画像解析・日付表現除去・アドバイス・評価生成)
- `NOTION_DATABASE_ID` (Variable) — 食事記録DBのID
- `SLACK_CHANNEL` (Variable) — 投稿先チャンネル名 (通常 `food`)

## 実装状況

- [x] Notion DB作成
- [x] リポジトリ作成
- [x] Slack Bot作成・トークン発行
- [x] Notion Integration作成・DB共有
- [x] 同期スクリプト実装
- [x] GitHub Actions ワークフロー
