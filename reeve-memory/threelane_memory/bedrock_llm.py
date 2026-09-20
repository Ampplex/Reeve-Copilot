"""Custom LangChain chat model for AWS Bedrock via API key auth or IAM boto3."""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from typing import Any, cast

import boto3
from botocore.config import Config as BotoConfig
import requests  # type: ignore[import-untyped]
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.embeddings import Embeddings
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from threelane_memory.config import (
    BEDROCK_CHAT_TIMEOUT_SECONDS,
    BEDROCK_EMBED_TIMEOUT_SECONDS,
    BEDROCK_MAX_RETRIES,
    BEDROCK_MIN_REQUEST_INTERVAL_SECONDS,
    BEDROCK_RETRY_BASE_DELAY_SECONDS,
    BEDROCK_RETRY_MAX_DELAY_SECONDS,
)

logger = logging.getLogger("bedrock_llm")

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_DEFAULT_MAX_RETRIES = BEDROCK_MAX_RETRIES
_DEFAULT_BASE_DELAY = BEDROCK_RETRY_BASE_DELAY_SECONDS
_DEFAULT_MAX_DELAY = BEDROCK_RETRY_MAX_DELAY_SECONDS
_DEFAULT_MIN_REQUEST_INTERVAL = BEDROCK_MIN_REQUEST_INTERVAL_SECONDS

_request_lock = threading.Lock()
_last_request_started_at = 0.0


def _wait_for_request_slot(min_interval_seconds: float) -> None:
    global _last_request_started_at
    if min_interval_seconds <= 0:
        return

    with _request_lock:
        now = time.monotonic()
        elapsed = now - _last_request_started_at
        if elapsed < min_interval_seconds:
            time.sleep(min_interval_seconds - elapsed)
        _last_request_started_at = time.monotonic()


