# decision_engine/recommender_db.py
"""
AI Tool Recommender using PostgreSQL database.
This replaces the CSV-based recommender with database queries.
"""

import difflib
import hashlib
import json
import logging
import os
import pickle
import re
import sys
import threading
from datetime import datetime, timedelta
from typing import Any, Optional

import numpy as np
import pandas as pd
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity
from sqlalchemy.orm import Session

# Set up logging (cloud-friendly: logs to stdout)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# Initialize the sentence-transformers model
try:
    model = SentenceTransformer("all-MiniLM-L6-v2")
    logger.info("SentenceTransformer model initialized")
except Exception as e:
    logger.error(f"Error initializing SentenceTransformer: {e}")
    raise

# agentic_analyzer.py runs one recommend_tools() call per action plan
# concurrently (asyncio.gather + asyncio.to_thread, one OS thread per plan),
# and every one of them calls into this single shared `model` object. The
# underlying HuggingFace fast tokenizer isn't safe for concurrent use across
# threads — two encode() calls landing at the same instant raise a Rust-level
# "Already borrowed" panic, which callers below catch and silently treat as
# "no candidates for this plan" (the same failure mode the Session race in
# get_recommender() had, just one layer down). Serializing access to `model`
# is cheap — a single encode() call is a few milliseconds — and removes the
# race entirely.
_model_lock = threading.Lock()

# find_tool_by_name() runs concurrently too — agentic_analyzer.py resolves
# every cited/user-named tool via asyncio.gather + asyncio.to_thread (one OS
# thread per name), and every one of them queries the SAME request-scoped
# db_session (unlike recommend_tools(), whose singleton loads its own
# dedicated session once at startup and never touches the caller's session
# again). SQLAlchemy Session objects aren't safe for concurrent use from
# multiple threads — two queries landing on the same session at once raise
# "This session is provisioning a new connection; concurrent operations are
# not permitted" (sqlalche.me/e/20/isce). Same fix as _model_lock above:
# serialize access. A name lookup is a handful of milliseconds even with the
# substring/fuzzy fallback below, so serializing costs nothing.
_db_lock = threading.Lock()


