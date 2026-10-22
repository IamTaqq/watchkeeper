# Watchkeeper セットアップ手順(日本語)

> **おすすめ**: AWSへのデプロイは GitHub Actions 経由で行う [deploy-ja.md](deploy-ja.md) の方法が簡単で安全です。
> このページは、自分のPCから直接デプロイする方法と、障害訓練の内容を説明しています。

このガイドの流れで、AWS上に監視基盤を立ち上げ、練習台アプリをわざと壊し、Slackに通知が届くところまで体験できます。所要時間は初回で1〜2時間ほどです。

> Windowsの場合は、WSL(Ubuntu)の中で作業するのが一番スムーズです。

---

## 0. 全体像

| 部品 | 役割 |
|---|---|
| checker Lambda | 毎分すべてのチェックを実行し、結果を CloudWatch のメトリクス(成功=1 / 失敗=0)として送る |
| CloudWatch Alarm | チェックごとに1つ。「N回連続で失敗」したら SNS に通知する |
| heartbeat Lambda | cronジョブが「終わったよ」と報告しに来る窓口(公開URL + トークン認証) |
| notifier Lambda | アラームの状態変化を受け取り、原因つきでSlackに投稿する |
| demo app / demo cron | わざと壊せる練習台。慣れたら `enable_demo = false` で消す |

ポイントは **「checkerは結果を報告するだけ、アラートを出すかどうかはアラームが決める」** という分担です。これにより、checker自体が止まった場合も「データが来ない」ことでアラームが鳴ります(=監視の監視)。

---

## 1. 必要なもの

| ツール | 確認コマンド | 入手先 |
|---|---|---|
| AWSアカウント | - | 学習用に個人アカウントを用意 |
| AWS CLI v2 | `aws --version` | https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html |
| Terraform 1.7以上 | `terraform version` | https://developer.hashicorp.com/terraform/install |
| Python 3.12以上 | `python3 --version` | - |
| make | `make --version` | Macは `xcode-select --install` |
| Slackワークスペース | - | 自分用の無料ワークスペースでOK |

### AWSアカウントの安全設定(最初に必ず)

1. ルートユーザーに **MFA** を設定する
2. 日常作業はルートではなく、**IAM Identity Center** のユーザー(学習中は `AdministratorAccess` でOK)で行う
3. CLIの設定:
   ```bash
   aws configure sso      # Identity Center の場合
   aws sts get-caller-identity   # 自分のアカウントIDが出ればOK
   ```

> アクセスキーをコードやGitHubに絶対に書かないこと。`terraform.tfvars` と `*.tfstate` は `.gitignore` 済みです。

---

## 2. コードを手元で動かす(AWS不要)

```bash
cd watchkeeper
make setup     # .venv を作ってテスト用ライブラリを入れる
make test      # Pythonの単体テスト + Terraformのplanテスト
```

Pythonのテストがすべて `passed`、Terraformのテストがすべて `pass` になれば成功です。Terraformのテストはモック(偽物のAWS)で動くので、まだAWSにはお金も変更も発生しません。

---

## 3. Slackの受け口を作る

1. https://api.slack.com/apps → **Create New App** → **From scratch**
2. **Incoming Webhooks** を ON → **Add New Webhook to Workspace** → 通知先チャンネル(例: `#alerts`)を選ぶ
3. 表示された `https://hooks.slack.com/services/...` をコピーして、AWSに保存:

```bash
aws ssm put-parameter --region ca-central-1 --type SecureString \
  --name /watchkeeper/slack_webhook_url \
  --value 'https://hooks.slack.com/services/XXXX/YYYY/ZZZZ'
```

Terraformではなく手で入れるのは、**秘密情報をTerraformのコードやstateファイルに残さないため**です。
(Slackなしで試したい場合は飛ばしてOK。通知はnotifierのログにだけ出ます)

---

## 4. デプロイ

```bash
cp terraform/terraform.tfvars.example terraform/terraform.tfvars
# 必要なら alert_email に自分のメールを入れる(予算アラートも有効になる)
make bootstrap         # 初回のみ: OIDCロール、状態保存用S3、権限の上限を作る
make backend-config    # terraform/backend.hcl を作る
make deploy
```

`terraform apply` が作るリソースの一覧を表示して確認を求めるので、内容を眺めてから `yes` と入力します。
**ここで一覧を読む習慣が、本番で事故を防ぐ一番の訓練です。**

完了したら:

```bash
make outputs       # heartbeat_url, demo_app_url, dashboard_url など
```

`dashboard_url` をブラウザで開くと、チェックの成功/失敗とレイテンシのグラフが見られます。

> デプロイ直後に、cronの初回実行前のタイミング次第で一度だけ通知が来ることがあります。数分で自然に戻ります。

---

## 5. 障害訓練(ここが本番)

1つずつ試して、**Slackに届くまでの時間と、メッセージの中身**を観察してください。チェックは毎分、アラームは基本「2回連続失敗」で鳴るので、2〜3分かかります。

