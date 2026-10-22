# TutorHub を監視する

| チェック | 何を見るか | 頻度 | 通知まで |
|---|---|---|---|
| `tutorhub-site` | トップページが4秒以内に開くか | 毎分 | 3回連続で失敗 |
| `tutorhub-api` | サーバー側の処理(`/api/health`)が動くか | 毎分 | 3回連続で失敗 |
| `tutorhub-dependencies` | Supabase(DBと公開キー)、Claude API、StripeのキーとプランIDが今も使えるか | 1時間ごと(失敗中は毎分) | 2回連続で失敗 |

`tutorhub-dependencies` の通知には、どのサービスが `auth_failed`(キーの期限切れ・失効)か、
`unavailable`(相手側の障害など)か、`not_configured`(Vercelの環境変数が空)かが入ります。
キーの値や外部サービスのエラー本文は、TutorHub側で返さないようにしています。

## 準備(1回だけ)

1. TutorHub の `/api/health` を追加するPRをマージする(Vercelが自動でデプロイ)
2. CloudShell で合言葉(トークン)を作り、AWSに保存する

   ```bash
   TOKEN="$(openssl rand -hex 32)"
   aws ssm put-parameter --type SecureString --name /watchkeeper/tutorhub_health_token --value "$TOKEN"
   echo "$TOKEN"   # Vercel に貼るために一度だけ表示
   ```

3. Vercel の TutorHub → Settings → Environment Variables に `HEALTH_CHECK_TOKEN` を追加
   (Production、値は上で表示したもの)し、**Redeploy** する
4. 動作確認

   ```bash
   curl -s https://tutorhubjp.com/api/health   # {"status":"ok"}
   curl -s -H "Authorization: Bearer $TOKEN" https://tutorhubjp.com/api/health
   ```

   2つ目で `"status":"ok"` と、各サービスの結果が出れば準備完了です。

5. Watchkeeper をデプロイ: `git pull && ./scripts/cloudshell.sh deploy`

## 分からないこと

- Stripe の Webhook 用シークレット(`STRIPE_WEBHOOK_SECRET`)は、外から有効性を確かめる方法がありません。
  決済のWebhookが失敗し続けると Stripe からメールが届くので、そちらで気づけます。
- Vercel のログは、まだAIの調査で読めません(フェーズ5でデプロイ履歴と一緒に対応予定)。
