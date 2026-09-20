"""Framework-agnostic Reeve middleware for persistent agent memory.

The middleware is intentionally client-side and small: it decides when memory is
useful, retrieves context from Reeve, injects that context into a chat request,
and stores durable knowledge after the model responds. Graph extraction,
relationship writing, entity resolution, and deduplication remain server-side.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

InjectMode = Literal["system", "prepend", "custom"]
MessageLike = Mapping[str, Any]
CustomInjector = Callable[[list[dict[str, Any]], str], list[dict[str, Any]]]


class ChatMessage(BaseModel):
    """Portable chat message used by Reeve middleware and examples."""

    model_config = ConfigDict(extra="allow")

    role: str
    content: Any

    def to_dict(self) -> dict[str, Any]:
        return dict(self.model_dump(mode="python"))


class RetrievalDecision(BaseModel):
    """Decision from the retrieval relevance policy."""

    should_retrieve: bool
    reason: str
    confidence: float = Field(ge=0.0, le=1.0)


class MemoryRelationship(BaseModel):
    """A durable subject-predicate-object relationship found in an interaction."""

    subject: str
    predicate: str
    object: str
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)


class MemoryCandidate(BaseModel):
    """Potential memory extracted from a user/assistant interaction."""

    text: str
    importance: float = Field(ge=0.0, le=1.0)
    kind: str = "fact"
    entities: list[str] = Field(default_factory=list)
    relationships: list[MemoryRelationship] = Field(default_factory=list)
    source: Literal["user", "assistant", "conversation"] = "conversation"

    @field_validator("text")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return value.strip()


class MemoryInjection(BaseModel):
    """Result of a before-call memory retrieval/injection pass."""

    messages: list[dict[str, Any]]
    query: str
    context: str = ""
    decision: RetrievalDecision
    injected: bool = False


class MemoryWriteResult(BaseModel):
    """Result of an after-call memory extraction/storage pass."""

    candidates: list[MemoryCandidate] = Field(default_factory=list)
    stored: list[dict[str, Any]] = Field(default_factory=list)
    skipped: list[MemoryCandidate] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    deduplicated: bool = False
    deduplication_result: dict[str, Any] | None = None


class ReeveMiddlewareConfig(BaseModel):
    """Configuration for :class:`ReeveMiddleware`."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    namespace: str = "default"
    inject_mode: InjectMode = "system"
    importance_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    deduplicate: bool = False
    retrieval_enabled: bool = True
    storage_enabled: bool = True
    max_context_chars: int = Field(default=6000, ge=256)
    context_header: str = "Relevant Reeve Memory"
    custom_injector: CustomInjector | None = None


@runtime_checkable
class MemoryBackend(Protocol):
    """Minimal backend ReeveMiddleware needs.

    Production use delegates to the remote Reeve/MCP server. Tests and custom
    deployments can supply this protocol directly.
    """

    def retrieve(self, query: str, *, namespace: str) -> Any:
        """Return Reeve memory context for a user query."""

    def store(self, text: str, *, namespace: str) -> dict[str, Any]:
        """Persist a durable memory."""

    def deduplicate(self) -> dict[str, Any]:
        """Run entity deduplication for the graph."""

    def clear(self, *, namespace: str, dry_run: bool = False) -> dict[str, Any]:
        """Permanently delete all memory for a namespace (hard reset)."""


class ReeveMemoryBackend:
    """Default backend using the public Reeve client tools."""

    def __init__(self, *, base_url: str | None = None, api_key: str | None = None) -> None:
        if base_url is not None:
            from reeve.tools import _resolve_hosted_base_url

            _resolve_hosted_base_url(base_url)
        self.base_url = base_url
        self.api_key = api_key
        self._client = None

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if self.base_url is not None or self.api_key is not None:
            if self._client is None:
                from reeve.tools import ReeveClient

                self._client = ReeveClient(base_url=self.base_url, api_key=self.api_key)
            return self._client.call_tool(name, arguments)

        from reeve.tools import _get_client

        return _get_client().call_tool(name, arguments)

    def retrieve(self, query: str, *, namespace: str) -> Any:
        return self._call_tool(
            "retrieve_memory_context",
            {"question": query, "speaker": namespace},
        )

    def store(self, text: str, *, namespace: str) -> dict[str, Any]:
        result = self._call_tool("store_memory", {"text": text, "speaker": namespace})
        return result if isinstance(result, dict) else {"stored": bool(result), "result": result}

    def deduplicate(self) -> dict[str, Any]:
        result = self._call_tool("deduplicate_memory_entities", {"dry_run": False})
        return result if isinstance(result, dict) else {"result": result}

    def clear(self, *, namespace: str, dry_run: bool = False) -> dict[str, Any]:
        result = self._call_tool("clear_memory", {"speaker": namespace, "dry_run": dry_run})
        return result if isinstance(result, dict) else {"result": result}


