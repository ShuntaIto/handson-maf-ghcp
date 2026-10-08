from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from dotenv import load_dotenv

# agent.py reads environment variables at import time, so load .env first.
load_dotenv()

from handson_maf_ghcp.agent import (  # noqa: E402
    InvocationInput,
    close_shared_resources,
    run_invocation,
)


async def run_once(invocation: InvocationInput) -> None:
    try:
        result = await run_invocation(invocation)
        print(result.model_dump_json(indent=2))
    finally:
        await close_shared_resources()


def main() -> None:
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
