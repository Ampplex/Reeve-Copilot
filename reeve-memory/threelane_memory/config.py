import os
import secrets
import sys

from dotenv import load_dotenv

load_dotenv()

# ── Supported Providers ──────────────────────────────────────────────────────
SUPPORTED_PROVIDERS = ("ollama", "openai", "bedrock")
SUPPORTED_EMBEDDING_PROVIDERS = ("ollama", "openai", "bedrock")

# ── Neo4j Configuration ──────────────────────────────────────────────────────
NEO4J_URI = os.getenv("NEO4J_URI", "neo4j+s://your-instance.databases.neo4j.io")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")

# ── LLM Provider ──────────────────────────────────────────────────────────────
# Set LLM_PROVIDER to "ollama", "openai", or "bedrock".
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").lower().strip()

if LLM_PROVIDER not in SUPPORTED_PROVIDERS:
    print(
        f"[config] WARNING: Unknown LLM_PROVIDER='{LLM_PROVIDER}'. "
        f"Supported: {', '.join(SUPPORTED_PROVIDERS)}. Falling back to 'ollama'.",
        file=sys.stderr,
    )
    LLM_PROVIDER = "ollama"

EMBEDDING_PROVIDER = (
    os.getenv(
        "EMBEDDING_PROVIDER",
        LLM_PROVIDER if LLM_PROVIDER in SUPPORTED_EMBEDDING_PROVIDERS else "openai",
    )
    .lower()
    .strip()
)

if EMBEDDING_PROVIDER not in SUPPORTED_EMBEDDING_PROVIDERS:
    print(
        f"[config] WARNING: Unknown EMBEDDING_PROVIDER='{EMBEDDING_PROVIDER}'. "
        f"Supported: {', '.join(SUPPORTED_EMBEDDING_PROVIDERS)}. Falling back to 'openai'.",
        file=sys.stderr,
    )
    EMBEDDING_PROVIDER = "openai"

# Ollama settings (default)
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_CHAT_MODEL = os.getenv("OLLAMA_CHAT_MODEL", "llama3.2:3b")
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")

# OpenAI settings (used when LLM_PROVIDER=openai)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_CHAT_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o")
OPENAI_EMBED_MODEL = os.getenv("OPENAI_EMBED_MODEL", "text-embedding-ada-002")

# Bedrock settings (used when LLM_PROVIDER=bedrock)
BEDROCK_API_KEY = os.getenv("BEDROCK_API_KEY", "")
AWS_REGION = os.getenv("AWS_REGION", "us-west-2")
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "mistral.mistral-large-2407-v1:0")
BEDROCK_EMBED_MODEL = os.getenv("BEDROCK_EMBED_MODEL", "amazon.titan-embed-text-v2:0")
BEDROCK_MAX_RETRIES = int(os.getenv("BEDROCK_MAX_RETRIES", "2"))
BEDROCK_RETRY_BASE_DELAY_SECONDS = float(os.getenv("BEDROCK_RETRY_BASE_DELAY_SECONDS", "2.0"))
BEDROCK_RETRY_MAX_DELAY_SECONDS = float(os.getenv("BEDROCK_RETRY_MAX_DELAY_SECONDS", "15.0"))
BEDROCK_MIN_REQUEST_INTERVAL_SECONDS = float(
    os.getenv("BEDROCK_MIN_REQUEST_INTERVAL_SECONDS", "0.5")
)
BEDROCK_REQUEST_TIMEOUT_SECONDS = float(os.getenv("BEDROCK_REQUEST_TIMEOUT_SECONDS", "30.0"))
BEDROCK_EMBED_TIMEOUT_SECONDS = float(
    os.getenv("BEDROCK_EMBED_TIMEOUT_SECONDS", str(BEDROCK_REQUEST_TIMEOUT_SECONDS))
)
BEDROCK_CHAT_TIMEOUT_SECONDS = float(
    os.getenv("BEDROCK_CHAT_TIMEOUT_SECONDS", str(BEDROCK_REQUEST_TIMEOUT_SECONDS))
)
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.15"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "4096"))

# Embedding dimension — auto-set based on provider
_EMBED_DIMS = {
    "nomic-embed-text": 768,
    "text-embedding-ada-002": 1536,
    "amazon.titan-embed-text-v2:0": 1024,
}
if EMBEDDING_PROVIDER == "ollama":
    _active_embed = OLLAMA_EMBED_MODEL
