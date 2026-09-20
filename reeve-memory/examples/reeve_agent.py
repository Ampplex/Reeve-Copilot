"""Minimal ReeveAgent usage."""

from reeve import ReeveAgent, ReeveMemory


def echo_llm(messages):
    # Replace with any callable LLM, LangChain chat model, or SDK model wrapper.
    return f"I saw {len(messages)} message(s)."


# Ensure REEVE_API_KEY is set in your environment for hosted MCP usage.
agent = ReeveAgent(
    llm=echo_llm,
    memory=ReeveMemory(namespace="company_memory"),
)

response = agent.chat("What architecture did we choose for Reeve?")
print(response)

