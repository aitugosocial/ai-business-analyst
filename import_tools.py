"""
import_tools.py — Import AI tools from a scraped CSV (e.g. aitugo/futurepedia export) into the Railway ai_tools table.

Usage:
  python import_tools.py                          # looks for the dated uuid csv or common names in cwd
  python import_tools.py /path/to/your-export.csv # explicit path (recommended, filenames change on re-download)

Target table columns (from database/pg_models.py AITool + live DB):
  name, url, description, summary,
  main_category, sub_category, ai_categories (json text),
  pricing, ratings (float),
  key_features (json text list), pros (json), cons (json),
  who_should_use (json), compatibility_integration (json),
  + auto timestamps + optional embedding (populated separately)

The recommender (recommender_db.py + agentic_analyzer) relies on good
description + key_features + who_should_use for semantic embeddings and
compatibility scoring in automation stacks. Leaving them NULL/empty means
weaker recommendations and less context passed to the LLM — so we map
whatever rich columns the source CSV provides.

CSV columns supported (case-insensitive match via .get):
  Basic (required for good results):
    "Tool Name", "Tool Website", "Description", "Excerpt",
    "AI Categories", "Pricing Model", "Rating"
  Rich (optional but HIGHLY recommended for accurate LLM recs; script will use if present):
    "Key Features", "Features", "Pros", "Cons", "Disadvantages",
    "Who Should Use", "Ideal For", "Target Audience",
    "Compatibility & Integration", "Integrations", "Works With", "Compatibility"
"""

import csv
import json
import os
import re
import sys

import psycopg2
from psycopg2.extras import execute_values

# ── Load DATABASE_URL from .env ─────────────────────────────────────────────
def load_env(path=".env"):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip("'\"")
    except FileNotFoundError:
        pass
    return env

env = load_env()
DATABASE_URL = env.get("DATABASE_URL") or os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    sys.exit("ERROR: DATABASE_URL not found in .env")

# Strip channel_binding parameter (Railway doesn't support it)
DATABASE_URL = re.sub(r"[&?]channel_binding=[^&]*", "", DATABASE_URL).rstrip("?&")

CSV_PATH = "2026-05-30-ai-tool-80ef0c79-4475-7ed1-1d58-7073b7dadafa.csv"


# ── HTML stripping ───────────────────────────────────────────────────────────
def strip_html(raw: str) -> str:
    """Remove HTML tags and decode common entities."""
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&nbsp;", " ").replace("&#8211;", "–").replace("&#8212;", "—")
    text = text.replace("&lsquo;", "'").replace("&rsquo;", "'")
    text = text.replace("&ldquo;", '"').replace("&rdquo;", '"')
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_list_field(raw: str) -> str | None:
    """
    Normalize a free-form 'features / pros / integrations' field into a JSON string list.
    Handles JSON arrays from source, pipe/ comma / newline separated text, or plain text.
    Returns None if empty so DB stays clean.
    """
    if not raw:
        return None
    text = strip_html(str(raw)).strip()
    if not text:
        return None

    # Already a JSON list?
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                cleaned = [str(x).strip() for x in parsed if str(x).strip()]
                return json.dumps(cleaned) if cleaned else None
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    # Split on common list separators (prefer | or newlines, then ; ,)
    separators = r"\s*\|\s*|\s*\n+\s*|\s*;\s*|\s*,\s*"
    parts = re.split(separators, text)
    cleaned = [p.strip() for p in parts if p and len(p.strip()) >= 1]
    if cleaned:
        return json.dumps(cleaned)

    # Fallback: single string as list of one
    return json.dumps([text]) if text else None


def parse_rating(raw: str) -> float:
    """Extract numeric rating (0-5) even if source says 'Rated 4.7 out of 5' or '4.5/5'."""
    if not raw:
        return 0.0
    match = re.search(r"(\d+(?:\.\d+)?)", str(raw))
    if match:
        try:
            val = float(match.group(1))
            return max(0.0, min(val, 5.0))
        except ValueError:
            pass
    return 0.0