def _has_aws_credentials() -> bool:
    return bool(os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY"))


def _get_boto3_bedrock_client(region: str) -> Any:
    aws_access_key = os.getenv("AWS_ACCESS_KEY_ID")
    aws_secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
    config = BotoConfig(
        retries={"max_attempts": _DEFAULT_MAX_RETRIES, "mode": "adaptive"},
        connect_timeout=10,
        read_timeout=max(BEDROCK_CHAT_TIMEOUT_SECONDS, BEDROCK_EMBED_TIMEOUT_SECONDS),
    )
    if aws_access_key and aws_secret_key:
        return boto3.client(
            "bedrock-runtime",
            region_name=region,
            aws_access_key_id=aws_access_key,
            aws_secret_access_key=aws_secret_key,
            config=config,
        )
    return boto3.client("bedrock-runtime", region_name=region, config=config)


def _retry_after_seconds(response: requests.Response | None) -> float | None:
    if response is None:
        return None
    value = response.headers.get("retry-after") or response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _compute_backoff(
    attempt: int,
    base_delay: float,
    max_delay: float,
    response: requests.Response | None,
) -> float:
    retry_after = _retry_after_seconds(response)
    if retry_after is not None and retry_after > 0:
        return min(retry_after, max_delay)
    delay = min(max_delay, base_delay * (2**attempt))
    jitter = random.uniform(0, min(1.0, delay * 0.1))
    return delay + jitter


def _response_error_detail(response: requests.Response) -> str:
    request_id = (
        response.headers.get("x-amzn-requestid")
        or response.headers.get("x-amzn-RequestId")
        or response.headers.get("x-amz-request-id")
    )
    parts = [f"status={response.status_code}"]
    if request_id:
        parts.append(f"request_id={request_id}")

    body = response.text.strip()
    if body:
        if len(body) > 1000:
            body = f"{body[:1000]}..."
        parts.append(f"body={body}")
    return ", ".join(parts)


def _raise_for_status_with_detail(response: requests.Response) -> None:
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise requests.HTTPError(
            f"{exc}; {_response_error_detail(response)}",
            response=response,
        ) from exc


def _post_with_backoff(
    url: str,
    *,
    headers: dict[str, str | bytes],
    json: dict[str, Any],
    timeout: float,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    base_delay: float = _DEFAULT_BASE_DELAY,
    max_delay: float = _DEFAULT_MAX_DELAY,
    min_request_interval_seconds: float = _DEFAULT_MIN_REQUEST_INTERVAL,
) -> requests.Response:
    attempt = 0
    while True:
        response: requests.Response | None = None
        try:
            _wait_for_request_slot(min_request_interval_seconds)
            response = requests.post(url, headers=headers, json=json, timeout=timeout)
        except requests.RequestException as exc:
            if attempt >= max_retries:
                raise
            delay = _compute_backoff(attempt, base_delay, max_delay, None)
            log_message = (
                "Bedrock request error (%s). Retry %s/%s in %.1fs"
                if attempt == 0
                else "Bedrock request still erroring (%s). Retry %s/%s in %.1fs"
            )
            log_level = logging.WARNING if attempt == 0 else logging.DEBUG
            logger.log(
                log_level,
                log_message,
                type(exc).__name__,
                attempt + 1,
                max_retries,
                delay,
            )
            time.sleep(delay)
            attempt += 1
            continue

        if response.status_code in _RETRYABLE_STATUS:
            if attempt >= max_retries:
                _raise_for_status_with_detail(response)
            delay = _compute_backoff(attempt, base_delay, max_delay, response)
            log_message = (
                "Bedrock request throttled (%s). Retry %s/%s in %.1fs"
                if attempt == 0
                else "Bedrock request still throttled (%s). Retry %s/%s in %.1fs"
            )
            log_level = logging.WARNING if attempt == 0 else logging.DEBUG
            logger.log(
                log_level,
                log_message,
                response.status_code,
                attempt + 1,
                max_retries,
                delay,
            )
            time.sleep(delay)
            attempt += 1
            continue

        _raise_for_status_with_detail(response)
        return response


class BedrockEmbeddingsAPIKey(Embeddings):
    """LangChain embeddings model that calls AWS Bedrock via boto3 IAM or HTTP API key."""

    def __init__(
        self,
        api_key: str,
        model_id: str,
        region: str = "us-west-2",
    ):
        self.api_key = api_key
        self.model_id = model_id
        self.region = region

    @property
    def _endpoint(self) -> str:
        return f"https://bedrock-runtime.{self.region}.amazonaws.com/model/{self.model_id}/invoke"

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of documents."""
        return [self.embed_query(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query."""
        if _has_aws_credentials():
            _wait_for_request_slot(_DEFAULT_MIN_REQUEST_INTERVAL)
            client = _get_boto3_bedrock_client(self.region)
            body = json.dumps({
                "inputText": text,
                "dimensions": 1024,
                "normalize": True,
            })
            resp = client.invoke_model(
                modelId=self.model_id,
                body=body,
                contentType="application/json",
                accept="application/json",
            )
            data = json.loads(resp["body"].read())
            try:
                from threelane_memory.usage import add_usage

                token_count = data.get("inputTextTokenCount")
                if token_count is not None:
                    add_usage(tokens_in=int(token_count))
                    self._reeve_tokens_recorded = True
            except Exception:
                pass
            return cast(list[float], data["embedding"])

        headers: dict[str, str | bytes] = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        body_dict: dict[str, Any] = {
            "inputText": text,
            "dimensions": 1024,
            "normalize": True,
        }

        response = _post_with_backoff(
            self._endpoint,
            headers=headers,
            json=body_dict,
            timeout=BEDROCK_EMBED_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()

        try:
            from threelane_memory.usage import add_usage

            token_count = data.get("inputTextTokenCount")
            if token_count is None and isinstance(data.get("usage"), dict):
                token_count = data["usage"].get("prompt_tokens")
            if token_count is not None:
                add_usage(tokens_in=int(token_count))
                self._reeve_tokens_recorded = True
        except Exception:
            pass

        if "data" in data and isinstance(data["data"], list):
            return cast(list[float], data["data"][0]["embedding"])
        if "embedding" in data:
            return cast(list[float], data["embedding"])

        raise RuntimeError(f"Unexpected response format from Bedrock proxy: {data}")


class ChatBedrockAPIKey(BaseChatModel):
    """LangChain chat model that calls AWS Bedrock via boto3 IAM or HTTP API key."""

    api_key: str
    model_id: str
    region: str = "us-west-2"
    temperature: float = 0.7
    max_tokens: int = 4096

    @property
    def _llm_type(self) -> str:
        return "bedrock-api-key"

    @property
    def _endpoint(self) -> str:
        return f"https://bedrock-runtime.{self.region}.amazonaws.com/model/{self.model_id}/converse"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "region": self.region,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

    def _message_text(self, content: object) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                part.get("text", "") if isinstance(part, dict) else str(part) for part in content
            )
        return str(content)

    def _convert_messages(
        self, messages: list[BaseMessage]
    ) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
        """Convert LangChain messages to Bedrock Converse payload parts."""
        system: list[dict[str, str]] = []
        result: list[dict[str, Any]] = []
        for msg in messages:
            if isinstance(msg, SystemMessage):
                system.append({"text": self._message_text(msg.content)})
                continue
            elif isinstance(msg, HumanMessage):
                role = "user"
            elif isinstance(msg, AIMessage):
                role = "assistant"
            else:
                role = "user"
            result.append({"role": role, "content": [{"text": self._message_text(msg.content)}]})
        return system, result

    def _extract_content(self, data: dict[str, Any]) -> str:
        """Normalize completion content to a plain string."""
        content = data["choices"][0]["message"]["content"]
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part) for part in content
            )
        else:
            text = str(content)

        if text.startswith("</s>"):
            text = text[4:].lstrip()
        return text

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        system, converted = self._convert_messages(messages)

        if _has_aws_credentials():
            _wait_for_request_slot(_DEFAULT_MIN_REQUEST_INTERVAL)
            client = _get_boto3_bedrock_client(self.region)
            converse_kwargs: dict[str, Any] = {
                "modelId": self.model_id,
                "messages": converted,
                "inferenceConfig": {
                    "maxTokens": kwargs.get("max_tokens", self.max_tokens),
                    "temperature": kwargs.get("temperature", self.temperature),
                },
            }
            if system:
                converse_kwargs["system"] = system
            if stop:
                converse_kwargs["inferenceConfig"]["stopSequences"] = stop

            data = client.converse(**converse_kwargs)

            try:
                content_blocks = data["output"]["message"]["content"]
                content = "".join(
                    block.get("text", "") if isinstance(block, dict) else str(block)
                    for block in content_blocks
                )
                raw_usage = data.get("usage", {})
                usage = {
                    "prompt_tokens": int(raw_usage.get("inputTokens", 0) or 0),
                    "completion_tokens": int(raw_usage.get("outputTokens", 0) or 0),
                    "total_tokens": int(raw_usage.get("totalTokens", 0) or 0),
                }
                finish_reason = data.get("stopReason")
            except (KeyError, IndexError, TypeError, ValueError):
                content = str(data)
                usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                finish_reason = None

            logger.debug("Bedrock completion received via boto3 for model %s", self.model_id)

            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(content=content),
                        generation_info={
                            "finish_reason": finish_reason,
                            "usage": usage,
                        },
                    )
                ],
                llm_output={
                    "model": data.get("model", self.model_id),
                    "token_usage": usage,
                },
            )

        headers: dict[str, str | bytes] = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        body: dict[str, Any] = {
            "messages": converted,
            "inferenceConfig": {
                "maxTokens": kwargs.get("max_tokens", self.max_tokens),
                "temperature": kwargs.get("temperature", self.temperature),
            },
        }
        if system:
            body["system"] = system

        if stop:
            body["inferenceConfig"]["stopSequences"] = stop

        response = _post_with_backoff(
            self._endpoint,
            headers=headers,
            json=body,
            timeout=BEDROCK_CHAT_TIMEOUT_SECONDS,
        )
        data = response.json()

        try:
            content_blocks = data["output"]["message"]["content"]
            content = "".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content_blocks
            )
            raw_usage = data.get("usage", {})
            usage = {
                "prompt_tokens": int(raw_usage.get("inputTokens", 0) or 0),
                "completion_tokens": int(raw_usage.get("outputTokens", 0) or 0),
                "total_tokens": int(raw_usage.get("totalTokens", 0) or 0),
            }
            finish_reason = data.get("stopReason")
        except (KeyError, IndexError, TypeError, ValueError):
            content = str(data)
            usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            finish_reason = None

        logger.debug("Bedrock completion received for model %s", self.model_id)

        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(content=content),
                    generation_info={
                        "finish_reason": finish_reason,
                        "usage": usage,
                    },
                )
            ],
            llm_output={
                "model": data.get("model", self.model_id),
                "token_usage": usage,
            },
        )