class MemoryRelevancePolicy:
    """Heuristic retrieval policy that avoids wasting memory calls on trivia."""

    _greeting_re = re.compile(
        r"^\s*(hi|hello|hey|yo|good\s+(morning|afternoon|evening)|namaste)[!. ]*\s*$",
        re.IGNORECASE,
    )
    _smalltalk_re = re.compile(
        r"\b(how are you|what'?s up|thank you|thanks|nice to meet you|goodbye|bye)\b",
        re.IGNORECASE,
    )
    _calculation_re = re.compile(
        r"^\s*(what\s+is|calculate|compute|solve)?\s*[-+*/().\d\s]+=?\s*\??\s*$",
        re.IGNORECASE,
    )
    _generic_fact_re = re.compile(
        r"^\s*(what is|who is|define|explain|when is|where is|how does|how many)\b",
        re.IGNORECASE,
    )
    _question_like_re = re.compile(
        r"^\s*(what|who|when|where|why|how|which|do|did|does|can|could|should|would|is|are)\b",
        re.IGNORECASE,
    )
    _storage_directive_re = re.compile(
        r"^\s*(please\s+)?(store|save|note)\b|"
        r"^\s*(please\s+)?remember\s+(this|that|the following|to)\b|"
        r"\b(remember|store|save|note)\s+(this|that|the following)\b",
        re.IGNORECASE,
    )
    _durable_statement_re = re.compile(
        r"\b(i|we|our team|my team)\s+"
        r"(prefer|like|love|hate|use|always|usually|avoid)\b|"
        r"\b(i|we|our team|my team)\s+"
        r"(decided|chose|selected|agreed|switched|standardized|deprecated|migrated)\b|"
        r"\b(my|our|team|project)\s+"
        r"(goal|objective|task|todo|deadline|milestone|plan|roadmap)\b",
        re.IGNORECASE,
    )
    _memory_cue_re = re.compile(
        r"\b("
        r"remember|recall|previously|last time|earlier|again|continue|"
        r"did\s+(we|i)|have\s+(we|i)|what\s+did\s+(we|i)|"
        r"my|our|we|i|me|us|mine|ours|"
        r"preference|prefer|goal|task|todo|decision|decided|"
        r"choose|chose|selected|architecture|migration|roadmap|"
        r"project|product|startup|company|team|client|customer|"
        r"repo|codebase|design|spec|plan|milestone"
        r")\b",
        re.IGNORECASE,
    )

    def decide(self, query: str) -> RetrievalDecision:
        text = query.strip()
        if not text:
            return RetrievalDecision(
                should_retrieve=False,
                reason="empty input",
                confidence=1.0,
            )
        if self._greeting_re.match(text):
            return RetrievalDecision(
                should_retrieve=False,
                reason="greeting",
                confidence=0.98,
            )
        if self._smalltalk_re.search(text) and len(text.split()) <= 8:
            return RetrievalDecision(
                should_retrieve=False,
                reason="small talk",
                confidence=0.94,
            )
        if self._calculation_re.match(text) and re.search(r"\d", text):
            return RetrievalDecision(
                should_retrieve=False,
                reason="simple calculation",
                confidence=0.95,
            )
        if self._storage_directive_re.search(text):
            return RetrievalDecision(
                should_retrieve=False,
                reason="memory storage directive",
                confidence=0.9,
            )
        is_question_like = text.endswith("?") or bool(self._question_like_re.match(text))
        if not is_question_like and self._durable_statement_re.search(text):
            return RetrievalDecision(
                should_retrieve=False,
                reason="durable memory statement",
                confidence=0.86,
            )
        if self._memory_cue_re.search(text):
            return RetrievalDecision(
                should_retrieve=True,
                reason="contains personal, team, project, or decision cue",
                confidence=0.84,
            )
        if self._generic_fact_re.match(text):
            return RetrievalDecision(
                should_retrieve=False,
                reason="generic factual question",
                confidence=0.82,
            )
        if text.endswith("?"):
            return RetrievalDecision(
                should_retrieve=True,
                reason="question without generic-fact pattern",
                confidence=0.58,
            )
        return RetrievalDecision(
            should_retrieve=False,
            reason="no retrieval cue",
            confidence=0.67,
        )


