# CLAUDE.md

このファイルは、このリポジトリで作業するClaude Codeセッションが、再説明なしにプロジェクトの背景・仕様・設計方針を理解するためのものです。

## プロジェクト概要

三菱UFJ eスマート証券の **kabuステーション®API**（REST API / PUSH API）を使い、日本株のデイトレードを自動化するシステム。現物取引・買いエントリーがメインで、スクリーニングした30銘柄を常時監視し、テクニカル指標に基づくシグナルでエントリー・決済を自動実行する。GUI（PySide6）で設定・状態確認を行い、バックテスト機能も備える。

- 要件定義書・基本設計書は同リポジトリ内（または別途共有）の以下を参照：
  - `要件定義書_日本株デイトレード自動売買システム.md`（v0.9、要確認事項は全て確定済み）
  - `基本設計書_日本株デイトレード自動売買システム.md`（v0.1、12章の残項目も全て確定済み）
- 本CLAUDE.mdは上記2文書のサマリ＋実装規約であり、詳細な理由や背景は原文書を参照すること

## 技術スタック

| 項目 | 内容 |
|---|---|
| 言語 | Python 3.11以降 |
| GUI | PySide6 + pyqtgraph（確定） |
| DB | SQLite（確定） |
| REST通信 | requests（kabuステーションREST API） |
| WebSocket | websocket-client または websockets（kabuステーションPUSH API） |
| データ処理 | pandas |
| 稼働環境 | kabuステーション®と**同一Windows PC上**で常駐稼働（API制約上、別PCからのアクセス不可） |

## 確定済みの主要仕様（変更時は要相談）

### 取引ロジック
- 現物取引・買いエントリーのみ（信用取引・空売りはスコープ外）
- **注文方式：エントリー・決済（利確／損切とも）すべて成行**
- 1銘柄あたりの発注株数は**固定100株**（設定で変更可）
- 買付順序：スクリーナー順位の上位から、現物買付余力がなくなるまで順次エントリー
- **同一銘柄への重複エントリーは禁止**（1銘柄につき同時に持てるポジションは1つまで）。異なる銘柄の同時保有は前提

### リスク管理
- ~~損切ライン：買値の97%を下回ったら成行決済~~ → **廃止・トレール決済に統一**：寄り付き直後4本（既定、3分足なら09:00〜09:12）のTR平均から「当日のAR（値幅目安）」を算出し、保有中の最高値からAR×倍率（既定**2.0**、GUIから変更可）だけ下落したら成行決済する。詳細は下記「AR×倍率トレール決済」の項を参照
- 日次利益上限：**2万円**（設定で変更可）。到達したら新規エントリー停止＋保有ポジション全決済
- 日次最大損失：**2万円**（設定で変更可）。到達したら新規エントリー停止＋保有ポジション全決済
- 強制引け決済：目安**15:00**（設定で変更可）に未決済ポジションを成行で自動決済
- 新規エントリー開始時刻：既定**09:12**（設定で変更可）より前は新規エントリーを行わない（寄り付き直後の高ボラティリティを回避。決済は対象外）
- PUSH API切断時：**最大3回まで自動再接続をリトライ**、失敗したら保有ポジション全件を成行で強制決済

### シグナル・指標
- 初期実装の指標：移動平均、RSI、ボリンジャーバンド、VWAP
- **拡張性が必須要件**：新指標は`Indicator`抽象基底クラスを継承し`indicators/`配下に追加、レジストリに登録するだけで組み込める設計にすること（既存コードの変更を伴う実装は避ける）
- シグナル判定ルール（`entry_rule`/`exit_rule`）は**GUI上のJSON直接編集**で管理する（当面は専用エディタUIを作らない）
- 本番のシグナル判定ロジックとバックテストのロジックは**必ず共通モジュール化**し、両者で分岐・乖離させない

### データソース
- **スクリーニング／銘柄リストCSV**：TradingView等から**手動エクスポート**した銘柄情報をもとに、下記フォーマットのCSVを用意する。GUIの監視状況タブ「銘柄リストCSVを読込」機能（`gui/tabs/monitor_tab.py`）で読み込む形で実装済み（`ScreenerService`本体＝出来高等の条件によるフィルタ・自動順位付けはまだ未実装で、現状はCSVに記載した順序がそのままランクになる）：
  ```
  銘柄コード,銘柄名,前日終値,前日始値,前日高値,前日安値,前日出来高,前日RSI14,前日BB上限,前日BBミドル,前日BB下限
  ```
  「銘柄コード」列のみ必須、他は列名で認識するため順序自由・省略可。サンプル: `config/example_watchlist.csv`
- **バックテスト**：J-Quants APIの過去分足データをCSVで取得し取り込む方式に加え、別バッチ`minute_data_import`がPostgreSQL（`equities_bars_minute`テーブル）に取り込んだデータからも読み込めるようになった（バックテストタブの「データソース」選択）

### 稼働時間・通知
- 稼働対象：前場・後場の両方
- 外部通知（メール・Slack等）は本フェーズでは実装しない（ログ・GUI表示のみで対応）

## セキュリティ・設定管理（重要：他プロジェクトと統一）

- **設定は properties/JSON ファイルに集約し、コード内にハードコードしない**
- **APIトークン等の機密情報は暗号化して保存する**：AES-256-GCM、鍵はPBKDF2で環境変数由来の値から導出、暗号化値は`ENC(...)`形式でプレフィックスを付与し、平文の設定ファイルとは別ファイル（例：`secrets.properties`）に保存する
- 機密情報を含む設定ファイルは**Gitの管理対象から除外**する（`.gitignore`に追加）
- 取引履歴を含む`data/kabu_daytrader.db`（SQLite）も同様に`.gitignore`に追加すること
- この方式は他のJavaプロジェクトと共通の規約であり、本プロジェクト（Python）でも同様の考え方で実装する

