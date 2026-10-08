"""Medium tests: build the container and exercise it over localhost only.

These tests never call the model. They verify that the image starts, serves the
Invocations API, rejects invalid input, and contains the tools the workflow needs.
Requires Docker.
"""

import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
IMAGE = "handson-maf-ghcp:medium-test"
CONTAINER = "handson-maf-ghcp-medium-test"

pytestmark = [
    pytest.mark.medium,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker is required."),
]


def docker(*args: str) -> str:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout.strip()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def base_url() -> Iterator[str]:
    docker("build", "--tag", IMAGE, str(PROJECT_ROOT))
    subprocess.run(["docker", "rm", "--force", CONTAINER], capture_output=True)
    port = free_port()
    docker("run", "--detach", "--name", CONTAINER, "--publish", f"127.0.0.1:{port}:8088", IMAGE)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            try:
                if httpx.get(f"{url}/readiness").status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(1)
        else:
            pytest.fail(docker("logs", CONTAINER))
        yield url
    finally:
        subprocess.run(["docker", "rm", "--force", CONTAINER], capture_output=True)
        subprocess.run(["docker", "image", "rm", IMAGE], capture_output=True)


def test_readiness(base_url: str) -> None:
    assert httpx.get(f"{base_url}/readiness").json() == {"status": "healthy"}


def test_openapi_describes_invocations(base_url: str) -> None:
    spec = httpx.get(f"{base_url}/invocations/docs/openapi.json").json()

    assert "/invocations" in spec["paths"]
    assert {"WorkspaceFile", "WorkspaceArchive"} <= spec["components"]["schemas"].keys()


def test_invalid_request_is_rejected(base_url: str) -> None:
    response = httpx.post(f"{base_url}/invocations", json={})

    assert response.status_code == 422
    assert response.json()["error"] == "invalid_request"


@pytest.mark.usefixtures("base_url")
def test_image_contains_git_and_copilot_runtime() -> None:
    assert "git version" in docker("exec", CONTAINER, "git", "--version")
    cli_path = docker(
        "exec",
        CONTAINER,
        "python",
        "-c",
        "from copilot._cli_download import get_cached_cli_path; print(get_cached_cli_path())",
    )
    assert cli_path != "None"
