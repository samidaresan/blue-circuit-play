# GitHub Pages と Render で共有リンクを作る手順

ゲームの表示は GitHub Pages、ルームと対戦通信は Render の無料 Python Web Service で提供します。ゲームの共有 URL は `https://GitHubユーザー名.github.io/リポジトリ名/`、通信先は `wss://サービス名.onrender.com/ws` です。

公開用ファイルを用意しました。アカウント登録・ログイン・契約・GitHub へのアップロード・実際の公開は行っていません。以下のサービス上の操作は、ご自身のアカウントで行ってください。

## 1. 手元で GitHub 用のファイルを準備する

PowerShell / Python コマンドは、元のゲームプロジェクト `blue-circuit-fc` のフォルダーで実行します。公開用リポジトリには、Godot の元プロジェクトや書き出しツールは入れません。

現在の Web 出力を公開用にまとめます。

```powershell
python tools/prepare_pages.py
```

`build/pages_repository/` に、GitHub へアップロードする独立したリポジトリの中身が生成されます。現在の Web 出力が `build_manifest.json` と一致することを検証してから処理します。この検証だけでは、ゲームの元ファイルを変更した後に書き出し済みかは判断しません。ゲームを変更した後は、必ず次の順に実行してください。検証が失敗した場合も、エラーを解消してから再実行します。

```powershell
powershell -ExecutionPolicy Bypass -File tools/build_web.ps1
python tools/prepare_pages.py
```

生成される配置は次のとおりです。

```text
build/pages_repository/
├── .github/workflows/pages.yml   手動実行する Pages 公開処理
├── .gitignore
├── README.md                    この公開手順
├── render.yaml                  Render のサービス設定
├── server/relay_server.py        ルーム・対戦中継
├── tools/restore_pages.py        分割ファイルの復元・検証
└── publish/web/
    ├── manifest.json            元ファイルのサイズと SHA256
    └── chunks/                  最大 20 MiB の分割ファイル
```

**GitHub へアップロードするのは `build/pages_repository/` の中身だけです。** 元のゲームフォルダー全体や `build/web/` の原本を追加しないでください。実際の `.env`、元 GLB・Blender ファイル、テスト用ブラウザプロファイル、テンプレート、キャッシュは含まれません。

ゲームパックは約 125 MB、WebAssembly は約 40 MB です。GitHub は Git の単一ファイルを 100 MiB、ブラウザからの追加を 25 MiB までに制限します。このため最大 20 MiB に分け、GitHub Actions 内でサイズと SHA256 を確認して元の名前へ復元します。分割ファイルをゲームが直接読むことはありません。[GitHub のファイルサイズ制限](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)

Pages 用には原本の Web ファイルを使い、ローカル Python 配信向けの `.gz` 複製は含めません。公開データは約 165 MB です。配信時の圧縮や回線速度によって初回の読み込み時間は変わります。

## 2. GitHub に公開用リポジトリを作る

無料アカウントで Pages を使う場合は、**Public** リポジトリを作成します。リポジトリ内のファイルと公開ゲームは誰でも閲覧・ダウンロードできます。[Pages を利用できるプラン](https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages)

1. GitHub にログインし、**New repository** で新しいリポジトリを作ります。名前の例は `blue-circuit-play` です。
2. 公開範囲を **Public** にします。
3. **Add file → Upload files** から、`build/pages_repository/` の中身をフォルダー構造ごと追加し、コミットします。
4. リポジトリ直下に `render.yaml`、`server/`、`tools/`、`publish/` があることを確認します。`.github/workflows/pages.yml` と `.gitignore` も必要です。

`pages_repository/` 自体をリポジトリ内の子フォルダーとして置かないでください。`publish/web/manifest.json` と `publish/web/chunks/` のファイルが揃っていることを確認します。

この段階ではゲームの Pages 公開は始まりません。用意したワークフローは **手動実行のみ**で、アップロードをきっかけに自動公開しません。

## 3. Render に無料の対戦サーバーを作る

Render にログインし、GitHub の公開用リポジトリを接続します。**New → Web Service** でリポジトリを選び、`render.yaml` の設定に合わせます。静的サイトではなく **Web Service** を選択してください。Render は Python 中継だけを動かし、大きなゲームファイルは Pages が配信します。

