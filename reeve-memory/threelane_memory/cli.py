"""CLI interface for Reeve.

Usage::

    reeve chat                        # interactive chat
    reeve store "I met Alice today"   # store a single memory
    reeve query "Who did I meet?"     # query memories
    reeve config                      # show active provider & config
    reeve backup                      # export graph to JSON
    reeve dedup                       # merge duplicate entities
    reeve dedup --dry-run             # preview dedup merges
    reeve consolidate                 # merge old low-importance episodes
    reeve reindex                     # re-embed episodes after model change
    reeve eval                        # run ground-truth evaluation set
"""

from __future__ import annotations

import argparse
import sys


def _preflight_check() -> None:
    """Run a dimension-mismatch check and warn the user if necessary."""
    from threelane_memory.database import check_index_dimension

    check_index_dimension()


def _cmd_chat(args: argparse.Namespace) -> None:
    """Launch interactive chat loop."""
    _preflight_check()
    from threelane_memory.chat import main as chat_main

    chat_main(speaker=args.speaker)


def _cmd_store(args: argparse.Namespace) -> None:
    """Store a single memory from the command line."""
    _preflight_check()
    from threelane_memory import store

    episode_id = store(args.text, speaker=args.speaker)
    print(f"Stored episode {episode_id}")


def _cmd_query(args: argparse.Namespace) -> None:
    """Query memories from the command line."""
    _preflight_check()
    from threelane_memory import query

    answer = query(args.question, speaker=args.speaker)
    print(answer)


def _cmd_config(args: argparse.Namespace) -> None:
    """Show active provider configuration and run health checks."""
    from threelane_memory.config import SUPPORTED_PROVIDERS, get_provider_summary
    from threelane_memory.database import check_index_dimension, get_index_dimension

    info = get_provider_summary()

    print("╔══════════════════════════════════════════════════════════════╗")
    print("║                     Reeve · Configuration                    ║")
    print("╠══════════════════════════════════════════════════════════════╣")
    print(f"║  Provider        : {info['provider']:<40} ║")
    print(f"║  Chat model      : {info['chat_model']:<40} ║")
    print(f"║  Embed model     : {info['embed_model']:<40} ║")
    print(f"║  Embedding dim   : {str(info['embedding_dim']):<40} ║")
    print(f"║  Neo4j URI       : {info['neo4j_uri'][:40]:<40} ║")
    print("╠══════════════════════════════════════════════════════════════╣")

    # Neo4j connectivity
    try:
        from threelane_memory.database import run_query

        run_query("RETURN 1 AS ok")
        print("║  Neo4j status    : ✅ connected                            ║")
    except Exception as e:
        msg = str(e)[:35]
        print(f"║  Neo4j status    : ❌ {msg:<38} ║")

    # Vector index dimension
    idx_dim = get_index_dimension()
    if idx_dim is None:
        print("║  Vector index    : ⚠  not found (will auto-create)        ║")
    elif idx_dim == info["embedding_dim"]:
        padding = "   " if idx_dim < 1000 else "  "
        print(f"║  Vector index    : ✅ {idx_dim}-dim (matches config)          {padding}║")
    else:
        expect = info["embedding_dim"]
        print(f"║  Vector index    : ❌ {idx_dim}-dim (config expects {expect})      ║")

    # Ollama connectivity (if applicable)
    if info["provider"] == "ollama":
        try:
            import requests

            from threelane_memory.config import OLLAMA_BASE_URL

            requests.get(OLLAMA_BASE_URL, timeout=3)
            print("║  Ollama status   : ✅ running                             ║")
        except Exception:
            print("║  Ollama status   : ❌ not reachable (is ollama serve on?)  ║")

    # Bedrock status (if applicable)
    if info["provider"] == "bedrock" or info["embedding_provider"] == "bedrock":
        from threelane_memory.config import BEDROCK_API_KEY

        if not BEDROCK_API_KEY:
            print("║  Bedrock status  : ⚠  API key missing                      ║")
        else:
            print("║  Bedrock status  : ✅ configured                           ║")

    print("╚══════════════════════════════════════════════════════════════╝")
    print()

    # Detailed mismatch warning (if any)
    check_index_dimension()

    # Switching help
    other = [p for p in SUPPORTED_PROVIDERS if p != info["provider"]]
    if other:
        print(f"  To switch to {other[0]}, see: .env.example")
        print("  After switching, run: reeve config")
        print()

    # OpenAI key check
    if info["provider"] == "openai":
        from threelane_memory.config import OPENAI_API_KEY

        if not OPENAI_API_KEY:
            print("  ⚠  OPENAI_API_KEY is empty — set it in .env")
            print()


