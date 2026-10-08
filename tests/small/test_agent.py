"""Small tests: single process, no network, no model calls."""

import asyncio
import base64
import hashlib
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Never, cast

import pytest
from agent_framework import AgentExecutorResponse, AgentResponse, Message, WorkflowContext

from handson_maf_ghcp.agent import (
    InvocationInput,
    ReviewResult,
    format_report,
    review_is_approved,
)
from handson_maf_ghcp.workspace import (
    WorkspaceFile,
    create_archive,
    list_files,
    temporary_workspace,
)

pytestmark = pytest.mark.small


class ReportContext:
    def __init__(self) -> None:
        self.outputs: list[str] = []

    async def yield_output(self, output: str) -> None:
        self.outputs.append(output)


def review_response(result: ReviewResult, conversation: list[Message] | None = None) -> AgentExecutorResponse:
    agent_response = AgentResponse[ReviewResult](
        messages=[Message("assistant", [result.model_dump_json()])],
        value=result,
        response_format=ReviewResult,
    )
    return AgentExecutorResponse(
        executor_id="review-agent",
        agent_response=cast(AgentResponse, agent_response),
        full_conversation=conversation or [],
    )


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"hypothesis": "Try a cache."}, "Try a cache."),
        ({"message": "Try a queue."}, "Try a queue."),
        ({"prompt": "Try a stack."}, "Try a stack."),
        ("Try a retry policy.", "Try a retry policy."),
    ],
)
def test_payload_accepts_hypothesis_and_aliases(payload: object, expected: str) -> None:
    invocation = InvocationInput.from_payload(payload)

    assert invocation.hypothesis == expected
    assert invocation.persist_workspace is False


def test_workspace_is_seeded_archived_and_deleted() -> None:
    files = [
        WorkspaceFile(path="README.md", content="# Sample"),
        WorkspaceFile(
            path="data/value.bin",
            content=base64.b64encode(b"\x00\x01").decode(),
            encoding="base64",
        ),
    ]

    async def inspect() -> tuple[Path, bool, list[str], bytes, str]:
        async with temporary_workspace(files) as workspace:
            archive = create_archive(workspace, "test-run")
            return (
                workspace,
                (workspace / ".git").is_dir(),
                list_files(workspace),
                base64.b64decode(archive.data),
                archive.sha256,
            )

    workspace, has_git, listed, archive_bytes, sha256 = asyncio.run(inspect())

    assert has_git
    assert listed == ["README.md", "data/value.bin"]
    assert sha256 == hashlib.sha256(archive_bytes).hexdigest()
    with zipfile.ZipFile(BytesIO(archive_bytes)) as archive:
        assert sorted(archive.namelist()) == ["README.md", "data/value.bin"]
    assert not workspace.exists()


@pytest.mark.parametrize("path", ["../outside.txt", ".git/config"])
def test_workspace_rejects_unsafe_paths(path: str) -> None:
    async def open_workspace() -> None:
        async with temporary_workspace([WorkspaceFile(path=path, content="blocked")]):
            pass

    with pytest.raises(ValueError, match="not allowed"):
        asyncio.run(open_workspace())


@pytest.mark.parametrize(
    ("verdict", "approved"),
    [("approved", True), ("changes_requested", False)],
)
def test_review_verdict_controls_approval(verdict: str, approved: bool) -> None:
    result = ReviewResult.model_validate(
        {"verdict": verdict, "summary": "Reviewed.", "evidence": ["Tests ran."]}
    )

    assert review_is_approved(review_response(result)) is approved


def test_format_report_emits_external_output() -> None:
    result = ReviewResult(
        verdict="approved",
        summary="All requirements are verified.",
        evidence=["The focused test suite passed."],
    )
    response = review_response(
        result,
        [
            Message("user", ["Implement the cache hypothesis."]),
            Message("assistant", ["Changed cache.py and all tests passed."]),
            Message("assistant", [result.model_dump_json()]),
        ],
    )
    context = ReportContext()

    asyncio.run(format_report(response, cast(WorkflowContext[Never, str], context)))

    assert len(context.outputs) == 1
    report = context.outputs[0]
    assert "Implement the cache hypothesis." in report
    assert "Changed cache.py and all tests passed." in report
    assert "**Verdict:** approved" in report
