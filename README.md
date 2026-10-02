# 履修登録キャンセル待ちbot (KULASIS)

京都大学の履修登録システム（KULASIS）で、先着順の科目の空きを監視し、空きが出たら「候補科目への追加」を自動で送って Discord に通知する。

## 概要
- **ログイン**: 京大統合認証（ID/パスワード + TOTP）と SAML 中継を自動処理する。
- **空き監視**: 「先着順対象科目」の検索結果から、科目ごとの申込数/定員を判定する。
- **自動申込**: 空きを検知すると candidate_add（候補科目への追加）を送り、時間割に載ったことを確認する。
- **通知**: 空き・満席に戻った・申込失敗・エラーを Discord Webhook に送る。
- **定期実行**: cron-job.org が GitHub Actions の `workflow_dispatch` を起動する。状態は `state.json` に保存し、Actions が自動コミットする。

履修登録の**確定は自動化していない**。登録期間中に「登録科目の決定へ」から手動で行う。

## 動作モード（watch / rush）

| 項目 | watch（通常。既定） | rush（先着順の公開直後） |
| :--- | :--- | :--- |
| 方針 | 速さ優先。回復しなければ短く切り上げ、次の定期実行に任せる | 混雑で重いサイトでも待ち切る |
| 事前取得（トップ・時間割ページ） | なし | なし |
| 履修登録ページの読み取り待ち | 30秒 | 240秒 |
| GET の最大試行回数 | 3回（失敗後の待ち 0.5 / 1 / 2秒） | 5回（待ち 1 / 2 / 4 / 8秒） |
| 申込の最大試行回数 | 5回（待ち 0.3 / 0.5 / 1 / 2秒） | 5回（待ち 0.5 / 1 / 2 / 3秒） |
| 再試行の予算 / 実行の上限 | 90秒 / 120秒 | 360秒 / 480秒 |
| 科目間の待ち | 0秒 | 0秒 |
| `lecture_no` による直接申込 | しない（常に検索して空きを判定） | する（検索を省略して先に送る） |

共通の安全策: POST は二重処理の恐れがあるため再送しない。申込の再送前には時間割で反映を確認する。429 / Retry-After に従う。TOTP は同じコードを再送せず、次の30秒枠まで待つ。

数値は `config.yml` の `modes.<モード名>` で上書きできる。

```yaml
modes:
  watch:
    timeslot_read_timeout_sec: 45
    retry_waits: [0.5, 1, 2]
```

使える項目: `warmup` / `timeslot_read_timeout_sec` / `course_gap_sec` / `req_max_attempts` / `retry_waits` / `apply_max_attempts` / `apply_retry_waits` / `retry_budget_sec` / `run_deadline_sec` / `direct_apply`

### モードの決まり方
優先順位は `--mode` > 環境変数 `KULASIS_MODE` > `config.yml` の `mode` > `watch`。

### GitHub Actions からの切り替え
- **手動**: Actions タブ → KULASIS Monitor → Run workflow → `mode` のプルダウンで `watch` / `rush` を選ぶ。
  確認: 実行ログの `[モード]` の行が選んだモードになっている。
- **cron-job.org**: GitHub API の `POST /repos/{owner}/{repo}/actions/workflows/KULASIS_Monitor.yml/dispatches` を叩き、Request body で指定する。
  - `ref`: 実行するブランチ（例: `main`）
  - `inputs.mode`: `watch` または `rush`。省略するとワークフローの既定値（`watch`）が使われる。
  - 例: `{"ref":"main","inputs":{"mode":"rush"}}`
  - 運用: 通常の定期ジョブは watch、公開直前の単発ジョブだけ rush にする。
- **watch と rush は同時に動かさない**。実行時刻は運用側で重ならないように調整する。ワークフローの `concurrency` は、重なった場合に後の実行を待たせるだけで、待たされた分は遅れる。

## 監視科目の設定（courses.yml）

```yaml
courses:
  - name: Programming Practice (Python) -E2
    day: 水
    period: 5
    lecture_no: "64073"   # 任意
```

