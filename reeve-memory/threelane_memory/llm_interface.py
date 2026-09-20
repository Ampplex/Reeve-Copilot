"""LLM wrapper — supports Ollama, OpenAI, and Bedrock providers."""

from __future__ import annotations

from typing import Any, cast

from langchain_core.language_models.chat_models import BaseChatModel

from threelane_memory.config import (
    AWS_REGION,
    BEDROCK_API_KEY,
    BEDROCK_MODEL_ID,
    LLM_MAX_TOKENS,
    LLM_PROVIDER,
    LLM_TEMPERATURE,
    OLLAMA_BASE_URL,
    OLLAMA_CHAT_MODEL,
    OPENAI_API_KEY,
    OPENAI_CHAT_MODEL,
)

# ── Build the LLM client based on provider ───────────────────────────────────

client: BaseChatModel

if LLM_PROVIDER == "ollama":
    from langchain_ollama import ChatOllama

    client = ChatOllama(
        model=OLLAMA_CHAT_MODEL,
        base_url=OLLAMA_BASE_URL,
        temperature=LLM_TEMPERATURE,
    )
elif LLM_PROVIDER == "bedrock":
    from threelane_memory.bedrock_llm import ChatBedrockAPIKey

    if not BEDROCK_API_KEY:
        raise ValueError(
            "Bedrock API key not set. Add to .env:\n  BEDROCK_API_KEY=your-bedrock-api-key"
        )
    client = ChatBedrockAPIKey(
        api_key=BEDROCK_API_KEY,
        model_id=BEDROCK_MODEL_ID,
        region=AWS_REGION,
        temperature=LLM_TEMPERATURE,
        max_tokens=LLM_MAX_TOKENS,
    )
else:
    try:
        from langchain_openai import ChatOpenAI  # type: ignore[import-not-found]
    except ImportError:
        raise ImportError(
            "OpenAI chat dependencies not found. "
            "Install with: pip install 'threelane-memory[openai]'"
        )

    if not OPENAI_API_KEY:
        raise ValueError("OpenAI API key not set. Add to .env:\n  OPENAI_API_KEY=sk-your-api-key")
    client = ChatOpenAI(
        model=OPENAI_CHAT_MODEL,
        api_key=cast(Any, OPENAI_API_KEY),
        temperature=LLM_TEMPERATURE,
        model_kwargs={"max_tokens": LLM_MAX_TOKENS},
        streaming=False,
    )


def _normalize_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    return str(content)


def invoke_llm(prompt: str) -> str:
    """Send a single prompt to the LLM and return the raw text response."""
    return invoke_llm_tracked(prompt)["text"]


def invoke_llm_tracked(prompt: str) -> dict:
    """
    Like invoke_llm() but also returns token usage.
    Returns {"text": str, "tokens_in": int, "tokens_out": int}
    invoke_llm() is unchanged for all existing callers.
    """
    response = client.invoke([{"role": "user", "content": prompt}])
    text = _normalize_content(response.content)
    tokens_in = 0
    tokens_out = 0
    try:
        meta = response.response_metadata or {}
        provider = LLM_PROVIDER.lower()
        if provider == "ollama":
            tokens_in = int(meta.get("prompt_eval_count", 0))
            tokens_out = int(meta.get("eval_count", 0))
        elif provider == "bedrock":
            usage = meta.get("usage", {})
            tokens_in = int(usage.get("prompt_tokens", 0))
            tokens_out = int(usage.get("completion_tokens", 0))
        else:
            usage = meta.get("token_usage", {})
            tokens_in = int(usage.get("prompt_tokens", 0))
            tokens_out = int(usage.get("completion_tokens", 0))
    except Exception:
        pass
    # Feed the request-scoped accumulator so callers can report total real spend
    # (this is the single choke point for every generative LLM call).
    try:
        from threelane_memory.usage import add_usage

        add_usage(tokens_in, tokens_out)
    except Exception:
        pass
    return {"text": text, "tokens_in": tokens_in, "tokens_out": tokens_out}


def ask_llm(context: str, question: str) -> str:
    """Answer a question using the provided memory context."""
    prompt = f"Answer ONLY using the memory below.\n\nMemory:\n{context}\n\nQuestion:\n{question}\n"
    return invoke_llm(prompt)


def synthesize_llm(context: str, question: str, goal: str | None = None) -> str:
    """Reason over memory to answer a question, optionally with a goal context."""
    goal_line = f"Goal context: {goal}\n" if goal else ""
    prompt = (
        "You are a personal memory assistant with access to the user's stored memories. "
        "Your job is to reason over these memories — connecting people, events, feelings, "
        "facts, patterns, and experiences — to give a thoughtful, personalised answer.\n\n"
        "Do NOT just list facts. Synthesize them into a coherent response that actually "
        "addresses what the user is asking, drawing on whatever is relevant in memory.\n\n"
        "If the memory context is sparse, use what is available and acknowledge any gaps honestly.\n\n"
        f"{goal_line}"
        f"Memory Context:\n{context}\n\n"
        f"Question: {question}\n"
    )
    return invoke_llm(prompt)