class AIToolRecommender:
    """
    AI Tool recommendation engine using PostgreSQL.
    Generates embeddings and finds similar tools based on semantic similarity.
    Includes caching for embeddings to improve performance.
    """

    # Cache settings
    CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
    EMBEDDINGS_CACHE_FILE = os.path.join(CACHE_DIR, "tool_embeddings.pkl")

    def __init__(self, db_session: Session, use_cache: bool = True):
        """
        Initialize recommender with database session.

        Args:
            db_session: SQLAlchemy database session
            use_cache: Whether to use cached embeddings (default: True)
        """
        self.db = db_session
        self.tools_df = None
        self.embeddings = None
        self.use_cache = use_cache
        self.last_loaded: Optional[datetime] = None

        # Create cache directory if it doesn't exist
        os.makedirs(self.CACHE_DIR, exist_ok=True)

        self._load_tools()

    def _get_data_hash(self, tools_data: list[dict]) -> str:
        """
        Generate hash of tool data to detect changes.

        Args:
            tools_data: List of tool dictionaries

        Returns:
            MD5 hash of the data
        """
        # Include composite fields in hash so embedding strategy changes invalidate cache
        data_str = str(sorted([
            (
                t["id"],
                t["name"],
                (t.get("description") or "")[:50],
                (str(t.get("key_features") or ""))[:30],
                (str(t.get("who_should_use") or ""))[:30],
            )
            for t in tools_data
        ])) + ":v2"  # version tag — bump when embedding strategy changes
        return hashlib.md5(data_str.encode()).hexdigest()

    def _is_cache_valid(self) -> bool:
        """
        Check if a cache file exists to try loading.

        Freshness itself is decided by _load_tools()'s data-hash comparison,
        not by file age: this used to also reject any cache file older than a
        hardcoded 24h, regardless of whether the underlying tool data had
        actually changed. That was harmless when _load_tools() only
        ever ran once per process (the original design), but get_recommender()
        now calls refresh() periodically to self-heal stale tool URLs/metadata
        without a redeploy (see _RECOMMENDER_METADATA_REFRESH) — combined with
        the 24h age gate, that turned into a recurring, fully-synchronous,
        multi-minute re-embedding of the ENTIRE ~1700-tool catalog roughly
        once a day purely because the cache file had gotten "old", even when
        no tool's data had changed at all. That rebuild ran inside
        _recommender_lock, so it blocked every concurrent tool search for its
        full duration — the actual cause of an incident where every analysis
        in-flight during that window got zero tool recommendations. The hash
        check below is the real, content-based signal for "did anything
        change"; age no longer overrides it.

        Returns:
            True if a cache file exists to attempt loading.
        """
        return os.path.exists(self.EMBEDDINGS_CACHE_FILE)

    def _load_from_cache(self):
        """
        Load embeddings from cache file.

        Returns:
            Tuple of (tools_df, embeddings, data_hash) or None if cache invalid
        """
        try:
            with open(self.EMBEDDINGS_CACHE_FILE, "rb") as f:
                cache_data = pickle.load(f)

            logger.info(f"✅ Loaded embeddings from cache ({len(cache_data['embeddings'])} tools)")
            return cache_data["tools_df"], cache_data["embeddings"], cache_data["data_hash"]

        except Exception as e:
            logger.warning(f"Failed to load cache: {e}")
            return None

    def _save_to_cache(self, tools_df, embeddings, data_hash):
        """
        Save embeddings to cache file.

        Args:
            tools_df: DataFrame of tools
            embeddings: Numpy array of embeddings
            data_hash: Hash of the data
        """
        try:
            cache_data = {
                "tools_df": tools_df,
                "embeddings": embeddings,
                "data_hash": data_hash,
                "timestamp": datetime.now().isoformat(),
            }

            with open(self.EMBEDDINGS_CACHE_FILE, "wb") as f:
                pickle.dump(cache_data, f)

            logger.info(f"💾 Saved embeddings to cache ({len(embeddings)} tools)")

        except Exception as e:
            logger.error(f"Failed to save cache: {e}")

    def _load_tools(self):
        """
        Load tools from database and generate/load embeddings.
        Uses caching to avoid regenerating embeddings on every restart.
        """
        from database.pg_connections import SessionLocal
        from database.pg_models import AITool

        # This instance is a process-wide singleton (see get_recommender)
        # reused across every request for the rest of the process's life —
        # querying through the caller's own request-scoped self.db would tie
        # this long-lived object to whichever request happened to trigger the
        # first load, and that session may already be closed by the time a
        # later .refresh() runs. Open a short-lived session of our own instead.
        session = SessionLocal()
        try:
            # Query all tools from database
            tools = session.query(AITool).all()

            if not tools:
                logger.warning("No tools found in database. Run migration script first.")
                self.tools_df = pd.DataFrame()
                self.embeddings = np.array([])
                return

            # Convert to DataFrame for easier processing
            tools_data = []
            for tool in tools:
                tools_data.append(
                    {
                        "id": tool.id,
                        "name": tool.name,
                        "description": tool.description,
                        "main_category": tool.main_category,
                        "sub_category": tool.sub_category,
                        "pricing": tool.pricing,
                        "ratings": tool.ratings,
                        "key_features": tool.key_features,
                        "pros": tool.pros,
                        "cons": tool.cons,
                        "who_should_use": tool.who_should_use,
                        "compatibility_integration": tool.compatibility_integration,
                        "url": tool.url or "",
                    }
                )

            tools_df = pd.DataFrame(tools_data)
            logger.info(f"Loaded {len(tools_df)} tools from database")

            # Calculate data hash to detect changes
            current_hash = self._get_data_hash(tools_data)

            # Try to use cache if enabled
            if self.use_cache and self._is_cache_valid():
                cached_data = self._load_from_cache()

                if cached_data is not None:
                    cached_df, cached_embeddings, cached_hash = cached_data

                    # Verify data hasn't changed
                    if cached_hash == current_hash and len(cached_df) == len(tools_df):
                        self.tools_df = tools_df  # Use fresh data from DB
                        self.embeddings = cached_embeddings  # Use cached embeddings
                        self.last_loaded = datetime.now()
                        logger.info("🚀 Using cached embeddings (data unchanged)")
                        return
                    else:
                        logger.info("Data changed, regenerating embeddings...")

            # Generate new embeddings (cache miss or disabled)
            logger.info("Generating embeddings... (this may take a moment)")
            # Composite text gives the semantic search more signal:
            # tool description alone is too generic when action plans are
            # semantically similar — adding key_features and who_should_use
            # steers the embedding toward the tool's actual use-case niche.
            composite_texts: list[str] = []
            for _, row in tools_df.iterrows():
                parts = [str(row.get("description") or "").strip()]
                features = str(row.get("key_features") or "").strip()
                who = str(row.get("who_should_use") or "").strip()
                if features and features not in ("[", "[]"):
                    parts.append(features[:250])
                if who and who not in ("[", "[]"):
                    parts.append(who[:150])
                composite_texts.append(" ".join(parts))
            # Chunked rather than one encode() call over the whole catalog:
            # _model_lock also gates every live single-query embedding at
            # request time (see its docstring above), sized on the
            # assumption that a lock holder is "a few milliseconds" — true
            # for a single query, false for ~1700 tools at once (a full pass
            # takes minutes). Encoding in small chunks and releasing the lock
            # between them means a concurrent live query waits at most one
            # chunk's worth of time instead of the whole multi-minute pass —
            # this is what actually fixed an incident where a background
            # catalog refresh (see get_recommender's _background_refresh)
            # blocked every concurrent tool search for its full duration.
            _EMBED_CHUNK_SIZE = 64
            embedding_chunks = []
            for i in range(0, len(composite_texts), _EMBED_CHUNK_SIZE):
                chunk = composite_texts[i : i + _EMBED_CHUNK_SIZE]
                with _model_lock:
                    embedding_chunks.append(
                        model.encode(chunk, convert_to_tensor=False, show_progress_bar=False)
                    )
            embeddings = np.concatenate(embedding_chunks, axis=0) if embedding_chunks else np.array([])

            self.tools_df = tools_df
            self.embeddings = embeddings
            self.last_loaded = datetime.now()

            logger.info(f"✅ Generated embeddings for {len(embeddings)} tools")

            # Save to cache for next time
            if self.use_cache:
                self._save_to_cache(tools_df, embeddings, current_hash)

        except Exception as e:
            logger.error(f"Error loading tools from database: {e}")
            raise
        finally:
            session.close()

    def recommend(self, user_query: str, top_k: int = 5) -> list[dict]:
        """
        Recommend top_k AI tools based on cosine similarity with user query.

        Args:
            user_query: User input describing their needs
            top_k: Number of recommendations to return

        Returns:
            List of dicts with tool_name, similarity_score, and description
        """
        try:
            if self.tools_df.empty:
                logger.warning("No tools available for recommendations")
                return []

            # Generate embedding for user query
            with _model_lock:
                query_embedding = model.encode([user_query], convert_to_tensor=False)[0]

            # Compute cosine similarity
            similarities = cosine_similarity([query_embedding], self.embeddings)[0]

            # Get top_k indices and scores
            top_indices = np.argsort(similarities)[::-1][:top_k]
            top_scores = [float(score) for score in similarities[top_indices]]

            # Map to tool details
            recommendations = []
            for idx, i in enumerate(top_indices):
                tool = self.tools_df.iloc[i]
                recommendations.append(
                    {
                        "tool_name": tool["name"],
                        "similarity_score": top_scores[idx],
                        "description": tool["description"],
                        "url": tool.get("url", ""),
                        # Already loaded into tools_df and already used to build
                        # this tool's retrieval embedding (see _load_tools) —
                        # surfacing them here too lets a downstream scoring LLM
                        # (agentic_analyzer.py::_score_plan) defend a relevance
                        # score against real documented features instead of a
                        # bare description. Deliberately excludes pricing.
                        "key_features": tool.get("key_features"),
                        "who_should_use": tool.get("who_should_use"),
                        "compatibility_integration": tool.get("compatibility_integration"),
                    }
                )

            logger.info(f"Generated {len(recommendations)} recommendations for: '{user_query}'")
            return recommendations

        except Exception as e:
            logger.error(f"Error in recommend: {e}")
            raise

    def refresh(self, clear_cache: bool = True):
        """
        Refresh tools from database (call after adding new tools).

        Args:
            clear_cache: Whether to clear the embedding cache (default: True)
        """
        logger.info("Refreshing tool data from database...")

        if clear_cache and os.path.exists(self.EMBEDDINGS_CACHE_FILE):
            os.remove(self.EMBEDDINGS_CACHE_FILE)
            logger.info("Cleared embedding cache")

        self._load_tools()

    def clear_cache(self):
        """Manually clear the embedding cache."""
        if os.path.exists(self.EMBEDDINGS_CACHE_FILE):
            os.remove(self.EMBEDDINGS_CACHE_FILE)
            logger.info("✅ Embedding cache cleared")
        else:
            logger.info("No cache to clear")


