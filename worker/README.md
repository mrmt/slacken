# slacken Cloudflare Worker

Slack Events API を受信し、署名検証のうえ GitHub Actions `repository_dispatch` に転送する薄い proxy。GitHub との認証は **GitHub App の installation token** を Worker 内で JWT (RS256) から都度取得する方式。実行ロジックは持たず、Actions 側 `on-slack-event.yml` → `slacken-process` が本処理を担う。

## 前提

- Cloudflare アカウント + Workers 無料枠 (100k req/day で十分)
- Node.js 18+ (wrangler 実行用)
- GitHub App (下記手順で作成)
- Slack App の Signing Secret

## 1. GitHub App の作成

1. https://github.com/settings/apps/new を開く
2. 設定:
   - **GitHub App name**: 任意 (例: `slacken-dispatcher`)
   - **Homepage URL**: 任意
   - **Webhook**: **Active のチェックを外す** (受信しない)
   - **Repository permissions** → **Contents: Read and write** (これで dispatch 権限が付く)
   - **Where can this GitHub App be installed?**: **Only on this account**
3. Create GitHub App
4. 作成後の画面で:
   - **App ID** をメモ (数値)
   - **Generate a private key** で `.pem` をダウンロード
5. 左メニュー **Install App** → 自分のアカウントに Install → `mrmt/slacken` のみを選択
6. Install 完了画面の URL 末尾に `installations/<数字>` が付く。この数字が **Installation ID** (URLの `settings/installations/<id>` または `installation_id=<id>`)

## 2. Slack Signing Secret 取得

Slack App → **Basic Information** → **App Credentials** → **Signing Secret**

## 3. Worker のデプロイ

```bash
cd worker
npm install
npx wrangler login
```

`wrangler.toml` の [vars] を編集して値を埋める:

```toml
[vars]
GH_REPO = "mrmt/slacken"
GH_APP_ID = "123456"            # GitHub App の App ID
GH_INSTALLATION_ID = "78901234" # #1 手順の Installation ID
SLACK_CHANNEL_ID = "C0123ABCD"  # #food のチャンネルID
```

シークレット登録:

```bash
npx wrangler secret put SLACK_SIGNING_SECRET
# → 対話で貼り付け

# Private key は改行を含むので pipe で流し込む
cat ~/Downloads/slacken-dispatcher.*.private-key.pem | npx wrangler secret put GH_APP_PRIVATE_KEY
```

デプロイ:

```bash
npm run deploy
```

出力される `https://slacken-events.<subdomain>.workers.dev` が Slack Events エンドポイント。

## 4. Slack App の Event Subscriptions 設定

1. https://api.slack.com/apps で対象アプリを開く
2. **Event Subscriptions** を ON
3. **Request URL** に Worker URL を入力 → 自動で `url_verification` チャレンジ → Verified になれば OK
4. **Subscribe to bot events** に追加:
   - `message.channels`
   - `message.groups` (private channel に投稿する場合)
5. **Save Changes** → 上部の "reinstall your app" バナーから再インストール

## 動作確認

```bash
npm run tail  # Worker のログをリアルタイム表示
```

別ターミナルで `#food` に投稿 → tail に `event_callback` が出力されて (エラーが無い状態で) GitHub Actions `on-slack-event` ワークフローが起動する。

## セキュリティ

- Slack 署名 (v0 HMAC-SHA256) 検証済リクエストのみ処理
- タイムスタンプ 5分超過は拒否 (リプレイ攻撃対策)
- Slack retry (`x-slack-retry-num` あり) は無視 (Python 側の冪等性でカバーしつつ Worker 段でも予防)
- installation token は 1時間有効 & isolate 内キャッシュで再利用
- Worker → GitHub は都度 JWT で installation token 取得。PAT 不要
- Private Key は Cloudflare secret として暗号化保管、平文ログに出ない

## トークンの更新・剥がし方

- App の Private key: GitHub App 設定 → Private keys → **Generate a new private key**、旧key削除後に Worker secret を差し替え
- Installation の削除: GitHub → Settings → Applications → 該当 App → **Uninstall** で即座に dispatch 権限が消える