elif EMBEDDING_PROVIDER == "bedrock":
    _active_embed = BEDROCK_EMBED_MODEL
else:
    _active_embed = OPENAI_EMBED_MODEL

EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", str(_EMBED_DIMS.get(_active_embed, 768))))
EMBEDDING_MODEL_VERSION = os.getenv("EMBEDDING_MODEL_VERSION", _active_embed)


# ── Provider Info (used by CLI `config` and startup checks) ──────────────────


def get_provider_summary() -> dict:
    """Return a dict summarising the active provider configuration."""
    if LLM_PROVIDER == "ollama":
        chat_model = OLLAMA_CHAT_MODEL
    elif LLM_PROVIDER == "bedrock":
        chat_model = BEDROCK_MODEL_ID
    else:
        chat_model = OPENAI_CHAT_MODEL

    if EMBEDDING_PROVIDER == "ollama":
        embed_model = OLLAMA_EMBED_MODEL
    elif EMBEDDING_PROVIDER == "bedrock":
        embed_model = BEDROCK_EMBED_MODEL
    else:
        embed_model = OPENAI_EMBED_MODEL

    return {
        "provider": LLM_PROVIDER,
        "embedding_provider": EMBEDDING_PROVIDER,
        "chat_model": chat_model,
        "embed_model": embed_model,
        "embedding_dim": EMBEDDING_DIM,
        "neo4j_uri": NEO4J_URI,
    }


# ── Memory / Retrieval Thresholds ────────────────────────────────────────────
SIMILARITY_THRESHOLD = 0.70
FALLBACK_SIMILARITY_THRESHOLD = 0.60

# ── Retrieval Scoring Weights ────────────────────────────────────────────────
# 70-year-safe: recency is a light tiebreaker, not a dominant signal.
WEIGHT_SIMILARITY = float(os.getenv("WEIGHT_SIMILARITY", "0.55"))
WEIGHT_IMPORTANCE = float(os.getenv("WEIGHT_IMPORTANCE", "0.35"))
WEIGHT_RECENCY = float(os.getenv("WEIGHT_RECENCY", "0.10"))
RECENCY_HALF_LIFE_DAYS = float(os.getenv("RECENCY_HALF_LIFE_DAYS", "365.0"))
IMPORTANCE_FLOOR = float(os.getenv("IMPORTANCE_FLOOR", "0.75"))
# Episodes with importance >= IMPORTANCE_FLOOR ignore recency entirely.

# ── Dynamic Candidate Pool ───────────────────────────────────────────────────
VECTOR_CANDIDATES_MIN = int(os.getenv("VECTOR_CANDIDATES_MIN", "50"))
VECTOR_CANDIDATES_MAX = int(os.getenv("VECTOR_CANDIDATES_MAX", "500"))
VECTOR_CANDIDATES_RATIO = float(os.getenv("VECTOR_CANDIDATES_RATIO", "0.02"))
# candidates = clamp(total_episodes * RATIO, MIN, MAX)
# The ANN index is global (all speakers) and results are post-filtered by
# speaker, so a small tenant in a large shared graph can get zero hits from the
# base pool. GLOBAL_MAX caps a one-shot widening retry that oversamples the
# global index so the tenant's own nearest neighbours survive the filter.
VECTOR_CANDIDATES_GLOBAL_MAX = int(os.getenv("VECTOR_CANDIDATES_GLOBAL_MAX", "2000"))

# ── Consolidation Settings ───────────────────────────────────────────────────
CONSOLIDATION_AGE_DAYS = int(os.getenv("CONSOLIDATION_AGE_DAYS", "90"))
CONSOLIDATION_BATCH_SIZE = int(os.getenv("CONSOLIDATION_BATCH_SIZE", "50"))
CONSOLIDATION_IMPORTANCE_CAP = float(os.getenv("CONSOLIDATION_IMPORTANCE_CAP", "0.3"))
# Only consolidate episodes older than AGE_DAYS **and** importance <= CAP.

# ── Backup / Export ──────────────────────────────────────────────────────────
BACKUP_DIR = os.getenv("BACKUP_DIR", os.path.join(os.path.dirname(__file__), "..", "backups"))