# The singleton below is built once and then reused for the rest of the
# process's life (see get_recommender) — _load_tools() was never called
# again after that first build, so any DB edit to a tool's metadata (e.g. a
# corrected `url` after scripts/fix_tool_urls.py ran) stayed invisible to
# every request served by this process until it happened to restart. The
# embeddings themselves are the expensive part (a full SentenceTransformer
# encode() pass over ~1700 tools) and are already protected from needless
# regeneration by the data-hash check inside _load_tools() — so refreshing
# tools_df periodically is cheap in the common case (DB requery + hash
# match, embeddings reused) and self-heals metadata drift without a deploy.
_RECOMMENDER_METADATA_REFRESH = timedelta(hours=1)

# Global recommender instance (initialized when first needed)
_recommender_instance = None
# Guards singleton construction. agentic_analyzer.py fires one
# recommend_tools() call per action plan concurrently via asyncio.gather, each
# on its own thread (asyncio.to_thread) — on a cold instance (process start,
# or right after a worker restart), those threads used to race straight into
# AIToolRecommender(db_session), all querying the SAME caller-supplied
# SQLAlchemy Session at once. SQLAlchemy Sessions aren't safe for concurrent
# use across threads, so every racing thread but one raised "This session is
# provisioning a new connection; concurrent operations are not permitted",
# recommend_tools() silently swallowed it and returned [], and 2 of 3 plans
# lost their entire real candidate pool on every cold start — the actual
# cause of the "only 1 of N steps got a tool" sparsity, not an LLM/scoring
# weakness. The lock makes only the first thread build the singleton; the
# rest block until it's ready, then reuse it — no more concurrent access to
# a shared Session during that first load. _load_tools() also now opens its
# own dedicated session (see above) so even the winning thread no longer
# touches the caller's session at all.
_recommender_lock = threading.Lock()