## アーキテクチャ概要

```
PushClient(WebSocket, 自動再接続最大3回)
   → SignalEngine（指標更新・シグナル判定、プラグイン式Indicator群を保持）
   → OrderExecutor（REST APIで成行発注、レート制限順守：発注5件/秒・情報10件/秒）
   → PositionManager（1銘柄1ポジション制約の担保）
   → RiskManager（日次損益・損切・日次上限・強制決済の判定）
   → SQLite（注文/ポジション/日次損益/シグナルログ等を永続化）

GUI（PySide6）はメインスレッドのみでUI更新。PUSH受信・シグナル判定・発注処理は
QThreadワーカーに分離し、Qt Signal/Slotで結果をGUIへ反映する。
注文・約定発生時は自動的に「注文/決済タブ」へ切り替える。
```

詳細なクラス設計・DBスキーマ・画面設計・処理フローは`基本設計書_日本株デイトレード自動売買システム.md`を参照。

## パッケージ構成（想定）

```
kabu_daytrader/
├── main.py
├── config/            # 設定読み書き（ConfigManager, SecretsManager）
├── api/               # kabuステーションAPI（REST/PUSH）ラッパー
├── screener/          # TradingView CSV読込・監視銘柄リスト生成
├── indicators/         # 指標プラグイン群（拡張ポイント）
├── signals/           # シグナル判定エンジン（本番・バックテスト共通）
├── trading/           # 発注・ポジション管理・リスク管理
├── backtest/          # J-Quants CSV取込・バックテストエンジン
├── storage/           # SQLite接続・リポジトリ層
└── gui/               # PySide6 GUI（タブ構成）
```

## kabuステーションAPIに関する制約（実装時の注意）

- kabuステーション®アプリがWindows上で常時起動している必要がある（API利用の前提）
- APIはkabuステーションと**同一PC（同一IP）**からのリクエストのみ受け付ける
- 本番用ポート：18080／検証用ポート：18081
- レート制限：発注系5件/秒、情報・登録系10件/秒（超過でエラー）
- PUSH配信は間引き間隔あり（目安400ms）
- kabuステーションは日次自動ログアウトされる（利用可能時間の目安：6:30〜翌6:15）。日次のトークン再取得処理が必要
- 銘柄登録数の上限に注意（監視30銘柄は上限内）

## 未実装・詳細設計待ちの項目

- ~~スクリーナーCSVの取込トリガーと銘柄順位の決定基準（ScreenerService本体の自動フィルタ・順位付け）~~ → **対応不要と確定**：銘柄の絞り込み・順位付けは他ツールで行い、その結果をCSV（現状フォーマットのまま）としてGUIに読み込ませる運用でよいとのユーザー判断。アプリ側でのフィルタリングロジックは実装しない
- ~~認証トークンの自動再取得（日次ログアウト対応）の実装詳細~~ → **対応不要と確定**：毎朝アプリを起動する運用のため、日次自動ログアウトへの対応（長時間稼働中の自動再認証）は不要というユーザー判断
- ~~テスト方針の詳細化~~ → **実質的に確立**：正式なテスト方針文書こそ作っていないが、各パッケージにユニットテストを揃え（累計約152ケース、`tests/`配下）、モック化（psycopg2, requests session, kabuステーションAPI等）による外部依存の排除、実機での既存機能への影響確認を毎回の変更後に徹底する運用が定着している。新機能追加のたびに実際にバグを発見・修正できており（sqlite3スレッド問題、FundType不足、大引け後ログスパム等）、テストが実効的に機能していることを確認済み
- ~~【重要】約定確認の非同期化~~ → **実装完了**：`RestClient._send_market_order()`が`sendorder`直後に`GET /orders?product=0&id=...`を短間隔（既定0.3秒間隔、最大15回＝約4.5秒）でポーリングし、`State==5`（終了）かつ`CumQty`（約定数量）が発注数量以上になったことを確認してから`FILLED`＋約定価格を返すようにした（`_confirm_fill()`）。約定価格は`Details`配列内のPrice>0の明細をQty加重平均して算出（`_extract_filled_price()`）。**この加重平均のロジックは実機レスポンス例からの推定であり、正式仕様書での確認は取れていない**ため、実運用開始時は少額での実地確認が必須。タイムアウトした場合はPENDINGのまま返す（OrderExecutorはFILLED以外を「未発注」扱いにするため、この場合ポジションとして記録されない＝実運用ではGET /positionsとの突き合わせ運用を推奨）。~~既知のトレードオフ：このポーリングは呼び出し元スレッド（本番監視ループ）を最大約4.5秒ブロックする~~ → 下記「発注処理の別スレッド化」で解消済み
- **発注処理をTick処理から別スレッドに分離**：`TradingController`に発注専用のワーカースレッド（`_order_worker_loop`、`threading.Thread`、`start()`で起動・`stop()`で停止）を追加した。
  - `_on_tick`（PUSH受信スレッド）は、生Tickのバー集約・指標更新（`SignalEngine.update`）・PostgreSQLへの1分足書き込みまでを同期的に行うが、**実際のエントリー/決済判定とブローカーへの発注実行（`OrderExecutor`呼び出し）は行わない**。代わりに`(symbol, completed_bar)`を`queue.Queue`へ投入して即座に返る
  - 発注ワーカースレッドがキューを1件ずつ取り出し、`_process_symbol_order()`でトレール判定・exit_rule判定・entry_rule判定と実際の発注（`sendorder`＋約定確認ポーリング）を行う。これにより、1銘柄の約定確認待ち（最大約4.5秒）が他銘柄のPUSH Tick処理を妨げなくなった
  - 同一銘柄について発注処理中（キュー投入済み〜処理完了前）は`_pending_symbols`（`_lock`で保護）により新しいバー確定があってもキューへ再投入しない（重複発注防止）。処理完了後、次に確定したバーで改めて判定される
  - `check_forced_liquidation()`（`MainWindow`のQTimerがGUIスレッドから10秒ごとに呼ぶ）も、以前はGUIスレッド上で同期的に決済処理を行いブローカー呼び出し中はGUIがフリーズし得たが、キュー投入のみに変更し即座に返るようにした（実処理は`_process_force_liquidation()`として同じ発注ワーカースレッドで行う）
  - テスト用に`process_pending_orders_sync()`（ワーカースレッドを使わず呼び出し元スレッドで同期的にキューを処理する）を追加。実機のQThreadは使わずロジックだけを検証したいテストで使用
  - 新しいテストで実際の別スレッド動作・非ブロッキング性を検証済み：`test_controller_start_runs_real_worker_thread_that_processes_orders_async`（実スレッドで非同期処理されること）、`test_controller_check_forced_liquidation_does_not_block_caller`（即座に返ること）、`test_controller_pending_symbols_prevents_duplicate_queueing`（重複投入されないこと）