# ── Async Write-Ahead Buffer ─────────────────────────────────────────────────
ASYNC_WRITE_ENABLED = os.getenv("ASYNC_WRITE_ENABLED", "false").lower().strip() == "true"
TEMP_BUFFER_MAX_AGE_SECONDS = float(os.getenv("TEMP_BUFFER_MAX_AGE_SECONDS", "300"))
TEMP_BUFFER_MAX_SIZE = int(os.getenv("TEMP_BUFFER_MAX_SIZE", "500"))

# ── Auth & Rate Limiting ─────────────────────────────────────────────────────
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
DEV_AUTH_ENABLED = os.getenv("DEV_AUTH_ENABLED", "false").lower().strip() == "true"
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "500"))
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))

# Plan-based rate limits (requests per window)
PLAN_LIMITS = {
    "free": 220,
    "pro": 1000,
    "enterprise": 5000,
}
DEFAULT_PLAN = "free"

# Plan-based monthly query quotas — query_memory + retrieve_memory_context
# calls per calendar month (the pricing page's "Queries/mo"). Reads and
# full queries count the same. Env-overridable business knobs.
PLAN_MONTHLY_QUERY_QUOTAS = {
    "free": int(os.getenv("MONTHLY_QUERY_QUOTA_FREE", "20000")),
    "pro": int(os.getenv("MONTHLY_QUERY_QUOTA_PRO", "100000")),
    "enterprise": int(os.getenv("MONTHLY_QUERY_QUOTA_ENTERPRISE", "1000000")),
}

# Plan-based monthly TOKEN quotas — total tokens (in+out, all operations
# incl. stores) per calendar month (the pricing page's "Tokens/mo").
PLAN_MONTHLY_TOKEN_QUOTAS = {
    "free": int(os.getenv("MONTHLY_TOKEN_QUOTA_FREE", "1000000")),        # 1M
    "pro": int(os.getenv("MONTHLY_TOKEN_QUOTA_PRO", "4000000")),          # 4M
    "enterprise": int(os.getenv("MONTHLY_TOKEN_QUOTA_ENTERPRISE", "100000000")),
}

# Plans allowed to exceed their token quota instead of being hard-blocked. Free
# is not in this set, so it stops at its cap. See PRO_TOPUP_PAISE_PER_MILLION
# for the rate — and note that overflow is currently displayed but NOT charged.
TOKEN_TOPUP_PLANS = frozenset(
    p.strip() for p in os.getenv("TOKEN_TOPUP_PLANS", "pro,enterprise").split(",") if p.strip()
)

# OAuth 2.1 Settings
# When OAUTH_JWT_SECRET is not explicitly provided we fall back to a random,
# per-process value. That is safe for local/stdio/CLI use but MUST NOT be used
# for the network server: each worker/restart would get a different signing key,
# silently invalidating every issued token and API-key HMAC. The network entry
# point (see mcp_server.run_server) refuses to start when these are ephemeral.
OAUTH_JWT_SECRET_IS_EPHEMERAL = not os.getenv("OAUTH_JWT_SECRET")
API_KEY_HASH_SECRET_IS_EPHEMERAL = OAUTH_JWT_SECRET_IS_EPHEMERAL and not os.getenv(
    "API_KEY_HASH_SECRET"
)
OAUTH_JWT_SECRET = os.getenv("OAUTH_JWT_SECRET", secrets.token_urlsafe(32))
API_KEY_HASH_SECRET = os.getenv("API_KEY_HASH_SECRET", OAUTH_JWT_SECRET)
OAUTH_EXPECTED_ISSUER = os.getenv("OAUTH_EXPECTED_ISSUER", "").rstrip("/")

# Users allowed to run graph-wide maintenance operations (entity deduplication)
# that mutate the shared, cross-tenant Entity graph. Empty by default so the
# operation is closed to normal callers.
ADMIN_UIDS = frozenset(
    uid.strip()
    for uid in os.getenv("ADMIN_UIDS", "").split(",")
    if uid.strip()
)
OAUTH_ALLOWED_REDIRECT_HOSTS = tuple(
    host.strip().lower()
    for host in os.getenv(
        "OAUTH_ALLOWED_REDIRECT_HOSTS",
        "localhost,127.0.0.1,::1,reeve.co.in,www.reeve.co.in,mcp.reeve.co.in,claude.ai",
    ).split(",")
    if host.strip()
)
# Default to 30 days for developer convenience (3600 * 24 * 30)
OAUTH_TOKEN_EXPIRY_SECONDS = int(os.getenv("OAUTH_TOKEN_EXPIRY_SECONDS", "2592000"))
TRUSTED_PROXY_HOSTS = tuple(
    host.strip()
    for host in os.getenv("TRUSTED_PROXY_HOSTS", "127.0.0.1,localhost,172.16.0.0/12").split(",")
    if host.strip()
)