# Guards the periodic background refresh below (a completely separate
# concern from _recommender_lock, which only guards one-time singleton
# construction). acquire(blocking=False) here doubles as an "already
# refreshing" flag: whichever request first notices staleness wins the
# acquire and spawns the background thread; every other concurrent request
# in that window fails the non-blocking acquire and just moves on with the
# existing (still-usable, at most slightly stale) instance rather than
# piling up duplicate refreshes or waiting on this one.
#
# This refresh must run in the background, never inline on a request: when
# the catalog's data hash has genuinely changed (a tool added/edited/
# removed), _load_tools() does a real SentenceTransformer encode() pass over
# the ~1700-tool catalog, which takes minutes. Running that inline while
# holding a shared lock previously blocked every concurrent tool search for
# the whole duration — the cause of an incident where every analysis
# in-flight during that window returned zero tool recommendations.
_refresh_lock = threading.Lock()


def _background_refresh(instance: "AIToolRecommender") -> None:
    try:
        # clear_cache=False: keep cached embeddings unless the data hash
        # actually changed — _load_tools() already regenerates them itself
        # when it does.
        instance.refresh(clear_cache=False)
    except Exception:
        logger.exception("Periodic tool-catalog refresh failed; serving existing data")
    finally:
        _refresh_lock.release()