| 項目 | 設定 |
| --- | --- |
| Language / Runtime | Python 3 |
| Root Directory | `server` |
| Build Command | `python -m py_compile relay_server.py` |
| Start Command | `python relay_server.py --host 0.0.0.0 --root public` |
| Instance Type / Plan | **Free** |
| Region | Singapore（`render.yaml` の `singapore`） |
| Health Check Path | `/health` |
| Auto-Deploy | Off（サーバー更新も手動） |
| 環境変数 `PYTHON_VERSION` | `3.12.15` |

起動コマンドの `--host 0.0.0.0` で外部からの通信を受けます。`BLUE_CIRCUIT_PORT` は設定しません。Render が提供する `PORT` をサーバーが使用します。公開 URL に内部ポート番号を追加する必要はありません。Python は標準ライブラリだけを使い、追加パッケージは不要です。`public` は静的ファイルの参照先です。**`server/public/` は作成不要です。** `/health` と WebSocket `/ws` は静的ファイルとは独立して処理されるため、フォルダーがなくても起動・中継できます。ゲームを置かない Render のトップページ `/` が 404 になるのは正常です。[Render のポート設定](https://render.com/docs/web-services#port-binding)

サービス作成後、表示された `https://サービス名.onrender.com` に `/health` を付けてブラウザで開き、`"status": "ok"` が表示されるまで待ちます。これは準備確認用です。友人へ渡すゲームのリンクは、後で Pages が表示する URL を使います。

## 4. 通信 URL を 1 か所に設定する

GitHub の公開用リポジトリで、**Settings → Secrets and variables → Actions → Variables → New repository variable** を開きます。

| Name | Value の例 |
| --- | --- |
| `PUBLIC_WEBSOCKET_URL` | `wss://サービス名.onrender.com/ws` |

Render の URL の `https://` を `wss://` にし、末尾へ `/ws` を付けます。通信 URL は公開情報なので **Variables** に設定します。アクセストークンやパスワードは不要です。

Actions がこの値を復元した Web 出力の `client-config.js` に入れます。ゲーム起動前に相対パスで読み込むため、Pages の `/リポジトリ名/` 配下でも使えます。Render の URL が変わった場合はこの変数を更新し、次の Pages 公開処理を再実行します。URL 変更のために Godot の再書き出しは不要です。

HTTPS のゲームからの通信には `wss://` を使います。`127.0.0.1`、`localhost`、`ws://` は公開接続先に指定しません。[Render の WebSocket 接続](https://render.com/docs/websocket)

## 5. GitHub Pages を手動で公開する

1. リポジトリの **Settings → Pages** を開き、**Build and deployment → Source** を **GitHub Actions** にします。
2. **Actions** を開き、`.github/workflows/pages.yml` のワークフローを選びます。
3. **Run workflow** でアップロードしたブランチを選び、実行します。
4. 分割データの復元・検証と Pages デプロイの成功を確認します。
5. **Settings → Pages** または実行結果に表示された URL を開きます。

```text
https://GitHubユーザー名.github.io/リポジトリ名/
```

Actions は `build/pages_site/` に復元した通常の `index.html`、`.js`、`.wasm`、`.pck` と設定・ライセンスを Pages の成果物へ渡します。`.nojekyll` も生成します。大きな復元ファイルを Git の履歴へ追加する必要はありません。[Pages のカスタムワークフロー](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)

Pages の公開サイト全体は 1 GB 以下、デプロイの制限時間は 10 分です。公式成果物条件では単一ファイル 100 MiB の制限は記載されていませんが、今回の約 125 MB PCK が実際の Pages で配信できるかは、公開後に確認してください。[Pages の利用制限](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits)、[公式成果物条件](https://github.com/actions/upload-pages-artifact#artifact-validation)

## 6. 2 台の PC で対戦を確認して共有する

キーボード・マウスを使える 2 台の PC で確認します。可能なら片方を別のインターネット回線にします。

1. Render の `/health` を別タブで開き、`"status": "ok"` を確認します。休止後の起動には約 1 分かかる場合があります。
2. 両者が Pages のゲーム URL を開き、日本語と両キャラクターの表示を確認します。
3. 1P が「オンライン対戦」からルームを作成し、表示された 6 文字のコードを 2P に伝えます。
4. 2P がルームに参加し、両者が独立してアスカ／ミサキを選びます。
5. 片方だけ準備完了にして開始しないことを確認し、双方を準備完了にします。
6. 移動、旋回、飛行姿勢、共通ブイ、得点、試合終了が両者で一致することを確認します。
7. 片方のゲームを閉じて切断表示を確認し、タイトルへ戻って CPU 対戦も開始します。

確認後に共有するリンクは **Pages のゲーム URL** です。対戦ごとのルームコードも相手に伝えます。URL だけでは既存ルームに自動参加しません。CPU 対戦は Render への接続なしで遊べます。

Render が起動中に接続失敗になった場合は、`/health` が準備完了になった後でルーム作成・参加をやり直します。休止・再起動後の古いルームコードは使わず、新しく作成してください。

## 無料サービスの制約と更新

Render 無料版は、15 分間 HTTP リクエストも既存 WebSocket のメッセージも受信しないと休止します。次のアクセスで起動しますが、約 1 分かかります。稼働中の試合には WebSocket 通信があります。無料サービスは予告なく再起動される場合があり、その際は接続とメモリー内のルームが失われます。[Render 無料サービスの仕様](https://render.com/docs/free)

無料稼働時間はワークスペース全体で月 750 時間です。転送量・ビルドの無料枠もあります。支払い方法を登録している場合は超過分が課金されることがあるため、Dashboard の利用量を確認してください。支払い方法がない場合は、上限到達後にサービスや新しいビルドが停止する場合があります。常時稼働や無制限の無料対戦を保証する構成ではありません。[Render の利用枠](https://render.com/docs/free#monthly-usage-limits)

Pages にも月 100 GB の転送量目安やレート制限があります。初回データが大きいため、広く共有する前に読み込みと利用量を確認してください。[Pages の利用制限](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits)

ゲーム更新時は元のゲームフォルダーで Web 書き出しと `prepare_pages.py` を実行し、生成された `build/pages_repository/` の中身で公開用リポジトリを更新します。不要になった古い分割ファイルも公開用リポジトリから取り除き、その後 **Run workflow** を実行します。中継スクリプトを変更した場合は Render のサービス画面で **Manual Deploy → Deploy latest commit** を実行します。サーバー更新中の試合は切断されるため、対戦中の更新は避けてください。

## 確認範囲

追加確認として、生成済みの `build/pages_repository/server/` を作業場所にし、上記と同じ起動コマンドをローカルで実行しました。`public` が存在しない状態と、環境変数 `PORT` による待ち受けを確認し、`/health` の 200、トップページの 404、二つの WebSocket 接続、ルーム作成・参加・双方の準備完了・試合開始・操作と得点／共通ブイデータの中継・切断通知・ルーム片付けが成功しました。確認用プロセスは終了済みです。証拠は `tests/artifacts/pages_render/render_startup_recheck.json` と `.log`、再確認用スクリプトは同じ場所の `.py` に保存しています。これは Windows 上の Python による HTTP/WS 試験で、Render 上の Linux/Python や公開 HTTPS/WSS を実行した結果ではありません。

ローカルで Pages と同じサブパス `/blue-circuit-fc/` から、COOP/COEP ヘッダーのない静的 HTTP 配信で実際のゲームを読み込みました。別ポートの中継専用サーバーへ独立した2つのEdgeから接続し、ルーム作成・コード参加・アスカ／ミサキの個別選択・双方の準備・移動・得点 `[1, 0]` と共通目標 `BUOY 02` の同期・相手切断・CPU対戦への復帰まで確認しました。最終試験に初回読み込み再試行やブラウザエラーはありません。これはローカルHTTP/WSによる試験で、公開HTTPS/WSSの実通信確認ではありません。

Python中継22テスト、既存Godotの共通ブイ・CPU移動46項目と切断復帰9項目、分割データ復元・破損拒否・URL/公開範囲の8テストが成功しています。復元したPCK・WASM・画像・音声補助・フォントライセンスはWeb原本とSHA256で一致し、公開用設定だけを差し替えます。確認用Edgeはソフトウェア描画のため、2画面同時実行ではゲーム内時間がゆっくり進みます。証拠は元プロジェクトの `tests/artifacts/pages_render/` に保存しています。

GitHub と Render の公式資料を確認して構成と手順を用意しました。GitHub へのアップロード、Actions の実行、Pages 公開、Render のサービス作成・起動、公開回線での対戦は未実施です。ローカルでの復元・ゲーム・通信の確認結果は、実際の公開後の確認と分けて扱います。