- ~~sendorderのアカウント関連コード値の確認~~ → **口座種別・買いFundTypeは確定**：`api.rest_client.AccountConfig`のデフォルト値をユーザーに確認済み。
  - `account_type`＝**4（特定口座）** で確定（コード変更不要、既定値のまま）
  - 現物**買い**：`buy_deliv_type`＝**2**（お預り金）、`buy_fund_type`＝**"AA"（信用代用）** で確定（同一銘柄を1日に何度も取引する可能性があるため。コード変更不要、既定値のまま）
  - 現物**売り**：`sell_deliv_type`＝**0**、`sell_fund_type`＝**半角スペース2文字**は引き続き未検証の推定値。**バックテスト結果が良好であれば、実際に売り注文（決済）を試して確認する**とのこと
  - 上記はいずれも複数の公開実装例からの推定値がベースになっている点は変わらず、`AccountConfig`は買い用・売り用に分離済み（`_send_market_order`が`Side`に応じて自動的に使い分ける）
- ~~J-Quants分足CSVの実際のカラム名の確認~~ → **確定**：`Date,Time,Code,O,H,L,C,Vo,Va`（DateとTimeが別列、出来高列は`Vo`、`Va`＝売買代金は未使用）。実データ例: `2025-01-06,09:00,31820,1283,1284,1278,1278,27000,34636900`。`backtest.data_loader.DEFAULT_COLUMN_MAP`をこの形式に更新済み。銘柄コードは5桁（例: 3182→31820）で、末尾の0を落として4桁に変換する処理は既存のまま。単一のDateTime列を持つ別形式のCSVを読みたい場合は`load_bars_from_csv(..., column_map={"timestamp": "列名"})`のように指定すればtimestamp単一列モードにフォールバックできる。サンプル: `config/example_backtest_bars.csv`
- **バックテストタブの改善**：(1) 損益推移グラフのX軸が単なる連番（0,1,2...）になっていた不具合を修正し、`pyqtgraph.DateAxisItem`で実際の日時を表示するようにした。(2) 結果表に「実行完了日時」「対象データ期間」の行を追加。(3) `backtest.data_loader.resample_bars()`を新設し、1分足CSVを3分足・5分足・15分足に変換してからバックテストできる「シミュレーション足間隔」選択（GUI）を追加。集約ルールは始値=区間最初、高値=区間最大、安値=区間最小、終値=区間最後、出来高=合計という一般的な足の合成方法
- **バックテスト専用のエントリー・決済ログを追加**：`backtest.models.OrderLogEntry`（日時・種別ENTRY/EXIT・銘柄・株数・価格・理由・実現損益）を新設し、`BacktestEngine.run()`がエントリー・決済（利確/損切/日次上限/強制引け/データ終了時強制決済）を時系列で`BacktestResult.log_entries`に記録するようにした。`backtest.write_backtest_log_csv()`でCSV出力（列は日本語ラベル：日時/種別/銘柄コード/株数/価格/理由/実現損益、理由列はSTOP_LOSS→「損切」等に日本語化）。GUIのバックテストタブはバックテスト完了時に自動で`logs/backtest_log_<実行日時>.csv`へ保存し、保存先をステータス欄に表示する
- **【次の統合作業】GUIと実APIの配線** → **実装完了**：`gui.TradingController`が`api.PushClient`→`signals.SignalEngine`→`trading.OrderExecutor`→GUI各タブ（Qt Signal経由）を1本につないだ。`MainWindow`に「監視開始/停止」コントロールを追加し、起動時にAPIパスワードを入力・環境（本番18080／検証用18081）を選択できる。監視状況タブに銘柄リストCSV読込機能を追加。10秒間隔のQTimerで`RiskManager`の強制決済トリガーを定期チェックする。ロジック部分（Tick処理・エントリー/決済判定）は`SimulatedBrokerClient`を使い実ネットワークなしでテスト済み
- **既知の設計上の注意点**：バックテストは同一タイムスタンプの複数銘柄シグナルをスクリーナー順位でまとめて評価してから余力が尽きるまで発注する（`try_entries_in_rank_order`）のに対し、本番監視ループは各銘柄のバー確定タイミングでキューに投入され、発注ワーカースレッドが1件ずつ順番に処理する（詳細は下記「発注処理をTick処理から別スレッドに分離」）。結果として、複数銘柄が同時にシグナルを出した場合の発注順序が、バックテストの「厳密なランク順」と本番の「バー確定・キュー投入順」とで一致しない可能性がある（余力チェック自体は両方とも正しく機能する）。実運用で気になる場合は、一定間隔でバッファリングしてバッチ評価する方式への変更を検討
- **指標のウォームアップ機能をCSV由来の要約値でシードする方式に刷新**：kabuステーションAPIには分足の過去データを取得するエンドポイントがないため、当初は前日終値を繰り返し投入する簡易ウォームアップにしていたが、**銘柄リストCSVに前日の要約値（終値・始値・高値・安値・出来高・RSI14・ボリンジャーバンド±2σ/ミドル）を持たせ、指標ごとに直接シードする方式に作り直した**（精度が大幅に向上）。
- **さらに、PostgreSQL実データによるウォームアップを最優先にする方式に刷新**：`SignalEngine.warmup_symbol_from_bars()`を新設し、`equities_bars_minute`テーブルにある実際の過去1分足データ（本番のbar_interval_minutesへリサンプリング後）を、CSV要約値からのseed()近似ではなく**通常のupdate()で1本ずつ実際に投入**することで、より正確な状態を再現できるようにした。
  - `TradingController._warmup_indicators()`は、まず`_try_db_warmup()`でDB実データを試し、DBに接続できない・該当銘柄のデータが無い場合のみ、従来のCSV要約値ベースのウォームアップ（`warmup_symbol_from_summary()`）にフォールバックする（ユーザー確定仕様：「DBに値がなければ今まで通りで良い」）
  - `warmup_symbol_from_summary()`は、既に`is_ready()`になっている指標（＝DB実データで準備できた指標）へは`seed()`を呼ばないようガードを追加した（呼んでしまうと実データによる正確な状態をCSV要約値の近似で上書きしてしまうため）。これにより、DB実データで一部の指標だけ準備できた銘柄でも、残りの指標だけCSV要約値で補う、という併用が安全に行える
  - VWAP・AR（opening_range_ar）のように日次リセットが前提の指標は、DB実データ（過去日のバー）を投入しても、当日最初のTickが届いた時点でセッションが切り替わり正しくリセットされる（過去日のリプレイが当日に誤って持ち越されることはない）