def get_recommender(db_session: Session) -> AIToolRecommender:
    """
    Get or create recommender instance.
    Uses singleton pattern to avoid reloading embeddings.

    Args:
        db_session: Database session

    Returns:
        AIToolRecommender instance
    """
    global _recommender_instance

    if _recommender_instance is None:
        with _recommender_lock:
            if _recommender_instance is None:  # re-check: lost the race while waiting
                _recommender_instance = AIToolRecommender(db_session)
        return _recommender_instance

    # Metadata staleness check — see _RECOMMENDER_METADATA_REFRESH comment.
    # Cheap in the common case: just a datetime comparison. The refresh
    # itself always runs in a background thread (see _refresh_lock above) so
    # this call never blocks waiting on it — the caller gets the existing
    # instance immediately either way.
    last_loaded = _recommender_instance.last_loaded
    if last_loaded is None or datetime.now() - last_loaded > _RECOMMENDER_METADATA_REFRESH:
        if _refresh_lock.acquire(blocking=False):
            threading.Thread(
                target=_background_refresh, args=(_recommender_instance,), daemon=True
            ).start()

    return _recommender_instance


def recommend_tools(user_query: str, top_k: int = 5, db_session: Session = None) -> list[dict]:
    """
    Convenience function for tool recommendations.

    Args:
        user_query: User input describing their needs
        top_k: Number of recommendations
        db_session: Database session (required)

    Returns:
        List of tool recommendations
    """
    if db_session is None:
        raise ValueError("Database session is required")

    recommender = get_recommender(db_session)
    return recommender.recommend(user_query, top_k)


def find_tool_by_name(name: str, db_session: Session) -> Optional[dict]:
    """Exact (case-insensitive) name lookup, bypassing semantic search, with a
    substring/fuzzy fallback when nothing matches exactly.

    Used for tools the user names directly in their prompt — semantic search
    over an action *description* can miss a specific named product entirely,
    but the user already told us exactly which tool they mean, so look it up
    directly instead of hoping retrieval surfaces it.

    A citation frequently doesn't match a catalog name verbatim (e.g. "Notion"
    vs. the catalog's "Notion AI", or "Google Sheet" vs. "Google Sheets") —
    without a fallback those resolve to nothing and the caller falls back
    further to an ungrounded stub (no real url/description). Substring
    containment (either direction) catches brand-vs-product-line mismatches;
    difflib catches typos/near-misses. Both still only ever return a real
    catalog row, never a fabricated one.
    """
    from database.pg_models import AITool

    if not name or not name.strip():
        return None
    query = name.strip()

    # Whole lookup (all queries below) is one critical section — see _db_lock.
    with _db_lock:
        tool = (
            db_session.query(AITool)
            .filter(AITool.name.ilike(query))
            .first()
        )

        if not tool and len(query) >= 3:
            query_lower = query.lower()
            all_tools = db_session.query(AITool.id, AITool.name).all()

            substring_matches = [
                t for t in all_tools
                if query_lower in t.name.lower() or t.name.lower() in query_lower
            ]
            if substring_matches:
                best = min(substring_matches, key=lambda t: abs(len(t.name) - len(query)))
                tool = db_session.query(AITool).get(best.id)
            else:
                close = difflib.get_close_matches(
                    query, [t.name for t in all_tools], n=1, cutoff=0.82
                )
                if close:
                    tool = db_session.query(AITool).filter(AITool.name == close[0]).first()

    if not tool:
        return None

    return {
        "tool_name": tool.name,
        "similarity_score": 1.0,
        "description": tool.description,
        "url": tool.url or "",
        "key_features": tool.key_features,
        "who_should_use": tool.who_should_use,
        "compatibility_integration": tool.compatibility_integration,
    }


def compile_catalog_name_pattern(catalog_names: list[str]) -> Optional["re.Pattern"]:
    """Build one compiled regex matching any real catalog tool name as a
    whole word/phrase, case-insensitively. Build ONCE per analysis (catalog
    is ~1700 names — compiling per step or per plan would be wasteful) and
    reuse across every plan via find_catalog_names_in_text below.

    Names are sorted longest-first so a more specific name wins over a
    shorter one it contains (e.g. "Notion AI" matches before bare "Notion"
    when both are present at the same text position) — alternation in `re`
    takes the first alternative that matches, not the longest, so ordering
    is what makes this deterministic rather than order-of-insertion luck.
    Names under 4 characters are dropped: a short catalog name (e.g. a
    2-3 letter brand) risks matching generic words/substrings inside
    unrelated step text, which is a worse failure mode than an occasional
    miss on a very short name.
    """
    usable = sorted((n for n in catalog_names if n and len(n) >= 4), key=len, reverse=True)
    if not usable:
        return None
    pattern = r'\b(' + '|'.join(re.escape(n) for n in usable) + r')\b'
    return re.compile(pattern, re.IGNORECASE)


