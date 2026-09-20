"""Interactive chat loop – stores memories and answers questions from the graph."""

from __future__ import annotations

import asyncio
from threelane_memory.backup import save_backup
from threelane_memory.database import close
from threelane_memory.entity_dedup import deduplicate_entities
from threelane_memory.operator import operator_extract
from threelane_memory.query_enhancer import EnhancedQuery, enhance_query
from threelane_memory.reconciler import consolidate, reconcile

# ── Retrieval + answering ────────────────────────────────────────────────────


async def answer_question(question: str, speaker: str, enhanced: EnhancedQuery | None = None) -> str:
    """Retrieve the most relevant subgraph context and answer via LLM."""
    from threelane_memory import aquery
    # We pass the question directly to the core query function which now
    # handles the enhancement, retrieval, and synthesis.
    return await aquery(question, speaker=speaker, enhanced=enhanced)



# ── Main loop ─────────────────────────────────────────────────────────────────


def main(speaker: str = "default") -> None:
    print("╔══════════════════════════════════════════════╗")
    print("║       Memory Chat  (type 'quit' to exit)    ║")
    print("╠══════════════════════════════════════════════╣")
    print("║  • Tell me facts → stored in the graph      ║")
    print("║  • Ask questions → answered from the graph   ║")
    print("║  • /consolidate  → merge old low-importance  ║")
    print("║  • /backup       → export graph to JSON      ║")
    print("║  • /dedup        → merge duplicate entities   ║")
    print("╚══════════════════════════════════════════════╝\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("Bye!")
            break

        # ── Slash commands ──
        if user_input.lower() == "/consolidate":
            print("  🔄 Running consolidation …")
            try:
                result = consolidate(speaker)
                if result["merged"]:
                    print(
                        f"  ✅ Merged {result['merged']} episodes → "
                        f"{result['consolidated_episode_id']}"
                    )
                else:
                    print("  ℹ️  Nothing to consolidate right now.")
            except Exception as e:
                print(f"  ❌ Error: {e}")
            print()
            continue

        if user_input.lower().startswith("/backup"):
            print("  📦 Exporting graph …")
            try:
                save_backup(speaker=speaker)
            except Exception as e:
                print(f"  ❌ Error: {e}")
            print()
            continue

        if user_input.lower() == "/dedup":
            print("  🔗 Scanning for duplicate entities …")
            try:
                result = deduplicate_entities(dry_run=False, speaker=speaker)
                if result["merged"]:
                    print(f"  ✅ Merged {result['merged']} duplicate entity pair(s)")
                else:
                    print("  ℹ️  No duplicate entities found.")
            except Exception as e:
                print(f"  ❌ Error: {e}")
            print()
            continue

        # ── Intelligence Layer: Intent Detection ──
        print("  🧠 Analyzing intent …")
        enhanced = enhance_query(user_input)

        if enhanced["intent"] == "storage":
            # ── Store mode ──
            print("  📥 Extracting semantics …")
            try:
                semantics, _, _ = operator_extract(user_input)
                episode_id = reconcile(semantics, speaker=speaker, raw_text=user_input)
                print(f"  ✅ Stored episode {episode_id}")
                print(f"     Summary: {semantics['summary']}")
                print(f"     Entities: {', '.join(semantics['entities'])}")
                if semantics.get("location"):
                    print(f"     Location: {semantics['location']}")
                print()
            except Exception as e:
                print(f"  ❌ Error: {e}\n")
        else:
            # ── Answer mode ──
            print("  🔍 Searching memory …")
            answer = asyncio.run(answer_question(user_input, speaker, enhanced))
            print(f"  🧠 {answer}\n")

    close()



if __name__ == "__main__":
    main()
