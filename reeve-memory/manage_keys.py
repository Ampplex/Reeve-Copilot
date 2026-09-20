"""Helper script to manage API keys for the reeve backend.

Usage:
    python manage_keys.py create --user-id "user-123" --email "user@example.com" --plan "pro"
    python manage_keys.py revoke --api-key "reeve_..."
    python manage_keys.py list
"""

import argparse

# Ensure we can import from the current directory
import os
import secrets
import sys

sys.path.append(os.getcwd())

from threelane_memory.database import run_query


def _api_key_exists(api_key: str) -> bool:
    query = "MATCH (u:User {api_key: $api_key}) RETURN u.uid AS uid LIMIT 1"
    return bool(run_query(query, {"api_key": api_key}))


def create_api_key(user_id: str, email: str | None = None, plan: str = "free") -> str:
    """Generate a new API key and store it in Neo4j."""
    api_key = f"reeve_live_{secrets.token_urlsafe(32)}"
    if _api_key_exists(api_key):
        print("❌ Generated API key already exists. Try again.")
        return ""

    query = """
    MERGE (u:User {uid: $uid})
    SET u.api_key = $api_key,
        u.email = $email,
        u.plan = $plan,
        u.updated_at = timestamp()
    RETURN u.api_key AS key
    """
    params = {"uid": user_id, "api_key": api_key, "email": email, "plan": plan}

    results = run_query(query, params)
    if results:
        print(f"✅ Successfully created key for user: {user_id}")
        print(f"🔑 API KEY: {api_key}")
        print("⚠️  Copy this key now. It won't be shown again in this format.")
        return api_key
    else:
        print("❌ Failed to create API key.")
        return ""


def revoke_api_key(api_key: str):
    """Remove an API key from a user."""
    query = """
    MATCH (u:User {api_key: $api_key})
    SET u.api_key = null,
        u.updated_at = timestamp()
    RETURN u.uid AS uid
    """
    results = run_query(query, {"api_key": api_key})
    if results:
        print(f"✅ Successfully revoked key for user: {results[0]['uid']}")
    else:
        print("❌ No user found with that API key.")


def list_users():
    """List all users and their plans (hiding keys)."""
    query = """
    MATCH (u:User)
    RETURN u.uid AS uid, u.email AS email, u.plan AS plan, u.api_key IS NOT NULL AS has_key
    """
    results = run_query(query)
    print(f"{'User ID':<20} | {'Email':<30} | {'Plan':<10} | {'Has Key'}")
    print("-" * 75)
    for row in results:
        line = (
            f"{str(row['uid']):<20} | {str(row['email']):<30} | "
            f"{str(row['plan']):<10} | {row['has_key']}"
        )
        print(line)


def main():
    parser = argparse.ArgumentParser(description="Reeve API Key Manager")
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Create command
    create_parser = subparsers.add_parser("create", help="Create a new API key")
    create_parser.add_argument("--user-id", required=True, help="Unique ID for the user")
    create_parser.add_argument("--email", help="User email address")
    create_parser.add_argument(
        "--plan",
        choices=["free", "pro", "enterprise"],
        default="free",
        help="Subscription plan",
    )

    # Revoke command
    revoke_parser = subparsers.add_parser("revoke", help="Revoke an existing API key")
    revoke_parser.add_argument("--api-key", required=True, help="The API key to revoke")

    # List command
    subparsers.add_parser("list", help="List all users")

    args = parser.parse_args()

    if args.command == "create":
        create_api_key(args.user_id, args.email, args.plan)
    elif args.command == "revoke":
        revoke_api_key(args.api_key)
    elif args.command == "list":
        list_users()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
help()


if __name__ == "__main__":
    main()
