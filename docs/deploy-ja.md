# AWSへのデプロイ(GitHub Actions経由)

> **組織のルールでOIDCプロバイダが作れないアカウント**(使えるリージョンや機能が制限されたアカウント)では、
> この方法は使えません。代わりに [deploy-cloudshell-ja.md](deploy-cloudshell-ja.md) の、CloudShellから直接デプロイする方法を使ってください。

コードの変更とデプロイは、**GitHubにpushすると自動で行われる**仕組みにしています。
AWSのアクセスキーはどこにも保存しません。GitHub ActionsがOIDCという仕組みで、その都度、短時間だけ有効な権限を受け取ります。

拓海さんが手作業でやるのは、最初の1回の準備だけです(30分ほど)。

```
Claude がコードを push ──▶ GitHub Actions ──(OIDCで一時的な権限)──▶ AWS
                               テスト → plan → apply
                               (任意: 拓海さんの承認ボタン)
```

---

## 手順1. GitHubに空のリポジトリを作る

1. https://github.com/new
2. Repository name: `watchkeeper`
3. Public を推奨(ポートフォリオとして見せられる。秘密情報はリポジトリに一切入れない設計)。Private でも動きます
4. README などは**追加しない**(空のまま)で作成

作ったら教えてください。こちらからコード一式をpushします。

---

## 手順2. AWSの準備(CloudShellで1回だけ)

AWSコンソール右上のリージョンを **カナダ (中部) ca-central-1** にして、画面上部の **CloudShell** アイコン(`>_`)を開きます。ブラウザの中で動くターミナルなので、PCに何もインストールする必要はありません。

```bash
# リポジトリが Public の場合
curl -fsSLO https://raw.githubusercontent.com/IamTaqq/watchkeeper/main/bootstrap/bootstrap.yaml

# Private の場合: GitHubから bootstrap/bootstrap.yaml をダウンロードし、
# CloudShell の「アクション → ファイルのアップロード」で上げる

aws cloudformation deploy \
  --stack-name watchkeeper-bootstrap \
  --template-file bootstrap.yaml \
  --capabilities CAPABILITY_NAMED_IAM

aws cloudformation describe-stacks --stack-name watchkeeper-bootstrap \
  --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output table
```

最後のコマンドで、`DeployRoleArn` と `StateBucketName` が表示されます。手順3で使います。

**これで作られるもの**

| リソース | 役割 |
|---|---|
| GitHub OIDCプロバイダ | GitHub Actionsを「身元確認できる相手」としてAWSに登録 |
| `github-deploy-watchkeeper` ロール | デプロイ用。**このリポジトリの `aws` / `aws-ops` 環境で動くジョブだけ**が使える |
| `watchkeeper-lambda-boundary` | Terraformが作るすべてのロールに付ける「権限の上限」。万一おかしなポリシーが書かれても、この範囲を超えられない |
| S3バケット | Terraformの状態ファイルの保管場所(暗号化、バージョン管理、公開ブロック) |

> デプロイ用ロールは、IAM以外のほぼすべての操作ができる強い権限です。そのぶん、使える場所をこのリポジトリの特定の環境に限定しています。IAMについては「`watchkeeper-` で始まるロールを、権限の上限つきでしか作れない」ように絞っています。

---

## 手順3. GitHubの設定

リポジトリの **Settings** で設定します。

### Environments(環境)

**Settings → Environments → New environment** で2つ作ります。

| 名前 | 用途 | 設定 |
|---|---|---|
| `aws` | デプロイ | 「Required reviewers」に自分を入れると、**デプロイのたびに承認ボタンを押すまで止まる**。最初は入れておくのがおすすめ |
| `aws-ops` | 障害訓練や状態確認 | reviewer なし |

どちらも「Deployment branches」を `main` のみに制限しておくと安全です。

### Variables(変数)

**Settings → Secrets and variables → Actions → Variables タブ → New repository variable**

| 名前 | 値 |
|---|---|
| `AWS_ROLE_ARN` | 手順2の `DeployRoleArn` |
| `TF_STATE_BUCKET` | 手順2の `StateBucketName` |
| `AWS_REGION` | `ca-central-1` |
| `ALERT_EMAIL` | 自分のメールアドレス(予算アラートとアラーム通知用。空でも可) |
| `ENABLE_TRIAGE` | 最初は `false`。手順5が済んだら `true` |

どれも秘密情報ではないので、Secrets ではなく Variables で大丈夫です。

---

## 手順4. 秘密情報をAWSに入れる(CloudShell)

SlackのwebhookとClaudeのAPIキーは、GitHubにもこちらにも渡さず、AWSに直接入れます。

```bash
aws ssm put-parameter --type SecureString \
  --name /watchkeeper/slack_webhook_url --value 'https://hooks.slack.com/services/...'
```

(Slackのwebhookの作り方は [setup-ja.md](setup-ja.md) の手順3)

---

## 手順5. Maxプランの無料APIクレジットを使えるようにする

Max 5x プランには、**毎月$100分のAPIクレジット**が付いています。AIの調査エージェントは、これで動かせます。

1. https://platform.claude.com/ で Console の組織を作る(またはログイン)
2. claude.ai の **設定 → 請求(Billing)** で **Link organization** を選び、その組織を紐づける
   - Maxプランに7日以上加入している必要があります
   - 紐づけられる組織は1つだけで、自分では変更できません
3. Console の **API Keys** でキーを作り、AWSに保存:

```bash
aws ssm put-parameter --type SecureString \
  --name /watchkeeper/anthropic_api_key --value 'sk-ant-...'
```

4. GitHubの変数 `ENABLE_TRIAGE` を `true` にする

**安全装置として**: Console に支払い方法を登録しなければ、$100を使い切った時点でAPIは止まり、追加料金は発生しません。クレジットは毎月リセットされ、翌月への繰り越しはありません。

---

## 手順6. デプロイ

pushすると自動で始まります。手動で動かす場合は **Actions → deploy → Run workflow**。

`aws` 環境に reviewer を設定していれば、テストとビルドのあとで止まって承認待ちになります。**Review deployments → Approve** で、planとapplyが実行されます。

完了すると、Actions の画面に URL などの出力が表示されます。

---

## 手順7. 障害訓練をGitHubから

**Actions → ops → Run workflow** で、操作を選んで実行します。実行記録はすべてGitHubに残ります。

| action | 内容 |
|---|---|
| `status` | アラームの状態と、練習台の設定を表示 |
| `fault` + mode | 練習台を壊す(`error` / `slow` / `stale` / `down` / `cron_fail`)。`none` で元に戻す |
| `pause-cron` / `resume-cron` | 練習台のcronジョブを止める / 再開する |
| `run-checker` | チェックをいますぐ1回実行 |
| `test-alert` | テスト用のアラートを流して、Slack通知の経路を確認 |

---

## 片付け

全部消すときは、`terraform destroy` をCloudShellか自分のPCから実行します(`make backend-config && cd terraform && terraform init -backend-config=backend.hcl && terraform destroy`)。
その後、CloudFormationの `watchkeeper-bootstrap` スタックを削除します。S3バケットは誤削除防止のため残る設定なので、不要なら空にしてから手動で削除してください。
