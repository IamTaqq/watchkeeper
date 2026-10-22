# CloudShellからデプロイする

AWSアカウントの組織ルールで、GitHubとAWSをつなぐOIDCプロバイダが作れない場合の方法です。
（使えるリージョンが限られたアカウントでも動くように、リージョンはCloudShellの設定に合わせます）

**仕組み**: Claudeがコードを作ってGitHubにpushし、拓海さんがCloudShellでそれを取り込んでデプロイします。
AWSの鍵は作りません。コマンドはすべて、コンソールにログイン中の拓海さんの権限で動きます。
デプロイの前に必ず変更内容(plan)が表示され、`yes` と入力するまで何も変わりません。

```
Claude ──push──▶ GitHub ──git pull──▶ CloudShell ──(ログイン中の権限)──▶ AWS
                  (テストは自動)        plan を確認して yes
```

---

## 初回だけ

CloudShell のタブが、使えるリージョン(例: `us-east-2`)になっていることを確認してから実行します。

```bash
git clone https://github.com/IamTaqq/watchkeeper.git
cd watchkeeper

./scripts/cloudshell.sh setup       # Terraform を ~/.local/bin に入れる(チェックサムも確認)
./scripts/cloudshell.sh bootstrap   # 状態保存用のS3と「権限の上限」を作る
```

CloudShell のホームディレクトリ(1GB)は保存され続けるので、次回からは `cd watchkeeper` だけで続きから使えます。
(120日間使わないと消えることがあります。その場合は初回の手順をもう一度)

### Slackの通知先を登録する(任意、あとからでも可)

```bash
aws ssm put-parameter --type SecureString \
  --name /watchkeeper/slack_webhook_url --value 'https://hooks.slack.com/services/...'
```

---

## デプロイ(初回も、更新のたびも同じ)

```bash
cd ~/watchkeeper
git pull
./scripts/cloudshell.sh deploy
```

1. Lambda用のパッケージを作る
2. 変更内容(plan)を表示する ← ここで内容を確認
3. `Apply this plan? Type yes to continue:` と聞かれるので、問題なければ `yes`

完了すると、URLやダッシュボードのアドレスが表示されます。

設定は `~/.watchkeeper.env` に書いておくと、次回以降のデプロイでも引き継がれます。

```bash
echo ENABLE_TRIAGE=true >> ~/.watchkeeper.env               # AIの調査を有効にする(APIキーの登録が先)
echo ALERT_EMAIL=you@example.com >> ~/.watchkeeper.env      # 予算アラートとメール通知を有効にする
cat ~/.watchkeeper.env                                      # 今の設定を確認
```

その場だけ変えたい場合は、`ENABLE_TRIAGE=false ./scripts/cloudshell.sh deploy` のようにコマンドの前に書きます。
デプロイの最初に `Settings: demo=... triage=...` と、使われる設定が表示されます。

> メールアドレスを設定すると AWS Budgets(予算アラート)も作られます。
> アカウントのルールで止められた場合は、エラーを見せてください。

---

## 障害訓練と状態確認

```bash
./scripts/cloudshell.sh status            # アラームの状態と練習台の設定
./scripts/cloudshell.sh fault error       # 練習台を壊す(none / error / slow / stale / down / cron_fail)
./scripts/cloudshell.sh pause-cron        # 練習台のcronを止める
./scripts/cloudshell.sh resume-cron       # 再開
./scripts/cloudshell.sh run-checker       # チェックをいますぐ1回実行
./scripts/cloudshell.sh test-alert        # テスト用のアラートを流す
./scripts/cloudshell.sh logs              # チェックのログを流し続ける(Ctrl+C で終了)
```

---

## 片付け

```bash
./scripts/cloudshell.sh destroy
aws cloudformation delete-stack --stack-name watchkeeper-bootstrap
```

状態保存用のS3バケットは、誤削除防止のため残ります。不要なら、空にしてから手動で削除してください。