# ── MCP transport security (DNS rebinding protection) ────────────────────────
# The MCP transports validate the Host and Origin headers so a page on another
# site cannot drive a browser into talking to this server on the user's behalf.
#
# This was silently OFF in production: the server binds 0.0.0.0, and passing that
# host made FastMCP derive `transport_security = None` rather than an allowlist —
# so the code appeared to configure protection it never applied. Setting the
# allowlist explicitly is the only way to actually have it.
#
# The hosts MUST include whatever nginx forwards (`proxy_set_header Host $host`
# => mcp.reeve.co.in) or every MCP client gets a 421. Localhost entries keep
# direct-to-container access working for debugging and health checks.
MCP_ALLOWED_HOSTS: list[str] = [
    h.strip()
    for h in os.getenv(
        "MCP_ALLOWED_HOSTS",
        "mcp.reeve.co.in,mcp.reeve.co.in:*,localhost:*,127.0.0.1:*,[::1]:*",
    ).split(",")
    if h.strip()
]
# Mirrors the CORS allowlist: the browser origins that legitimately call this API.
MCP_ALLOWED_ORIGINS: list[str] = [
    o.strip()
    for o in os.getenv(
        "MCP_ALLOWED_ORIGINS",
        "https://reeve.co.in,https://www.reeve.co.in,https://mcp.reeve.co.in,"
        "http://localhost:3000",
    ).split(",")
    if o.strip()
]
# Escape hatch. If an allowlist mistake ever starts 421-ing real clients, this
# restores the previous (unprotected) behaviour without a code deploy.
MCP_DNS_REBINDING_PROTECTION: bool = (
    os.getenv("MCP_DNS_REBINDING_PROTECTION", "true").lower() == "true"
)

# Razorpay billing
RAZORPAY_KEY_ID: str = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET: str = os.getenv("RAZORPAY_KEY_SECRET", "")
RAZORPAY_PLAN_ID: str = os.getenv("RAZORPAY_PLAN_ID", "")  # Pro (unchanged)
RAZORPAY_WEBHOOK_SECRET: str = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
RAZORPAY_TOTAL_COUNT: int = int(os.getenv("RAZORPAY_TOTAL_COUNT", "120"))
WEBHOOK_MAX_BODY_BYTES: int = int(os.getenv("WEBHOOK_MAX_BODY_BYTES", "262144"))

# ── Enterprise / pay-as-you-go ────────────────────────────────────────────────
# Enterprise has no monthly fee and no included quota: you pay for the tokens you
# actually use. Razorpay has no usage-based product, so this is built out of
# subscription QUANTITY, and the mechanism is worth understanding before changing
# any number here.
#
# A subscription charges `plan.amount x quantity` each cycle. Critically, that
# same product is what the customer's e-mandate authorises: an auth page for
# quantity 400 on a Rs1 plan reads "RECURRING AMOUNT Rs400.00" — verified live.
# So quantity at signup sets the mandate CEILING, and lowering it before a charge
# bills less. Usage is billed IN ARREARS: a month's tokens are charged at the
# start of the next cycle.
#
# This replaced an earlier design that held a nominal Rs1 mandate and added usage
# as an ADD-ON. That was broken twice over: Razorpay documents add-ons as
# deprecated, and a Rs1 mandate cannot authorise a Rs500 usage charge, so the
# auto-debit would simply have failed a month after signup.
#
# The unit is the granularity of a charge. Remainder below one unit is NOT
# rounded away — it stays unbilled and rolls into the next cycle, so the customer
# is never over- or under-charged, only charged slightly later.
RAZORPAY_ENTERPRISE_PLAN_ID: str = os.getenv("RAZORPAY_ENTERPRISE_PLAN_ID", "")
PAYG_UNIT_PAISE: int = int(os.getenv("PAYG_UNIT_PAISE", "1000"))  # Rs10 per unit
# Razorpay hard-caps subscription quantity at 500 (verified: "The quantity may
# not be greater than 500"), which is what bounds the maximum possible mandate.
PAYG_MAX_QUANTITY: int = int(os.getenv("PAYG_MAX_QUANTITY", "500"))