class DurableMemoryExtractor:
    """Extract durable memory candidates without storing conversational filler."""

    _ignore_re = re.compile(
        r"^\s*(hi|hello|hey|thanks|thank you|ok|okay|cool|great|nice|bye)[!. ]*\s*$",
        re.IGNORECASE,
    )
    _preference_re = re.compile(
        r"\b(i|we|our team|my team)\s+"
        r"(prefer|like|love|hate|use|always|usually|want|need|avoid)\b"
        # Possessive phrasing — "my favorite editor is …" — is the most common
        # way users state preferences and must score as one.
        r"|\b(my|our)\s+(favorite|favourite|preferred|go-to)\b",
        re.IGNORECASE,
    )
    _goal_task_re = re.compile(
        r"\b(goal|objective|task|todo|deadline|milestone|plan|roadmap|next step)\b",
        re.IGNORECASE,
    )
    _decision_re = re.compile(
        r"\b("
        r"decided|decision|chose|choose|selected|agreed|architecture|"
        r"migrated|migration|switched|standardized|deprecated|uses|built on|"
        r"powered by|integrates with"
        r")\b",
        re.IGNORECASE,
    )
    _lesson_re = re.compile(
        r"\b(learned|lesson|root cause|postmortem|takeaway|blocked by|unblocked by)\b",
        re.IGNORECASE,
    )
    _relationship_re = re.compile(
        r"\b(?P<subject>[A-Z][A-Za-z0-9_.-]*(?:\s+[A-Z][A-Za-z0-9_.-]*){0,3})\s+"
        # First-person forms ("I work at …") matter as much as third-person.
        r"(?P<predicate>uses|leads|owns|manages|reports to|works? at|works? for|"
        r"depends on|"
        r"integrates with|built on|migrated from|migrated to|replaced|replaces)\s+"
        r"(?P<object>[A-Z][A-Za-z0-9_.-]*(?:\s+[A-Z][A-Za-z0-9_.-]*){0,3})\b"
    )
    _entity_re = re.compile(r"\b[A-Z][A-Za-z0-9_.-]*(?:\s+[A-Z][A-Za-z0-9_.-]*){0,3}\b")

    def extract(
        self,
        user_input: str,
        assistant_output: str = "",
        *,
        messages: Sequence[MessageLike] | None = None,
    ) -> list[MemoryCandidate]:
        del messages
        candidates: list[MemoryCandidate] = []
        candidates.extend(self._extract_from_text(user_input, source="user"))
        candidates.extend(self._extract_from_text(assistant_output, source="assistant"))
        return self._dedupe_candidates(candidates)

    def _extract_from_text(
        self,
        text: str,
        *,
        source: Literal["user", "assistant"],
    ) -> list[MemoryCandidate]:
        clean = text.strip()
        if not clean or self._ignore_re.match(clean):
            return []

        candidates = []
        for sentence in self._sentences(clean):
            if self._ignore_re.match(sentence):
                continue
            importance, kind = self._score(sentence, source=source)
            if importance <= 0:
                continue
            relationships = self._relationships(sentence)
            entities = self._entities(sentence)
            candidates.append(
                MemoryCandidate(
                    text=sentence,
                    importance=importance,
                    kind=kind,
                    entities=entities,
                    relationships=relationships,
                    source=source,
                )
            )
        return candidates

    def _score(self, sentence: str, *, source: Literal["user", "assistant"]) -> tuple[float, str]:
        if len(sentence.split()) < 3 and not self._relationship_re.search(sentence):
            return 0.0, "filler"
        if "?" in sentence and not re.search(r"\b(remember|store|note)\b", sentence, re.IGNORECASE):
            return 0.0, "question"

        score = 0.42 if source == "assistant" else 0.52
        kind = "fact"

        if self._preference_re.search(sentence):
            score += 0.26
            kind = "preference"
        if self._goal_task_re.search(sentence):
            score += 0.24
            kind = "task"
        if self._decision_re.search(sentence):
            score += 0.28
            kind = "decision"
        if self._lesson_re.search(sentence):
            score += 0.22
            kind = "lesson"
        if self._relationship_re.search(sentence):
            score += 0.18
            kind = "relationship" if kind == "fact" else kind

        if source == "assistant" and kind == "fact":
            return 0.0, "assistant-filler"

        return min(round(score, 2), 1.0), kind

    def _relationships(self, sentence: str) -> list[MemoryRelationship]:
        relationships = []
        for match in self._relationship_re.finditer(sentence):
            relationships.append(
                MemoryRelationship(
                    subject=match.group("subject").strip(),
                    predicate=match.group("predicate").strip(),
                    object=match.group("object").strip(),
                    confidence=0.82,
                )
            )
        return relationships

    def _entities(self, sentence: str) -> list[str]:
        ignored = {"I", "We", "Our", "The", "A", "An"}
        entities = []
        for match in self._entity_re.finditer(sentence):
            value = match.group(0).strip()
            if value not in ignored and value not in entities:
                entities.append(value)
        return entities

    def _sentences(self, text: str) -> list[str]:
        parts = re.split(r"(?<=[.!?])\s+|\n+", text)
        return [part.strip(" -\t") for part in parts if part.strip(" -\t")]

    def _dedupe_candidates(self, candidates: list[MemoryCandidate]) -> list[MemoryCandidate]:
        seen: set[str] = set()
        unique = []
        for candidate in candidates:
            key = candidate.text.casefold()
            if key in seen:
                continue
            seen.add(key)
            unique.append(candidate)
        return unique