| キー | 説明 |
| :--- | :--- |
| `name` | 通知に表示する名前 |
| `day` / `period` | 曜日（月〜土）と時限（1〜6） |
| `match` | 任意。KULASIS上の科目名と照合する文字列（省略時は `name`）。全角/半角・Ⅱ/II・空白の違いは吸収される |
| `lecture_no` | 任意。数字のみ。先頭の0もそのまま保たれる |

### lecture_no による直接申込（rush 用）
`lecture_no` を書いた科目は、rush では検索を省略して先に申込を送る。ページ遷移が減り、公開直後に最短で申込める。

1. watch（または `--dry-run`）を1回実行する。見つかった科目の下に `lecture_no: 数字` が出る。
2. その数字を `courses.yml` の該当科目の `lecture_no` に写して push する。
3. rush で実行する。成功の目安: `[申込] ✅ ... 直接申込 成功` が出て、`[検索] ⏭ 全科目を直接申込で処理したため、検索は省略` になる。

注意:
- 直接申込は、空き状況を見ずに「追加」を送る。満席の科目に送った場合の挙動は未確認。失敗した科目は、そのあと通常の検索で状態（満席など）を確認する。
- `courses.yml` の**上の科目から順に**送る。優先したい科目を上に書く。
- `state.json` に申込済みの記録がある科目は、直接申込をしない。
- `apply.auto_apply: false` のときは、直接申込をせず通常の検索だけ行う。

## ログの読み方
各行の先頭 `[  3.21s]` は、プロセス開始からの経過秒。最初の `[開始]` の行に開始時刻（JST と UTC）が出る。`⏱` の行は、その処理の所要時間（ログイン、検索、直接申込、Discord通知、state保存）。実行全体の時間は最後の `[完了]` の行に出る。

| 記号 | 意味 |
| :--- | :--- |
| 🟢 | 空きあり |
| ❌ | 満席・失敗 |
| 🚀 | 直接申込 |
| ⏱ | 所要時間 |
| ⚠️ | 検索結果に見つからない |

## 構成

```
.
├── .github/workflows/KULASIS_Monitor.yml  # 実行定義（workflow_dispatch + mode 選択）
├── src/
│   ├── main.py            # エントリポイント・申込と通知の制御
│   ├── config.py          # courses.yml / config.yml の読み込み・動作モード（Profile）
│   ├── kulasis_client.py  # ログイン・検索・申込・再送
│   ├── parser.py          # 検索結果HTMLの解析
│   ├── state.py           # state.json の読み書き・通知する状態遷移
│   ├── notify.py          # Discord 通知
│   └── logutil.py         # 経過秒つきログ・所要時間の計測
├── courses.yml            # 監視科目
├── config.yml             # 接続先URL・自動申込・モードの上書き
├── state.json             # 状態（Actions が自動コミット）
├── test_core.py / test_modes.py
└── requirements.txt
```

## セットアップ

### GitHub Secrets
`Settings > Secrets and variables > Actions` に登録する。

| Key | 説明 |
| :--- | :--- |
| `KULASIS_USER` | ECS-ID |
| `KULASIS_PASSWORD` | ECS-ID のパスワード |
| `TOTP_SECRET` | TOTP の Base32 シークレットキー |
| `DISCORD_WEBHOOK_URL` | 通知先の Discord Webhook URL |

### ローカル実行
環境変数を設定するか `.env` を作る。

```bash
pip install -r requirements.txt
python -m src.main --mode watch --dry-run   # 通知・申込・state保存をせず確認
python -m src.main --mode rush              # 本番（rush）
python -m src.main --test-discord           # Discord にテスト通知だけ送る
python -m pytest -q                         # テスト
```

`--dry-run` は `debug_fetched.html` を保存する（`.gitignore` 済み）。

## 注意事項
1. **アカウントロック**: 認証への連続アクセスが多いと、IP ブロックや一時凍結を受ける恐れがある。定期実行の間隔は5分以上にする。
2. **rush の取り消し**: 追加は「候補科目」への追加。取り消しの可否・手順は KULASIS 側の画面で確認する。
3. **Discord 通知の失敗**: 通知に失敗しても処理は続き、ログに `❌ Discord通知に失敗` が出る（state は保存される）。