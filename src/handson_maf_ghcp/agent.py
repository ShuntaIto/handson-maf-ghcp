from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Never

from agent_framework import (
    Agent,
    AgentExecutorResponse,
    Case,
    Default,
    FunctionExecutor,
    WorkflowAgent,
    WorkflowBuilder,
    WorkflowConvergenceException,
    WorkflowContext,
)
from agent_framework_foundry import FoundryChatClient, FoundryChatOptions
from agent_framework_github_copilot import GitHubCopilotAgent, GitHubCopilotOptions
from azure.identity.aio import DefaultAzureCredential
from copilot import CopilotClient, PermissionHandler
from copilot.session import ProviderTokenArgs
from pydantic import BaseModel, Field, ValidationError

from .workspace import (
    WorkspaceArchive,
    WorkspaceFile,
    create_archive,
    create_review_tools,
    list_files,
    temporary_workspace,
)

DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_FOUNDRY_RESOURCE_URL = "https://mf-foundry-book.openai.azure.com"
DEFAULT_FOUNDRY_PROJECT_ENDPOINT = (
    "https://mf-foundry-book.services.ai.azure.com/api/projects/first-project"
)
AZURE_OPENAI_SCOPE = "https://cognitiveservices.azure.com/.default"
MODEL_DEPLOYMENT = (
    os.getenv("FOUNDRY_MODEL_DEPLOYMENT_NAME")
    or os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME")
    or DEFAULT_MODEL
)
FOUNDRY_RESOURCE_URL = (
    os.getenv("FOUNDRY_RESOURCE_URL")
    or os.getenv("AZURE_OPENAI_ENDPOINT")
    or DEFAULT_FOUNDRY_RESOURCE_URL
).rstrip("/")

IMPLEMENTATION_INSTRUCTIONS = """
You implement and validate exactly one implementation hypothesis in the current
temporary repository. The repository has no remote and must remain local.

1. Inspect the temporary repository and restate the user's requirements.
2. Record the relevant baseline behavior before changing code.
3. Implement the hypothesis with focused changes. Do not create a commit.
4. Run the narrowest relevant tests, linters, builds, or executable checks.
5. If a check fails because of your implementation, inspect the evidence, revise
   the implementation, and test again.
6. Return an implementation report containing the requirements, changed files,
   trial log, verification evidence, trade-offs, and remaining risks.

When the latest conversation context contains a structured review with
"verdict": "changes_requested", correct every item in "required_changes",
rerun the relevant checks, and return a revised implementation report. Do not
start another implementation hypothesis.
""".strip()

REVIEW_INSTRUCTIONS = """
You are the independent reviewer for an implementation-hypothesis workflow.
Review the latest implementation against the original user's request.

Independently inspect the temporary repository, changed files, and relevant
tests. Run the narrowest checks needed to verify the implementation. Do not
modify files and do not approve solely from the implementer's report.

Return "approved" only when the implementation satisfies every user requirement
and the verification evidence is sufficient. Otherwise return
"changes_requested" with specific actionable corrections. Use the same language
as the user's request in all text fields.
""".strip()


class InvocationInput(BaseModel):
    hypothesis: str = Field(min_length=1)
    supplemental_context: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    files: list[WorkspaceFile] = Field(default_factory=list)
    persist_workspace: bool = False

    @classmethod
    def from_payload(cls, payload: Any) -> InvocationInput:
        if isinstance(payload, str):
            return cls(hypothesis=payload)
        if not isinstance(payload, dict):
            raise TypeError("Request body must be a JSON object or string.")

        normalized = dict(payload)
        if "hypothesis" not in normalized:
            for alias in ("prompt", "message"):
                if isinstance(normalized.get(alias), str):
                    normalized["hypothesis"] = normalized[alias]
                    break
        return cls.model_validate(normalized)


class InvocationOutput(BaseModel):
    run_id: str
    report: str
    converged: bool
    error: str | None = None
    workspace_files: list[str]
    workspace_archive: WorkspaceArchive | None = None
    archive_error: str | None = None


class ReviewResult(BaseModel):
    verdict: Literal["approved", "changes_requested"]
    summary: str
    evidence: list[str] = Field(min_length=1)
    required_changes: list[str] = Field(default_factory=list)


@dataclass
class WorkflowRuntime:
    agent: WorkflowAgent
    implementation_agent: GitHubCopilotAgent
    copilot_client: CopilotClient

    async def close(self) -> None:
        await self.implementation_agent.stop()
        await self.copilot_client.stop()


credential = DefaultAzureCredential()
foundry_client: FoundryChatClient[FoundryChatOptions[ReviewResult]] = FoundryChatClient(
    project_endpoint=os.getenv("FOUNDRY_PROJECT_ENDPOINT", DEFAULT_FOUNDRY_PROJECT_ENDPOINT),
    model=MODEL_DEPLOYMENT,
    credential=credential,
)


async def get_azure_token(_: ProviderTokenArgs) -> str:
    token = await credential.get_token(AZURE_OPENAI_SCOPE)
    return token.token


def _review_result(response: AgentExecutorResponse) -> ReviewResult:
    review = response.agent_response.value
    if isinstance(review, ReviewResult):
        return review
    return ReviewResult.model_validate_json(response.agent_response.text)


def review_is_approved(response: AgentExecutorResponse) -> bool:
    try:
        return _review_result(response).verdict == "approved"
    except (ValidationError, ValueError):
        return False