class ReeveMiddleware:
    """Persistent-memory middleware for agents and chat pipelines.

    The class has explicit `before_call` and `after_call` hooks so it can be
    adapted to most agent frameworks without making Reeve depend on them.
    """

    def __init__(
        self,
        *,
        namespace: str = "default",
        inject_mode: InjectMode = "system",
        importance_threshold: float = 0.7,
        deduplicate: bool = False,
        retrieval_enabled: bool = True,
        storage_enabled: bool = True,
        max_context_chars: int = 6000,
        context_header: str = "Relevant Reeve Memory",
        custom_injector: CustomInjector | None = None,
        backend: MemoryBackend | None = None,
        relevance_policy: MemoryRelevancePolicy | None = None,
        extractor: DurableMemoryExtractor | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.config = ReeveMiddlewareConfig(
            namespace=namespace,
            inject_mode=inject_mode,
            importance_threshold=importance_threshold,
            deduplicate=deduplicate,
            retrieval_enabled=retrieval_enabled,
            storage_enabled=storage_enabled,
            max_context_chars=max_context_chars,
            context_header=context_header,
            custom_injector=custom_injector,
        )
        if self.config.inject_mode == "custom" and custom_injector is None:
            raise ValueError("custom_injector is required when inject_mode='custom'")

        self.backend = backend or ReeveMemoryBackend(base_url=base_url, api_key=api_key)
        self.relevance_policy = relevance_policy or MemoryRelevancePolicy()
        self.extractor = extractor or DurableMemoryExtractor()

    @property
    def namespace(self) -> str:
        return self.config.namespace

    def should_retrieve(self, query: str) -> RetrievalDecision:
        if not self.config.retrieval_enabled:
            return RetrievalDecision(
                should_retrieve=False,
                reason="retrieval disabled",
                confidence=1.0,
            )
        return self.relevance_policy.decide(query)

    def retrieve_memory(self, query: str) -> str:
        raw_context = self.backend.retrieve(query, namespace=self.namespace)
        return self.format_memory_context(raw_context)

    def clear(self, *, dry_run: bool = False) -> dict[str, Any]:
        """Permanently delete all memory in this instance's namespace (hard reset).

        Destructive and irreversible. On the hosted server the namespace is
        composed under the caller's account (``uid:namespace``), so this only ever
        clears this instance's own namespace — never another account's. Pass
        ``dry_run=True`` to preview the counts without deleting.
        """
        return self.backend.clear(namespace=self.namespace, dry_run=dry_run)

    def format_memory_context(self, memory: Any) -> str:
        if memory is None:
            return ""
        if isinstance(memory, str):
            body = memory.strip()
        elif isinstance(memory, Mapping):
            body = self._format_mapping(memory)
        elif isinstance(memory, Sequence) and not isinstance(memory, (bytes, bytearray)):
            body = "\n".join(self.format_memory_context(item) for item in memory)
        else:
            body = str(memory).strip()

        if not body:
            return ""
        if len(body) > self.config.max_context_chars:
            body = body[: self.config.max_context_chars].rstrip() + "\n[truncated]"
        if body.startswith(self.config.context_header):
            return body
        return f"{self.config.context_header}:\n\n{body}"

    def before_call(
        self,
        user_input: str | Sequence[MessageLike] | None = None,
        *,
        messages: Sequence[MessageLike] | None = None,
    ) -> MemoryInjection:
        # If user_input is a list, treat it as messages
        if isinstance(user_input, (list, tuple)):
            messages = list(user_input)
            user_input = None

        normalized = self._normalize_messages(messages, user_input=user_input)
        query = (user_input or self._last_user_message(normalized) or "")
        # Ensure query is a string before stripping
        if not isinstance(query, str):
            query = str(query)
        query = query.strip()

        decision = self.should_retrieve(query)

        context = ""
        if decision.should_retrieve:
            try:
                context = self.retrieve_memory(query)
            except Exception:
                # If memory retrieval fails (e.g. timeout, connection error),
                # we proceed without memory context to ensure agent availability.
                context = ""

        injected_messages = self.inject_memory(normalized, context) if context else normalized
        return MemoryInjection(
            messages=injected_messages,
            query=query,
            context=context,
            decision=decision,
            injected=bool(context),
        )

    def inject_memory(self, messages: Sequence[MessageLike], context: str) -> list[dict[str, Any]]:
        normalized = self._normalize_messages(messages)
        if not context.strip():
            return normalized

        if self.config.inject_mode == "custom":
            assert self.config.custom_injector is not None
            return self.config.custom_injector(normalized, context)
        if self.config.inject_mode == "prepend":
            return self._inject_prepend(normalized, context)
        return self._inject_system(normalized, context)

    def extract_memories(
        self,
        user_input: str,
        assistant_output: str = "",
        *,
        messages: Sequence[MessageLike] | None = None,
    ) -> list[MemoryCandidate]:
        return self.extractor.extract(user_input, assistant_output, messages=messages)

    def after_call(
        self,
        user_input: str,
        assistant_output: str = "",
        *,
        messages: Sequence[MessageLike] | None = None,
    ) -> MemoryWriteResult:
        result = MemoryWriteResult()
        if not self.config.storage_enabled:
            return result

        candidates = self.extract_memories(user_input, assistant_output, messages=messages)
        result.candidates = candidates

        for candidate in candidates:
            if candidate.importance < self.config.importance_threshold:
                result.skipped.append(candidate)
                continue
            try:
                stored = self.backend.store(candidate.text, namespace=self.namespace)
                result.stored.append(stored)
            except Exception as exc:
                result.errors.append(str(exc))

        if result.stored and self.config.deduplicate:
            try:
                result.deduplication_result = self.backend.deduplicate()
                result.deduplicated = True
            except Exception as exc:
                result.errors.append(str(exc))

        return result

    def wrap_call(
        self,
        call: Callable[..., Any],
        user_input: str | None = None,
        *,
        messages: Sequence[MessageLike] | None = None,
        **kwargs: Any,
    ) -> Any:
        injection = self.before_call(user_input, messages=messages)
        response = call(injection.messages, **kwargs)
        response_text = normalize_response_text(response)
        self.after_call(injection.query, response_text, messages=injection.messages)
        return response

    def _inject_system(self, messages: list[dict[str, Any]], context: str) -> list[dict[str, Any]]:
        injected = [dict(message) for message in messages]
        for message in injected:
            if str(message.get("role", "")).lower() == "system":
                current = str(message.get("content", "")).strip()
                message["content"] = f"{current}\n\n{context}" if current else context
                return injected
        return [{"role": "system", "content": context}, *injected]

    def _inject_prepend(self, messages: list[dict[str, Any]], context: str) -> list[dict[str, Any]]:
        injected = [dict(message) for message in messages]
        for index in range(len(injected) - 1, -1, -1):
            if str(injected[index].get("role", "")).lower() == "user":
                current = str(injected[index].get("content", "")).strip()
                injected[index]["content"] = f"{context}\n\nUser request:\n{current}"
                return injected
        return [{"role": "user", "content": context}, *injected]

    def _normalize_messages(
        self,
        messages: Sequence[MessageLike] | None = None,
        *,
        user_input: str | None = None,
    ) -> list[dict[str, Any]]:
        normalized = [coerce_message(message) for message in messages or []]
        if user_input is not None and not self._last_user_message(normalized):
            normalized.append({"role": "user", "content": user_input})
        return normalized

    def _last_user_message(self, messages: Sequence[MessageLike]) -> str | None:
        for message in reversed(messages):
            if str(message.get("role", "")).lower() == "user":
                return str(message.get("content", ""))
        return None

    def _format_mapping(self, value: Mapping[Any, Any], *, indent: int = 0) -> str:
        lines: list[str] = []
        prefix = " " * indent
        for raw_key, raw_val in value.items():
            key = str(raw_key).replace("_", " ").strip().title()
            if isinstance(raw_val, Mapping):
                lines.append(f"{prefix}{key}:")
                lines.append(self._format_mapping(raw_val, indent=indent + 2))
            elif isinstance(raw_val, Sequence) and not isinstance(raw_val, (str, bytes, bytearray)):
                lines.append(f"{prefix}{key}:")
                for item in raw_val:
                    if isinstance(item, Mapping):
                        lines.append(self._format_mapping(item, indent=indent + 2))
                    else:
                        lines.append(f"{prefix}  - {item}")
            else:
                lines.append(f"{prefix}{key}: {raw_val}")
        return "\n".join(line for line in lines if line.strip())


class ReeveMemory(ReeveMiddleware):
    """Preferred memory layer API (alias of ReeveMiddleware)."""


class AgentMemory(ReeveMiddleware):
    """Alias of ReeveMiddleware for discoverability."""


def normalize_response_text(response: Any) -> str:
    """Best-effort extraction of text from common LLM response shapes."""

    if response is None:
        return ""
    if isinstance(response, str):
        return response
    if isinstance(response, Mapping):
        if "content" in response:
            return str(response["content"])
        choices = response.get("choices")
        if isinstance(choices, Sequence) and choices:
            first = choices[0]
            if isinstance(first, Mapping):
                message = first.get("message")
                if isinstance(message, Mapping) and "content" in message:
                    return str(message["content"])
                if "text" in first:
                    return str(first["text"])
        return str(response)

    content = getattr(response, "content", None)
    if content is not None:
        if isinstance(content, list):
            return "".join(
                part.get("text", "") if isinstance(part, Mapping) else str(part)
                for part in content
            )
        return str(content)

    choices = getattr(response, "choices", None)
    if choices:
        first = choices[0]
        message = getattr(first, "message", None)
        if message is not None and getattr(message, "content", None) is not None:
            return str(message.content)
        if getattr(first, "text", None) is not None:
            return str(first.text)

    return str(response)


def coerce_message(message: Any) -> dict[str, Any]:
    """Convert common framework message shapes into OpenAI-style dicts."""

    if isinstance(message, Mapping):
        return ChatMessage(**dict(message)).to_dict()

    if isinstance(message, tuple) and len(message) == 2:
        role, content = message
        return {"role": _normalize_role(str(role)), "content": content}

    role = getattr(message, "role", None) or getattr(message, "type", None)
    content = getattr(message, "content", None)
    if role is not None and content is not None:
        return {"role": _normalize_role(str(role)), "content": content}

    raise TypeError(f"Unsupported message shape: {type(message)!r}")


def _normalize_role(role: str) -> str:
    role_map = {
        "human": "user",
        "ai": "assistant",
        "bot": "assistant",
    }
    return role_map.get(role.lower(), role)
