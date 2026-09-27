# 履修登録早押しbot (KULASIS)

京都大学教務情報システム（KULASIS）の履修制限（抽選・定員オーバー）科目の空き状況を監視し、空きが出た際に Discord へ通知、設定によっては自動で申込を行う監視システムです。

## 概要
- **統合認証突破**: 京大統合認証システム（ID/Password）および TOTP（Time-based One-Time Password：時間依存型ワンタイムパスワード）を用いた MFA（Multi-Factor Authentication：多要素認証）を自動処理。
- **セッション維持**: SAML（Security Assertion Markup Language：シングルサインオン規格）認証フローおよび URL 内の `sessid` を追跡してセッション切れを防止。
- **空き枠監視**: 履修制限一覧ページ（`student/la/entrylimit/regist`）を周期的にスクレイピングし、指定した科目の「申込数 / 定員」を判定。
- **自動申込**: `config.yml`の`apply.auto_apply`が`true`の場合、空きを検知した科目に自動で申込（`regist_check`→確認画面`primaryId`→`regist_save`の2段階）を送信。
- **通知機能**: 空き検知・満席復帰・科目未検出・自動申込結果・連続失敗・エラー発生時に Discord Webhook（ウェブフック：イベント発生時にリアルタイム通知する仕組み）へメッセージを送信。
- **状態保存**: `state.json`に科目ごとの状態（available/full/not_found）と申込済みフラグのみを保存し、`git-auto-commit-action`でコミット（申込者数など毎回変わる値は保存しない）。
- **自動化**: GitHub Actions（`workflow_dispatch`）を cron-job.org 等の外部スケジューラから5分間隔でキックすることで無人定期監視を実現。

---

## ディレクトリ構造と役割

```
履修登録キャンセル待ち/
├── .github/
│   └── workflows/
│       └── KULASIS_Monitor.yml  # GitHub Actions の実行定義（workflow_dispatch起動）
├── src/
│   ├── __init__.py
│   ├── main.py               # エントリポイント（全体の制御・自動申込判定）
│   ├── config.py             # courses.yml / config.yml の読み込み、科目名正規化
│   ├── kulasis_client.py     # KULASIS ログイン・ページ取得・申込送信ロジック
│   ├── parser.py             # 履修制限一覧ページのHTML解析
│   └── notify.py             # Discord への通知送信モジュール
├── config.yml                # ログイン先・ページURL・自動申込の設定ファイル
├── courses.yml                # 監視対象科目の設定ファイル
├── state.json                 # 監視状態の保存ファイル（Actionsが自動コミット）
├── test_core.py               # パーサ・照合・状態遷移のユニットテスト
├── requirements.txt           # 依存ライブラリ一覧
├── .gitignore                 # Git 管理対象外ファイルの設定
└── README.md                  # 本ドキュメント
```

### 主要ファイルの役割

- **`src/main.py`**:
  `courses.yml`・`config.yml`・`state.json`を読み込み、`KulasisClient`でログイン・ページ取得・解析を実行。科目ごとに状態遷移を判定し、Discord通知および（`auto_apply`有効時の）自動申込を行う。
- **`src/config.py`**:
  `courses.yml`（科目リスト）と`config.yml`（接続設定）を読み込み、`Course`データクラスを構築。科目名のNFKC正規化（全角/半角・ローマ数字Ⅱ/II・空白の違いを吸収）を行う。
- **`src/kulasis_client.py`**:
  `requests`および`BeautifulSoup4`を使用し、京大統合認証（ID/PW → SAML中継 → authselect.php → otplogin.cgi）のリダイレクト・`sessid`補完・TOTP自動生成でのログインと、履修制限ページ取得、および自動申込（`regist_check`→確認画面→`regist_save`）を処理する。
- **`src/parser.py`**:
  履修(人数)制限ページ（状態/曜時限/科目名/担当教員/開講期/群/旧群/申込数・定員/抽選方法/申込の10列構成）をパースし、`EntryRow`のリストを返す。
- **`src/notify.py`**:
  DiscordのWebhook URLに対して、検知結果やエラーログを整形してPOST送信する。
