# kulasis-waitlist

KULASISの「履修(人数)制限」ページを監視し、空きが出たらDiscordに通知する（設定次第で自動申込も送る）ボット。
構成: **GitHub Actions + cron-job.org + Discord Webhook**（mano-notifier と同じ）。

```
cron-job.org (5分おき)
  └─ POST GitHub API workflow_dispatch
       └─ Actions: python -m src.main
            ├─ KULASISにログイン → 履修(人数)制限ページを1回取得 → 全行を解析
            ├─ state.json と比較（満席→空き の遷移だけ通知）
            ├─ (任意) 空きを検知したら自動で「申込」を送信
            └─ Discord Webhook に通知 / state.json をコミット
```

## ⚠️ 最重要：対象5科目がまだ見つかっていません

いただいた「履修(人数)制限」ページ（森川 駿 さんのMy Page、`student/la/entrylimit/regist`）を確認しましたが、
`courses.yml` の5科目（人文地理学、宗教学各論II（死生学）、宗教学II、言語学II、社会学II）はこの一覧の中に
**目視では見つかりませんでした**（プログラムでの再検証はしていません）。このページには「無作為抽選」対象の
人数制限科目が並んでおり、ご依頼にあった「先着順」とは別の仕組みの可能性があります。

**使う前に**: `--offline-html`（下記「手順3」参照）で実際に一致するか確認し、一致しなければ
- 科目名・曜時限が合っているか（シラバスで確認）
- このページが本当に対象のページか（他に「履修登録」ページの先着順申込対象科目一覧があるかもしれない）

を見直してください。

## ⚠️ その他の未検証の部分

- `config.yml` の `pages.entrylimit` のドメイン名（`https://www.k.kyoto-u.ac.jp` は推測、要確認）
- ログイン後にSAMLで戻ってくる流れ、および多要素認証（ワンタイムパスワード）が実際にどう出るか
  - いただいたログイン画面のボタンHTMLを元に「OTP送信ボタンが非表示のままかどうか」で判定するよう修正済みだが、実際にOTPが必要なアカウントかどうかは未確認
  - KULMS+ 拡張機能によるOTP省略の仕組みは、次回いただく情報を元に別途対応する予定（今回は未実装）
- `apply()`（自動申込）: 1クリックで確定するのか確認画面を挟むのか未検証。確認フォームが続く限り最大3回送信するようにしているが、実際の挙動は要確認

## 自動申込について（要注意）

`config.yml` の `apply.auto_apply: true` にすると、空きを検知した瞬間に自動で申込を送信します。
**取り消せない可能性がある操作**なので、必ず次の順で確認してください。

1. `python -m src.main --offline-html local/sample.html` でパーサ・科目の一致を確認
2. `python -m src.main --dry-run` でログイン・取得・判定だけ確認（申込は送信されない）
3. `auto_apply: false` のまま1回 `python -m src.main` を手動実行し、通知が正しいか確認
4. 上記が全て問題なければ `auto_apply: true` にし、まず自分で1科目だけ試す

同じ科目には一度申込を送信したら（`state.json` に記録）、それ以降は再送信しません。

## 科目の追加

`courses.yml` に1ブロック足して push するだけ。

```yaml
  - name: 経済学II
    day: 水
    period: 3
```

## セットアップ（各手順の「成功の目安」つき）

### 1. リポジトリ作成

`kulasis-waitlist` を **Public** で作成し、このフォルダの中身を push。
（Private だと Actions は月2,000分の枠で、5分おき=月約8,600回実行は枠を超える。Public の標準ランナーは無料）
Public にしても Secrets は非公開。`courses.yml` と `state.json` は見える。

- ✅ 成功: GitHub の Actions タブに `check` ワークフローが表示される。

### 2. Secrets 登録（Settings → Secrets and variables → Actions）