- **AR×倍率トレール決済を実装（固定%損切りを廃止）**：
  - `indicators/opening_range_ar.py`（`OpeningRangeARIndicator`）を新設：寄り付き直後のbar_count本（既定4本）のTR（`max(高値-安値, |高値-前本終値|, |安値-前本終値|)`、窓開け含む）平均を算出し、その日は値を凍結する。日付が変わると自動リセット。indicators配下の他クラスと同じくregistry経由で`{"type": "opening_range_ar", "bar_count": 4}`のように設定する
  - `trading.Position`に`high_water_mark`（保有中の最高値）を追加。`PositionManager.update_high_water_mark()`で更新
  - `RiskManager.trailing_multiplier`（既定**2.0**）、`OrderExecutor.check_and_apply_trailing_stop(symbol, price, ar_value)`：`保有中の最高値 - AR×trailing_multiplier`を下回ったら成行決済（新設`OrderReason.TRAILING_STOP`）。**AR未確定（is_ready()前）の間はトレール判定自体を行わない**
  - `stop_loss_pct`はAPI・データモデル上は後方互換のため残しているが、`BacktestEngine`・`TradingController`の主要ループからは呼び出されなくなった（`RiskManager.is_stop_loss_triggered()`自体は引き続き動作するが未使用）
  - GUI：設定タブ・バックテストタブの「損切ライン」欄を「トレール倍率（AR×倍率）」に置き換え。未設定でも既定2.0（AR×2）で動作する
  - **kabuステーションAPI自体にはネイティブなトレール注文機能が無い**（あるのは固定トリガー価格の逆指値のみ）ことを事前に確認済み。他の開発者の実装例でも、価格が動くたびに逆指値の取消→再発注を繰り返す方式が使われており、100〜200msの無防備な時間が生じるとの報告があった。そのため**本システムはブローカー側の注文機能に頼らず、アプリ側で毎バー確定ごとに監視し、条件を満たしたら成行決済する方式**を採用している（PUSH切断時は既存の強制決済フェイルセーフでカバーされる）
