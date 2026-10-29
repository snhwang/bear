"""``python -m bear.integrations.hermes install [--hermes-home DIR] [--force]``

Writes ``<hermes home>/plugins/bear/`` with a ``plugin.yaml`` manifest and an
``__init__.py`` that imports :func:`bear.integrations.hermes.register`. Use it
only where ``bear[hermes]`` cannot be pip-installed into Hermes's environment,
since the pip install registers the plugin through an entry point already.
BEAR must still be importable from that environment. Hermes loads the plugin
only once ``plugins.enabled`` in its ``config.yaml`` lists ``bear``.

``python -m bear.integrations.hermes lint CORPUS_DIR`` checks a corpus's gate
rules without starting Hermes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .plugin import HOOKS
from .settings import hermes_home

MANIFEST = """\
name: bear
version: {version}
description: "BEAR guidance in every turn and a policy gate on every tool call, with a replayable audit log."
author: "Scott N. Hwang (Penn State)"
provides_hooks:
{hooks}
"""

SHIM = '''\
"""Directory shim for the BEAR plugin. The code lives in the bear package."""
from bear.integrations.hermes import register  # noqa: F401
'''


def install(home: Path, force: bool = False) -> Path:
    from bear import __version__

    target = home / "plugins" / "bear"
    if target.exists() and not force and any(target.iterdir()):
        raise SystemExit(f"{target} already exists. Use --force to overwrite.")
    target.mkdir(parents=True, exist_ok=True)
    hooks = "\n".join(f"  - {h}" for h in HOOKS)
    (target / "plugin.yaml").write_text(MANIFEST.format(version=__version__, hooks=hooks),
                                        encoding="utf-8")
    (target / "__init__.py").write_text(SHIM, encoding="utf-8")
    return target


def lint_dir(path: Path) -> int:
    from bear.corpus import Corpus
    from bear.gate import lint

    issues = lint(Corpus.from_directory(path), widening_prefixes=("platform:",))
    for i in issues:
        print(f"{i.level:7} [{i.instruction_id}] {i.message}")
    errors = sum(1 for i in issues if i.level == "error")
    print(f"{len(issues)} issue(s), {errors} error(s)")
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bear.integrations.hermes")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_install = sub.add_parser("install", help="write the plugin into a Hermes home")
    p_install.add_argument("--hermes-home", type=Path, default=None)
    p_install.add_argument("--force", action="store_true")
    p_lint = sub.add_parser("lint", help="check a corpus's gate rules")
    p_lint.add_argument("corpus_dir", type=Path)
    args = parser.parse_args(argv)
    if args.cmd == "install":
        target = install(args.hermes_home or hermes_home(), force=args.force)
        print(f"wrote {target}")
        print("Next: add bear to plugins.enabled in Hermes's config.yaml and set "
              "plugins.entries.bear.settings.corpus_dir.")
        return 0
    return lint_dir(args.corpus_dir)


if __name__ == "__main__":
    sys.exit(main())
