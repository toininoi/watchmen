"""Model pricing — fetch from OpenRouter API, cache locally, fall back to hardcoded defaults.

OpenRouter's /models endpoint returns pricing as **per-token** strings.
We cache the response locally so we don't hammer the API on every metrics run.

Cached data: ~/.watchmen/cache/model_prices.json (expires after 24h)

If the API is unavailable (no key, network error, quota exceeded), we fall back
to the hardcoded HARDCODED_PRICES dict (per-million convention).

Lookup order in `price_for_model`:
  1. normalize the model name + exact match against HARDCODED_PRICES (the
     authoritative path for every Claude / OpenAI model we ship support for)
  2. fuzzy match against the API-fetched DB (catches models we don't yet
     hardcode but OpenRouter knows about)
  3. family-pattern fallback (claude-opus-99-99 → current Opus pricing)

TODO(phase-3 pricing cleanup):
  - `_parse_pricing` stores OpenRouter prices verbatim (per-token), while
    the hardcoded fallback + `turn_cost_usd` math assume per-million. Step 1
    above means this currently has no observable effect (we never use API
    pricing for any supported model), but step 2 will start using API pricing
    once we start querying unknown models. Multiply by 1M in `_parse_pricing`
    before that becomes a hot path.
  - First metrics call blocks on a 30s API request when the cache is cold;
    pre-warm the cache during `watchmen init` or move the fetch off the hot
    path.
"""

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from watchmen.paths import WATCHMEN_HOME

# ─── API endpoint ──────────────────────────────────────────────────────────

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
CACHE_DIR = WATCHMEN_HOME / "cache"
CACHE_FILE = CACHE_DIR / "model_prices.json"
CACHE_TTL = 24 * 60 * 60  # 24 hours

# ─── Data structures ───────────────────────────────────────────────────────

@dataclass
class ModelPricing:
    """Price per 1M tokens for a model. Tuple: (input, cache_write_5m, cache_write_1h, cache_read, output)."""
    input: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float
    output: float

    def as_tuple(self) -> tuple[float, float, float, float, float]:
        return (self.input, self.cache_write_5m, self.cache_write_1h, self.cache_read, self.output)

    def cost_for(
        self,
        input_tokens: int,
        cache_creation_5m: int,
        cache_creation_1h: int,
        cache_read: int,
        output_tokens: int,
    ) -> float:
        """Cost in USD for one turn with this pricing."""
        return (
            input_tokens * self.input
            + cache_creation_5m * self.cache_write_5m
            + cache_creation_1h * self.cache_write_1h
            + cache_read * self.cache_read
            + output_tokens * self.output
        ) / 1_000_000


@dataclass
class PriceDatabase:
    """In-memory price database loaded from cache or API."""
    prices: dict[str, ModelPricing] = field(default_factory=dict)
    fetched_at: float = 0.0
    source: str = "hardcoded"  # "api", "cache", or "hardcoded"

    def get_price(self, model_name: str) -> Optional[ModelPricing]:
        """Get price for a model by name. Model names are matched case-insensitively."""
        key = model_name.lower()
        for name, pricing in self.prices.items():
            if name.lower() == key:
                return pricing
        return None

    def find_price(self, model_name: str) -> Optional[ModelPricing]:
        """Fuzzy match: try exact, then substring match (longest key wins)."""
        # Exact match first
        if pricing := self.get_price(model_name):
            return pricing

        # Substring match — longest key wins
        model_lower = model_name.lower()
        best = None
        best_len = 0
        for name in self.prices:
            if name.lower() in model_lower and len(name) > best_len:
                best = self.prices[name]
                best_len = len(name)
        return best


# ─── Hardcoded fallback ─────────────────────────────────────────────────────

