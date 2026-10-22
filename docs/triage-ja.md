# AI障害調査エージェント(フェーズ3〜4)を手元で動かす

サーバーもAWSも使わずに、**記録された障害シナリオ**をAIに調査させる手順です。
本番のLambdaで動くのとまったく同じエージェントとツールが、偽の「障害が起きている世界」を相手に動きます。

---

## 1. 仕組み

```
アラート ──▶ エージェント ──▶ 読み取り専用ツール ──▶ データ
              (Claude)        list_checks            手元: scenarios/*.yaml
                 │            get_check_history      AWS : CloudWatch / Logs /
                 │            search_logs                  CloudTrail / DynamoDB
                 │            get_heartbeat
                 │            recent_changes
                 ▼
           submit_report ──▶ 検証(許可リストと照合) ──▶ レポート(Slack / 画面)
```

- エージェントが使えるのは**読み取り専用のツールだけ**です。何も実行できません。
- 最後に必ず `submit_report` で、決まった形式のレポートを出させます(要約、影響、証拠、仮説と確信度、提案)。
- 提案が「許可リストの操作」だった場合は、**コード側で値を検証**します。たとえばロールバック先が実在する過去のデプロイでなければ、却下して「人間に引き継ぎ」に格下げします。
- ログに含まれるパスワード、APIキー、メールアドレス、電話番号は、**AIに送る前に伏せ字**にします。
- 1回の調査は最大8往復までです。最後の往復ではレポートの提出を強制するので、AIが調査をだらだら続けて料金が膨らむことはありません。

---

## 2. まずは無料で中身を見る(APIキー不要)

```bash
make setup
make tools SCENARIO=bad-deploy
```

AIが調査のときに見ることになるデータ(チェックの状態、履歴、ログ、直近の変更)が表示されます。
自分ならこのデータから何を疑うか、先に考えてみてください。あとでAIの答えと比べると勉強になります。

用意してあるシナリオは4つです。

| シナリオ | 何が起きているか | 正解 |
|---|---|---|
| `bad-deploy` | デプロイ直後から `/health` が500エラー | 直前のデプロイが原因 → 1つ前のデプロイへのロールバックを提案 |
| `cron-disabled` | ジョブが黙って止まった。エラーは一切なし | 誰かがスケジュールを無効にした → スケジュール再開を提案 |
| `upstream-rate-limit` | データの更新が止まった | 外部APIの利用制限(429)。再実行は逆効果 → 人間に引き継ぎ |
| `red-herring-deploy` | タイムアウト。直前にデプロイもある | **デプロイは無関係**(ドキュメント修正のみで、遅延はその前から始まっている)。外部の決済サービスが原因 → 人間に引き継ぎ |

後半の2つは「わざと間違えやすく作ったシナリオ」です。直前の変更に飛びつかず、時刻を照らし合わせられるかを試しています。

---

## 3. AIに調査させる(APIキーが必要)

### APIキーの用意

Max 5x プランには毎月$100分のAPIクレジットが付いています([deploy-ja.md](deploy-ja.md) の手順5)。

1. https://platform.claude.com/ で Console の組織を作り、claude.ai の **設定 → 請求** で **Link organization** から紐づける
2. Console の **API Keys** でキーを作る
3. 支払い方法は登録しないでおくと、クレジットを使い切った時点で止まり、追加料金は発生しません

```bash
export ANTHROPIC_API_KEY='sk-ant-...'
make simulate SCENARIO=bad-deploy
```

`--verbose` 付きで動くので、AIがどのツールをどの順番で呼んだかが流れていきます。最後にレポートと採点(PASS / FAIL)が出ます。

安いモデルで試す場合:

```bash
make simulate SCENARIO=red-herring-deploy MODEL=claude-haiku-5-5
```

### 料金の目安(1回の調査)

| モデル | 目安 |
|---|---|
| claude-haiku-5-5 | 1セント未満 |
| claude-sonnet-5-5 | 5セント前後 |

月$100のクレジットなら、Sonnetでも1,000回以上調査できる計算です。

