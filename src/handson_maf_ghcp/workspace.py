"""一時ディレクトリを、使い捨てのローカル Git リポジトリとして扱うための関数群。

GitHub などからリポジトリを取得せず、API で受け取った補助ファイルだけを置いた
ディレクトリを 1 回の実行ごとに作り、終わったら削除する。
"""

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

# ---------------------------------------------------------------------------
# 設定値
# ツール出力・ZIP サイズ・コマンド実行時間の上限、ファイル一覧や ZIP から
# 除外するディレクトリ、レビューエージェントが実行してよいコマンド。
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# データモデル
# API で受け取る補助ファイルと、API で返すワークスペースの ZIP。
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# 共通の小さな処理
# 長すぎる出力の切り詰め、ワークスペース内でのコマンド実行、パスの安全確認。
# ---------------------------------------------------------------------------
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
    # 終了コードと出力をまとめた文字列を返す。check=True なら失敗時に例外にする。
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
    # ワークスペースの外や .git の中を指すパスは、外部からの書き込み・読み取りを防ぐため拒否する。
    path = (workspace / relative_path).resolve()
    if not path.is_relative_to(workspace) or ".git" in path.relative_to(workspace).parts:
        raise ValueError(f"Path is not allowed in the temporary workspace: {relative_path}")
    return path


# ---------------------------------------------------------------------------
# ワークスペースの作成と削除
# ---------------------------------------------------------------------------
@asynccontextmanager
async def temporary_workspace(files: list[WorkspaceFile]) -> AsyncGenerator[Path]:
    # async with で使うと、ブロックの間だけワークスペースが存在し、抜けると丸ごと削除される。
    with tempfile.TemporaryDirectory(prefix="implementation-hypothesis-") as temp_dir:
        # Copilot の状態ファイルを横に置けるよう、一時ディレクトリの下に workspace を作る。
        workspace = Path(temp_dir).resolve() / "workspace"
        workspace.mkdir()

        # API で受け取った補助ファイルを配置する。
        for file in files:
            path = resolve_path(workspace, file.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            if file.encoding == "base64":
                path.write_bytes(base64.b64decode(file.content))
            else:
                path.write_text(file.content, encoding="utf-8")

        # ローカルだけの Git リポジトリにし、初期状態をコミットしておく。
        # これでエージェントの変更を git diff で確認できる。
        for command in (
            ["git", "init", "--quiet"],
            ["git", "config", "user.name", "Implementation Hypothesis Agent"],
            ["git", "config", "user.email", "agent@localhost"],
            ["git", "add", "--all"],
            ["git", "commit", "--quiet", "--allow-empty", "-m", "Initial workspace"],
        ):
            await run_command(workspace, command, check=True)

        yield workspace


# ---------------------------------------------------------------------------
# 実行結果の取り出し
# ワークスペース内のファイル一覧と、ZIP 化して Base64 で返す処理。
# ---------------------------------------------------------------------------
def list_files(workspace: Path) -> list[str]:
    # .git や依存パッケージ、ビルド成果物は除いた、利用者に意味のあるファイルだけを返す。
    return sorted(
        path.relative_to(workspace).as_posix()
        for path in workspace.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and not EXCLUDED_PARTS.intersection(path.relative_to(workspace).parts)
    )


def create_archive(workspace: Path, run_id: str) -> WorkspaceArchive:
    # ファイル一覧と同じ対象をメモリ上で ZIP にし、JSON で返せるよう Base64 にする。
    # 受け取った側が壊れていないか確認できるよう SHA-256 も付ける。
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


# ---------------------------------------------------------------------------
# レビューエージェント用のツール
# GitHub Copilot を使わないレビューエージェントが、ワークスペースを自分で
# 確認できるようにするための関数。docstring はモデルへのツール説明として使われる。
# ---------------------------------------------------------------------------
def create_review_tools(workspace: Path) -> list[Callable[..., Any]]:
    # 実行ごとに別のワークスペースを見るよう、ツールを毎回このワークスペース用に作る。

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
        # 任意のシェル実行は許さず、テストやビルド用のコマンドだけを受け付ける。
        if not command or Path(command[0]).name not in VALIDATION_COMMANDS:
            raise ValueError(f"Unsupported validation command: {command}")
        return await run_command(workspace, command)

    return [inspect_workspace, read_workspace_file, run_validation]