# These are the last-known prices (verified 2026-05-12). Used when API is unavailable.
HARDCODED_PRICES: dict[str, tuple[float, float, float, float, float]] = {
    # Opus family
    "opus-4.7": (5.00, 6.25, 10.00, 0.50, 25.00),
    "opus-4.6": (5.00, 6.25, 10.00, 0.50, 25.00),
    "opus-4.5": (5.00, 6.25, 10.00, 0.50, 25.00),
    "opus-4.1": (15.00, 18.75, 30.00, 1.50, 75.00),
    "opus-4": (15.00, 18.75, 30.00, 1.50, 75.00),
    # Sonnet family
    "sonnet-4.6": (3.00, 3.75, 6.00, 0.30, 15.00),
    "sonnet-4.5": (3.00, 3.75, 6.00, 0.30, 15.00),
    "sonnet-4": (3.00, 3.75, 6.00, 0.30, 15.00),
    # Haiku family
    "haiku-4.5": (1.00, 1.25, 2.00, 0.10, 5.00),
    "haiku-3.5": (0.80, 1.00, 1.60, 0.08, 4.00),
    # GPT / OpenAI
    "gpt-5.5": (1.25, 1.25, 1.25, 0.125, 10.00),
    "gpt-5.4": (1.25, 1.25, 1.25, 0.125, 10.00),
    "gpt-5-mini": (0.25, 0.25, 0.25, 0.025, 2.00),
    "gpt-5": (1.25, 1.25, 1.25, 0.125, 10.00),
    "gpt-4.1": (2.00, 2.00, 2.00, 0.500, 8.00),
    "gpt-4o": (2.50, 2.50, 2.50, 1.250, 10.00),
    "o3": (2.00, 2.00, 2.00, 0.500, 8.00),
    "o4-mini": (1.10, 1.10, 1.10, 0.275, 4.40),
}

DEFAULT_HARDCODED = HARDCODED_PRICES["sonnet-4.6"]


def _load_hardcoded_db() -> PriceDatabase:
    """Create a PriceDatabase from hardcoded prices."""
    db = PriceDatabase()
    for name, (inp, cw5, cw1, cr, out) in HARDCODED_PRICES.items():
        db.prices[name] = ModelPricing(inp, cw5, cw1, cr, out)
    db.source = "hardcoded"
    return db


# ─── API fetching ──────────────────────────────────────────────────────────

def _get_api_key() -> Optional[str]:
    """Get OpenRouter API key from env or config."""
    if k := os.environ.get("OPENROUTER_API_KEY"):
        return k
    from watchmen.config import read_env_var
    return read_env_var("OPENROUTER_API_KEY")


def _normalize_model_name(api_name: str) -> str:
    """Normalize OpenRouter model name to our key format.

    OpenRouter returns names like:
      - "anthropic/claude-opus-4-7" → "opus-4.7"
      - "openai/gpt-5" → "gpt-5"
      - "deepseek/deepseek-v4-flash" → "deepseek-v4-flash"
    """
    # Strip provider prefix (everything before first /)
    if "/" in api_name:
        api_name = api_name.split("/", 1)[1]

    # Convert dashes to dots for version matching (claude-opus-4-7 → opus-4.7)
    # But keep "deepseek-v4-flash" as-is (already has dash)
    if api_name.startswith("claude-"):
        parts = api_name.split("-")
        if len(parts) >= 3:
            # claude-opus-4-7 → opus-4.7
            return f"{parts[1]}-{parts[2]}.{parts[3]}" if len(parts) > 3 else f"{parts[1]}-{parts[2]}"
    elif api_name.startswith("gpt-"):
        # gpt-5-5-mini → gpt-5.5-mini (but our keys are gpt-5.5, gpt-5-mini)
        parts = api_name.split("-")
        if len(parts) >= 2:
            return api_name  # keep as-is, match against gpt-5, gpt-5-mini, etc.

    return api_name


def _parse_pricing(pricing: dict) -> Optional[ModelPricing]:
    """Parse pricing from OpenRouter response.

    OpenRouter returns pricing as strings like "$3.00" per million tokens.
    We need: input, cache_write_5m, cache_write_1h, cache_read, output.
    """
    try:
        prompt = float(pricing.get("prompt", "0").replace("$", "").replace(",", ""))
        completion = float(pricing.get("completion", "0").replace("$", "").replace(",", ""))

        # Cache pricing: OpenRouter returns separate fields
        # If not available, approximate: cache_write ≈ input, cache_read ≈ input * 0.1
        cache_write_5m = float(pricing.get("cache_creation_input", pricing.get("cache_write", prompt)))
        cache_write_1h = float(pricing.get("cache_creation_input_1h", cache_write_5m))
        cache_read = float(pricing.get("cache_read_input", prompt * 0.1))

        return ModelPricing(prompt, cache_write_5m, cache_write_1h, cache_read, completion)
    except (ValueError, TypeError):
        return None