def find_catalog_names_in_text(text: str, compiled_pattern: Optional["re.Pattern"]) -> list[str]:
    """Deterministic backstop for _extract_mentioned_tools (an LLM call that
    silently returns [] on any failure, with no retry): a plain regex scan
    of `text` against every real catalog name via compiled_pattern (see
    compile_catalog_name_pattern). This never depends on the LLM correctly
    judging what counts as a "specific named product" — if a catalog tool's
    exact name literally appears in the step text, it's flagged, full stop.
    Pure in-memory string matching, no DB access — safe to call from
    anywhere, including inside a concurrent asyncio.gather.
    """
    if not text or compiled_pattern is None:
        return []
    seen_lower: set[str] = set()
    found: list[str] = []
    for m in compiled_pattern.finditer(text):
        matched = m.group(0)
        key = matched.lower()
        if key not in seen_lower:
            seen_lower.add(key)
            found.append(matched)
    return found


def _safe_parse_text_list(value: Any) -> list[str]:
    """Parse semi-structured text/json fields into a normalized string list."""
    if value is None:
        return []

    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]

    if isinstance(value, str):
        text_value = value.strip()
        if not text_value:
            return []

        # Try JSON list first
        if text_value.startswith("[") and text_value.endswith("]"):
            try:
                parsed = json.loads(text_value)
                if isinstance(parsed, list):
                    return [str(item).strip() for item in parsed if str(item).strip()]
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        # Split by common separators
        parts = re.split(r"\||,|;", text_value)
        return [part.strip() for part in parts if part.strip()]

    return []


def _normalize_tokens(values: list[str]) -> set[str]:
    """Normalize tokens for lightweight overlap-based compatibility scoring."""
    tokens: set[str] = set()
    for value in values:
        for token in re.findall(r"[a-zA-Z0-9\-\+]+", value.lower()):
            if len(token) >= 3:
                tokens.add(token)
    return tokens


def _compute_pair_compatibility(left_tool: dict, right_tool: dict) -> float:
    """Heuristic compatibility score between two tools in range [0, 1]."""
    left_name = str(left_tool.get("name", "")).lower()
    right_name = str(right_tool.get("name", "")).lower()
    if not left_name or not right_name:
        return 0.0

    left_integrations = _safe_parse_text_list(left_tool.get("compatibility_integration"))
    right_integrations = _safe_parse_text_list(right_tool.get("compatibility_integration"))
    left_integration_tokens = _normalize_tokens(left_integrations)
    right_integration_tokens = _normalize_tokens(right_integrations)

    left_use_cases = _normalize_tokens(_safe_parse_text_list(left_tool.get("who_should_use")))
    right_use_cases = _normalize_tokens(_safe_parse_text_list(right_tool.get("who_should_use")))

    score = 0.0

    # Explicit integration mention by name is a strong signal.
    if any(right_name in integration.lower() for integration in left_integrations):
        score += 0.35
    if any(left_name in integration.lower() for integration in right_integrations):
        score += 0.35

    # Shared integration ecosystem and use-case overlap are medium signals.
    if left_integration_tokens and right_integration_tokens:
        overlap = len(left_integration_tokens.intersection(right_integration_tokens))
        score += min(0.2, overlap * 0.05)

    if left_use_cases and right_use_cases:
        overlap = len(left_use_cases.intersection(right_use_cases))
        score += min(0.2, overlap * 0.05)

    # Similar category usually indicates easier workflow fit.
    if left_tool.get("main_category") and left_tool.get("main_category") == right_tool.get("main_category"):
        score += 0.1

    return max(0.0, min(score, 1.0))