# ── Category parser ───────────────────────────────────────────────────────────
def parse_categories(raw: str):
    """
    Split 'AI for Content Creation > AI Writing, AI for Marketing & Growth'
    into main_category, sub_category, and JSON array of all categories.
    """
    if not raw:
        return None, None, None

    parts = [p.strip() for p in raw.split(",") if p.strip()]
    main_cat = None
    sub_cat = None

    for part in parts:
        if ">" in part:
            segments = [s.strip() for s in part.split(">")]
            if main_cat is None:
                main_cat = segments[0]
            if sub_cat is None and len(segments) > 1:
                sub_cat = segments[1]
        else:
            if main_cat is None:
                main_cat = part

    return main_cat, sub_cat, json.dumps(parts) if parts else None


# ── Read and parse CSV ────────────────────────────────────────────────────────
def read_tools(path: str) -> list[dict]:
    tools = []
    seen_names: set[str] = set()

    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        # Normalize fieldnames for more forgiving matching (some exports have slight variations)
        field_map = { (k or "").strip().lower(): k for k in (reader.fieldnames or []) }

        def get_row(col_names: list[str]) -> str:
            for c in col_names:
                key = (c or "").strip().lower()
                if key in field_map:
                    return row.get(field_map[key]) or ""
                # also direct
                if c in row:
                    return row.get(c) or ""
            return ""

        for row in reader:
            name = (get_row(["Tool Name", "Name", "tool name", "name"]) or "").strip()
            if not name or name in seen_names:
                continue
            seen_names.add(name)

            url = (get_row(["Tool Website", "Website", "URL", "url", "Link"]) or "").strip() or None

            # Description: prefer the rich/long Description column; fall back to Excerpt
            raw_desc = get_row(["Description", "Long Description", "Full Description"]) or get_row(["Excerpt", "Short Description"])
            description = strip_html(raw_desc)
            if not description:
                description = f"{name} — an AI tool."

            # Summary: short excerpt, prefer explicit Excerpt if present
            raw_excerpt = get_row(["Excerpt", "Short Description", "Summary"])
            summary = strip_html(raw_excerpt)[:500] or None

            # Categories (supports the hierarchical "AI for X > Y" format)
            raw_cats = get_row(["AI Categories", "Categories", "Category", "ai_categories", "Tags"])
            main_cat, sub_cat, ai_cats_json = parse_categories(raw_cats)

            # Pricing
            pricing = (get_row(["Pricing Model", "Pricing", "Price", "pricing"]) or "").strip() or None

            # Rating — robust to "4.7", "Rated 4.7 out of 5", "4.5/5" etc.
            rating_raw = get_row(["Rating", "Ratings", "Score", "rating"])
            ratings = parse_rating(rating_raw)

            # === Rich fields (only populated if the source CSV actually contains them) ===
            # This is critical so that embeddings and LLM context in recommender / agentic_analyzer
            # have accurate key_features, use cases, and integrations instead of NULLs.
            key_features = parse_list_field(
                get_row(["Key Features", "Features", "Key Feature", "key_features"])
            )
            pros = parse_list_field(
                get_row(["Pros", "Advantages", "Benefits", "pros"])
            )
            cons = parse_list_field(
                get_row(["Cons", "Disadvantages", "Drawbacks", "Limitations", "cons"])
            )
            who = parse_list_field(
                get_row(["Who Should Use", "Ideal For", "Target Audience", "Target Users", "Best For", "who_should_use"])
            )
            compat = parse_list_field(
                get_row([
                    "Compatibility & Integration", "Integrations", "Works With",
                    "Compatibility", "Integration", "compatibility_integration"
                ])
            )

            tools.append({
                "name": name,
                "url": url,
                "description": description,
                "summary": summary,
                "main_category": main_cat,
                "sub_category": sub_cat,
                "ai_categories": ai_cats_json,
                "pricing": pricing,
                "ratings": ratings,
                "key_features": key_features,
                "pros": pros,
                "cons": cons,
                "who_should_use": who,
                "compatibility_integration": compat,
            })

    return tools