- **本番監視にライブ分足集約（`trading.LiveBarAggregator`）を追加**：AR算出には「確定したN分足」が前提となるが、本番のPUSH配信は生Tick（価格変化のたびに届く）であり、バックテストの分足CSVとは粒度が異なっていた。`LiveBarAggregator`が生Tickをbar_interval_minutes（既定**3分**、GUIから変更可）単位のOHLCVバーに集約し、バーが確定したタイミングでのみ`SignalEngine`の指標更新・エントリー/決済判定・トレール決済判定を行うよう`TradingController._on_tick`を全面的に書き換えた。**監視状況タブの現在値表示自体は生Tickのたびに更新**するため、価格はリアルタイムに動くが指標値はバー確定時点のスナップショットのまま、という見え方になる
- **分足データの保存先をPostgreSQLに変更（別バッチ`minute_data_import`とテーブル共有）**：
  - ユーザー側で別途、分足データをPostgreSQLへ取り込むバッチ（`minute_data_import`）を用意済み。テーブル定義（このアプリ側では作成しない、既存のまま利用）:
    ```sql
    CREATE TABLE IF NOT EXISTS equities_bars_minute (
        code        VARCHAR(10)   NOT NULL,
        datetime    TIMESTAMP     NOT NULL,
        open_price  NUMERIC(12,2) NOT NULL,
        high_price  NUMERIC(12,2) NOT NULL,
        low_price   NUMERIC(12,2) NOT NULL,
        close_price NUMERIC(12,2) NOT NULL,
        volume      BIGINT        NOT NULL,
        PRIMARY KEY (code, datetime)
    );
    ```
  - 接続情報はconfig.ini（`[postgresql]`セクション：host/port/dbname/user/password）から読み込む。**パスワードはコードに一切ハードコードしない**（`storage/postgres_bars.py`の`load_pg_config()`が都度ファイルから読む）
  - `storage/postgres_bars.py`を新設：`load_bars()`（バックテスト用の期間・銘柄指定読込）、`LiveBarWriter`（本番用、1本の接続を使い回して`INSERT ... ON CONFLICT (code, datetime) DO UPDATE`でUPSERT書き込み）
  - **codeカラムの表記形式は4桁で確定**（kabuステーション形式と同一）。`normalize_jquants_code()`/`to_db_code()`は変換不要のため素通し実装にしている（後方互換のため関数自体は残している）
  - **本番監視**：`TradingController`に`pg_config_path`を渡すと、指標計算用のバー集約（bar_interval_minutes、既定3分）とは**別に、常に1分足で**バーを集約する専用の`LiveBarAggregator`をもう1系統持ち、1分足が確定するたびにPostgreSQLへ書き込む（「本番時は1分ごとに4本値・出来高を登録する」という要件どおり）。書き込み失敗時はログに記録するだけで、監視・取引自体は止めない
  - **バックテスト**：バックテストタブに「データソース」選択（CSVファイル／PostgreSQL）を追加。PostgreSQL選択時は config.iniパス・銘柄コード（カンマ区切りで複数可）・開始日時・終了日時を指定して`storage.pg_load_bars()`から読み込む
  - GUI設定タブに「PostgreSQL設定ファイル(config.ini)パス」欄を追加。空欄ならDB書き込みを行わない
  - 実際のPostgreSQLサーバーへの接続はこの開発環境から検証できないため、`psycopg2.connect`をモック化した単体テストのみで検証済み。**実機での動作確認が必須**
  - 各`Indicator`サブクラスに`seed(prev_close, prev_rsi, prev_bb_upper, prev_bb_middle, prev_bb_lower, ...) -> bool`を追加：
    - `MovingAverageIndicator`：前日終値でウィンドウ（またはEMA初期値）を埋める
    - `RSIIndicator`：前日RSI値から内部の平均上昇幅/平均下落幅を逆算して再現（実測誤差ほぼゼロ）
    - `BollingerBandIndicator`：前日のミドル・±2σから、平均・母標準偏差が数学的に厳密に一致するウィンドウを再構成（偶数・奇数期間どちらも対応、実測誤差ほぼゼロ）
    - `VWAPIndicator`：seed非対応（日次リセット前提のためウォームアップ不要）
  - `SignalEngine.warmup_symbol_from_summary()`が各指標の`seed()`を呼び、対応していない指標は前日終値のみのフォールバック（旧方式）に自動的に切り替える
  - **銘柄リストCSVフォーマットを確定**（`gui/tabs/monitor_tab.py`で読込。1行目はヘッダー必須、列名で認識するため順序は自由、「銘柄コード」列のみ必須）：
    ```
    銘柄コード,銘柄名,前日終値,前日始値,前日高値,前日安値,前日出来高,前日RSI14,前日BB上限,前日BBミドル,前日BB下限
    ```
    サンプル: `config/example_watchlist.csv`。RSI14・BB(period=20, num_std=2)は設定タブの指標設定の期間と揃えて計算しておくこと（期間が異なると再現精度が落ちる）
  - `signals.WatchlistEntry`データモデルを新設（銘柄コード・名前・順位・前日要約値一式を保持）。`TradingController`は`RestClient.get_previous_close()`への問い合わせをやめ、`WatchlistEntry`の値を直接使うようになった（`RestClient.get_previous_close()`メソッド自体は汎用ユーティリティとして残しているが、ウォームアップ用途では未使用）
  - これに伴い、RSIは「値動きが完全にゼロ」の場合に中立値50を返すよう修正済み（従来は誤って100=強い買われすぎと判定していたバグ。前日終値のみのフラットシード時に特に問題になっていた）
