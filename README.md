# slacken

Slack `#food` チャンネルへの食事投稿を、Notion DBに自動取り込みするツール。健康管理用の食事ログを蓄積する。

## 概要

- 対象: Slack ワークスペース `shakuji.slack.com` の `#food` チャンネル
- 蓄積先: Notion DB「食事記録」 (Diet Teamspace / 「21世紀の体重異常者」配下)
- 実行方式: GitHub Actions cron (1時間おき)

## Notion DB 構造

| プロパティ | 型 | 用途 |
|---|---|---|
| 名前 | Title | 例「2026-07-14 昼食」自動生成 |
| 日付 | Date | 投稿の日付指定 or 投稿時刻 |
| 区分 | Select | 朝食 / 昼食 / 夕食 / 間食 |
| 内容 | Rich Text | テキスト投稿 or 画像解析結果 |
| 写真 | Files | Slack添付画像 (任意) |
| Slack投稿元 | URL | 元メッセージのpermalink (Upsert判定キー) |
| 取込元 | Select | テキスト / 画像OCR / 画像解析 / テキスト+画像 |

DB URL: <https://app.notion.com/p/b12824e541a7443bbd804ed58dd13f12>

## 取込ロジック

### 区分判定

投稿本文のキーワードを優先、なければ投稿時刻で判定。

- **朝食**: 「朝飯」「朝は」「朝食」等 / 時間帯 4:00-10:00
- **昼食**: 「昼飯」「昼は」「ランチ」等 / 時間帯 10:00-15:00
- **夕食**: 「晩飯」「晩は」「夜は」「ディナー」等 / 時間帯 17:00-22:00
- **間食**: 上記以外の時間帯

### 日付判定

投稿本文に日付指定があればそれを優先。なければ投稿タイムスタンプ。

### 内容抽出

- テキスト投稿: そのまま「内容」に格納
- 画像投稿: Claude API (vision) で解析
  - メニュー/注文票の写真 → OCRテキスト
  - 食事そのものの写真 → 内容の推定 (箇条書き)
- 両方あれば併記

### Upsert

Slack投稿のpermalinkを「Slack投稿元」に格納し、これをキーに新規/更新判定。編集された投稿にも追随。

## 同期方式

GitHub Actions cron (1時間おき) で `conversations.history` を叩き、前回同期以降の差分を処理する。

要件は「4時間おきでOK」だが、編集追随の観点で1時間間隔を採用。

## 必要な認証情報 (GitHub Secrets)

- `SLACK_BOT_TOKEN` — `#food` の読み取り権限 (`channels:history`, `files:read`)
- `NOTION_TOKEN` — 食事記録DBへの読み書き権限
- `GOOGLE_API_KEY` — 画像解析用 (Gemini)

## 実装状況

- [x] Notion DB作成
- [x] リポジトリ作成
- [ ] Slack Bot作成・トークン発行
- [ ] Notion Integration作成・DB共有
- [ ] 同期スクリプト実装
- [ ] GitHub Actions ワークフロー
