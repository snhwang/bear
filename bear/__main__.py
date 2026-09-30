"""Command-line entry point: ``python -m bear <command> ...``.

Commands:
    verify-audit LOG    Re-run every retrieval in an audit log (see bear.audit).
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["verify-audit"]:
        from bear.audit import _verify_main
        return _verify_main(argv[1:])
    print(__doc__.strip())
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