- **【Windows実機で発見・修正済み】sqlite3接続の未クローズによるファイルロック**：`TradingController`がDB接続を開いたまま明示的にクローズしていなかったため、Windowsでは「別のプロセスが使用中」エラー（一時ファイル削除時やDBファイル操作時）の原因になっていた（Linuxでは同様の制約がなく問題が顕在化しなかった）。`TradingController.close()`を追加し、`MainWindow`の「停止」ボタン押下時（`stop()`に加えて`close()`も呼ぶ）で確実にDB接続を閉じるよう修正
- **寄り付き直後のエントリー禁止時間帯を実装**：`RiskManager`に`entry_start_time`（既定"09:12"、`config/default_config.json`）を追加。`OrderExecutor.try_entry()`/`try_entries_in_rank_order()`はこの時刻より前は新規エントリーを行わない（決済・損切・強制決済はこの制約の対象外）。設定タブ・バックテストタブ双方にGUIの時刻設定欄を追加し、本番・バックテストで同条件を再現できる。`BacktestEngine`は各Tickのタイムスタンプを`try_entries_in_rank_order(..., now=timestamp)`に渡し、`TradingController._on_tick`は`tick.timestamp`を`try_entry(..., now=tick.timestamp)`に渡す（`datetime.now()`への暗黙のフォールバックは、実運用でのAPI呼び出し時にのみ使われる想定）
- 実際のkabuステーションAPI・WebSocketへの接続（`StartupWorker`経由の`TradingController.start()`、本物の`PushClient`受信）は、この開発環境ではネットワーク制約により検証できていない。実機（Windows・kabuステーション起動済み環境）での動作確認が必須
- ~~storage/パッケージ（SQLite永続化）が未実装~~ → **実装完了**：`storage/db.py`（スキーマ定義：symbols/watchlist/signal_log/orders/positions/closed_trades/daily_pnl/app_settings/backtest_bars/backtest_runsの10テーブル）と`storage/repositories.py`（各テーブルへのCRUD）を実装。`gui.TradingController`に`db_path`引数を追加し、`MainWindow`起動時に`data/kabu_daytrader.db`（Git管理対象外にすること）へ注文・ポジション・シグナルログ・日次損益を自動永続化するよう配線済み。バックテスト結果も`backtest_runs`テーブルへ保存される
- **【実機で発見・修正済み】sqlite3のスレッド制約バグ**：`TradingController`はGUIスレッドで生成されるが、実際のTick処理は`PushClient`のWebSocket受信スレッドから呼ばれるため、sqlite3のデフォルト設定（`check_same_thread=True`）だと`SQLite objects created in a thread can only be used in that same thread`エラーが発生していた。`storage/db.py`の`connect()`で`check_same_thread=False`を設定して解消（排他制御は`TradingController`の`threading.Lock`が担う前提）
- **【実機で発見・修正済み】sendorderにFundType（資金区分）が必須**：`AccountConfig`に`fund_type`（既定値"02"=保護預り、"AA"=信用代用）を追加し、`sendorder`のペイロードに含めるよう修正。値は口座の資金区分設定によって異なるため、発注エラー（Code: 4001005）が出る場合は"AA"に切り替えて再確認すること
- **【実機で発見・修正済み】銘柄登録（PUT /register）は追加方式で上書きされない**：前回セッションで登録した銘柄が解除されないまま残り、新しい監視銘柄リストに古い銘柄が混ざってPUSH配信され続ける不具合があった。対策として (1) `RestClient.unregister_all_symbols()`（`PUT /unregister/all`）を追加し、`TradingController.start()`で銘柄登録前に必ず全解除するよう修正、(2) 保険として`TradingController._on_tick()`で現在の監視銘柄リストに含まれないTickは無視するよう二重にガードした
- **【実機で発見・修正済み】大引け後のログスパムと新規エントリー停止漏れ**：大引け時刻（既定15:00）を過ぎている間、10秒ごとの定期チェックで`RiskManager.should_force_close_all()`が毎回`FORCE_CLOSE_TIME`を返し続け、保有ポジションが既に0件でも`OrderExecutor`が警告ログを出し続けていた。`check_forced_liquidation()`で保有ポジションが0件なら何もしないよう修正。あわせて、大引け時刻経過後は`RiskManager.trading_halted`をTrueにし、決済だけでなく新規エントリーも止まるよう修正（従来は決済のみで、大引け後も理論上は新規エントリーが可能な状態だった）

## 検証用環境（ポート18081）の既知の制約（実機確認済み）

- トークン認証（`POST /token`）は検証用パスワードで正常に機能する
- 一方、**検証用環境では口座残高・銘柄登録・PUSH配信のリアルタイムデータが返らない**（応答自体はエラーにならず200 OKだが、`wallet/cash`は各項目が`None`、`register`の`RegistList`は空、PUSH配信も届かない）
- そのため、テスト方針は以下のように分離する
  - **発注・決済のリクエスト形式／エラーハンドリングの確認**：検証用(18081)を使用（資金リスクなし）
  - **口座残高・銘柄登録・PUSH配信（リアルタイム監視ロジック）の確認**：**本番用(18080)のGET系・PUSH系エンドポイントのみ**を使用（読み取り専用のため資金リスクはない）。ただし`POST /sendorder`等の発注系APIは本番用では絶対に自動テストで呼び出さないこと（テストコードを物理的に分離するか、環境変数等でガードする）

## 追加機能：BBW・再エントリークールダウン

- **ボリンジャーバンド幅（BBW）を追加**：`indicators.BollingerBandIndicator.value()`に`bbw = (upper - lower) / middle`を追加した（銘柄・価格帯を問わずボラティリティの拡大・収縮を比較できる正規化指標）。既存の`middle`/`upper`/`lower`/`percent_b`/`position`と同じ`bb`インジケータのフィールドとして参照できる（例：`{"indicator": "bb", "field": "bbw", "op": ">", "value": 0.01}`）。既定の`entry_rule`に`0.01 < bbw <= 0.018`の条件を追加した（`config/default_config.json`）
- **決済後の再エントリークールダウンを実装**：決済直後に同じ銘柄へすぐ再エントリーしてしまうのを防ぐため、`RiskManager.reentry_cooldown_bars`（既定**3本**）を追加した。
  - バー本数ベースで管理する（絶対時間ではない）ため、足の間隔（1分足・3分足等）を問わず「決済後◯本のバーが経過するまで」を正しく表現できる（`record_exit()`で開始、`tick_reentry_cooldown()`を該当銘柄のバー確定ごとに呼んでカウントダウン、`is_reentry_allowed()`で判定）
  - `OrderExecutor.try_entry()`・`try_entries_in_rank_order()`の両方でチェックし、クールダウン中の銘柄への新規エントリーを見送る（決済・強制決済はこの制約の対象外）
  - `try_exit()`の決済成功時に自動的に`record_exit()`を呼ぶため、理由（シグナル/トレール決済/強制決済等）を問わずすべての決済がクールダウンの起点になる
  - `RiskManager.start_new_trading_day()`でクールダウン状態もリセットされる（日をまたいで持ち越さない）
  - `BacktestEngine`は各バー確定のたびに`tick_reentry_cooldown()`を呼ぶよう対応済み。`TradingController._on_tick`（PUSH受信スレッド）でも同様に呼ぶ（軽量な処理のため同期実行のままで問題ない）
  - GUI：設定タブ・バックテストタブに「再エントリー禁止バー数」欄を追加（既定3、0で無効化）

## 追加機能：ローソク足連続パターン戦略（candle_strategy、既存戦略とは完全に独立）

ユーザー要望により、既存のRSI等ベース戦略（`trading`/`signals`パッケージ）とは**意図的に完全に分離**した、新しい独立戦略パッケージ`candle_strategy/`を追加した。

