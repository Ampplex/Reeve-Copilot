"""Query Enhancer – analyzes user queries to improve retrieval and synthesis."""

from __future__ import annotations

import json
import logging
from typing import Any, Literal, TypedDict

from threelane_memory.llm_interface import invoke_llm

logger = logging.getLogger(__name__)

class EnhancedQuery(TypedDict):
    intent: Literal["fact_lookup", "synthesis", "recommendation", "storage"]
    goal_context: str | None
    sub_queries: list[str]
    expanded_query: str

VALID_INTENTS = {"fact_lookup", "synthesis", "recommendation", "storage"}

ENHANCER_PROMPT = """You are a query enhancer for a personal AI memory system. Your job is to analyze the user's raw query and make it retrieval-ready.

Given a raw query, you must:
1. Detect the intent — is it a "fact_lookup", "synthesis", "recommendation", or "storage"?
   - "storage": Use this for statements of fact, personal experiences, or information the user wants to remember.
   - "fact_lookup": A specific question that likely has a single answer in memory.
   - "synthesis": A complex question that requires connecting multiple memories.
   - "recommendation": A request for advice or suggestions based on stored history.

2. Decompose the query into sub-queries whenever it asks about more than one thing.
   - If intent is "synthesis" or "recommendation", break it into 3-5 atomic sub-queries that can individually be answered by a memory store.
   - If the query asks about two or more SEPARATE things in one sentence, however it is phrased ("X and Y", "what about A, B and C", "tell me about my P and my Q"), emit ONE sub-query per thing. This applies to every intent, including "fact_lookup".
   - Retrieval embeds each sub-query on its own. A query covering two subjects that is left unsplit becomes a single embedding, which retrieves one subject and silently loses the other, so splitting is what decides whether the second one is answerable at all.
3. Extract the goal context if one exists (e.g. "Amazon ML Summer School", "job application", "interview prep").
4. Expand the query with inferred entities that would be relevant to retrieve (e.g. if the user says "my application", infer "user's projects, skills, achievements, experience").

Respond ONLY in JSON. No preamble, no explanation, no markdown backticks.

Schema:
{{
  "intent": "fact_lookup" | "synthesis" | "recommendation" | "storage",
  "goal_context": "<extracted goal or null>",
  "sub_queries": ["<sub-query 1>", "<sub-query 2>", ...],
  "expanded_query": "<a single enriched version of the original query for fallback retrieval>"
}}

Rules:
- For a "fact_lookup" about a SINGLE thing, sub_queries is just one rephrased version of the original query.
- For a "fact_lookup" about SEVERAL things, sub_queries has one entry per thing. Never collapse them back into one.
- sub_queries should be phrased as if the memory system is being asked directly — short, specific, retrieval-friendly.
- Never add information that isn't implied by the query. Only expand, don't invent.
- goal_context should be null if no specific goal is present.

User query: "{raw_query}"
"""

def enhance_query(raw_query: str) -> EnhancedQuery:
    """Analyze the raw query and return a structured enhancement plan."""
    # Fast-path for common direct questions (e.g. "What phone do I use?", "Where is Jeff?")
    # These are almost always simple fact lookups.
    words = raw_query.strip().split()
    if len(words) <= 6:
        return {
            "intent": "fact_lookup",
            "goal_context": None,
            "sub_queries": [raw_query],
            "expanded_query": raw_query,
        }

    prompt = ENHANCER_PROMPT.format(raw_query=raw_query)
    response_text = invoke_llm(prompt)

    # Robust markdown fence stripping
    cleaned = response_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        cleaned = cleaned.rsplit("```", 1)[0].strip()

    try:
        parsed = json.loads(cleaned)
        
        # Validate intent
        intent = parsed.get("intent", "fact_lookup")
        if intent not in VALID_INTENTS:
            intent = "fact_lookup"
            
        return {
            "intent": intent, # type: ignore
            "goal_context": parsed.get("goal_context"),
            "sub_queries": parsed.get("sub_queries", [raw_query]),
            "expanded_query": parsed.get("expanded_query", raw_query),
        }
    except Exception as e:
        logger.warning(f"QueryEnhancer parse failed: {e} | raw: {cleaned[:200]}")
        # Fallback to basic factual lookup if parsing fails
        return {
            "intent": "fact_lookup",
            "goal_context": None,
            "sub_queries": [raw_query],
            "expanded_query": raw_query,
        }
