# worktree-cleanup

GitHubのPRがマージされた後、不要になったローカルのGit worktreeを整理するmacOS向けツールです。既定では削除せず、候補だけを表示します。launchdを使うと毎朝10:15に自動確認できます。

元のチェックアウトとローカルブランチは保持し、追加で作成したworktreeだけを削除候補にします。

## 動作環境

- macOS、Python 3.9以降、Git 2.36以降、GitHub CLI（`gh`）、direnv、`lsof`
- 各リポジトリのdirenv環境で、GitHub CLIの認証が済んでいること
- `origin`がSSHまたはHTTPSのGitHub.comリポジトリを指していること。フォークの場合は、フォーク元のリポジトリも確認します。

追加のPythonパッケージは不要です。

## 削除せずに試す

このリポジトリを取得してそのディレクトリへ移動し、ローカル用の設定ファイルを作成します。

```sh
mkdir -p "$HOME/.config"
cp -n config.example.json "$HOME/.config/worktree-cleanup.json"
```

設定ファイルの`your-github-login`を、自分のGitHubユーザー名へ変更してください。`scan_roots`には、リポジトリを探索するディレクトリを指定します。設定例ではホームディレクトリ全体を探索します。必要に応じて範囲を絞れます。

複数のアカウントを使う場合は、ディレクトリごとの設定を追加します。

```json
{
  "scan_roots": ["~/projects", "~/company-projects"],
  "accounts": [
    {"path": "~", "login": "personal-login"},
    {"path": "~/company-projects", "login": "work-login"}
  ]
}
```

対象に一致する`path`のうち、最も深いディレクトリのアカウント設定を使います。認証の確認は、元のチェックアウトで`direnv exec`を通して行います。実際のアカウントが設定と一致しなければ削除を見送ります。アカウントの切り替えや、許可されていないdirenv設定の有効化は行いません。

```sh
python3 scripts/worktree-cleanup.py
```

結果は1行ごとのJSONで出力します。`candidate`は削除条件を満たすworktree、`skipped`や`blocked`は保持したworktreeを表し、その理由も記録します。上記のコマンドではworktreeを削除しません。

## 削除条件

次の条件をすべて満たしたworktreeだけが削除対象になります。

- `origin`のリポジトリと対象ブランチに対応するPRがマージ済みで、PRの最終コミットとローカルのHEADが完全に一致すること。同じブランチを再利用したオープン中のPRがあれば保持します。
- 追跡対象ファイルの変更、ステージ済みの変更、未追跡ファイルがないこと。
- Gitで変更の確認を省く設定（`assume-unchanged`／`skip-worktree`）が付いたファイルがないこと。
- Gitの無視対象ファイルが残っていないこと。ただし、再取得できる依存関係として扱う`node_modules`内のファイルは例外です。
- 現在のユーザーのプロセスが、その配下のファイルや作業ディレクトリを使用中であると`lsof`で検出されないこと。文字の大小や濁点の表記だけが違う場合も、使用中として保持します。`lsof`の出力で通常の文字と区別できない制御文字をパスに含む場合も保持します。
- リポジトリ独自の外部変換処理（Gitの`clean`／`process`フィルター）が設定されていないこと。
- ロック中、ディレクトリが存在しない状態、Gitの登録情報の整理対象（`prunable`）、シンボリックリンク、ブランチを選択していない状態（detached HEAD）のいずれでもないこと。また、ブランチが`main`、`master`、`develop`、`staging`、`production`、`trunk`のいずれでもないこと。
- サブモジュールがなく、マージ、rebase、cherry-pick、revertなどの処理途中や、インデックスのロック状態が残っていないこと。
- 認証と、必要なGit・GitHubへの問い合わせがすべて成功すること。

削除直前に、worktreeの登録情報、ローカルのHEAD、ファイル、ロック、プロセスの利用状況を再確認します。削除には`--force`を付けずに`git worktree remove`を使い、ローカルブランチは残します。

エディタのメモリ内だけにある未保存の内容は、`lsof`で検出できない場合があります。また、確認後に別のプロセスが作業を始める可能性もあります。PRの状態にかかわらず残したいworktreeは、ロックしてください。

```sh
git worktree lock /path/to/worktree
```

リポジトリの探索では、`Library`、認証情報のディレクトリ、依存関係のディレクトリ、一部のキャッシュを除外します。ディレクトリのシンボリックリンクはたどりません。見つかったリポジトリに登録されているworktreeは、`scan_roots`の外にあっても確認します。

Gitに設定された監視プログラム（`core.fsmonitor`）の起動は、このツールのGitコマンドに限って抑止します。

## 条件を満たすworktreeを削除する

まず削除しないモードで候補を確認し、削除する場合は`--apply`を指定します。

```sh
python3 scripts/worktree-cleanup.py --apply
```

削除したworktreeは、残したブランチから再作成できます。

```sh
git -C /path/to/primary-checkout worktree add /path/to/worktree feature/branch
```

`node_modules`内の依存関係は、再度インストールする必要があります。

## 定期実行する

macOSの定期実行設定（LaunchAgent）を作成し、現在のユーザーで読み込みます。

```sh
python3 scripts/worktree-cleanup.py --install
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/local.worktree-cleanup.plist"
```

Macの現地時刻で毎朝10:15に、削除しないモードで実行します。ログイン時には実行しません。生成するplistファイルにはPythonの実行ファイル、スクリプト、必要なコマンドの場所を記録するため、登録後はこのリポジトリを同じ場所に置いてください。

ログの保存先:

- `~/Library/Logs/worktree-cleanup/events.jsonl`
- `~/Library/Logs/worktree-cleanup/stderr.log`

ログにはローカルのパス、コミットID、PRのURLが含まれます。公開せず、手元で管理してください。

登録済みのジョブを自動削除モードへ切り替える場合は、ジョブの登録を解除して、生成したplistファイルを作り直します。

```sh
launchctl bootout "gui/$(id -u)/local.worktree-cleanup"
rm "$HOME/Library/LaunchAgents/local.worktree-cleanup.plist"
python3 scripts/worktree-cleanup.py --install --apply
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/local.worktree-cleanup.plist"
```

既存のplistファイルは上書きしません。処理の重複実行はロックで防ぎます。APIやコマンドでエラーが起きた場合は、対象のworktreeを保持します。worktreeごとのエラーはJSONログに記録するため、プロセスの終了コードが0でも、すべてのリポジトリを正常に確認できたかどうかはログで確認してください。

定期実行を停止する場合は、次のコマンドを実行します。

```sh
launchctl bootout "gui/$(id -u)/local.worktree-cleanup"
```

## テスト

```sh
python3 tests/worktree-cleanup-test.py
```

テストでは一時的なGitリポジトリと、GitHubの応答を模したデータを使います。既存のworktreeは削除しません。

## ライセンス

[MITライセンス](LICENSE)。