def _cmd_backup(args: argparse.Namespace) -> None:
    """Export the graph to JSON."""
    from threelane_memory.backup import save_backup

    path = save_backup(speaker=args.speaker, since=args.since)
    print(f"Done: {path}")


def _cmd_dedup(args: argparse.Namespace) -> None:
    """Run entity deduplication (one speaker, or all speakers by default)."""
    from threelane_memory.entity_dedup import deduplicate_all_speakers, deduplicate_entities

    if args.speaker:
        result = deduplicate_entities(dry_run=args.dry_run, speaker=args.speaker)
    else:
        result = deduplicate_all_speakers(dry_run=args.dry_run)
    mode = "DRY-RUN" if args.dry_run else "APPLIED"
    print(f"[{mode}] Duplicates found: {result['duplicates_found']}, Merged: {result['merged']}")


def _cmd_migrate(args: argparse.Namespace) -> None:
    """Apply pending graph schema migrations (per-speaker isolation).

    Run with --dry-run first to preview; run without it (ideally against prod
    before deploying the new code) to apply.
    """
    from threelane_memory.migrations import (
        apply_entity_speaker_v1,
        apply_location_speaker_v1,
        plan_entity_speaker_v1,
        plan_location_speaker_v1,
    )

    if args.dry_run:
        for plan in (plan_entity_speaker_v1(), plan_location_speaker_v1()):
            print(f"[DRY-RUN] {plan['migration']} plan:")
            for key, value in plan.items():
                print(f"  {key}: {value}")
        return

    for apply in (apply_entity_speaker_v1, apply_location_speaker_v1):
        result = apply()
        print(f"Migration result: {result}")


def _cmd_geo_backfill(args: argparse.Namespace) -> None:
    """Enrich Location nodes created before geo enrichment was enabled."""
    from threelane_memory.geo import backfill_locations, refresh_vibe_embeddings

    if args.refresh_vibes:
        refreshed = refresh_vibe_embeddings(limit=args.limit)
        print(f"[DONE] vibe embeddings refreshed for {refreshed} enriched locations")
        return

    result = backfill_locations(limit=args.limit, dry_run=args.dry_run)
    mode = "DRY-RUN" if args.dry_run else "DONE"
    print(
        f"[{mode}] pending={result['pending']} enriched={result['enriched']} "
        f"not_found={result['not_found']} unresolved={result['unresolved']}"
    )
    if args.dry_run and result["sample"]:
        print("Sample of pending locations:")
        for name in result["sample"]:
            print(f"  - {name}")


def _cmd_consolidate(args: argparse.Namespace) -> None:
    """Consolidate old low-importance episodes."""
    from threelane_memory.reconciler import consolidate

    result = consolidate(args.speaker)
    if result["merged"]:
        print(f"Merged {result['merged']} episodes → {result['consolidated_episode_id']}")
    else:
        print("Nothing to consolidate right now.")


def _cmd_reindex(args: argparse.Namespace) -> None:
    """Re-embed all episodes with a new model."""
    from threelane_memory.reconciler import reindex_embeddings

    reindex_embeddings(old_model=args.old_model, speaker=args.speaker, batch_size=args.batch_size)


def _cmd_eval(args: argparse.Namespace) -> None:
    """Run evaluation against a ground-truth dataset."""
    from threelane_memory.evaluation import evaluate_cases, load_eval_cases

    cases = load_eval_cases(args.file)
    results = evaluate_cases(cases, speaker=args.speaker)

    print("\nEvaluation Complete:")
    print(f"  Total cases : {results['total']}")
    print(f"  Correct     : {results['correct']}")
    print(f"  Accuracy    : {results['accuracy']:.1%}")


def _cmd_mcp(args: argparse.Namespace) -> None:
    """Run Reeve as an MCP server."""
    from threelane_memory.mcp_server import run_server

    run_server(transport=args.transport, host=args.host, port=args.port)