**確定仕様**（ユーザー確認済み）：
- エントリー：3分足でN本連続陽線→買い建て、N本連続陰線→**信用取引で空売り**（Nの既定値3）
- 決済：優先順位は **① 損切(-1.5%) → ② 利確(+3.5%) → ③ 通算逆足M本（Mの既定値2）**。1回のバー確定で複数条件を同時に満たしても、優先順位の高い1つの理由でのみ決済する
- 同一銘柄では既存戦略（RSI等ベース）と**排他的**：どちらか一方のみ保有可能（`external_has_position`コールバックで連携）

**実装構成**（`trading`/`signals`パッケージへの依存は一切なし。`backtest.data_loader`の分足読込・リサンプリングのみ共有基盤として再利用）：
- `candle_strategy/models.py`：`CandleDirection`（LONG/SHORT/NEUTRAL）、`CandlePosition`（`reverse_bar_count`で通算逆足を保持）、`CandleOrderReason`等
- `candle_strategy/strategy_engine.py`：`CandleStrategyEngine`が確定バーを1本ずつ受け取り、連続同色バーをカウント。日付が変わると自動リセット（VWAP等と同じセッション方式）
- `candle_strategy/position_manager.py`：`CandlePositionManager`（trading.PositionManagerとは別クラス。LONG/SHORT両対応、SHORTは値下がりが利益になるようpnl計算を反転）
- `candle_strategy/broker_client.py`：`MarginBrokerClient`（信用取引用インターフェース：新規買い/新規売り(空売り)/返済買い/返済売り の4メソッド）、テスト用`SimulatedMarginBrokerClient`
- `candle_strategy/order_executor.py`：`CandleOrderExecutor`。`check_exit_conditions()`が損切→利確→逆足M本の順でチェックする
- `candle_strategy/backtest_engine.py`：`CandleBacktestEngine`（既存`BacktestEngine`とは別クラス、単独でも実行可能）

**テストで実際に見つけて修正したバグ**：`CandleStrategyEngine.evaluate_entry()`を「連続本数がN本**以上**」で判定していたところ、決済後も同一方向の連続バーが続いていると、決済した同じバー確定処理内で条件が再度真になり、**即座に同一銘柄へ再エントリーしてしまう**不具合があった。「連続本数がちょうどN本に達した瞬間のみ」発火する単発シグナルに修正して解消（回帰テスト`test_strategy_engine_does_not_refire_beyond_exact_n`を追加）。

**この時点で未実装・要対応の項目**：
1. **実際のkabuステーションAPIでの信用取引発注は未実装**：`api.RestClient`に信用新規売り・信用返済買い等のメソッドを追加する必要がある。信用注文は`CashMargin=2`（新規）/`3`（返済）、建玉を返済する際は対象の建玉番号（HoldID）を指定する必要がある等、現物注文とは異なる仕様の確認が必要（未着手・未検証）
2. ~~GUI（設定タブ・バックテストタブ・監視状況タブ等）への統合は未着手~~ → **バックテストのGUI統合は完了**：新規タブ「新戦略バックテスト」（`gui/tabs/candle_backtest_tab.py`）を追加した。既存のバックテストタブと同じくCSV/PostgreSQLからの分足読込・足間隔変換に対応し、戦略パラメータ（連続本数N・逆足本数M・利確%・損切%等）をGUIから設定してバックテストできる。専用ワーカー`CandleBacktestWorker`（`gui/workers.py`）でUIをブロックしない。結果は損益推移グラフ・成績表で表示、`logs/candle_backtest_log_<実行日時>.csv`へ自動保存（`candle_strategy.write_candle_log_csv`）。**ライブ監視（本番の`TradingController`）への統合はまだ行っていない**（上記1の信用発注APIが未実装のため、安全のため意図的に見送っている）
3. **既存戦略との「同一銘柄では排他的」の実地連携は未統合**：`external_has_position`という差し込み口は用意したが、実際に本番監視ループ（`TradingController`）や既存バックテスト（`BacktestEngine`）と同時に動かして排他制御する統合コードはまだ書いていない
4. **信用取引口座・証拠金余力の考慮は簡易シミュレーションのみ**：`SimulatedMarginBrokerClient`は「新規建てで余力を減らし決済で戻す」という単純化したモデルで、実際の委託保証金率・維持率等は考慮していない

**追加のAND条件（ヒゲの長さ）**：N本目のバーに対し、以下を追加で満たさないとエントリーしない。
- 買い（LONG）：上ヒゲ(高値-終値) ＜ 実体(終値-始値)
- 売り（SHORT）：下ヒゲ(終値-安値) ＜ 実体(始値-終値)

**【解釈に関する確認事項・要フォローアップ】**：ユーザーからの当初の指示は売り側を「（安値－終値）＜（始値－終値）」としていたが、安値は定義上終値以下（`low <= close`）になるため`(low - close)`は常に0以下となり、この式では条件が常に真になってしまう（実質的にフィルタとして機能しない）。買い側の条件と対称になるよう「（終値－安値）＜（始値－終値）」（＝下ヒゲ＜実体）と解釈して実装し、その旨をユーザーに伝えた。**ユーザーからの明示的な確認はまだ得られていない**ため、次にこの話題が出た際は解釈が正しかったか改めて確認すること。もし意図と異なる場合は`CandleStrategyEngine.evaluate_entry()`内の該当箇所を修正する。

## 指標セットの全面刷新（2026-08、進行中）

ユーザー指示により、最初の戦略（`trading`/`signals`パッケージ）の指標を全面的に入れ替える作業を開始した。**この作業はまだ完了しておらず、段階的に進めている**。