# Delay the first charge by one cycle. Quantity drives the CHARGE as well as the
# mandate, so without this a new customer is billed their whole spend cap on day
# one, having used nothing — the authorisation page reads "Rs2,000.00 billed for
# this month". Razorpay treats the gap as a trial: authorisation takes only a
# token amount and refunds it, so the mandate registers without taking money.
PAYG_FIRST_CHARGE_DELAY_SECONDS: int = int(
    os.getenv("PAYG_FIRST_CHARGE_DELAY_SECONDS", str(30 * 24 * 60 * 60))
)

# How many cycles the mandate runs for. Razorpay spells this out at checkout —
# the shared 120 renders as "until 9 Aug 2036", which reads like a ten-year
# commitment next to a plan sold as pay-as-you-go. 24 still comfortably outlasts
# any real subscription (users can cancel whenever) while reading as an ordinary
# term. Pro keeps RAZORPAY_TOTAL_COUNT untouched.
PAYG_TOTAL_COUNT: int = int(os.getenv("PAYG_TOTAL_COUNT", "24"))

# $3 per 1M tokens, charged in INR at the same ₹83/$ the Pro plan uses
# (Pro displays $15 and charges ₹1,250). Paise, so 25000 = ₹250.00.
PAYG_PAISE_PER_MILLION_TOKENS: int = int(os.getenv("PAYG_PAISE_PER_MILLION_TOKENS", "25000"))

# A plan with no ceiling and no monthly fee has nothing to stop a runaway agent
# loop or a leaked API key from running up an unbounded bill, and that bill would
# land on a real card. The cap makes the worst case a number the user chose.
# Users can raise or lower it; 0 means no cap (opt-in only, never the default).
# Rs500 rather than a larger figure, because the cap is the number the customer
# sees at checkout. Razorpay can only display the mandate ceiling, so its screen
# reads "Reeve will then charge <cap> every month" — it has no way to know the
# amount is lowered to real usage before each charge. A Rs2,000 default made an
# honest usage-based product look like an expensive flat subscription at the
# exact moment someone decides whether to sign up.
#
# Rs500 also lands sensibly against the other plans: it covers ~2M tokens, and
# anyone needing more is better served by Pro (4M for Rs1,250). Users can raise
# it in the dashboard, which re-authorises a larger mandate.
PAYG_DEFAULT_SPEND_CAP_PAISE: int = int(os.getenv("PAYG_DEFAULT_SPEND_CAP_PAISE", "50000"))
# The cap and the mandate are the SAME number: the cap chosen at signup becomes
# the quantity the customer authorises, so we can never debit more than they
# agreed to. That also means the ceiling is not ours to pick — it is whatever
# Razorpay's quantity limit allows (500 x Rs10 = Rs5,000). Raising a cap above
# the authorised mandate needs a new mandate, i.e. re-subscribing.
PAYG_MAX_SPEND_CAP_PAISE: int = PAYG_UNIT_PAISE * PAYG_MAX_QUANTITY

# Pro's top-up rate for usage beyond its 4M included tokens: $5 per 1M, at the
# same ₹83/$ (41500 paise = ₹415). Lives here rather than only on the pricing
# page so the quoted number has one source.
#
# HONEST CAVEAT: this rate is quoted to users but nothing charges it. Pro
# overflow has always been allowed through un-billed (see TOKEN_TOPUP_PLANS),
# and wiring it up would start charging existing Pro users for something they
# currently get free — a pricing decision, not a bug fix, so it is left alone.
PRO_TOPUP_PAISE_PER_MILLION: int = int(os.getenv("PRO_TOPUP_PAISE_PER_MILLION", "41500"))

# ── Supabase (server-side user profile sync) ─────────────────────────────────
# The backend mirrors the authenticated user's profile into Supabase using the
# service-role key (the frontend anon-key write is blocked by RLS). Left empty
# by default so the sync stays inert until both values are provided.
SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY: str = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