- **`src/state.py`**:
  `state.json`の読み書きと、通知すべき状態遷移（空き発生/満席復帰/未検出）の判定を行う。
- **`test_core.py`**:
  パーサ・科目照合・状態遷移ロジックのユニットテスト（実サイトのHTML構造を模した合成データを使用）。

---

## 監視科目の追加・変更方法

`courses.yml`を編集することで、監視対象の科目を自由に追加・削除できます。

```yaml
courses:
  - name: Programming Practice (Python) -E2
    day: 水
    period: 5

  - name: 宗教学各論II（死生学）
    day: 火
    period: 1
    match: 宗教学各論II
```

### 設定時の注意点
* **`name`**: 通知に表示する名前。
* **`day`** / **`period`**: 曜日（月〜土の1文字）と時限（1〜6）を指定。
* **`match`**: （任意）KULASIS上の科目名との照合に使う文字列。省略時は`name`が使われる。全角/半角・ローマ数字（Ⅱ/II）・空白の違いは自動で吸収されるため、通常は完全一致でなくてよい。

---

## 自動申込設定 (`config.yml`)

```yaml
apply:
  auto_apply: true          # trueで空き検知時に自動申込を送信
  fail_notify_threshold: 3  # 申込が何回"連続"失敗したら通知するか
```

- `auto_apply: true`の場合、空きを検知しかつKULASIS側・state側の双方で未申込の科目に対して自動で申込を送信する。
- 申込は**取り消し不可の可能性がある**ため、有効化前に必ず`--dry-run`とローカルでの手動実行で挙動を確認すること。
- 科目ごとの個別ON/OFFはなく、全科目まとめてのON/OFFのみ。

---

## セットアップと運用方法

### 1. 依存ライブラリのインストール（ローカル開発時）
```bash
pip install -r requirements.txt
```

### 2. 環境変数の設定 (GitHub Secrets)
本リポジトリでは認証情報をGitに含めないため、GitHub上のSecretsに登録して運用します。

リポジトリの`Settings > Secrets and variables > Actions`から以下のSecretを登録してください。

| Key | 説明 |
| :--- | :--- |
| `KULASIS_USER` | ECS-ID（例: `a0264398`） |
| `KULASIS_PASSWORD` | ECS-IDのパスワード |
| `TOTP_SECRET` | 統合認証システムで発行したTOTPのBase32シークレットキー |
| `DISCORD_WEBHOOK_URL` | 通知先のDiscord Webhook URL |

### 3. 定期実行の設定 (cron-job.org)
`KULASIS_Monitor.yml`は`workflow_dispatch`トリガーのため、GitHub単体では定期実行されません。cron-job.org等の外部スケジューラからGitHub API（`POST /repos/{owner}/{repo}/actions/workflows/KULASIS_Monitor.yml/dispatches`）を5分間隔で呼び出す設定が必要です（GitHubのPersonal Access Tokenが必要）。

---

## 実行コマンド (ローカル環境)

※ローカルで実行する場合は、事前に環境変数をセットするか`.env`ファイルを作成してください。

### ドライラン（動作確認、通知・state保存・自動申込なし）
```bash
python -m src.main --dry-run
```

### Discordテスト通知のみ
```bash
python -m src.main --test-discord
```

### 保存済みHTMLでパーサのみ確認
```bash
python -m src.main --offline-html debug_fetched.html
```

### 本番実行
```bash
python -m src.main
```

---

## 注意事項・リスク

1. **アカウントロックのリスク**:
   GitHub Actionsからのアクセス頻度が高すぎると、京大統合認証システムからIPブロックやアカウント一時凍結を受ける可能性があります。実行周期は最短でも5〜10分以上に設定してください。
2. **TOTPの時刻同期**:
   2段階認証コードの生成には正確な時刻が必要です。GitHub Actions上では自動的にUTC時刻が同期されています。
3. **自動申込の不可逆性**:
   `auto_apply: true`時の申込は取り消せない可能性があります。有効化前に必ずローカルで挙動確認をしてください。
4. **state.jsonのコミット**:
   `.gitignore`に`state.json`を含めないよう注意してください（含めるとActionsでの状態保存が機能しません）。