| やること | 壊れるもの | 期待するSlack通知 |
|---|---|---|
| `make fault MODE=error` | `/health` が500を返す | 🔴 FAILING: demo-app-health — `HTTP 500 (expected 200)` |
| `make fault MODE=slow` | 応答が遅すぎる | 🔴 FAILING: demo-app-health — `no response within 10s` |
| `make fault MODE=stale` | データの更新が止まる | 🔴 FAILING: demo-data-freshness — `data is stale ...` |
| `make fault MODE=down` | 全部503 | 2つ同時にFAILING |
| `make fault MODE=cron_fail` | ジョブが失敗を報告 | 🔴 FAILING: demo-cron — `job reported failure ...` |
| `make pause-cron` | ジョブが**そもそも動かない** | 約7分後に 🔴 FAILING: demo-cron — `last heartbeat ...s ago` |

戻すとき:

```bash
make fault MODE=none
make resume-cron
```

数分後に 🟢 RECOVERED が届けば成功です。

### 観察ポイント(面接で語れるネタになる)

- `pause-cron` は、エラーが一切出ない障害です。「何かが起きた」ではなく、**「起きるべきことが起きなかった」**をどう検知しているか、`checks.py` の `check_heartbeat` を読んでみてください。
- `make logs` で checker のログを見ると、1チェック1行のJSONが出ています。CloudWatch Logs Insights で `filter status = "FAIL"` と検索すると、失敗だけを抽出できます。
- checkerを止めたらどうなるか? EventBridgeのルール `watchkeeper-checker-schedule` を無効にしてみてください。全アラームが鳴り、Slackに「checker自体が止まっているかも」と出ます。

---

## 6. 自分のサービスを監視する

`checks.yaml` を編集します。`checks: []` を `checks:` に変えて、例のコメントを外すだけです。

```bash
make validate   # 書き方チェック
make local      # 自分のPCから一度だけ実行して結果を見る(AWS不要)
make deploy
```

### cronジョブからハートビートを送る

```bash
TOKEN=$(make -s token)
URL=$(cd terraform && terraform output -raw heartbeat_url)

# 成功したとき
curl -fsS -X POST -H "Authorization: Bearer $TOKEN" "${URL}ping/nightly-backup"
# 失敗したとき(本文がSlackに表示される)
curl -fsS -X POST -H "Authorization: Bearer $TOKEN" -d "disk full" "${URL}ping/nightly-backup/fail"
```

### データベースのSQLチェック

1. DBに**読み取り専用ユーザー**を作る(Supabase/Postgresの例):
   ```sql
   create role watchkeeper_ro login password '...';
   grant usage on schema public to watchkeeper_ro;
   grant select on all tables in schema public to watchkeeper_ro;
   ```
2. 接続文字列をSSMに保存。**Supabaseの場合は、ダッシュボードの Connect → 「Session pooler」の接続文字列を使ってください**(LambdaはIPv4のみで、Supabaseの直接接続はIPv6のため):
   ```bash
   aws ssm put-parameter --type SecureString --name /watchkeeper/dsn/app \
     --value 'postgresql://watchkeeper_ro:PASSWORD@HOST:5432/postgres?sslmode=require'
   ```
3. `checks.yaml` に `type: postgres` のチェックを追加(例はファイル内にあります)

コード側でも `READ ONLY` トランザクションで実行するので、万一ユーザー権限の設定を間違えても書き込みはできません(多層防御)。

---

## 7. 片付け

```bash
make destroy
```

Slackのwebhookを入れたSSMパラメータは自分で作ったものなので、Terraformでは消えません。不要なら:

```bash
aws ssm delete-parameter --name /watchkeeper/slack_webhook_url
```

---

## 8. コードを読む順番(学習用)

1. `checks.yaml` — 何を監視するか
2. `src/watchkeeper/config.py` — 設定の検証(間違いを早く・分かりやすく弾く)
3. `src/watchkeeper/checks.py` — 各チェックの判定ロジック。外部との通信はすべて `Context` 経由なので、テストで偽物に差し替えられる
4. `tests/test_checks.py` — 「どういう時に失敗とみなすか」の仕様書として読む
5. `src/handlers/checker.py` — 並列実行してメトリクスを送るだけ
6. `terraform/alarms.tf` — アラートの判断はここ。`evaluation_periods` と `treat_missing_data` が肝
7. `terraform/lambda.tf` — IAMの最小権限。各Lambdaが何をしてよいかを1つずつ確認

---

## 9. よくあるトラブル

| 症状 | 原因と対処 |
|---|---|
| `make deploy` で `build/lambda` がないと言われる | `make build` が失敗している。`make setup` を先に実行 |
| Slackに何も来ない | `make logs` ではなく notifier のログを確認: `aws logs tail /aws/lambda/watchkeeper-notifier --since 30m`。`cannot read /watchkeeper/slack_webhook_url` ならステップ3を確認 |
| メールが来ない | AWSから届く確認メールのリンクをクリックしたか確認 |
| ハートビートで401 | トークンが違う。`make -s token` で再取得 |
| ハートビートで404 | `checks.yaml` に `type: heartbeat` のチェック名がない、またはURLの末尾 `/ping/<名前>` が違う |
| 料金が心配 | `alert_email` を設定すると、月額予算の80%(予測)でメールが来る。使わない時は `make destroy` |

---

## 10. 次のステップ

- GitHub ActionsからOIDCでデプロイ(長期のアクセスキーを使わない)
- Terraformのstateを S3 に移す(チームで共有できるように)
- フェーズ3: SNSトピックにAIエージェントをつなぎ、アラート時にログを読んで原因を推定させる
