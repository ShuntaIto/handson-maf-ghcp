# Implementation Hypothesis Agent

自然言語の実装仮説を受け取り、隔離された一時ディレクトリを仮の
リポジトリとして実装・検証・レビューする Microsoft Agent Framework
workflow です。GitHub のリポジトリは取得しません。

## Workflow

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

各 invocation は次の順序で処理されます。

1. invocation 専用の `TemporaryDirectory` を作成する
2. API で受け取った補助ファイルを安全に展開する
3. ローカル Git リポジトリを初期化し、入力ファイルを baseline commit にする
4. GitHub Copilot が実装仮説を実装・検証する
5. `Agent` が差分とテストを確認し、Pydantic structured output で判定する
6. `changes_requested` なら実装エージェントへ差し戻す
7. `approved` ならレポートを整形する
8. 既定では一時ディレクトリを削除する
9. `persist_workspace=true` の場合だけ、ソースを ZIP 化して Base64 で返す

ZIP には実装成果物を含め、内部用の `.git`、`__pycache__`、
`.pytest_cache` は含めません。既定の ZIP 上限は 10 MiB です。

## ファイル構成

| ファイル | 役割 |
| --- | --- |
| [`src/handson_maf_ghcp/agent.py`](./src/handson_maf_ghcp/agent.py) | API モデル、実装・レビューエージェント、workflow、レポート整形 |
| [`src/handson_maf_ghcp/workspace.py`](./src/handson_maf_ghcp/workspace.py) | 一時リポジトリの作成・削除、ファイル一覧、ZIP 化、レビュー用ツール |
| [`run.py`](./run.py) | 1 個の仮説をローカルで一度だけ実行 |
| [`main.py`](./main.py) | Invocations API としてサーブ |

## API

サーバーは Invocations protocol 2.0 を使用し、`POST /invocations` を提供します。

### Request

```json
{
  "hypothesis": "キャッシュを追加して重複計算を削減する",
  "supplemental_context": "Python 3.12 を対象にする",
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

`hypothesis` の代わりに `prompt` または `message` も受け付けます。
JSON string 自体を実装仮説として送ることもできます。

| フィールド | 必須 | 説明 |
| --- | --- | --- |
| `hypothesis` | Yes | 自然言語の実装仮説 |
| `supplemental_context` | No | 制約や前提などの補助情報 |
| `metadata` | No | workflow へ渡す任意の JSON metadata |
| `files` | No | 一時リポジトリの初期ファイル |
| `persist_workspace` | No | `true` のときだけ最終 workspace ZIP を返す。既定は `false` |

ファイルパスは一時 workspace 内に制限され、absolute path や `..` による
workspace 外への書き込みは拒否されます。

### Response

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

`persist_workspace=false` の場合、`workspace_archive` は `null` です。

## ローカル実行

```bash
uv sync
az login

export FOUNDRY_RESOURCE_URL="https://mf-foundry-book.openai.azure.com"
export FOUNDRY_PROJECT_ENDPOINT="https://mf-foundry-book.services.ai.azure.com/api/projects/first-project"
export FOUNDRY_MODEL_DEPLOYMENT_NAME="gpt-6-luna"
export AZURE_TOKEN_CREDENTIALS="AzureCliCredential"

uv run python run.py \
  "Python で LRU キャッシュの実装パターンを検証する"
```

ZIP も取得する場合:

```bash
uv run python run.py --persist-workspace "実装仮説"
```

補助ファイルを含む構造化入力では、Invocations API と同じ JSON を使用します。

```bash
uv run python run.py --request request.json
```

## ローカル Invocations API

```bash
uv run python main.py
```

```bash
curl http://127.0.0.1:8088/invocations \
  -H "Content-Type: application/json" \
  --data-binary @request.json
```

OpenAPI document:

```text
http://127.0.0.1:8088/invocations/docs/openapi.json
```

## Foundry resources

| 項目 | 値 |
| --- | --- |
| Resource group | `rg-foundry-book` |
| Foundry resource | `mf-foundry-book` |
| Project | `first-project` |
| Project endpoint | `https://mf-foundry-book.services.ai.azure.com/api/projects/first-project` |
| Model deployment | `gpt-6-luna` |
| Model version | `2026-09-22` |
| Hosted Agent | `implementation-hypothesis-agent` |
| Active version | `3` |
| Invocations endpoint | `https://mf-foundry-book.services.ai.azure.com/api/projects/first-project/agents/implementation-hypothesis-agent/endpoint/protocols/invocations?api-version=v1` |