def _fetch_from_api() -> Optional[PriceDatabase]:
    """Fetch model prices from OpenRouter API."""
    api_key = _get_api_key()
    if not api_key:
        return None

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.get(
                OPENROUTER_MODELS_URL,
                headers={"Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
            data = response.json()
    except Exception as e:
        print(f"model_prices: API fetch failed: {e}", file=sys.stderr)
        return None

    db = PriceDatabase()
    for model in data.get("data", []):
        pricing = _parse_pricing(model.get("pricing", {}))
        if pricing:
            # Use canonical_slug if available, else id
            name = model.get("canonical_slug", model.get("id", ""))
            # Normalize for matching
            normalized = _normalize_model_name(name)
            db.prices[normalized] = pricing

    db.fetched_at = time.time()
    db.source = "api"
    return db


# ─── Cache management ──────────────────────────────────────────────────────

def _ensure_cache_dir() -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR


def _save_cache(db: PriceDatabase) -> Path:
    """Save price database to cache file."""
    _ensure_cache_dir()
    data = {
        "fetched_at": db.fetched_at,
        "source": db.source,
        "prices": {
            name: {
                "input": p.input,
                "cache_write_5m": p.cache_write_5m,
                "cache_write_1h": p.cache_write_1h,
                "cache_read": p.cache_read,
                "output": p.output,
            }
            for name, p in db.prices.items()
        },
    }
    CACHE_FILE.write_text(json.dumps(data, indent=2))
    return CACHE_FILE


def _load_cache() -> Optional[PriceDatabase]:
    """Load price database from cache file if fresh."""
    if not CACHE_FILE.exists():
        return None

    try:
        data = json.loads(CACHE_FILE.read_text())
        fetched_at = data.get("fetched_at", 0)

        # Check TTL
        if time.time() - fetched_at > CACHE_TTL:
            return None

        db = PriceDatabase()
        db.fetched_at = fetched_at
        db.source = data.get("source", "cache")
        for name, p in data.get("prices", {}).items():
            db.prices[name] = ModelPricing(
                p["input"],
                p["cache_write_5m"],
                p["cache_write_1h"],
                p["cache_read"],
                p["output"],
            )
        return db
    except Exception:
        return None


# ─── Public API ────────────────────────────────────────────────────────────

def get_price_database() -> PriceDatabase:
    """Get price database: try cache → API → hardcoded fallback."""

    # 1. Try cache first (fast, no network)
    if db := _load_cache():
        return db

    # 2. Try API
    if db := _fetch_from_api():
        _save_cache(db)
        return db

    # 3. Fallback to hardcoded
    db = _load_hardcoded_db()
    db.source = "hardcoded"
    return db


def price_for_model(model: str | None) -> tuple[float, float, float, float, float]:
    """Get price tuple for a model name. Falls back to family defaults.
    
    Uses exact name matching with normalization that converts dash-formatted
    API names to dot-formatted price keys (e.g., 'claude-opus-4-7' -> 'opus-4.7').
    Falls back to family patterns for unknown models.
    """
    if not model:
        return DEFAULT_HARDCODED

    # Step 1 — normalize to our canonical key shape and try an exact lookup
    # against the hardcoded table. This is the AUTHORITATIVE path for every
    # Claude / OpenAI model we explicitly support, and it runs first because
    # the substring fallback in step 2 (against the API-fetched db, which
    # may not even be populated) cannot tell `opus-4.7` from `opus-4` when
    # the query string uses dashes instead of dots — picking the wrong one
    # under-bills Opus 4.7 / over-bills Opus 4 by 3×. Bug caught in CI on
    # PR #26 when the API cache was empty and substring match resolved
    # "claude-opus-4-7" → "opus-4" instead of "opus-4.7".
    normalized = _normalize_exact_name(model)
    if normalized in HARDCODED_PRICES:
        return HARDCODED_PRICES[normalized]

    # Step 2 — fuzzy lookup against the (possibly OpenRouter-populated)
    # database. Useful for models we don't have hardcoded yet but the API
    # knows about.
    db = get_price_database()
    pricing = db.find_price(model)
    if pricing:
        return pricing.as_tuple()

    # Step 3 — family-pattern fallback for unknown future variants
    # (e.g. claude-opus-99-99). Worst case we over-bill by mapping to the
    # current generation's price; never silently zero.
    m = model.lower()
    if "opus" in m:
        return HARDCODED_PRICES["opus-4.7"]
    if "sonnet" in m:
        return HARDCODED_PRICES["sonnet-4.6"]
    if "haiku" in m:
        return HARDCODED_PRICES["haiku-4.5"]
    if "gpt-5" in m or m.startswith("o4"):
        return HARDCODED_PRICES["gpt-5"]
    if "gpt-4" in m or m.startswith("o3"):
        return HARDCODED_PRICES["gpt-4.1"]

    return DEFAULT_HARDCODED


def _normalize_exact_name(model_name: str) -> str:
    """Normalize model name to exact price key format.
    
    Converts dash-formatted API names to dot-formatted keys:
    - claude-opus-4-7 -> opus-4.7
    - claude-sonnet-4-6 -> sonnet-4.6  
    - claude-haiku-4-5 -> haiku-4.5
    - claude-opus-4 -> opus-4.1 (fallback to 4.1 for major version only)
    - gpt-5-5 -> gpt-5.5 (if normalized form exists)
    """
    model_lower = model_name.lower()
    
    # Handle Claude models: extract model family and version
    if model_lower.startswith("claude-"):
        parts = model_lower.split("-")
        if len(parts) >= 4:  # claude-family-version-subversion
            family = parts[1]
            major, minor = parts[2], parts[3]
            if family in ["opus", "sonnet", "haiku"]:
                # Convert to dot format: opus-4-7 -> opus-4.7
                return f"{family}-{major}.{minor}"
        elif len(parts) == 3:  # claude-family-version (no minor)
            family = parts[1]
            major = parts[2]
            if family == "opus":
                # opus-4 -> opus-4.1 (older, more expensive tier)
                return f"{family}-{major}.1"
            elif family in ["sonnet", "haiku"]:
                # sonnet-4 -> sonnet-4.6 (current tier)
                return f"{family}-{major}.6"
    
    # Handle GPT models: try to match normalized versions
    if model_lower.startswith("gpt-"):
        parts = model_lower.split("-")
        if len(parts) >= 3:  # gpt-major-minor
            major, minor = parts[1], parts[2]
            # Check if normalized version exists in our price keys
            normalized = f"gpt-{major}.{minor}"
            if normalized in HARDCODED_PRICES:
                return normalized
            # Check for mini variants
            if len(parts) >= 4 and parts[3] == "mini":
                mini_normalized = f"gpt-{major}.{minor}-mini"
                if mini_normalized in HARDCODED_PRICES:
                    return mini_normalized
    
    # Return original if no normalization needed
    return model_lower


def turn_cost_usd(
    model: str | None,
    input_tokens: int,
    cache_creation_5m: int,
    cache_creation_1h: int,
    cache_read: int,
    output_tokens: int,
) -> float:
    """Cost for one assistant turn, in USD."""
    p = price_for_model(model)
    return (
        input_tokens * p[0]
        + cache_creation_5m * p[1]
        + cache_creation_1h * p[2]
        + cache_read * p[3]
        + output_tokens * p[4]
    ) / 1_000_000


# ─── CLI for testing ───────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("usage: python model_prices.py <model_name>")
        print("       python model_prices.py --list")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "--list":
        db = get_price_database()
        print(f"Source: {db.source}, fetched: {time.ctime(db.fetched_at) if db.fetched_at else 'never'}")
        print(f"Models: {len(db.prices)}")
        for name in sorted(db.prices):
            p = db.prices[name]
            print(f"  {name}: in=${p.input:.2f} out=${p.output:.2f} cache5m=${p.cache_write_5m:.2f} cache1h=${p.cache_write_1h:.2f} read=${p.cache_read:.2f}")
    else:
        model = sys.argv[1]
        price = price_for_model(model)
        print(f"{model}: {price}")
