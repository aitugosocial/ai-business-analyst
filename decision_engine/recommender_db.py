# decision_engine/recommender_db.py
"""
AI Tool Recommender using PostgreSQL database.
This replaces the CSV-based recommender with database queries.
"""

import hashlib
import json
import logging
import os
import pickle
import re
import sys
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


class AIToolRecommender:
    """
    AI Tool recommendation engine using PostgreSQL.
    Generates embeddings and finds similar tools based on semantic similarity.
    Includes caching for embeddings to improve performance.
    """

    # Cache settings
    CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
    EMBEDDINGS_CACHE_FILE = os.path.join(CACHE_DIR, "tool_embeddings.pkl")
    CACHE_VALIDITY_HOURS = 24  # Refresh cache every 24 hours

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
        Check if cached embeddings are still valid.

        Returns:
            True if cache exists and is not expired
        """
        if not os.path.exists(self.EMBEDDINGS_CACHE_FILE):
            return False

        # Check file age
        cache_time = datetime.fromtimestamp(os.path.getmtime(self.EMBEDDINGS_CACHE_FILE))
        age = datetime.now() - cache_time

        if age > timedelta(hours=self.CACHE_VALIDITY_HOURS):
            logger.info(f"Cache expired (age: {age.total_seconds() / 3600:.1f} hours)")
            return False

        logger.info(f"Cache is valid (age: {age.total_seconds() / 3600:.1f} hours)")
        return True

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
        from database.pg_models import AITool

        try:
            # Query all tools from database
            tools = self.db.query(AITool).all()

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
            embeddings = model.encode(composite_texts, convert_to_tensor=False, show_progress_bar=True)

            self.tools_df = tools_df
            self.embeddings = embeddings

            logger.info(f"✅ Generated embeddings for {len(embeddings)} tools")

            # Save to cache for next time
            if self.use_cache:
                self._save_to_cache(tools_df, embeddings, current_hash)

        except Exception as e:
            logger.error(f"Error loading tools from database: {e}")
            raise

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


# Global recommender instance (initialized when first needed)
_recommender_instance = None


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
        _recommender_instance = AIToolRecommender(db_session)

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
        action_embedding = model.encode([query], convert_to_tensor=False)[0]
        action_sims = cosine_similarity([action_embedding], recommender.embeddings)[0]
        action_similarity_maps[action_id] = action_sims
        candidate_indices.update(np.argsort(action_sims)[::-1][:8].tolist())

        steps_list = action_step_texts.get(action_id) or []
        if steps_list:
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


