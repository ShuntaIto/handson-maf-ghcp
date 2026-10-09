"""large テストで共通に使うリクエストと結果の検証。

large テストはすべて実際のモデルを呼び出し、同じ実装仮説（calculator に subtract を
追加する）を、ローカルスクリプト・ローカルコンテナ・Hosted Agent のそれぞれで実行する。
"""

import base64
import copy
import hashlib
import io
import zipfile
from collections.abc import Callable
from typing import Any

import pytest

CALCULATOR_REQUEST: dict[str, Any] = {
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


@pytest.fixture
def calculator_request() -> dict[str, Any]:
    return copy.deepcopy(CALCULATOR_REQUEST)


@pytest.fixture
def assert_calculator_implemented() -> Callable[[dict[str, Any]], None]:
    # レビューで承認され、返ってきた ZIP に subtract が実装されていることを確認する。
    def check(result: dict[str, Any]) -> None:
        assert result["converged"]
        assert "**Verdict:** approved" in result["report"]

        archive = result["workspace_archive"]
        content = base64.b64decode(archive["data"])
        assert hashlib.sha256(content).hexdigest() == archive["sha256"]
        with zipfile.ZipFile(io.BytesIO(content)) as zip_file:
            assert sorted(zip_file.namelist()) == ["calculator.py", "test_calculator.py"]
            assert "def subtract" in zip_file.read("calculator.py").decode()

    return check
