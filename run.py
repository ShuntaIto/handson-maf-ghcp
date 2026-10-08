"""実装仮説をローカルで 1 回だけ実行するためのスクリプト。

サーバーを立てずに、Invocations API と同じ処理を直接呼び出して結果の JSON を表示する。
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from dotenv import load_dotenv

# agent.py は読み込まれた時点で環境変数を参照するため、先に .env を読み込む。
load_dotenv()

from handson_maf_ghcp.agent import (  # noqa: E402
    InvocationInput,
    close_shared_resources,
    run_invocation,
)


async def run_once(invocation: InvocationInput) -> None:
    # 1 回実行して結果を表示し、最後に共有している Azure の資格情報を閉じる。
    try:
        result = await run_invocation(invocation)
        print(result.model_dump_json(indent=2))
    finally:
        await close_shared_resources()


def main() -> None:
    # コマンドライン引数: 仮説を直接渡すか、API と同じ形式の JSON ファイルを渡す。
    parser = argparse.ArgumentParser(description="Implement and review one hypothesis.")
    parser.add_argument("hypothesis", nargs="?")
    parser.add_argument(
        "--request",
        type=Path,
        help="JSON file containing the same payload accepted by POST /invocations.",
    )
    parser.add_argument(
        "--persist-workspace",
        action="store_true",
        help="Include the final temporary workspace as a Base64 ZIP.",
    )
    args = parser.parse_args()

    # 引数から API と同じ入力モデルを組み立てる。補助ファイルを渡したい場合は --request を使う。
    if args.request:
        invocation = InvocationInput.model_validate_json(args.request.read_text())
    elif args.hypothesis:
        invocation = InvocationInput(
            hypothesis=args.hypothesis,
            persist_workspace=args.persist_workspace,
        )
    else:
        parser.error("provide a hypothesis or --request")

    asyncio.run(run_once(invocation))


if __name__ == "__main__":
    main()