# ── Geospatial Location Enrichment ────────────────────────────────────────────
# When enabled, the first mention of a place enriches its Location node with
# coordinates (via Nominatim), a compact LLM-written "place card" describing
# the place's character, and an embedding of that card — and folds the card
# into the mentioning episode's searchable text so vibe-level queries
# ("somewhere with beaches") retrieve it. Off by default; enabling it changes
# nothing for episodes stored while it was off.
GEO_ENRICHMENT_ENABLED = os.getenv("GEO_ENRICHMENT_ENABLED", "false").lower().strip() == "true"
NOMINATIM_BASE_URL = os.getenv("NOMINATIM_BASE_URL", "https://nominatim.openstreetmap.org")
GEOCODER_TIMEOUT_SECONDS = float(os.getenv("GEOCODER_TIMEOUT_SECONDS", "5.0"))
# Nominatim's usage policy requires an identifying User-Agent and ≤ 1 req/s.
GEOCODER_USER_AGENT = os.getenv("GEOCODER_USER_AGENT", "reeve-memory (https://reeve.co.in)")
GEOCODER_MIN_INTERVAL_SECONDS = float(os.getenv("GEOCODER_MIN_INTERVAL_SECONDS", "1.0"))
PLACE_CARD_MAX_CHARS = int(os.getenv("PLACE_CARD_MAX_CHARS", "400"))
# Retrieval-side geo lane: default radius for "near X" queries, and the cosine
# floor a place's vibe card must clear to count as "similar to X".
GEO_NEAR_RADIUS_KM = float(os.getenv("GEO_NEAR_RADIUS_KM", "100"))
GEO_VIBE_MIN_SIMILARITY = float(os.getenv("GEO_VIBE_MIN_SIMILARITY", "0.25"))
# Context-aware geocoding: if a place's first geocode lands farther than this
# from the speaker's other known places, treat it as a possibly-ambiguous
# namesake and retry the geocode qualified by the speaker's known places
# (so "City Palace" near a "Udaipur" memory resolves to Udaipur, not Potsdam).
GEO_DISAMBIGUATION_MAX_KM = float(os.getenv("GEO_DISAMBIGUATION_MAX_KM", "250"))
GEO_DISAMBIGUATION_MAX_TRIES = int(os.getenv("GEO_DISAMBIGUATION_MAX_TRIES", "3"))

# ── Vision (image memories) ───────────────────────────────────────────────────
# Separate multimodal model used ONLY to turn an image into a text memory
# record at ingestion; the main chat model (Mistral) handles all text work.
# Empty (default) disables image support entirely.
BEDROCK_VISION_MODEL_ID = os.getenv("BEDROCK_VISION_MODEL_ID", "")
VISION_MAX_TOKENS = int(os.getenv("VISION_MAX_TOKENS", "512"))
# Ingest-time description has a larger budget than the answering path: it may
# be asked to transcribe a whiteboard or timetable verbatim, and a transcript
# truncated mid-row is worse than none — it reads as complete while the last
# rows, often the ones being asked about, are missing.
VISION_DESCRIBE_MAX_TOKENS = int(os.getenv("VISION_DESCRIBE_MAX_TOKENS", "1400"))
VISION_MAX_IMAGE_BYTES = int(os.getenv("VISION_MAX_IMAGE_BYTES", str(4 * 1024 * 1024)))

