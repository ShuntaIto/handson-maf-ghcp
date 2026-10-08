"""Large tests: invoke the deployed Hosted Agent end to end.

These tests call the real Hosted Agent, GitHub Copilot, and the Foundry model,
so they are slow and incur cost. They run only when RUN_LARGE_TESTS=1 and
require `az login`.
"""

import base64
import hashlib
import io
import os
import zipfile

import httpx
import pytest
from azure.identity import AzureCliCredential

ENDPOINT = os.getenv(
    "HOSTED_AGENT_ENDPOINT",
    "https://mf-foundry-book.services.ai.azure.com/api/projects/first-project"
    "/agents/implementation-hypothesis-agent/endpoint/protocols/invocations?api-version=v1",
)
FOUNDRY_SCOPE = "https://ai.azure.com/.default"

CALCULATOR_REQUEST = {
    "hypothesis": "calculator.py に subtract(left, right) を追加し、既存の add を壊さずにテストしてください。",
    "files": [
        {
            "path": "calculator.py",
            "content": "def add(left: int, right: int) -> int:\n    return left + right\n",
        },
        {
            "path": "test_calculator.py",
            "content": (
                "import unittest\n\n"
                "from calculator import add\n\n\n"
                "class CalculatorTests(unittest.TestCase):\n"
                "    def test_adds_two_numbers(self) -> None:\n"
                "        self.assertEqual(add(2, 3), 5)\n"
            ),
        },
    ],
    "persist_workspace": True,
}

pytestmark = [
    pytest.mark.large,
    pytest.mark.skipif(
        os.getenv("RUN_LARGE_TESTS") != "1",
        reason="Set RUN_LARGE_TESTS=1 to call the Hosted Agent.",
    ),
]


@pytest.fixture(scope="module")
def headers() -> dict[str, str]:
    token = AzureCliCredential().get_token(FOUNDRY_SCOPE).token
    return {"Authorization": f"Bearer {token}"}


def test_invalid_request_is_rejected(headers: dict[str, str]) -> None:
    response = httpx.post(ENDPOINT, json={}, headers=headers, timeout=300)

    assert response.status_code == 422


def test_hypothesis_is_implemented_reviewed_and_archived(headers: dict[str, str]) -> None:
    response = httpx.post(ENDPOINT, json=CALCULATOR_REQUEST, headers=headers, timeout=1800)
    assert response.status_code == 200, response.text
    result = response.json()

    assert result["converged"]
    assert "**Verdict:** approved" in result["report"]

    archive = result["workspace_archive"]
    content = base64.b64decode(archive["data"])
    assert hashlib.sha256(content).hexdigest() == archive["sha256"]
    with zipfile.ZipFile(io.BytesIO(content)) as zip_file:
        assert sorted(zip_file.namelist()) == ["calculator.py", "test_calculator.py"]
        assert "def subtract" in zip_file.read("calculator.py").decode()
