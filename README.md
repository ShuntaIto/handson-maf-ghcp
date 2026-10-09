# Implementation Hypothesis Agent

## Agentの説明

自然言語の実装仮説を受け取り、隔離された一時ディレクトリを仮のリポジトリとして実装・検証・レビューするMicrosoft Agent Frameworkのworkflowです。GitHubのリポジトリは取得しません。

### Workflow

```text
implementation-agent (GitHubCopilotAgent)
        |
        v
   review-agent (Agent + structured output)
      |      \
  approved    changes_requested
      |              |
      v              +----> implementation-agent
 format-report
      |
      v
 Invocations API response
```

各invocationは次の順序で処理されます。

1. invocation専用の`TemporaryDirectory`を作成する
2. APIで受け取った補助ファイルを安全に展開する
3. ローカルGitリポジトリを初期化し、入力ファイルをbaseline commitにする
4. GitHub Copilotが実装仮説を実装・検証する
5. `Agent`が差分とテストを確認し、Pydantic structured outputで判定する
6. `changes_requested`なら実装エージェントへ差し戻す
7. `approved`ならレポートを整形する
8. 既定では一時ディレクトリを削除する
9. `persist_workspace=true`の場合だけ、ソースをZIP化してBase64で返す

ZIPには実装成果物を含め、内部用の`.git`、`__pycache__`、`.pytest_cache`は含めません。既定のZIP上限は10 MiBです。

### ファイル構成

| ファイル | 役割 |
| --- | --- |
| [`src/handson_maf_ghcp/agent.py`](./src/handson_maf_ghcp/agent.py) | APIモデル、実装・レビューエージェント、workflow、レポート整形 |
| [`src/handson_maf_ghcp/workspace.py`](./src/handson_maf_ghcp/workspace.py) | 一時リポジトリの作成・削除、ファイル一覧、ZIP化、レビュー用ツール |
| [`run.py`](./run.py) | 1個の仮説をローカルで一度だけ実行 |
| [`main.py`](./main.py) | Invocations APIとしてサーブ |
| [`tests/`](./tests) | small / medium / largeのテスト |

### API

サーバーはInvocations protocol 2.0を使用し、`POST /invocations`を提供します。OpenAPI定義は`GET /invocations/docs/openapi.json`で取得できます。

#### Request

```json
{
  "hypothesis": "キャッシュを追加して重複計算を削減する",
  "supplemental_context": "Python 3.12を対象にする",
  "metadata": {
    "experiment": "cache-v1"
  },
  "files": [
    {
      "path": "calculator.py",
      "content": "def add(a, b):\n    return a + b\n",
      "encoding": "utf-8"
    },
    {
      "path": "fixture.bin",
      "content": "AAE=",
      "encoding": "base64"
    }
  ],
  "persist_workspace": false
}
```

`hypothesis`の代わりに`prompt`または`message`も受け付けます。JSON string自体を実装仮説として送ることもできます。

| フィールド | 必須 | 説明 |
| --- | --- | --- |
| `hypothesis` | Yes | 自然言語の実装仮説 |
| `supplemental_context` | No | 制約や前提などの補助情報 |
| `metadata` | No | workflowへ渡す任意のJSON metadata |
| `files` | No | 一時リポジトリの初期ファイル |
| `persist_workspace` | No | `true`のときだけ最終workspace ZIPを返す。既定は`false` |

ファイルパスは一時workspace内に制限され、absolute pathや`..`によるworkspace外への書き込みは拒否されます。

#### Response

```json
{
  "run_id": "59d...",
  "report": "# Implementation Hypothesis Report\n...",
  "converged": true,
  "error": null,
  "workspace_files": [
    "calculator.py",
    "test_calculator.py"
  ],
  "workspace_archive": {
    "filename": "implementation-workspace-59d....zip",
    "media_type": "application/zip",
    "encoding": "base64",
    "data": "UEsDB...",
    "sha256": "...",
    "size_bytes": 512
  },
  "archive_error": null
}
```

`persist_workspace=false`の場合、`workspace_archive`は`null`です。

## セットアップ

### 前提

