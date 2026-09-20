# Reeve

**Add persistent memory to any AI agent in one line.**

```python
from reeve import ReeveAgent

agent = ReeveAgent(llm=llm)

agent.chat("I prefer Python over JavaScript")
print(agent.chat("What programming language do I prefer?"))
```

```text
Python
```

No manual retrieval. No manual storage. No prompt engineering.

Reeve automatically retrieves, updates, and shares memory across AI agents.

---

## Why Reeve?

Most AI applications rely on:

* Chat history
* Vector databases
* Custom memory prompts
* Hand-written retrieval pipelines

As applications grow, these approaches become difficult to maintain and often fail to preserve long-term context.

Reeve provides a persistent memory layer that:

* Stores durable knowledge instead of raw conversation logs
* Automatically retrieves relevant memory before model calls
* Extracts and stores new knowledge after conversations
* Shares memory across agents, frameworks, and models
* Works with existing AI stacks without changing application logic

---

## Quick Start

Install Reeve:

```bash
pip install reeve
```

### One-Line Agent Memory

```python
from reeve import ReeveAgent

agent = ReeveAgent(llm=llm)

agent.chat("Our startup uses Memgraph")

print(
    agent.chat("What database do we use?")
)
```

```text
Memgraph
```

Memory retrieval, storage, and updates happen automatically.

---

## Which API Should I Use?

### I want memory in 30 seconds

```python
from reeve import ReeveAgent

agent = ReeveAgent(llm=llm)
```

### I already use LangGraph, LangChain, CrewAI, Agno, or another framework

```python
from reeve import ReeveMemory

agent = Agent(
    middleware=[
        ReeveMemory()
    ]
)
```

### I want complete control

```python
from reeve.tools import (
    store_memory,
    query_memory
)
```

---

## How Reeve Works

```text
User Message
    ↓
Retrieve Memory
    ↓
Inject Context
    ↓
LLM
    ↓
Extract Knowledge
    ↓
Store Memory
```

Reeve handles the memory lifecycle automatically so developers can focus on building agents instead of memory infrastructure.

---

## Features

* ReeveAgent for one-line memory integration
* ReeveMemory middleware for existing agent frameworks
* Shared memory across agents and models
* Automatic memory retrieval and injection
* Durable knowledge extraction
* Relationship-aware memory graph
* Entity deduplication and reconciliation
* Importance-aware memory storage
* Team and organization memory via namespaces
* Framework adapters for:

  * OpenAI Agents SDK
  * LangGraph
  * LangChain
  * CrewAI
  * Agno
* Hosted MCP support
* Backward-compatible low-level APIs

---

## Shared Memory Across Agents

Use the same namespace across multiple agents:

```python
from reeve import ReeveMemory

memory = ReeveMemory(
    namespace="company_memory"
)
```

Claude, GPT, Gemini, Cursor, LangGraph agents, and custom applications can share the same memory layer.

---

## Framework Support

### OpenAI Agents SDK

```python
from reeve import ReeveMiddleware
from reeve.adapters import OpenAIAgentsReeveAdapter
```

### LangGraph

```python
from reeve import ReeveMemory
```

### LangChain

```python
from reeve import ReeveMemory
```

### CrewAI

```python
from reeve import ReeveMemory
```

### Agno

```python
from reeve import ReeveMemory
```

---

## Backward Compatibility

Existing integrations continue to work:

```python
from reeve.tools import (
    store_memory,
    query_memory
)
```

No migration is required.

---

## Documentation

The full developer guide includes:

* ReeveAgent
* ReeveMemory
* Framework adapters
* Shared memory
* Retrieval policies
* Advanced configuration
* Migration guides

---

## License

MIT