async def format_report(
    response: AgentExecutorResponse,
    ctx: WorkflowContext[Never, str],
) -> None:
    review = _review_result(response)
    review_message_count = len(response.agent_response.messages)
    prior_messages = (
        response.full_conversation[:-review_message_count]
        if review_message_count
        else response.full_conversation
    )
    user_request = next(
        (message.text for message in reversed(response.full_conversation) if message.role == "user"),
        "(user request unavailable)",
    )
    implementation_report = next(
        (message.text for message in reversed(prior_messages) if message.role == "assistant"),
        "(implementation report unavailable)",
    )

    await ctx.yield_output(
        "\n".join(
            [
                "# Implementation Hypothesis Report",
                "",
                "## User request",
                user_request,
                "",
                "## Implementation result",
                implementation_report,
                "",
                "## Review result",
                f"**Verdict:** {review.verdict}",
                "",
                review.summary,
                "",
                "### Evidence",
                *[f"- {item}" for item in review.evidence],
                "",
                "### Required changes",
                *([f"- {item}" for item in review.required_changes] or ["- None"]),
            ]
        )
    )


def create_workflow_agent(workspace: Path) -> WorkflowRuntime:
    workspace = workspace.resolve()
    copilot_client = CopilotClient(
        working_directory=str(workspace),
        base_directory=str(workspace.parent / "copilot-state"),
        use_logged_in_user=False,
    )
    provider_options: GitHubCopilotOptions = {
        "model": DEFAULT_MODEL,
        "timeout": float(os.getenv("AGENT_TIMEOUT_SECONDS", "900")),
        "on_permission_request": PermissionHandler.approve_all,
        "provider": {
            "type": "openai",
            "wire_api": "responses",
            "base_url": f"{FOUNDRY_RESOURCE_URL}/openai/v1/",
            "model_id": DEFAULT_MODEL,
            "wire_model": MODEL_DEPLOYMENT,
            "bearer_token_provider": get_azure_token,
        },
    }
    implementation_agent = GitHubCopilotAgent(
        instructions=IMPLEMENTATION_INSTRUCTIONS,
        client=copilot_client,
        name="implementation-agent",
        description="Implements and validates one hypothesis in a temporary repository.",
        default_options=provider_options,
    )

    review_options: FoundryChatOptions[ReviewResult] = {
        "response_format": ReviewResult,
        "store": False,
    }
    review_agent: Agent[FoundryChatOptions[ReviewResult]] = Agent(
        client=foundry_client,
        instructions=REVIEW_INSTRUCTIONS,
        name="review-agent",
        description="Reviews the temporary repository and returns a structured verdict.",
        tools=create_review_tools(workspace),
        default_options=review_options,
    )

    report_formatter = FunctionExecutor(format_report, id="format-report")
    workflow = (
        WorkflowBuilder(
            start_executor=implementation_agent,
            max_iterations=int(os.getenv("WORKFLOW_MAX_ITERATIONS", "12")),
            name="implementation-hypothesis-workflow",
            description="Implements, reviews, revises, and reports one implementation hypothesis.",
            output_from=[report_formatter],
        )
        .add_edge(implementation_agent, review_agent)
        .add_switch_case_edge_group(
            review_agent,
            [
                Case(condition=review_is_approved, target=report_formatter),
                Default(target=implementation_agent),
            ],
        )
        .build()
    )
    return WorkflowRuntime(
        agent=workflow.as_agent(
            name="implementation-hypothesis-agent",
            description="Implements and reviews one hypothesis in an isolated temporary repository.",
        ),
        implementation_agent=implementation_agent,
        copilot_client=copilot_client,
    )


def _build_prompt(invocation: InvocationInput) -> str:
    sections = [f"Implementation hypothesis:\n{invocation.hypothesis}"]
    if invocation.supplemental_context:
        sections.append(f"Supplemental context:\n{invocation.supplemental_context}")
    if invocation.metadata:
        sections.append(
            "Metadata:\n"
            + json.dumps(invocation.metadata, ensure_ascii=False, indent=2, default=str)
        )
    if invocation.files:
        sections.append(
            "The temporary repository was initialized with these auxiliary files:\n"
            + "\n".join(f"- {item.path}" for item in invocation.files)
        )
    return "\n\n".join(sections)


async def run_invocation(invocation: InvocationInput) -> InvocationOutput:
    run_id = uuid.uuid4().hex
    async with temporary_workspace(invocation.files) as workspace:
        runtime = create_workflow_agent(workspace)
        converged = True
        error: str | None = None
        try:
            response = await runtime.agent.run(_build_prompt(invocation))
            report = response.text
        except WorkflowConvergenceException as exc:
            converged = False
            error = str(exc)
            report = (
                "# Implementation Hypothesis Report\n\n"
                "The workflow reached its review iteration limit before approval."
            )
        finally:
            await runtime.close()

        archive: WorkspaceArchive | None = None
        archive_error: str | None = None
        if invocation.persist_workspace:
            try:
                archive = create_archive(workspace, run_id)
            except RuntimeError as exc:
                archive_error = str(exc)

        return InvocationOutput(
            run_id=run_id,
            report=report,
            converged=converged,
            error=error,
            workspace_files=list_files(workspace),
            workspace_archive=archive,
            archive_error=archive_error,
        )


async def close_shared_resources() -> None:
    await credential.close()
