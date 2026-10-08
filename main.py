"""Invocations API としてワークフローエージェントをサーブするエントリポイント。

Hosted Agent のコンテナでも、ローカルの `uv run --env-file .env python main.py` でも
このファイルを起動する。
"""

from __future__ import annotations

from typing import Any

from azure.ai.agentserver.invocations import InvocationAgentServerHost
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from handson_maf_ghcp.agent import (
    InvocationInput,
    InvocationOutput,
    close_shared_resources,
    run_invocation,
)

# ---------------------------------------------------------------------------
# OpenAPI 定義
# 入出力の Pydantic モデルからスキーマを作り、GET /invocations/docs/openapi.json で公開する。
# モデル間の参照 ($defs) は OpenAPI の components.schemas に移して解決できるようにする。
# ---------------------------------------------------------------------------
input_schema = InvocationInput.model_json_schema(
    ref_template="#/components/schemas/{model}"
)
output_schema = InvocationOutput.model_json_schema(
    ref_template="#/components/schemas/{model}"
)
component_schemas = {
    **input_schema.pop("$defs", {}),
    **output_schema.pop("$defs", {}),
}

OPENAPI_SPEC: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {
        "title": "Implementation Hypothesis Agent",
        "version": "1.0.0",
    },
    "paths": {
        "/invocations": {
            "post": {
                "summary": "Implement and review one implementation hypothesis",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": input_schema,
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Implementation report and optional workspace archive",
                        "content": {
                            "application/json": {
                                "schema": output_schema,
                            }
                        },
                    },
                    "422": {"description": "Invalid request"},
                },
            }
        }
    },
    "components": {"schemas": component_schemas},
}

# ---------------------------------------------------------------------------
# サーバー
# POST /invocations で実装仮説を受け取り、1 回の workflow 実行結果を JSON で返す。
# ---------------------------------------------------------------------------
app = InvocationAgentServerHost(openapi_spec=OPENAPI_SPEC)


@app.invoke_handler
async def invoke(request: Request) -> JSONResponse:
    # 入力が不正なら workflow を動かさず 422 を返す。
    try:
        invocation = InvocationInput.from_payload(await request.json())
    except (TypeError, ValueError, ValidationError) as exc:
        details = exc.errors() if isinstance(exc, ValidationError) else [{"msg": str(exc)}]
        return JSONResponse(
            {"error": "invalid_request", "details": details},
            status_code=422,
        )

    # 一時ワークスペースの作成から実装・レビュー・レポート作成までを 1 回実行する。
    result = await run_invocation(invocation)
    return JSONResponse(result.model_dump(mode="json"))


# サーバー停止時に、全実行で共有している Azure の資格情報を閉じる。
app.shutdown_handler(close_shared_resources)

if __name__ == "__main__":
    app.run()