## Azure Developer CLI deployment

```bash
azd ext install microsoft.foundry
az login
azd auth login
```

初期化時は既存 project と model deployment を指定します。workspace の
Git 初期化と Copilot runtime の事前配置が必要なため、Dockerfile を使用する
container deployment とします。

```bash
PROJECT_ID="$(
  az cognitiveservices account project show \
    --resource-group rg-foundry-book \
    --name mf-foundry-book \
    --project-name first-project \
    --query id \
    --output tsv
)"

azd ai agent init \
  --no-prompt \
  --project-id "$PROJECT_ID" \
  --agent-name implementation-hypothesis-agent \
  --model-deployment gpt-6-luna \
  --deploy-mode container \
  --protocol invocations \
  --src .

azd env set AZURE_TOKEN_CREDENTIALS "ManagedIdentityCredential"

azd up
```

初回の container deployment では ACR も provision されます。デプロイ後、
Hosted Agent の Managed Identity にモデル推論ロールを付与します。

```bash
AGENT_PRINCIPAL_ID="$(
  azd ai agent show implementation-hypothesis-agent --output json |
    python -c 'import json,sys; print(json.load(sys.stdin)["instance_identity"]["principal_id"])'
)"
FOUNDRY_SCOPE="$(
  az cognitiveservices account show \
    --resource-group rg-foundry-book \
    --name mf-foundry-book \
    --query id \
    --output tsv
)"

az role assignment create \
  --assignee-object-id "$AGENT_PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Cognitive Services OpenAI User" \
  --scope "$FOUNDRY_SCOPE"
```

構造化 request はファイルで渡します。

```bash
azd ai agent invoke \
  implementation-hypothesis-agent \
  --protocol invocations \
  --new-session \
  --timeout 1800 \
  --input-file request.json
```

## Configuration

| 環境変数 | 既定値 | 用途 |
| --- | --- | --- |
| `FOUNDRY_RESOURCE_URL` | `https://mf-foundry-book.openai.azure.com` | Copilot BYOK endpoint |
| `FOUNDRY_PROJECT_ENDPOINT` | `https://mf-foundry-book.services.ai.azure.com/api/projects/first-project` | review agent の Foundry project |
| `FOUNDRY_MODEL_DEPLOYMENT_NAME` | `gpt-6-luna` | model deployment |
| `AZURE_TOKEN_CREDENTIALS` | DefaultAzureCredential chain | ローカルまたは Managed Identity |
| `AGENT_TIMEOUT_SECONDS` | `900` | Copilot request timeout |
| `WORKFLOW_MAX_ITERATIONS` | `12` | workflow の最大 superstep 数 |

コマンド timeout や ZIP 上限などの細かな値は、
[`workspace.py`](./src/handson_maf_ghcp/workspace.py) 冒頭の定数で定義しています。

## Validation

Google のテストサイズ分類（small / medium / large）に沿って、テストを
`tests/` 以下の3ディレクトリに分けています。

| サイズ | ディレクトリ | 内容 | 外部依存 | 所要時間 |
| --- | --- | --- | --- | --- |
| small | [`tests/small`](./tests/small) | 単一プロセス内で入力モデル、一時 workspace、ZIP、workflow 構成、レビュー分岐、レポート整形を検証 | なし | 1 秒未満 |
| medium | [`tests/medium`](./tests/medium) | コンテナを build・起動し、localhost 経由で readiness、OpenAPI、入力検証、`git` と Copilot runtime の同梱を検証。モデルは呼ばない | Docker | 約 3 分 |
| large | [`tests/large`](./tests/large) | デプロイ済み Hosted Agent を呼び、実装・レビュー・承認・ZIP 返却までを検証 | Azure, モデル課金 | 約 5 分 |

```bash
uv sync

# small
uv run pytest tests/small

# medium (Docker が無い環境では skip)
uv run pytest tests/medium

# large (課金が発生するため明示的に有効化。要 az login)
RUN_LARGE_TESTS=1 uv run pytest tests/large
```

ディレクトリの代わりに marker でも選択できます（例: `uv run pytest -m small`）。
`uv run pytest` だけを実行すると small と medium が実行され、large は skip されます。

large テストの呼び出し先は `HOSTED_AGENT_ENDPOINT` で変更できます。
