"""bear.__version__ must match the version pyproject.toml declares.

Audit records copy ``__version__`` into ``bear_version``, so a release that
bumps only pyproject writes the wrong version into every log.
"""

import tomllib
from pathlib import Path

import bear

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_version_matches_pyproject():
    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    assert bear.__version__ == declared
