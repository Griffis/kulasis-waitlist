# 履修登録早押しbot (KULASIS)

京都大学教務情報システム（KULASIS）の履修制限（抽選・定員オーバー）科目の空き状況を監視し、空きが出た際に Discord へ通知する自動監視システムです。

## 概要
- **統合認証突破**: 京大統合認証システム（ID/Password）および TOTP（Time-based One-Time Password：時間依存型ワンタイムパスワード）を用いた MFA（Multi-Factor Authentication：多要素認証）を自動処理。
- **セッション維持**: SAML（Security Assertion Markup Language：シングルサインオン規格）認証フローおよび URL 内の `sessid` を追跡してセッション切れを防止。
- **空き枠監視**: 履修制限一覧ページを周期的にスクレイピングし、指定した科目の「申込数 / 定員」を判定。
- **通知機能**: 空き枠検知時およびエラー発生時に Discord Webhook（ウェブフック：イベント発生時にリアルタイム通知する仕組み）へメッセージを送信。
- **自動化**: GitHub Actions（Cron：指定日時に自動実行するスケジューラ）を利用した無人定期監視。

---

## ディレクトリ構造と役割

```
履修登録早押しbot/
├── .github/
│   └── workflows/
│       └── run.yml          # GitHub Actions の自動実行定義ファイル
├── src/
│   ├── __init__.py
│   ├── main.py              # アプリケーションのエントリポイント（全体の制御）
│   ├── kulasis_client.py    # KULASIS ログイン・ページ取得・HTML解析ロジック
│   └── notify.py            # Discord への通知送信モジュール
├── config.yml               # 監視対象科目の設定ファイル
├── requirements.txt         # 依存ライブラリ一覧
├── .gitignore               # Git 管理対象外ファイルの設定
└── README.md                # 本ドキュメント
```

### 主要ファイルの役割

- **`src/main.py`**:
  環境変数や `config.yml` を読み込み、`KulasisClient` を通じてログインとページ取得を実行します。解析結果に基づいて Discord 通知を呼び出します。
- **`src/kulasis_client.py`**:
  `requests` および `BeautifulSoup4` を使用し、京大統合認証（`login.cgi` ➔ `authselect.php` ➔ `otplogin.cgi`）のリダイレクト・`sessid` 補完・TOTP 自動生成を処理します。
- **`src/notify.py`**:
  Discord の Webhook URL に対して、検知結果やエラーログを整形して POST 送信します。
- **`config.yml`**:
  監視したい科目の名称や曜時限を YAML（ヤムル：構造化データを記述するフォーマット）形式で定義します。

---

## 監視科目の追加・変更方法

`config.yml` を編集することで、監視対象の科目を自由に追加・削除できます。

```yaml
courses:
  - name: "Programming Practice (Python) -E2"
    day_period: "水5"
  - name: "人文地理学"
    day_period: "月2"
```

### 設定時の注意点
* **`name`**: KULASIS の履修制限一覧画面に表示されている**正確な科目名**を指定してください（全角・半角やスペースの違いで判定エラーになります）。
* **`day_period`**: 曜時限（例: `月1`, `水5` など）を指定します。

---

## セットアップと運用方法

### 1. 依存ライブラリのインストール（ローカル開発時）
```bash
pip install -r requirements.txt
```

### 2. 環境変数の設定 (GitHub Secrets)
本リポジトリでは認証情報を Git に含めないため、GitHub 上の Secrets に登録して運用します。

リポジトリの `Settings > Secrets and variables > Actions` から以下の Secret を登録してください。

| Key | 説明 |
| :--- | :--- |
| `KULASIS_USER` | ECS-ID（例: `a0264398`） |
| `KULASIS_PASSWORD` | ECS-ID のパスワード |
| `TOTP_SECRET` | 統合認証システムで発行した TOTP の Base32 シークレットキー |
| `DISCORD_WEBHOOK_URL` | 通知先の Discord Webhook URL |

---

## 実行コマンド (ローカル環境)

※ ローカルで実行する場合は、事前に環境変数をセットするか `.env` ファイルを作成してください。

### ドライラン（動作確認）
実際の登録・通知処理テスト（デバッグログ出力あり）を行います。
```bash
python -m src.main --dry-run
```

### 本番実行
```bash
python -m src.main
```

---

## 注意事項・リスク

1. **アカウントロックのリスク**:
   GitHub Actions からのアクセス頻度が高すぎると、京大統合認証システムから IP ブロックやアカウント一時凍結を受ける可能性があります。Cron 実行の周期は最短でも 5 分〜10 分以上に設定してください。
2. **TOTP の時刻同期**:
   2 段階認証コードの生成には正確な時刻が必要です。GitHub Actions 上では自動的に UTC 時刻が同期されています。