def main(argv: list[str] | None = None) -> None:
    """Entry point for the ``reeve`` CLI."""
    parser = argparse.ArgumentParser(
        prog="reeve",
        description=(
            "Personal long-term memory on Neo4j — supports Ollama (local), OpenAI, and Bedrock"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s 0.1.8",
    )

    if argv is None:
        if len(sys.argv) <= 1:
            parser.print_help()
            raise SystemExit(1)
    elif len(argv) == 0:
        parser.print_help()
        raise SystemExit(1)

    subparsers = parser.add_subparsers(dest="command", required=True)

    # Chat
    p_chat = subparsers.add_parser("chat", help="Start an interactive chat session")
    p_chat.add_argument("--speaker", default="default", help="Set the speaker identity")
    p_chat.set_defaults(func=_cmd_chat)

    # Store
    p_store = subparsers.add_parser("store", help="Store a single memory")
    p_store.add_argument("text", help="Text to store")
    p_store.add_argument("--speaker", default="default", help="Set the speaker identity")
    p_store.set_defaults(func=_cmd_store)

    # Query
    p_query = subparsers.add_parser("query", help="Query long-term memory")
    p_query.add_argument("question", help="Question to ask")
    p_query.add_argument("--speaker", default="default", help="Set the speaker identity")
    p_query.set_defaults(func=_cmd_query)

    # Config
    p_config = subparsers.add_parser("config", help="Show current configuration and health")
    p_config.set_defaults(func=_cmd_config)

    # Backup
    p_backup = subparsers.add_parser("backup", help="Export graph to JSON")
    p_backup.add_argument("--speaker", help="Filter by speaker")
    p_backup.add_argument("--since", help="Filter by date (ISO-8601)")
    p_backup.set_defaults(func=_cmd_backup)

    # Dedup
    p_dedup = subparsers.add_parser("dedup", help="Merge duplicate entities")
    p_dedup.add_argument("--dry-run", action="store_true", help="Preview merges without applying")
    p_dedup.add_argument(
        "--speaker", default=None, help="Deduplicate only this speaker (default: all speakers)"
    )
    p_dedup.set_defaults(func=_cmd_dedup)

    # Consolidate
    p_consolidate = subparsers.add_parser("consolidate", help="Summarize old trivia")
    p_consolidate.add_argument("--speaker", default="default", help="Set the speaker identity")
    p_consolidate.set_defaults(func=_cmd_consolidate)

    # Reindex
    p_reindex = subparsers.add_parser("reindex", help="Re-embed episodes (e.g. after model swap)")
    p_reindex.add_argument("--old-model", required=True, help="Model name of current embeddings")
    p_reindex.add_argument("--speaker", default="default", help="Filter by speaker")
    p_reindex.add_argument("--batch-size", type=int, default=50, help="Episodes per batch")
    p_reindex.set_defaults(func=_cmd_reindex)

    # Eval
    p_eval = subparsers.add_parser("eval", help="Run ground-truth evaluation")
    p_eval.add_argument("--file", required=True, help="Path to evaluation JSON")
    p_eval.add_argument("--speaker", default="default", help="Set the speaker identity")
    p_eval.set_defaults(func=_cmd_eval)

    # Migrate
    p_migrate = subparsers.add_parser(
        "migrate", help="Apply pending graph schema migrations (per-speaker isolation)"
    )
    p_migrate.add_argument(
        "--dry-run", action="store_true", help="Preview the migration without applying"
    )
    p_migrate.set_defaults(func=_cmd_migrate)

    # Geo backfill
    p_geo = subparsers.add_parser(
        "geo-backfill",
        help="Geocode + vibe-card Location nodes created before geo enrichment",
    )
    p_geo.add_argument(
        "--limit", type=int, default=200, help="Max locations to enrich in one run"
    )
    p_geo.add_argument(
        "--dry-run", action="store_true", help="Only count and list pending locations"
    )
    p_geo.add_argument(
        "--refresh-vibes",
        action="store_true",
        help="Re-embed place cards of already-enriched locations (formula/model repair)",
    )
    p_geo.set_defaults(func=_cmd_geo_backfill)

    # MCP
    p_mcp = subparsers.add_parser("mcp", help="Run as an MCP server")
    p_mcp.add_argument("--transport", choices=["stdio", "sse"], default="stdio")
    p_mcp.add_argument("--host", default="127.0.0.1")
    p_mcp.add_argument("--port", type=int, default=8000)
    p_mcp.set_defaults(func=_cmd_mcp)

    # Dispatch
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
