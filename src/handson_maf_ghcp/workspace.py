"""Treat a temporary directory as a throwaway local Git repository."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import tempfile
import zipfile
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

MAX_OUTPUT_LENGTH = 20_000
MAX_ARCHIVE_BYTES = 10 * 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 300
EXCLUDED_PARTS = {
    ".git",
    ".gradle",
    ".mypy_cache",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
    "venv",
}
VALIDATION_COMMANDS = {
    "cargo",
    "dotnet",
    "go",
    "gradle",
    "gradlew",
    "mvn",
    "npm",
    "pnpm",
    "pytest",
    "python",
    "python3",
    "uv",
    "yarn",
}


class WorkspaceFile(BaseModel):
    path: str = Field(min_length=1)
    content: str
    encoding: Literal["utf-8", "base64"] = "utf-8"


class WorkspaceArchive(BaseModel):
    filename: str
    media_type: Literal["application/zip"] = "application/zip"
    encoding: Literal["base64"] = "base64"
    data: str
    sha256: str
    size_bytes: int


def truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_LENGTH:
        return text
    return f"{text[:MAX_OUTPUT_LENGTH]}\n... truncated ..."


async def run_command(
    workspace: Path,
    command: list[str],
    *,
    check: bool = False,
) -> str:
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await asyncio.wait_for(process.communicate(), timeout=COMMAND_TIMEOUT_SECONDS)
    result = f"$ {' '.join(command)}\nexit code: {process.returncode}\n{truncate(output.decode(errors='replace'))}"
    if check and process.returncode != 0:
        raise RuntimeError(result)
    return result


def resolve_path(workspace: Path, relative_path: str) -> Path:
    """Resolve a path and refuse anything outside the workspace or inside .git."""
    path = (workspace / relative_path).resolve()
    if not path.is_relative_to(workspace) or ".git" in path.relative_to(workspace).parts:
        raise ValueError(f"Path is not allowed in the temporary workspace: {relative_path}")
    return path


@asynccontextmanager
async def temporary_workspace(files: list[WorkspaceFile]) -> AsyncGenerator[Path]:
    """Yield a seeded Git repository that is deleted when the block exits."""
    with tempfile.TemporaryDirectory(prefix="implementation-hypothesis-") as temp_dir:
        workspace = Path(temp_dir).resolve() / "workspace"
        workspace.mkdir()

        for file in files:
            path = resolve_path(workspace, file.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            if file.encoding == "base64":
                path.write_bytes(base64.b64decode(file.content))
            else:
                path.write_text(file.content, encoding="utf-8")

        for command in (
            ["git", "init", "--quiet"],
            ["git", "config", "user.name", "Implementation Hypothesis Agent"],
            ["git", "config", "user.email", "agent@localhost"],
            ["git", "add", "--all"],
            ["git", "commit", "--quiet", "--allow-empty", "-m", "Initial workspace"],
        ):
            await run_command(workspace, command, check=True)

        yield workspace


def list_files(workspace: Path) -> list[str]:
    return sorted(
        path.relative_to(workspace).as_posix()
        for path in workspace.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and not EXCLUDED_PARTS.intersection(path.relative_to(workspace).parts)
    )


def create_archive(workspace: Path, run_id: str) -> WorkspaceArchive:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative_path in list_files(workspace):
            archive.write(workspace / relative_path, relative_path)

    content = buffer.getvalue()
    if len(content) > MAX_ARCHIVE_BYTES:
        raise RuntimeError(f"Workspace archive exceeds {MAX_ARCHIVE_BYTES} bytes.")
    return WorkspaceArchive(
        filename=f"implementation-workspace-{run_id}.zip",
        data=base64.b64encode(content).decode(),
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
    )


def create_review_tools(workspace: Path) -> list[Callable[..., Any]]:
    """Read-oriented tools that let the review agent inspect the workspace."""

    async def inspect_workspace() -> str:
        """Return repository status, current diff, and workspace file names."""
        status = await run_command(workspace, ["git", "status", "--short"])
        diff = await run_command(workspace, ["git", "--no-pager", "diff", "--no-ext-diff", "HEAD"])
        files = truncate("\n".join(list_files(workspace)))
        return f"{status}\n\n{diff}\n\nFiles:\n{files}"

    async def read_workspace_file(relative_path: str) -> str:
        """Read a UTF-8 text file under the temporary repository."""
        return truncate(resolve_path(workspace, relative_path).read_text(encoding="utf-8"))

    async def run_validation(command: list[str]) -> str:
        """Run a non-shell test or build command in the temporary repository."""
        if not command or Path(command[0]).name not in VALIDATION_COMMANDS:
            raise ValueError(f"Unsupported validation command: {command}")
        return await run_command(workspace, command)

    return [inspect_workspace, read_workspace_file, run_validation]
