"""Decoupled agent-memory interface.

The runner talks to memory through this Protocol only; concrete
implementations (mem0, custom vector store, rule book, ...) live in
sibling modules and are selected by :func:`build_memory` in
``__init__.py``.

Interface is intentionally tiny:

  * ``insert(text, metadata)`` — record something the agent just saw / did.
  * ``query(text, k)``         — fetch up-to-k relevant past records.
  * ``reset()``                — start a fresh episode / run scope.

Everything else (how to embed, how to dedupe, what backend to use) is
the implementation's business.  A backend that doesn't need one of
these methods (e.g. ``NullMemory``) just makes it a no-op.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class AgentMemory(Protocol):
    """Minimal memory interface used by :func:`gym_env.runner.run_episode`."""

    def insert(self, text: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Record a single memory item.

        ``text`` is the thing to remember (e.g. "turned left, d_goal went
        from 800 to 820, bad move").  ``metadata`` is optional structured
        context the backend may index or ignore.
        """
        ...

    def query(self, text: str, k: int = 5) -> List[str]:
        """Return up to ``k`` memory strings relevant to ``text``.

        Backends that don't do retrieval (e.g. a manual/rule-book) may
        ignore ``text`` and return the same static set every call.
        """
        ...

    def reset(self) -> None:
        """Called at the start of each episode.  No-op by default."""
        ...


class NullMemory:
    """Memory that remembers nothing.  Used when ``--memory none``."""

    name = "null"

    def insert(self, text: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        return

    def query(self, text: str, k: int = 5) -> List[str]:
        return []

    def reset(self) -> None:
        return


class ReadOnlyMemory:
    """Wrapper that silences ``insert()`` while forwarding ``query()`` and ``reset()``.

    Used in test/eval mode: the agent benefits from memories accumulated
    during training but cannot write new ones, ensuring evaluation is
    deterministic and does not contaminate the training memory store.
    """

    name = "read_only"

    def __init__(self, inner: AgentMemory) -> None:
        self._inner = inner
        self.name = f"read_only({getattr(inner, 'name', type(inner).__name__)})"

    def insert(self, text: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        return

    def query(self, text: str, k: int = 5) -> List[str]:
        return self._inner.query(text, k)

    def reset(self) -> None:
        self._inner.reset()

    def get_system_prompt_section(self) -> str:
        """Forward system prompt section from inner memory (read-only safe)."""
        if hasattr(self._inner, "get_system_prompt_section"):
            return self._inner.get_system_prompt_section()
        return ""
