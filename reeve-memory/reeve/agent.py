"""High-level Reeve agent wrapper."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from reeve.middleware import MessageLike, ReeveMiddleware, coerce_message, normalize_response_text


class ReeveAgent:
    """Small framework-agnostic chat wrapper with persistent Reeve memory."""

    def __init__(
        self,
        *,
        llm: Any,
        memory: ReeveMiddleware | None = None,
        middleware: ReeveMiddleware | None = None,
        system_prompt: str | None = None,
        namespace: str = "default",
        **middleware_kwargs: Any,
    ) -> None:
        self.llm = llm
        if memory is not None and middleware is not None and memory is not middleware:
            raise ValueError("Use either memory or middleware, not both")
        if memory is not None and (middleware_kwargs or namespace != "default"):
            raise ValueError("When memory is provided, omit namespace and middleware kwargs")
        self.middleware = memory or middleware or ReeveMiddleware(
            namespace=namespace,
            **middleware_kwargs,
        )
        self.system_prompt = system_prompt
        self.history: list[dict[str, Any]] = []

    def chat(
        self,
        message: str,
        *,
        history: Sequence[MessageLike] | None = None,
        remember: bool = True,
        **llm_kwargs: Any,
    ) -> str:
        """Send a chat message through retrieval, injection, LLM call, and storage."""

        messages = self._build_messages(message, history=history)
        injection = self.middleware.before_call(message, messages=messages)
        raw_response = self._call_llm(injection.messages, **llm_kwargs)
        response_text = normalize_response_text(raw_response)

        self.history = [
            *self._without_memory_context(injection.messages),
            {"role": "assistant", "content": response_text},
        ]

        if remember:
            self.middleware.after_call(
                message,
                response_text,
                messages=[*messages, {"role": "assistant", "content": response_text}],
            )

        return response_text

    def _build_messages(
        self,
        message: str,
        *,
        history: Sequence[MessageLike] | None,
    ) -> list[dict[str, Any]]:
        base_history = list(history) if history is not None else list(self.history)
        messages = [coerce_message(item) for item in base_history]
        if self.system_prompt and not any(
            str(item.get("role", "")).lower() == "system" for item in messages
        ):
            messages.insert(0, {"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": message})
        return messages

    def _call_llm(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        if hasattr(self.llm, "invoke"):
            return self.llm.invoke(messages, **kwargs)
        if hasattr(self.llm, "chat") and callable(self.llm.chat):
            return self.llm.chat(messages, **kwargs)
        if callable(self.llm):
            return self.llm(messages, **kwargs)
        raise TypeError("llm must be callable or expose an invoke(messages, ...) method")

    def _without_memory_context(
        self,
        messages: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        cleaned = []
        header = self.middleware.config.context_header
        for message in messages:
            role = str(message.get("role", ""))
            content = str(message.get("content", ""))
            if role.lower() == "system" and header in content:
                before_header = content.split(header, 1)[0].strip()
                if before_header:
                    cleaned.append({**dict(message), "content": before_header})
                continue
            if role.lower() == "user" and content.startswith(header):
                user_marker = "\n\nUser request:\n"
                if user_marker in content:
                    cleaned.append({**dict(message), "content": content.split(user_marker, 1)[1]})
                continue
            cleaned.append(dict(message))
        return cleaned
