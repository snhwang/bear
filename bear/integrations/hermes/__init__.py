"""BEAR as a Hermes Agent plugin.

Install BEAR into Hermes's Python environment with the ``hermes`` extra.
Hermes finds :func:`register` through the ``hermes_agent.plugins`` entry
point. Where that is not possible, ``python -m bear.integrations.hermes
install`` writes a small directory plugin that imports it instead. Either
way, enable it in Hermes's ``config.yaml``::

    plugins:
      enabled: [bear]
      entries:
        bear:
          settings:
            corpus_dir: ~/bear-corpora/hermes
            mode: observe
"""

from __future__ import annotations

from typing import Any

__all__ = ["register"]

_PLUGIN = None


def register(ctx: Any) -> None:
    """Hermes's entry point. Builds the plugin and registers its hooks."""
    global _PLUGIN
    from .plugin import make_plugin

    _PLUGIN = make_plugin(ctx)
    _PLUGIN.register(ctx)