| 名前 | 内容 |
|---|---|
| `KULASIS_USER` | ECS-ID |
| `KULASIS_PASSWORD` | ECS-IDのパスワード |
| `DISCORD_WEBHOOK_URL` | Discord Webhook URL |
| `DISCORD_USER_ID` | （任意）メンション用のDiscordユーザーID |

- ✅ 成功: Secrets 一覧に上記が並ぶ（値は再表示されない）。

### 3. 実サイトに合わせる（最重要）

1. ブラウザでKULASISにログイン → 「履修(人数)制限」ページを開く。
2. アドレスバーのURLを `config.yml` の `pages.entrylimit` に反映。
3. **ページを「名前を付けて保存」**して `local/sample.html` に置く（個人情報・自分の在籍情報が含まれるので取り扱い注意、コミットしない）。
4. パーサ・科目一致の確認:
   ```bash
   pip install -r requirements-dev.txt
   python -m src.main --offline-html local/sample.html
   ```
   - ✅ 成功: 5科目それぞれが `available / full / not_found` と `申込数/定員` で表示される。
   - ❌ 全て `not_found` の場合、科目名(`match`)またはこのページ自体が対象と違う可能性が高い（上記「最重要」参照）。
5. ログインと取得の確認（ローカル、通知・申込なし）:
   ```bash
   export KULASIS_USER=... KULASIS_PASSWORD=...
   python -m src.main --dry-run
   ```
   - ✅ 成功: `月2:人文地理学: None -> full 30/30` のような行が5つ出る。
   - ❌ `MfaRequired` → 多要素認証が実際に要求されている。別方式の検討が必要（KULMS+の情報待ち）。
   - ❌ `LoginError` → ID/パスワード誤り、またはフォーム構造の想定違い。

> **保存したHTML/HAR の取り扱い注意**: 個人の履修情報・Cookie・パスワードが含まれうる。共有・コミットしない（`.gitignore`済み）。

### 4. Discord 疎通

```bash
export DISCORD_WEBHOOK_URL=...
python -m src.main --test-discord
```
- ✅ 成功: Discordに「✅ kulasis-waitlist: テスト通知」が届く。

### 5. Actions を手動実行

Actions タブ → `check` → Run workflow。
- ✅ 成功: 緑のチェック。`state.json` が更新されたコミット（変化があった場合のみ）が入る。

### 6. cron-job.org から5分おきに起動

1. GitHub → Settings → Developer settings → Fine-grained personal access token を作成。
   対象リポジトリはこの1つだけ、権限は **Actions: Read and write**。
2. cron-job.org で新規ジョブ:
   - URL: `https://api.github.com/repos/Griffis/kulasis-waitlist/actions/workflows/check.yml/dispatches`
   - Method: `POST`
   - Headers: `Authorization: Bearer <トークン>` / `Accept: application/vnd.github+json` / `Content-Type: application/json`
   - Body: `{"ref":"main"}`
   - Schedule: 5分おき
- ✅ 成功: cron-job.org の実行履歴が 2xx（GitHub Docs上は 200/204）で、Actions タブに実行が5分ごとに増える。

## 通知の仕様

| 状況 | 通知 |
|---|---|
| 満席（または初回観測）→ 空き | 🟢 空きが出ました（`auto_apply: true` なら自動申込の結果も付記） |
| 空き → 満席 | 🔴 満席に戻りました（`notify_closed: false` で無効化） |
| 科目が見つからない | ⚠️ 状態が変わったとき1回だけ（`warn_not_found: false` で無効化） |
| ログイン失敗・取得失敗・自動申込失敗 | ❌ |
| 初回で満席 | 通知なし（基準として記録） |

## 運用メモ

- KULASISは同一画面で30分操作がないと自動ログアウトされる（本ボットは毎回ログインし直す）。
- 5分おきの自動ログインは大学システムへの継続的アクセスになる。負荷・利用規約に配慮すること。
- 開発: `pip install -r requirements-dev.txt && pytest`
