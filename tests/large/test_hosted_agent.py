"""Large test: デプロイ済みの Hosted Agent を呼び、実際のモデルで仮説を実装・レビューする。

`az login` の資格情報で Foundry の Invocations endpoint を直接呼ぶ。
呼び出し先は HOSTED_AGENT_ENDPOINT で変更できる。
"""

import os
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from azure.identity import AzureCliCredential

ENDPOINT = os.getenv(
    "HOSTED_AGENT_ENDPOINT",
    "https://mf-foundry-book.services.ai.azure.com/api/projects/first-project"
    "/agents/implementation-hypothesis-agent/endpoint/protocols/invocations?api-version=v1",
)
FOUNDRY_SCOPE = "https://ai.azure.com/.default"

pytestmark = pytest.mark.large


@pytest.fixture(scope="module")
def headers() -> dict[str, str]:
    token = AzureCliCredential().get_token(FOUNDRY_SCOPE).token
    return {"Authorization": "Bearer " + token}


def test_invalid_request_is_rejected(headers: dict[str, str]) -> None:
    response = httpx.post(ENDPOINT, json={}, headers=headers, timeout=300)

    assert response.status_code == 422


def test_hosted_agent_implements_hypothesis_with_the_model(
    headers: dict[str, str],
    calculator_request: dict[str, Any],
    assert_calculator_implemented: Callable[[dict[str, Any]], None],
) -> None:
    response = httpx.post(ENDPOINT, json=calculator_request, headers=headers, timeout=1800)

    assert response.status_code == 200, response.text
    assert_calculator_implemented(response.json())
