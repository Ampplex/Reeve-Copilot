"""Manual inference script for querying stored memories.

Usage:
  python examples/inference_chat.py --speaker default
  python examples/inference_chat.py --speaker default --question "Where was Arthur's family home?"
"""

from __future__ import annotations

import argparse

from threelane_memory import close
from threelane_memory.llm_interface import invoke_llm
from threelane_memory.retriever import retrieve


def _answer_question(question: str, speaker: str, show_context: bool) -> str:
    """Answer from retrieved memory context and optionally print the context."""
    context = retrieve(question, speaker=speaker)
    if not context.strip():
        return "I don't have any relevant memories for that question."

    if show_context:
        print("Retrieved memory context:")
        print(context)
        print()

    prompt = (
        "You are a personal memory assistant. Use ONLY the memory context below "
        "to answer the user's question. If the answer isn't in the context, say so.\n\n"
        f"Memory Context:\n{context}\n\n"
        f"Question: {question}"
    )
    return invoke_llm(prompt)


def run_interactive(speaker: str, show_context: bool) -> None:
    """Start a simple interactive question loop."""
    print("Memory inference chat")
    print("Type a question and press Enter. Type 'quit' to exit.\n")

    while True:
        question = input("Q> ").strip()
        if not question:
            continue
        if question.lower() in {"quit", "exit", "q"}:
            print("Exiting.")
            break

        try:
            answer = _answer_question(question=question, speaker=speaker, show_context=show_context)
            print(f"A> {answer}\n")
        except Exception as exc:  # noqa: BLE001
            print(f"Error: {exc}\n")


def run_once(speaker: str, question: str, show_context: bool) -> None:
    """Run one question and print the answer."""
    answer = _answer_question(question=question, speaker=speaker, show_context=show_context)
    print(answer)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Query your memory graph manually")
    parser.add_argument(
        "--speaker",
        default="default",
        help="Speaker namespace used when memories were stored",
    )
    parser.add_argument(
        "--question",
        default=None,
        help="Ask one question and exit",
    )
    parser.add_argument(
        "--show-context",
        action="store_true",
        help="Print retrieved memory context before answering",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    try:
        if args.question:
            run_once(speaker=args.speaker, question=args.question, show_context=args.show_context)
        else:
            run_interactive(speaker=args.speaker, show_context=args.show_context)
    finally:
        close()


if __name__ == "__main__":
    main()