# ── Multimodal Embeddings (image ↔ text shared vector space) ──────────────────
# A Bedrock multimodal embedding model that maps images AND text into ONE space,
# enabling image-to-image and text-to-image search. Separate from the text
# embedding model (which stays as-is). Empty (default) disables image vectors.
BEDROCK_MULTIMODAL_EMBED_MODEL = os.getenv(
    "BEDROCK_MULTIMODAL_EMBED_MODEL", ""
)
MULTIMODAL_EMBED_DIM = int(os.getenv("MULTIMODAL_EMBED_DIM", "1024"))
# Gate for the image lane when it joins ordinary retrieval.
#
# NOT an absolute similarity floor. Measured against live Titan multimodal
# vectors, text→image scores sit in a narrow band where the classes overlap:
#   genuine visual match   0.6618 – 0.7112
#   NON-visual question    0.6855 – 0.6928   ← outscores a real food query
#   unrelated visual       0.6234 – 0.6615
# "what is my startup runway" beat "a red curry dish", so any fixed cutoff either
# admits everything or rejects real matches.
#
# The lane looks for a natural BREAK in the ranking and admits the group above
# it. A real match separates from the rest; a question with nothing to match
# leaves an almost flat ranking.
#
# Calibrated on 22 complete score distributions measured against prod, across
# TWO library shapes (food-heavy and diverse) using SENTENCE-form queries —
# because that is what actually arrives. The query enhancer does not reduce a
# question to a tidy phrase: it emits "What was the name of the restaurant where
# I ate the red colored dish?", and those extra non-visual tokens flatten the
# score spread. Two earlier passes calibrated on clean phrases and on a
# truncated head, and both produced values that collapsed on real input:
#   min_gap 0.022, group 4  ->  precision 1.00, recall 0.78   (shipped)
#   min_gap 0.020, group 4  ->  precision 0.80, recall 0.89
#   min_gap 0.040, group 3  ->  precision 1.00, recall 0.56   (first attempt)
#
# Precision is favoured because a hit here does double duty: it joins the
# retrieval merge AND triggers the vision answer path, which costs an image
# fetch plus a vision call and can answer from the wrong photo. A miss costs
# nothing — the text lanes still retrieve that photo through its description.
#
# group 4 matters for homogeneous libraries: asked for a red dish against six
# food photos, the break can fall after FOUR red/orange dishes, all of which are
# legitimate answers.
#
# Both known misses are food-heavy queries where several dishes genuinely match
# and nothing breaks away. Explicit photo search (search_image_memories) skips
# this gate entirely, so asking for pictures always returns the best available.
IMAGE_LANE_MIN_GAP = float(os.getenv("IMAGE_LANE_MIN_GAP", "0.022"))
IMAGE_LANE_MAX_GROUP = int(os.getenv("IMAGE_LANE_MAX_GROUP", "4"))
# Below this many photos there is no ranking to find a break in, so the ambient
# lane stays out. Those photos remain reachable through their descriptions via
# the text lanes, exactly as before.
IMAGE_LANE_MIN_SAMPLE = int(os.getenv("IMAGE_LANE_MIN_SAMPLE", "3"))
# Server-side fetch of image_url (MCP path). Guarded against SSRF; only public
# http(s) hosts, capped size.
IMAGE_URL_FETCH_TIMEOUT_SECONDS = float(os.getenv("IMAGE_URL_FETCH_TIMEOUT_SECONDS", "10"))

# ── Image store (retained photo bytes) ────────────────────────────────────────
# Storing the ORIGINAL photo is what lets the vision model answer questions
# nobody anticipated at upload time ("how many people were there?"). The image
# embedding cannot do this: it is a one-way fingerprint, useful for matching and
# useless for reading.
#
# This is personal data at rest, so it is OPT-IN and stays off unless a bucket is
# configured. DPDP-relevant properties, all enforced below or in image_store.py:
#   • data minimisation  — off by default; retention TTL ages objects out while
#                          the description and embedding persist, so most recall
#                          keeps working after the photo itself is gone
#   • storage limitation — IMAGE_STORE_RETENTION_DAYS drives an S3 lifecycle rule
#   • localisation       — bucket region defaults to ap-south-1 (Mumbai). NOTE:
#                          AWS_REGION (Bedrock) is a SEPARATE transfer — photos
#                          are sent there for description/embedding regardless of
#                          where they are stored.
#   • confidentiality    — SSE-KMS at rest, TLS in transit, public access blocked
#   • purpose limitation — objects are read only to answer that speaker's own
#                          questions, never listed or exported in bulk
IMAGE_STORE_ENABLED = os.getenv("IMAGE_STORE_ENABLED", "false").lower() == "true"
IMAGE_STORE_BUCKET = os.getenv("IMAGE_STORE_BUCKET", "")
IMAGE_STORE_REGION = os.getenv("IMAGE_STORE_REGION", "ap-south-1")
IMAGE_STORE_PREFIX = os.getenv("IMAGE_STORE_PREFIX", "memories")
# Empty → SSE-S3 (AES256). Set to a KMS key id/arn for customer-managed keys.
IMAGE_STORE_KMS_KEY_ID = os.getenv("IMAGE_STORE_KMS_KEY_ID", "")
IMAGE_STORE_RETENTION_DAYS = int(os.getenv("IMAGE_STORE_RETENTION_DAYS", "30"))
IMAGE_STORE_TIMEOUT_SECONDS = float(os.getenv("IMAGE_STORE_TIMEOUT_SECONDS", "10"))
# How many stored photos may be re-read and shown to the vision model for a
# single question. Each one costs a fetch plus image tokens, so keep it small.
IMAGE_ANSWER_MAX_IMAGES = int(os.getenv("IMAGE_ANSWER_MAX_IMAGES", "2"))
