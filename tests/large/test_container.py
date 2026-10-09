"""Large test: コンテナをローカルで起動し、実際のモデルで仮説を実装・レビューする。

コンテナ内には `az login` の資格情報が無いため、テスト側でホストに小さなトークン
エンドポイントを立てる。コンテナには Hosted Agent と同じ Managed Identity
（App Service 形式の IDENTITY_ENDPOINT / IDENTITY_HEADER）として見せるので、
コンテナ内のコードは本番と同じ経路で認証する。要 Docker と `az login`。
"""

import json
import secrets
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from azure.identity import AzureCliCredential

PROJECT_ROOT = Path(__file__).resolve().parents[2]
IMAGE = "handson-maf-ghcp:large-test"
CONTAINER = "handson-maf-ghcp-large-test"

pytestmark = [
    pytest.mark.large,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker is required."),
]


def docker(*args: str) -> str:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout.strip()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def identity_endpoint() -> Iterator[tuple[str, str]]:
    # Managed Identity の代わりに、ホストの az login でトークンを発行するエンドポイント。
    credential = AzureCliCredential()
    header = secrets.token_hex(16)

    class TokenHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.headers.get("X-IDENTITY-HEADER") != header:
                self.send_response(403)
                self.end_headers()
                return
            resource = parse_qs(urlparse(self.path).query)["resource"][0]
            token = credential.get_token(f"{resource.rstrip('/')}/.default")
            body = json.dumps(
                {
                    "access_token": token.token,
                    "expires_on": str(token.expires_on),
                    "resource": resource,
                    "token_type": "Bearer",
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            # テスト出力を汚さないよう、アクセスログは出さない。
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), TokenHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/token", header
    finally:
        server.shutdown()


@pytest.fixture(scope="module")
def base_url(identity_endpoint: tuple[str, str]) -> Iterator[str]:
    # host network で起動し、コンテナからホストのトークンエンドポイントへ届くようにする。
    endpoint, header = identity_endpoint
    port = free_port()
    docker("build", "--tag", IMAGE, str(PROJECT_ROOT))
    subprocess.run(["docker", "rm", "--force", CONTAINER], capture_output=True)
    docker(
        "run",
        "--detach",
        "--name",
        CONTAINER,
        "--network",
        "host",
        "--env",
        f"PORT={port}",
        "--env",
        f"IDENTITY_ENDPOINT={endpoint}",
        "--env",
        f"IDENTITY_HEADER={header}",
        "--env",
        "AZURE_TOKEN_CREDENTIALS=ManagedIdentityCredential",
        # .env などで上書きしている場合は、その接続先をコンテナにも渡す。
        "--env",
        "FOUNDRY_RESOURCE_URL",
        "--env",
        "FOUNDRY_PROJECT_ENDPOINT",
        "--env",
        "FOUNDRY_MODEL_DEPLOYMENT_NAME",
        IMAGE,
    )
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


def test_container_implements_hypothesis_with_the_model(
    base_url: str,
    calculator_request: dict[str, Any],
    assert_calculator_implemented: Callable[[dict[str, Any]], None],
) -> None:
    response = httpx.post(f"{base_url}/invocations", json=calculator_request, timeout=1800)

    assert response.status_code == 200, f"{response.text}\n{docker('logs', '--tail', '50', CONTAINER)}"
    assert_calculator_implemented(response.json())