実際の金額はレポートの最後の行に毎回表示されます(トークン数、料金、所要時間)。

---

## 4. 精度を測る(フェーズ4)

```bash
make eval                                                   # 全シナリオを1回ずつ
make eval MODELS="claude-haiku-5-5 claude-sonnet-5-5" REPEAT=3   # モデル比較、各3回
```

出力の例:

```
model                   pass   root   tier    act     avg $   avg s  avg turns
claude-haiku-5-5         75%   100%    75%    75%    0.0040     6.2        4.5
claude-sonnet-5-5       100%   100%   100%   100%    0.0710    14.8        4.0
```

(数字はイメージです。実際の値は自分で測ってください)

- **root**: 原因を正しく言い当てたか
- **tier**: 対処の段階(操作 / PR / 引き継ぎ)が正しいか
- **act**: 提案した操作とパラメータが正しいか。禁止の操作(例: 外部障害なのに再実行)を提案していないか

AIの出力は毎回少し変わるので、`REPEAT=3` 以上で測るのがおすすめです。結果は `build/eval/` にJSONで保存されます。

**面接で語れる形にすると:**
「4種類の障害シナリオで評価し、Haikuは正答率◯%で1件あたり0.4セント、Sonnetは◯%で7セントだった。誤答の傾向から、プロンプトに『変更は容疑者であって証拠ではない』という原則を追加して改善した」

---

## 5. シナリオを自分で作る

`scenarios/` にYAMLを1つ足すだけで、評価の対象に入ります。時刻は「現在から何分前か」で書きます(`-10` = 10分前)。

```yaml
name: my-incident
description: 何が起きているかの説明
now: "2026-10-22T10:00:00Z"
alarm: {check: my-api, reason: "Threshold Crossed ..."}
checks:
  - {name: my-api, type: http, ok: false, message: "HTTP 503"}
history:
  my-api:
    - {from: -60, to: -6, success: true, latency_ms: 120}
    - {from: -5, to: 0, success: false, latency_ms: 30}
logs:
  my-api:
    - {every: 1, from: -5, to: 0, message: "ERROR database connection refused"}
changes:
  - {at: -6, actor: someone, action: deploy, target: my-api, deployment_id: dpl_2, details: "..."}
context:                      # 許可リストの操作で使ってよい値
  deployments: [dpl_1]
expected:
  root_cause: ["database|connection"]   # 正規表現。要約か一番目の仮説に含まれていればOK
  tier: escalate
  forbid_actions: [rerun_job]
```

実際に起きた障害(アイスホッケーPJやTutorHubで遭遇したもの)をシナリオにして溜めていくと、それ自体が**回帰テスト**になります。プロンプトやモデルを変えたときに、精度が下がっていないかを確かめられます。

---

## 6. AWSで動かすとき(まだやらなくてOK)

フェーズ1〜2をデプロイ済みなら、次の2つで有効になります。

```bash
aws ssm put-parameter --type SecureString --name /watchkeeper/anthropic_api_key --value 'sk-ant-...'
# terraform/terraform.tfvars に enable_triage = true を追記
make deploy
```

- アラームが「新たに」ALARMになったときだけ調査します(復旧時や鳴りっぱなしの間は調査しない)
- 同じチェックは30分に1回までしか調査しません(アラームが何度も鳴っても、料金が膨らまない)
- 調査結果はSlackに投稿され、DynamoDBにも記録が残ります
- `ai-triage-api-key` というチェックが自動で追加され、1時間ごとにAPIキーが有効かを確かめます(料金のかからない問い合わせ)。期限切れや無効化があれば、次の障害を待たずにSlackに通知されます
- Lambdaの権限は、ログ・メトリクス・CloudTrail・自分の状態の**読み取りだけ**です

---

## 7. まだ作っていないもの

- 承認ボタンと実行(フェーズ6)。今は提案を表示するだけです
- Vercelなどのデプロイ履歴の連携(フェーズ5)。AWS上では今のところ、ロールバック先の候補が空なので、ロールバックの提案は自動的に「人間に引き継ぎ」になります