- Python 3.12以上と[uv](https://docs.astral.sh/uv/)
- Azure CLI
- Docker（mediumテストとローカルでのコンテナ確認に使用。WSL2の場合は[WSL2上でDocker Engineを使う](#wsl2上でdocker-engineを使う)を参照）
- Azure Developer CLI（デプロイ時のみ）

### Foundryリソース

| 項目 | 値 |
| --- | --- |
| Resource group | `rg-foundry-book` |
| Foundry resource | `mf-foundry-book` |
| Project | `first-project` |
| Project endpoint | `https://mf-foundry-book.services.ai.azure.com/api/projects/first-project` |
| Model deployment | `gpt-6-luna` |
| Model version | `2026-09-22` |
| Global Standard capacity | `200` |

### インストールと設定

```bash
uv sync
az login
cp .env.example .env
```

`run.py`とpytestは起動時に`.env`を読み込みます（`python-dotenv`、dev依存）。`.env`の値は既存の環境変数を上書きしません。Hosted Agentは`.env`を読まず、設定は`azure.yaml`から渡されます。`.env`はGit、コンテナimage、agentのデプロイパッケージのいずれにも含まれません。

| 環境変数 | 既定値 | 用途 |
| --- | --- | --- |
| `FOUNDRY_RESOURCE_URL` | `https://mf-foundry-book.openai.azure.com` | Copilot BYOK endpoint |
| `FOUNDRY_PROJECT_ENDPOINT` | `https://mf-foundry-book.services.ai.azure.com/api/projects/first-project` | review agentのFoundry project |
| `FOUNDRY_MODEL_DEPLOYMENT_NAME` | `gpt-6-luna` | model deployment |
| `AZURE_TOKEN_CREDENTIALS` | DefaultAzureCredential chain | ローカルまたはManaged Identity |
| `AGENT_TIMEOUT_SECONDS` | `900` | Copilot request timeout |
| `WORKFLOW_MAX_ITERATIONS` | `12` | workflowの最大superstep数 |

コマンドtimeoutやZIP上限などの細かな値は、[`workspace.py`](./src/handson_maf_ghcp/workspace.py)冒頭の定数で定義しています。

### ローカルで実行する

`run.py`はサーバーを立てずに1回だけ実行し、結果のJSONを表示します。`--persist-workspace`を付けるとZIPも返し、`--request`を使うと補助ファイルを含むInvocations APIと同じJSONを入力にできます。

```bash
uv run python run.py "PythonでLRUキャッシュの実装パターンを検証する"
uv run python run.py --persist-workspace "実装仮説"
uv run python run.py --request request.json
```

`main.py`はコンテナでも使う本番用エントリポイントのためdotenvに依存させず、ローカルではuvの`--env-file`で`.env`を渡してInvocations APIとして起動します。

```bash
uv run --env-file .env python main.py
```

別のターミナルから呼び出します。

```bash
curl http://127.0.0.1:8088/invocations -H "Content-Type: application/json" --data-binary @request.json
```

### WSL2上でDocker Engineを使う

WSL2を使ってWindowsでDockerを動かすのではなく、WSL2上でDockerを使うための手順です（つまり軽量LinuxコンテナであるWSL2のさらに上でコンテナを動かす二重コンテナ）。

参考: [Install Docker Engine on Debian](https://docs.docker.com/engine/install/debian/#installation-methods)

```bash
sudo apt-get update
sudo apt-get install ca-certificates curl gnupg lsb-release
sudo mkdir -m 0755 -p /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt install docker-ce docker-ce-cli containerd.io docker-compose-plugin
```

dockerサービスを再起動します（2025/07/10現在、WSL2でもデフォルトでサービスを使えるようになっています）。

```bash
sudo service docker restart
```

sudoなしでdockerを使用できるよう権限を調整します。

```bash
sudo usermod -aG docker $USER
```

保存後、Windows側で`wsl --shutdown`を実行してWSLを再起動します。再起動後、dockerサービスを起動します。

```bash
sudo service docker start
```

## デプロイ

### デプロイ済みのHosted Agent

| 項目 | 値 |
| --- | --- |
| Hosted Agent | `implementation-hypothesis-agent` |
| Active version | `3` |
| Invocations endpoint | `https://mf-foundry-book.services.ai.azure.com/api/projects/first-project/agents/implementation-hypothesis-agent/endpoint/protocols/invocations?api-version=v1` |

### Azure Developer CLIでデプロイする

```bash
azd ext install microsoft.foundry
az login
azd auth login
```

初期化時は既存projectとmodel deploymentを指定します。workspaceのGit初期化とCopilot runtimeの事前配置が必要なため、Dockerfileを使用するcontainer deploymentとします。初回のcontainer deploymentではACRもprovisionされます。

```bash
PROJECT_ID="$(az cognitiveservices account project show --resource-group rg-foundry-book --name mf-foundry-book --project-name first-project --query id --output tsv)"
azd ai agent init --no-prompt --project-id "$PROJECT_ID" --agent-name implementation-hypothesis-agent --model-deployment gpt-6-luna --deploy-mode container --protocol invocations --src .
azd env set AZURE_TOKEN_CREDENTIALS "ManagedIdentityCredential"
azd up
```

コードを変更した後の再デプロイは`azd deploy`で行います。

### モデル推論ロールを付与する

デプロイ後、Hosted AgentのManaged Identityにモデル推論ロールを付与します。

```bash
AGENT_PRINCIPAL_ID="$(azd ai agent show implementation-hypothesis-agent --output json | python -c 'import json,sys; print(json.load(sys.stdin)["instance_identity"]["principal_id"])')"
FOUNDRY_SCOPE="$(az cognitiveservices account show --resource-group rg-foundry-book --name mf-foundry-book --query id --output tsv)"
az role assignment create --assignee-object-id "$AGENT_PRINCIPAL_ID" --assignee-principal-type ServicePrincipal --role "Cognitive Services OpenAI User" --scope "$FOUNDRY_SCOPE"
```

### Hosted Agentを呼び出す

構造化requestはファイルで渡します。

```bash
azd ai agent invoke implementation-hypothesis-agent --protocol invocations --new-session --timeout 1800 --input-file request.json
```


## テスト

テストサイズごとの分類（small / medium / large）に沿って、テストを`tests/`以下の3ディレクトリに分けています。

| サイズ | ディレクトリ | 内容 | 外部依存 | 所要時間 |
| --- | --- | --- | --- | --- |
| small | [`tests/small`](./tests/small) | 単一プロセス内で入力モデル、一時workspace、ZIP、レビュー分岐、レポート整形を検証 | なし | 1秒未満 |
| medium | [`tests/medium`](./tests/medium) | コンテナをbuild・起動し、localhost経由でreadiness、OpenAPI、入力検証、`git`とCopilot runtimeの同梱を検証。モデルは呼ばない | Docker | 約3分 |
| large | [`tests/large`](./tests/large) | 実際のモデルを呼び、同じ実装仮説を`run.py`のローカル実行・ローカルコンテナ・デプロイ済みHosted Agentの3通りで実行して、実装・レビュー・承認・ZIP返却までを検証 | Azure、モデル課金、Docker | 今回の実行で約5分 |

```bash
# small
uv run pytest tests/small
# medium (Dockerが無い環境ではskip)
uv run pytest tests/medium
# large (要az login。モデル課金が発生する)
uv run pytest tests/large
```

ディレクトリの代わりにmarkerでも選択できます（例: `uv run pytest -m small`）。`uv run pytest`だけを実行するとlargeを含むすべてのテストが実行され、モデル課金が発生します。

largeのコンテナテストではコンテナ内に`az login`の資格情報が無いため、テストがホスト側に`az login`でトークンを発行する小さなエンドポイントを立て、Hosted Agentと同じManaged Identityの形式（`IDENTITY_ENDPOINT` / `IDENTITY_HEADER`）でコンテナに渡します。コンテナはhost networkで起動します。largeテストの呼び出し先は`HOSTED_AGENT_ENDPOINT`で変更できます。

largeテストは同じ`gpt-6-luna`デプロイを複数回使います。3経路を並列実行した際にtoken rate limit（HTTP 429）に達したため、デプロイのGlobal Standard capacityを`10`から`200`に変更しました。テストは並列化せずに実行してください。