**確定仕様**（ユーザー確認済み）：
- 5分足、RSI(6)、MACD(7,26,7)、DMI(DI期間6, ADX期間14)を使用
- 新規買い：RSI<35 AND MACD<0 AND DEA<-10 AND ADX>50 AND PDI<MDI
- 新規売り：RSI>65 AND MACD>0 AND DEA>10 AND ADX<50 AND MDI<PDI（**信用取引での空売り**）
- 利確買い：RSI<65 AND (MACDがマイナス→プラスに反転 OR DIFがDEAを上抜く) AND ADX>50 AND PDI<MDI
- 利確売り：RSI>35 AND (MACDがプラス→マイナスに反転 OR DIFがDEAを下抜く) AND ADX>50 AND PDI>MDI
- 損切（新方式・固定値ラチェット式、45円/20円は設定項目）：
  - 買い：初期損切値=購入価格-45円。現在値が(損切値+45)+20円を上回ったら損切値=現在値-45円に更新。現在値が損切値を下回ったら損切
  - 売り：初期損切値=購入価格+45円。現在値が(損切値-45)-20円を下回ったら損切値=現在値+45円に更新。現在値が損切値を上回ったら損切
- 強制引け：15:00（設定項目、既存のforce_close_timeを流用可）
- 用語：MACD=ヒストグラム(DIF-DEA)、DIF=EMA(fast)-EMA(slow)、DEA=EMA(DIF, signal)。DEAとMACDは書き分けられている（ユーザー確認済み）

**この時点で実装完了した部分**：
- `indicators/macd.py`（新規）：DIF/DEA/MACD（ヒストグラム）を算出。`macd_crossed_up/down`（ヒストグラムのゼロクロス）・`dif_crossed_dea_up/down`（DIFとDEAのクロス）を`value()`に含め、利確条件の「反転」「上抜く/下抜く」判定に使えるようにした
- `indicators/dmi.py`（新規）：+DI（`pdi`）・-DI（`mdi`）・ADXをWilderの標準的な平滑化方式で算出
- **`indicators/moving_average.py`（SMA/EMA）・`bollinger_band.py`（ボリンジャーバンド）・`vwap.py`（VWAP）・`opening_range_ar.py`（AR）を削除**（ユーザー指示「古いインジケータは削除してください」）。`indicators/registry.py`・`indicators/__init__.py`を新指標セット（rsi/macd/dmi）のみに更新
- `config/default_config.json`・`config/example_rules.json`を新指標セットに更新。**ただし現時点ではentry_rule/exit_ruleは買い側（新規買い・利確買い）のみを設定している**（`SignalEngine`が現状entry_rule/exit_ruleを1つずつしか持てない設計のため）
- 関連する既存テスト（`test_indicators.py`は全面書き換え、`test_signal_engine.py`・`test_backtest.py`・`test_trading_controller.py`の旧指標参照箇所を新指標ベースに置き換え）。全テストパス

**未実装・次のステップ**：
1. ~~売り（空売り）側のentry_rule/exit_ruleが未実装~~ → **実装完了**：`SignalEngine`に`entry_rule_short`/`exit_rule_short`（省略時は「常にFalse」で後方互換）を追加し、`evaluate_entry_short()`/`evaluate_exit_short()`を新設。`BacktestEngine`・`TradingController`とも買い/売り両方向を毎バー評価し、同一銘柄では買いを優先しつつ排他的にエントリーする（両方の条件が同時に成立しても一方しか約定しない）。`default_config.json`/`example_rules.json`に実際の新規売り・利確売り条件（ユーザー確定仕様）を反映済み
2. ~~信用取引（買い・売りとも）への対応が未実装~~ → **実装完了**：`trading.PositionDirection`（LONG/SHORT）を新設し、`Position`/`ClosedPositionResult`が方向を持つようになった（`PositionManager.close_position()`はSHORTなら値下がりが利益になるよう損益計算を反転）。`trading.BrokerClient` Protocolに`place_margin_buy_to_open`/`place_margin_sell_to_open`/`place_margin_sell_to_close`/`place_margin_buy_to_close`の4メソッドを追加し、`SimulatedBrokerClient`にも実装。`OrderExecutor.try_entry()`は`direction`引数で買い建て/売り建てを切り替え、`try_exit()`は保有ポジションの`direction`を見て自動的に反対売買（信用返済）を選ぶ。`api.RestClient`は既にこれら4メソッドを実装済みのため、`BrokerClient` Protocolをそのまま満たす（テストで確認済み）。**これで第一戦略は買い・売りとも信用取引のみで動作する**（現物の`place_market_buy/sell`はもう呼ばれない）
   - **【要確認・未検証のまま】**`MarginConfig.margin_trade_type`（既定3=一般信用デイトレ）・`close_position_order`（既定0）は実機での確認が必要（前回までの記載のとおり、変更なし）
3. **新しい損切ロジック（固定値ラチェット式ストップ）が未実装**：既存の`RiskManager.trailing_multiplier`（AR×倍率のトレール決済）はAR指標の削除により実質無効化された状態のまま。新しい固定値（45円/20円、設定項目）でのラチェット式ストップロスに置き換える実装がまだ。**次の作業はここから**
4. **足の間隔を5分に変更する設定はdefault_config.jsonの`bar_interval_minutes`のみ更新済み**。GUI側の動作確認・実機での5分足運用確認はまだ
5. 上記3が未完了のため、**現状は決済がシグナル（exit_rule/exit_rule_short）のみに依存**しており、損切なしで運用すると想定より大きな損失が出うる状態。**実運用前に必ず3の実装完了を待つこと**

## コーディング規約

- 設定値・閾値（損切97%、日次上限2万円等）は必ず設定ファイル経由にし、コード中に定数として埋め込まない
- 指標追加はプラグイン方式を厳守し、`SignalEngine`本体のロジックを変更しないで済むようにする
- 本番用ロジックとバックテスト用ロジックのコード共通化を崩さない（乖離すると要件を満たさなくなる）
