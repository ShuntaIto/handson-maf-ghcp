"""Large test: run.py をローカルで実行し、実際のモデルで仮説を実装・レビューする。

`az login` 済みの資格情報と `.env` の設定をそのまま使う。
"""

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.large


def test_run_py_implements_hypothesis_with_the_model(
    tmp_path: Path,
    calculator_request: dict[str, Any],
    assert_calculator_implemented: Callable[[dict[str, Any]], None],
) -> None:
    # API と同じ JSON をファイルにして run.py に渡し、標準出力の結果 JSON を検証する。
    request_file = tmp_path / "request.json"
    request_file.write_text(json.dumps(calculator_request, ensure_ascii=False), encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, "run.py", "--request", str(request_file)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=1800,
    )

    assert completed.returncode == 0, completed.stderr[-2000:]
    assert_calculator_implemented(json.loads(completed.stdout))
