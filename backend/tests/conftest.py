from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.helpers import Env, make_env


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    yield make_env(tmp_path)