def recommend_automation_stacks(
    user_query: str,
    action_plans: list[dict],
    top_k_stacks: int = 3,
    max_tools_per_stack: int = 4,
    db_session: Session = None,
) -> list[dict]:
    """
    Build ranked automation stacks (1-4 tools) from DB tools using semantic similarity + compatibility.

    Each stack is generated dynamically from the tool catalog currently stored in the database.
    """
    if db_session is None:
        raise ValueError("Database session is required")

    if not user_query.strip():
        return []

    recommender = get_recommender(db_session)
    if recommender.tools_df is None or recommender.tools_df.empty:
        return []

    max_tools_per_stack = max(1, min(max_tools_per_stack, 4))
    top_k_stacks = max(1, min(top_k_stacks, 3))

    tools_df = recommender.tools_df.reset_index(drop=True)
    if recommender.embeddings is None or len(recommender.embeddings) == 0:
        return []

    with _model_lock:
        query_embedding = model.encode([user_query], convert_to_tensor=False)[0]
    global_similarities = cosine_similarity([query_embedding], recommender.embeddings)[0]

    action_queries: list[tuple[int, str]] = []
    # Per-plan list of individual step strings, keyed by the same action_id
    # used above — lets a stack's coverage be pinned to the ONE step it best
    # matches (not just "this plan somewhere"), same embedding-similarity
    # method as the plan-level query, just run per step instead of on the
    # whole plan joined together.
    action_step_texts: dict[int, list[str]] = {}
    for plan in action_plans or []:
        title = str(plan.get("title", "")).strip()
        what_to_do = plan.get("what_to_do", [])
        steps_list = [str(s) for s in what_to_do] if isinstance(what_to_do, list) else ([str(what_to_do)] if what_to_do else [])
        steps_text = " ".join(steps_list)
        query = f"{title} {steps_text}".strip()
        if query:
            action_id = int(plan.get("id", len(action_queries) + 1))
            action_queries.append((action_id, query))
            action_step_texts[action_id] = steps_list

    # Gather candidate indices from global query + each action query to preserve semantic relevance.
    candidate_indices: set[int] = set(np.argsort(global_similarities)[::-1][:20].tolist())

    action_similarity_maps: dict[int, np.ndarray] = {}
    action_step_similarity_maps: dict[int, list[np.ndarray]] = {}
    for action_id, query in action_queries:
        with _model_lock:
            action_embedding = model.encode([query], convert_to_tensor=False)[0]
        action_sims = cosine_similarity([action_embedding], recommender.embeddings)[0]
        action_similarity_maps[action_id] = action_sims
        candidate_indices.update(np.argsort(action_sims)[::-1][:8].tolist())

        steps_list = action_step_texts.get(action_id) or []
        if steps_list:
            with _model_lock:
                step_embeddings = model.encode(steps_list, convert_to_tensor=False)
            action_step_similarity_maps[action_id] = [
                cosine_similarity([step_embedding], recommender.embeddings)[0]
                for step_embedding in step_embeddings
            ]

    if not candidate_indices:
        return []

    candidate_tools: list[dict] = []
    for index in sorted(candidate_indices):
        tool_row = tools_df.iloc[index]
        candidate_tools.append(
            {
                "index": index,
                "id": int(tool_row["id"]),
                "name": str(tool_row["name"]),
                "description": str(tool_row.get("description", "") or ""),
                "main_category": tool_row.get("main_category"),
                "sub_category": tool_row.get("sub_category"),
                "pricing": tool_row.get("pricing"),
                "ratings": float(tool_row.get("ratings") or 0.0),
                "key_features": tool_row.get("key_features"),
                "compatibility_integration": tool_row.get("compatibility_integration"),
                "who_should_use": tool_row.get("who_should_use"),
                "url": str(tool_row.get("url") or ""),
                "query_similarity": float(global_similarities[index]),
            }
        )

    candidate_tools.sort(key=lambda item: item["query_similarity"], reverse=True)
    if not candidate_tools:
        return []

    stack_candidates: list[dict] = []
    seen_signatures: set[tuple[int, ...]] = set()

    seed_count = min(8, len(candidate_tools))
    for seed in candidate_tools[:seed_count]:
        chosen: list[dict] = [seed]
        remaining = [tool for tool in candidate_tools if tool["id"] != seed["id"]]

        while len(chosen) < max_tools_per_stack and remaining:
            best_tool = None
            best_score = 0.0

            for tool in remaining:
                pair_scores = [_compute_pair_compatibility(tool, selected) for selected in chosen]
                compatibility_score = sum(pair_scores) / len(pair_scores) if pair_scores else 0.0
                combined_score = (0.7 * tool["query_similarity"]) + (0.3 * compatibility_score)
                if combined_score > best_score:
                    best_score = combined_score
                    best_tool = tool

            if best_tool is None or best_score < 0.45:
                break

            chosen.append(best_tool)
            remaining = [tool for tool in remaining if tool["id"] != best_tool["id"]]

        signature = tuple(sorted(tool["id"] for tool in chosen))
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)

        action_coverage: list[dict] = []
        for action_id, action_query in action_queries:
            sims = action_similarity_maps.get(action_id)
            if sims is None:
                continue
            match = max(float(sims[tool["index"]]) for tool in chosen)
            if match >= 0.45:
                # Which single step (within this plan) the stack's tools are
                # most semantically similar to, so the frontend can attach the
                # whole workflow inline under that step instead of showing it
                # as a plan-level footer. None if the plan has no step text or
                # no step clears the same 0.45 relevance bar used everywhere
                # else in this function.
                step_index: Optional[int] = None
                step_match_score: Optional[float] = None
                for i, step_sims in enumerate(action_step_similarity_maps.get(action_id) or []):
                    step_match = max(float(step_sims[tool["index"]]) for tool in chosen)
                    if step_match >= 0.45 and (step_match_score is None or step_match > step_match_score):
                        step_index = i
                        step_match_score = step_match
                action_coverage.append(
                    {
                        "action_id": action_id,
                        "action": action_query[:160],
                        "match_score": round(match, 3),
                        "step_index": step_index,
                        "step_match_score": round(step_match_score, 3) if step_match_score is not None else None,
                    }
                )

        pairwise_scores: list[float] = []
        for i in range(len(chosen)):
            for j in range(i + 1, len(chosen)):
                pairwise_scores.append(_compute_pair_compatibility(chosen[i], chosen[j]))

        compatibility_avg = sum(pairwise_scores) / len(pairwise_scores) if pairwise_scores else 0.0
        relevance_avg = sum(tool["query_similarity"] for tool in chosen) / len(chosen)
        coverage_bonus = min(0.25, 0.1 * len(action_coverage))
        complexity_penalty = 0.03 * max(0, len(chosen) - 3)

        stack_score = (0.65 * relevance_avg) + (0.25 * compatibility_avg) + coverage_bonus - complexity_penalty
        confidence = round(max(0.0, min(stack_score, 1.0)) * 100, 1)

        stack_name = f"{' + '.join(tool['name'] for tool in chosen)}"
        # summary and automation_logic are intentionally left empty here;
        # _enrich_single_stack will fill them with LLM-generated, action-specific text.
        effort = "Low" if len(chosen) == 1 else "Medium" if len(chosen) <= 3 else "High"

        stack_candidates.append(
            {
                "stack_name": stack_name,
                "summary": "",
                "score": round(stack_score, 4),
                "confidence": confidence,
                "estimated_effort": effort,
                "coverage_actions": action_coverage,
                "automation_logic": "",
                "tools": [
                    {
                        "tool_id": tool["id"],
                        "tool_name": tool["name"],
                        "description": tool["description"],
                        "key_features": tool.get("key_features"),
                        "compatibility_integration": tool.get("compatibility_integration"),
                        "main_category": tool.get("main_category"),
                        "sub_category": tool.get("sub_category"),
                        "pricing": tool.get("pricing"),
                        "ratings": tool.get("ratings"),
                        "url": tool.get("url", ""),
                        "similarity_score": round(tool["query_similarity"], 4),
                        "position": position + 1,
                    }
                    for position, tool in enumerate(chosen)
                ],
                # setup_order also left empty — LLM enrichment fills this with
                # specific "why set up X first" reasoning per user context.
                "setup_order": [],
            }
        )

    stack_candidates.sort(key=lambda item: item["score"], reverse=True)
    top_stacks = stack_candidates[:top_k_stacks]

    for idx, stack in enumerate(top_stacks, start=1):
        stack["stack_id"] = idx
        stack.pop("score", None)

    return top_stacks


