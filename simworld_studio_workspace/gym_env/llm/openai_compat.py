"""OpenAI-compatible LLM client.

Handles three model families through one code path by varying
``base_url`` + ``api_key``:

  * GPT (default OpenAI endpoint)
  * Gemini (Google's OpenAI-compatible endpoint)
  * Qwen (DashScope OpenAI-compatible endpoint)

All three accept tool schemas in the standard OpenAI format and emit
``tool_calls`` blocks.  This module deliberately does not try to map
vendor-specific reasoning fields — if a provider exposes them later we
can add a small probe in :meth:`_extract_reasoning`.
"""

from __future__ import annotations

import base64
import io
import logging
import os
from typing import Any, Dict, List, Optional

import numpy as np

from .base import LLMClient, LLMMessage, LLMResponse, ToolCall

log = logging.getLogger(__name__)


_API_KEY_ENV = {
    "gpt": ("OPENAI_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "qwen": ("DASHSCOPE_API_KEY", "QWEN_API_KEY"),
}


class OpenAICompatClient(LLMClient):
    """OpenAI Python SDK pointed at any OpenAI-compatible endpoint."""

    def __init__(
        self,
        *,
        name: str,
        model: str,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ImportError("OpenAICompatClient requires `pip install openai`") from exc

        self.name = name
        self.model = model
        if api_key is None:
            for env in _API_KEY_ENV.get(name, ("OPENAI_API_KEY",)):
                api_key = os.environ.get(env)
                if api_key:
                    break
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._text_action_mode = False

    # ------------------------------------------------------------------

    def chat(
        self,
        messages: List[LLMMessage],
        tools: List[Dict[str, Any]],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse:
        oai_messages = self._convert_messages(messages)
        oai_tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                },
            }
            for t in tools
        ]

        # Try with tool_calls first.  If the server doesn't support
        # tools (vLLM without --enable-auto-tool-choice), fall back to
        # plain text where we parse the action name from the response.
        if not self._text_action_mode:
            try:
                log.debug("[%s] sending %d messages, %d tools",
                          self.name, len(oai_messages), len(oai_tools))
                resp = self._client.chat.completions.create(
                    model=self.model,
                    messages=oai_messages,
                    tools=oai_tools,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                return self._parse_response(resp)
            except Exception as exc:
                if "tool" in str(exc).lower() and "400" in str(exc):
                    log.warning(
                        "[%s] server rejected tools param (%s); "
                        "switching to text-action mode for this session",
                        self.name, exc,
                    )
                    self._text_action_mode = True
                else:
                    raise

        # ── Text-action fallback: inject action list + nav strategy into
        # system prompt so the model understands bearing semantics.
        tool_names = [t["name"] for t in tools]
        tool_desc = ", ".join(tool_names)
        inject = (
            f"\n\nAvailable actions: {tool_desc}\n"
            "\nYou MUST follow these rules EXACTLY:\n"
            "1. Read the 'bearing' number from the user message.\n"
            "2. If distance < 200 → reply: STOP\n"
            "3. If bearing is between -45 and +45 → reply: MOVE_FORWARD\n"
            "4. If bearing > +45 → reply: TURN_LEFT\n"
            "5. If bearing < -45 → reply: TURN_RIGHT\n"
            "\nReply with ONLY the action name. Nothing else. One word.\n"
            "Example: MOVE_FORWARD"
        )
        patched = list(oai_messages)
        if patched and patched[0].get("role") == "system":
            patched[0] = dict(patched[0])
            patched[0]["content"] = patched[0]["content"] + inject
        else:
            patched.insert(0, {"role": "system", "content": inject.strip()})

        # In text-action mode there are no real tool_call_ids.  Convert
        # role=tool → role=user and role=assistant with tool_calls →
        # plain assistant text so the model sees a clean user/assistant
        # alternation with action feedback.
        cleaned: List[Dict[str, Any]] = []
        for m in patched:
            if m.get("role") == "tool":
                # Merge tool result into a user message
                cleaned.append({"role": "user", "content": m.get("content", "ok")})
            elif m.get("role") == "assistant" and m.get("tool_calls"):
                # Strip tool_calls; keep only the text
                cleaned.append({"role": "assistant", "content": m.get("content") or ""})
            else:
                cleaned.append(m)
        # Merge consecutive same-role messages (required by many providers)
        def _to_str(c) -> str:
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                return "\n".join(
                    b.get("text", "") for b in c if isinstance(b, dict)
                )
            return str(c) if c else ""

        merged: List[Dict[str, Any]] = []
        for m in cleaned:
            if merged and merged[-1]["role"] == m["role"]:
                merged[-1]["content"] = _to_str(merged[-1]["content"]) + "\n" + _to_str(m.get("content"))
            else:
                mc = dict(m)
                # Flatten list content to string for text-action mode
                if isinstance(mc.get("content"), list):
                    mc["content"] = _to_str(mc["content"])
                merged.append(mc)
        patched = merged

        log.debug("[%s] text-action mode: %d messages", self.name, len(patched))
        # Cap output tokens tightly in text-action mode — the model
        # should emit at most one action name (~3 tokens).  Larger
        # budgets cause small models to hallucinate entire trajectories.
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=patched,
            max_tokens=min(max_tokens, 32),
            temperature=temperature,
        )
        return self._parse_text_action(resp, tool_names)

    # ------------------------------------------------------------------

    @staticmethod
    def _convert_messages(messages: List[LLMMessage]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for msg in messages:
            if msg.role == "tool":
                # OpenAI requires content to be a string for role=tool
                text_blocks = [b["text"] for b in msg.content if b["type"] == "text"]
                out.append({
                    "role": "tool",
                    "tool_call_id": msg.tool_call_id,
                    "content": "\n".join(text_blocks) or "ok",
                })
                continue

            if msg.role == "assistant":
                m: Dict[str, Any] = {"role": "assistant"}
                text_blocks = [b["text"] for b in msg.content if b["type"] == "text"]
                if text_blocks:
                    m["content"] = "\n".join(text_blocks)
                else:
                    m["content"] = None
                if msg.tool_calls:
                    m["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": _json_dumps(tc.arguments),
                            },
                        }
                        for tc in msg.tool_calls
                    ]
                out.append(m)
                continue

            # system / user
            content = _convert_content_blocks(msg.content)
            if msg.role == "system":
                # Some providers require text-only system content; flatten.
                text_only = "\n".join(
                    b["text"] for b in content if b.get("type") == "text"
                )
                out.append({"role": "system", "content": text_only})
            else:
                out.append({"role": "user", "content": content})
        return out

    @staticmethod
    def _parse_response(resp) -> LLMResponse:
        choice = resp.choices[0]
        msg = choice.message
        tool_calls: List[ToolCall] = []
        for tc in (msg.tool_calls or []):
            try:
                args = _json_loads(tc.function.arguments or "{}")
            except Exception:
                args = {}
            tool_calls.append(ToolCall(
                id=tc.id,
                name=tc.function.name,
                arguments=args,
            ))
        usage = {}
        if getattr(resp, "usage", None):
            usage = {
                "input_tokens": resp.usage.prompt_tokens,
                "output_tokens": resp.usage.completion_tokens,
                "total_tokens": resp.usage.total_tokens,
            }
        try:
            raw = resp.model_dump()
        except Exception:
            raw = {"_repr": repr(resp)}
        return LLMResponse(
            text=msg.content,
            tool_calls=tool_calls,
            reasoning=None,  # most OpenAI-compat providers don't expose this
            usage=usage,
            raw=raw,
        )

    @staticmethod
    def _parse_text_action(resp, valid_names: List[str]) -> LLMResponse:
        """Parse an action name from the model's plain-text response.

        Scans lines for one that exactly matches a known action name.
        Falls back to scanning for the name anywhere in the text.
        """
        import uuid
        choice = resp.choices[0]
        text = choice.message.content or ""

        # Try exact line match first (most reliable)
        found = None
        for line in text.strip().splitlines():
            cleaned = line.strip().upper()
            if cleaned in valid_names:
                found = cleaned
                break
        # Fallback: find first occurrence of any action name in text
        if found is None:
            upper = text.upper()
            for name in valid_names:
                if name in upper:
                    found = name
                    break

        tool_calls: List[ToolCall] = []
        if found:
            tool_calls.append(ToolCall(
                id=f"text_{uuid.uuid4().hex[:8]}",
                name=found,
                arguments={},
            ))

        usage = {}
        if getattr(resp, "usage", None):
            usage = {
                "input_tokens": resp.usage.prompt_tokens,
                "output_tokens": resp.usage.completion_tokens,
                "total_tokens": resp.usage.total_tokens,
            }
        try:
            raw = resp.model_dump()
        except Exception:
            raw = {"_repr": repr(resp)}
        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            reasoning=None,
            usage=usage,
            raw=raw,
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _convert_content_blocks(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for b in blocks:
        if b["type"] == "text":
            out.append({"type": "text", "text": b["text"]})
        elif b["type"] == "image":
            data_url = "data:image/png;base64," + _np_to_b64_png(b["image"])
            out.append({
                "type": "image_url",
                "image_url": {"url": data_url},
            })
    return out


def _np_to_b64_png(arr: np.ndarray) -> str:
    from PIL import Image
    img = Image.fromarray(arr.astype(np.uint8))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _json_dumps(obj):
    import json
    return json.dumps(obj, ensure_ascii=False)


def _json_loads(s):
    import json
    return json.loads(s)