# ── Insert into database ──────────────────────────────────────────────────────
INSERT_SQL = """
INSERT INTO ai_tools (
    name, url, description, summary,
    main_category, sub_category, ai_categories,
    pricing, ratings,
    key_features, pros, cons,
    who_should_use, compatibility_integration
)
VALUES %s
ON CONFLICT (name) DO UPDATE SET
    url                       = EXCLUDED.url,
    description               = EXCLUDED.description,
    summary                   = EXCLUDED.summary,
    main_category             = EXCLUDED.main_category,
    sub_category              = EXCLUDED.sub_category,
    ai_categories             = EXCLUDED.ai_categories,
    pricing                   = EXCLUDED.pricing,
    ratings                   = EXCLUDED.ratings,
    key_features              = EXCLUDED.key_features,
    pros                      = EXCLUDED.pros,
    cons                      = EXCLUDED.cons,
    who_should_use            = EXCLUDED.who_should_use,
    compatibility_integration = EXCLUDED.compatibility_integration,
    updated_at                = NOW()
"""

def run_import(tools: list[dict]):
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    try:
        with conn:
            with conn.cursor() as cur:
                rows = [
                    (
                        t["name"], t["url"], t["description"], t["summary"],
                        t["main_category"], t["sub_category"], t["ai_categories"],
                        t["pricing"], t["ratings"],
                        t["key_features"], t["pros"], t["cons"],
                        t["who_should_use"], t["compatibility_integration"],
                    )
                    for t in tools
                ]
                execute_values(cur, INSERT_SQL, rows, page_size=200)
                cur.execute("SELECT COUNT(*) FROM ai_tools")
                total = cur.fetchone()[0]
        print(f"Import complete — {len(tools)} tools upserted. Total in DB: {total}")
    finally:
        conn.close()


if __name__ == "__main__":
    # Support explicit path (e.g. the dated download name changes every time)
    # or auto-discover common names when run from project root.
    if len(sys.argv) > 1:
        csv_path = sys.argv[1]
    else:
        candidates = [
            CSV_PATH,
            "ai-tools.csv", "tools.csv", "ai_tool_export.csv",
            "2026-05-30-ai-tool.csv",  # partial match fallback
        ]
        # Also pick up any csv whose name contains "ai" or "tool" (new downloads)
        try:
            for f in os.listdir("."):
                if f.lower().endswith(".csv") and ("ai" in f.lower() or "tool" in f.lower()):
                    candidates.append(f)
        except Exception:
            pass

        csv_path = None
        for cand in candidates:
            if cand and os.path.exists(cand):
                csv_path = cand
                break

        if not csv_path:
            sys.exit(
                f"ERROR: CSV file not found.\n"
                f"Provide it explicitly:\n"
                f"  python import_tools.py /path/to/2026-05-30-ai-tool-....csv\n"
                f"Or place a matching *.csv in the project root (cwd: {os.getcwd()})."
            )

    print(f"Reading tools from '{csv_path}' ...")
    tools = read_tools(csv_path)
    print(f"Parsed {len(tools)} unique tools. Importing / upserting into Railway ai_tools ...")
    run_import(tools)

    # Delete the stale embedding cache (used by recommender_db) so new/updated tools get fresh embeddings
    # that include key_features + who_should_use for better semantic matches.
    cache_path = "decision_engine/cache/tool_embeddings.pkl"
    if os.path.exists(cache_path):
        os.remove(cache_path)
        print("Embedding cache cleared — will regenerate on next analysis run (includes rich fields).")

    print("Done. If using DB-level pgvector embeddings, also consider running:")
    print("  python scripts/generate_tool_embeddings.py")
