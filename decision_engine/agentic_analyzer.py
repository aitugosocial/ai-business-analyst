# decision_engine/agentic_analyzer.py
"""
LAVOO Agentic Business Analyzer
AI-powered business analysis with bottleneck identification and strategic planning.

Architecture:
- Stage 1: Primary Bottleneck Agent (identifies THE critical constraint + consequences)
- Stage 2: Secondary Constraints Agent (finds 2-4 supporting issues)
- Stage 3: Action Plans Agent (generates ranked, leveraged action plans with toolkits)
- Stage 3B: Automation Stack Agent (composes multi-tool stacks from DB, LLM-enriched)
- Stage 4: Roadmap & Execution Agent (creates timeline + motivational quote)

OUTPUT FORMAT:
- Primary bottleneck (single, with impact/consequence)
- Secondary constraints (2-4 items)
- What to stop (critical action to discontinue)
- Strategic priority (main focus)
- Ranked action plans (ordered by leverage, with optional toolkits)
- Recommended tool stacks (1-4 tools each, LLM-reasoned workflow)
- Execution roadmap (phases with days and tasks)
- Exclusions note (what was intentionally excluded)
- LLM-generated motivational quote
"""

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from sqlalchemy.orm import Session

from decision_engine.recommender_db import recommend_automation_stacks, recommend_tools

load_dotenv(".env.local")

try:
    from openai import AsyncOpenAI
except ImportError as exc:
    raise RuntimeError(
        "openai package not installed — run: uv pip install openai"
    ) from exc

logger = logging.getLogger(__name__)


def _repair_json(text: str) -> str:
    """
    Attempt to repair JSON that was truncated mid-stream by the LLM.
    Closes any open strings and unclosed brackets/braces so json.loads can parse it.
    """
    in_string = False
    escape_next = False
    stack: list[str] = []

    for ch in text:
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if not in_string:
            if ch in "{[":
                stack.append("}" if ch == "{" else "]")
            elif ch in "}]" and stack and stack[-1] == ch:
                stack.pop()

    result = text
    if in_string:
        result += '"'          # close the open string literal
    result += "".join(reversed(stack))   # close open objects/arrays
    return result


def _safe_json_loads(text: str) -> Any:
    """json.loads with automatic repair on truncated output."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        repaired = _repair_json(text)
        return json.loads(repaired)


class AgenticAnalyzer:
    """
    Agentic business analyzer with 4 specialized agents + automation stack composer.
    Requires XAI_API_KEY in environment — no mock fallbacks.
    """

    def __init__(self, db_session: Session):
        self.db = db_session
        self.model = "grok-4-1-fast-reasoning"
        self.reasoning_model = "grok-4-1-fast-reasoning"
        self.fast_model = "grok-4-1-fast-non-reasoning"

        api_key = os.getenv("XAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "XAI_API_KEY is not set — add it to .env.local before running analysis"
            )

        # AsyncOpenAI uses httpx.AsyncClient — every completion call is native
        # async I/O. Unlimited concurrent callers share the same event loop without
        # thread pool slots. max_retries automatically retries 429 rate-limit
        # responses with exponential backoff so the caller never sees them —
        # kept low (not the default 5) because this pipeline makes ~9-13
        # sequential calls per analysis; a high retry count on any one of them
        # multiplies into a very long worst-case wait for the user.
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://api.x.ai/v1",
            timeout=120.0,
            max_retries=2,
        )
        logger.info("xAI Grok async client initialized for agentic analysis")

    async def _llm(self, **kwargs):
        """Async LLM call — no thread pool, no blocking, truly concurrent."""
        return await self.client.chat.completions.create(**kwargs)

    # =========================================================================
    # SEMANTIC TOOL SEARCH (used by Stage 3)
    # =========================================================================

    async def _extract_mentioned_tools(self, what_to_do_list: list[str]) -> list[str]:
        """Return names of software/SaaS tools explicitly cited in action plan steps."""
        if not what_to_do_list:
            return []
        steps_text = "\n".join(f"{i+1}. {s}" for i, s in enumerate(what_to_do_list))
        prompt = (
            "List every named software tool, SaaS product, or platform explicitly mentioned "
            "in these action steps. Return a JSON array of strings. Return [] if none.\n\n"
            f"{steps_text}\n\n"
            "Rules: only named products (e.g. Zapier, HubSpot, Notion). "
            "Exclude generic words like 'spreadsheet', 'email', 'CRM', 'software'.\n"
            "Output: JSON array only, no explanation."
        )
        try:
            resp = await self._llm(
                model=self.fast_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=120,
            )
            raw = resp.choices[0].message.content.strip()
            if "```" in raw:
                raw = raw.split("```")[1].split("```")[0].strip()
                if raw.startswith("json"):
                    raw = raw[4:].strip()
            parsed = _safe_json_loads(raw)
            if isinstance(parsed, list):
                return [str(t).strip() for t in parsed if t and str(t).strip()]
            return []
        except Exception:
            return []

    async def _search_ai_tools(
        self, user_query: str, action_description: str, top_k: int = 3
    ) -> list[dict]:
        """Semantic search for relevant AI tools from the database.

        Searches using ONLY the action-specific description so each plan's
        embedding is unique. Prepending the user_query (which is identical for
        all plans) dilutes the per-plan signal and makes every plan return the
        same top tools from the database.
        """
        try:
            # recommend_tools is synchronous and, on a cache miss, calls
            # SentenceTransformer.encode() over the full tool catalog — a
            # CPU-bound call that can take ~90s. Run it in a worker thread so
            # it can't block the shared asyncio event loop (which would stall
            # every other concurrent request/SSE stream in this process, not
            # just this one — see agentic_analyzer.py incident 2026-07-28).
            tools = await asyncio.to_thread(
                recommend_tools, action_description, top_k=top_k, db_session=self.db
            )
            logger.info(
                f"Found {len(tools)} tools via semantic search for: {action_description[:60]}..."
            )
            return tools
        except Exception as e:
            logger.error(f"Semantic search failed: {e}")
            return []

    # =========================================================================
    # MAIN PIPELINE
    # =========================================================================

    async def analyze(self, user_query: str, user_id: int, progress_callback=None) -> Dict[str, Any]:
        """
        Main analysis pipeline — orchestrates all agents.

        Args:
            user_query: User's business challenge/goal
            user_id: Current user ID
            progress_callback: Optional async callable(pct: int, msg: str) for SSE progress events

        Returns:
            Complete analysis dict matching the frontend result page format
        """
        async def _emit(pct: int, msg: str):
            if progress_callback:
                try:
                    await progress_callback(pct, msg)
                except Exception:
                    pass

        logger.info(f"Starting agentic analysis for user {user_id}")
        start_time = datetime.now()

        try:
            await _emit(5, "Starting analysis...")

            # Pre-flight: block harmful or malicious queries before touching LLM stages
            logger.info("Pre-flight: checking query safety...")
            await _emit(8, "Reviewing your query...")
            safety_result = await self._check_query_safety(user_query)
            if not safety_result["is_safe"]:
                raise ValueError(
                    f"UNSAFE_QUERY::{safety_result['reason']}::{safety_result['suggestions']}"
                )

            logger.info("Stage 1: Identifying primary bottleneck...")
            await _emit(10, "Identifying your primary bottleneck...")
            primary_result = await self._stage1_primary_bottleneck(user_query)
            recommendation_mode = primary_result.get("recommendation_mode", "automation_stack")
            await _emit(25, f"Found: {primary_result.get('primary_bottleneck', {}).get('title', 'bottleneck identified')}")

            logger.info("Stage 2: Finding secondary constraints...")
            await _emit(30, "Mapping secondary constraints...")
            secondary_result = await self._stage2_secondary_constraints(
                user_query, primary_result
            )
            await _emit(45, "Constraints mapped")

            logger.info("Stage 3: Generating ranked action plans...")
            await _emit(50, "Building ranked action plans...")
            action_plans_result = await self._stage3_action_plans(
                user_query, primary_result, secondary_result
            )
            await _emit(65, "Action plans ready")

            # Stages 3B and 4 are independent — run in parallel to cut latency
            logger.info("Stages 3B + 4: running automation stacks and roadmap in parallel...")
            await _emit(70, "Selecting tool stacks and generating roadmap...")
            automation_stack_result, roadmap_result = await asyncio.gather(
                self._stage3_automation_stacks(
                    user_query=user_query,
                    action_plans_result=action_plans_result,
                    primary_result=primary_result,
                    secondary_result=secondary_result,
                    recommendation_mode=recommendation_mode,
                ),
                self._stage4_roadmap_and_motivation(user_query, action_plans_result),
            )
            await _emit(92, "Roadmap complete")

            duration_seconds = (datetime.now() - start_time).total_seconds()

            confidence_score = self._calculate_confidence_score(
                primary_result=primary_result,
                action_plans_result=action_plans_result,
                roadmap_result=roadmap_result,
                automation_stack_result=automation_stack_result,
            )

            await _emit(95, "Saving your analysis...")
            analysis_id = await self._save_to_database(
                user_id=user_id,
                user_query=user_query,
                primary_result=primary_result,
                secondary_result=secondary_result,
                action_plans_result=action_plans_result,
                automation_stack_result=automation_stack_result,
                roadmap_result=roadmap_result,
                duration=duration_seconds,
                confidence_score=confidence_score,
                recommendation_mode=recommendation_mode,
            )

            response = self._format_for_frontend(
                analysis_id=analysis_id,
                user_query=user_query,
                primary_result=primary_result,
                secondary_result=secondary_result,
                action_plans_result=action_plans_result,
                automation_stack_result=automation_stack_result,
                roadmap_result=roadmap_result,
                recommendation_mode=recommendation_mode,
            )

            await _emit(100, "Analysis complete!")
            logger.info(f"Analysis complete in {duration_seconds:.1f}s")
            return response

        except Exception as e:
            logger.error(f"Analysis failed: {e}", exc_info=True)
            raise

    # =========================================================================
    # PRE-FLIGHT: SAFETY GUARD
    # =========================================================================

    async def _check_query_safety(self, user_query: str) -> Dict[str, Any]:
        """
        Fast safety check using the non-reasoning model.
        Returns {"is_safe": bool, "reason": str, "suggestions": list[str]}.
        A query is UNSAFE if it asks for help with: illegal activity, fraud, hacking,
        money laundering, harm to people, weapons, counterfeit goods, exploitation,
        or any other clearly unethical or criminal purpose.
        Legitimate business challenges — even edgy ones — are SAFE.
        """
        prompt = f"""You are the safety filter for Lavoo, a business diagnostic and decision engine serving solo founders, small business owners, creators, and consultants worldwide.

USER QUERY: "{user_query}"

TASK: Determine whether this query is a legitimate business challenge.

SAFE — allow through:
- Any real business problem across any industry, market, or stage: marketing, growth, operations, hiring, finance, product, technology, pricing, partnerships, competitive strategy, pivoting, recovery, monetization, market entry, positioning, unit economics, cash flow, scaling, sunsetting
- Businesses that are struggling, pre-revenue, in turnaround, or in legal but stigmatized industries (adult platforms, cannabis where legal, gambling, debt collection, nightlife, controversial content)
- Queries that sound aggressive but have clear business intent ("crush competitors" = competitive strategy, "steal market share" = positioning, "kill my old product" = sunsetting, "dominate the market" = market leadership)

UNSAFE — block:
- Financial crime: fraud, tax evasion, money laundering, bribery, counterfeiting
- Unauthorized access: hacking, data theft, scraping protected systems, phishing, identity fraud
- Harm: deliberate harm to individuals, employees, competitors, or the public
- Illegal trade: weapons, drugs, controlled substances, black-market operations
- Exploitation: worker exploitation, child labor, trafficking, coercion

DECISION RULES:
- Default to SAFE when ambiguous. Founders describe problems in raw, frustrated, unpolished language — that is signal, not risk.
- Only block when harmful intent is the ONLY plausible interpretation. One legitimate reading is enough to pass.
- Never penalize poor grammar, slang, profanity, frustration, or unconventional framing.

Return JSON only, no markdown:
{{
    "is_safe": true | false,
    "reason": "One sentence. If safe: the legitimate business framing. If unsafe: the specific violation category.",
    "suggestions": []
}}"""

        try:
            response = await self._llm(
                model=self.fast_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=300,
            )
            result_text = response.choices[0].message.content.strip()
            if "```json" in result_text:
                result_text = result_text.split("```json")[1].split("```")[0].strip()
            elif "```" in result_text:
                result_text = result_text.split("```")[1].split("```")[0].strip()
            result = _safe_json_loads(result_text)
            return {
                "is_safe": bool(result.get("is_safe", True)),
                "reason": result.get("reason", ""),
                "suggestions": result.get("suggestions", []),
            }
        except Exception as e:
            # On error, default to safe so we don't block legitimate users
            logger.warning(f"Safety check failed (defaulting to safe): {e}")
            return {"is_safe": True, "reason": "", "suggestions": []}

    # =========================================================================
    # STAGE 1: PRIMARY BOTTLENECK AGENT
    # =========================================================================

    async def _stage1_primary_bottleneck(self, user_query: str) -> Dict[str, Any]:
        """
        Identify THE single most critical bottleneck.

        Returns:
            {
                "primary_bottleneck": {"title", "description", "consequence"},
                "strategic_priority": str,
                "what_to_stop": str
            }
        """
        prompt = f"""You are operating at the level of a senior partner at McKinsey, BCG, or Bain — but optimized for solo founders and small businesses, not Fortune 500 companies. You combine analytical rigor with execution specificity that a one-person or small-team business actually needs.

You have pattern recognition across thousands of businesses in retail, SaaS, services, manufacturing, healthcare, e-commerce, fintech, hospitality, agriculture, creator economy, agencies, consulting, education, and real estate. You have seen this problem before — in different industries, at different scales — and you know what the root cause actually is.

PERSONA RULE: Write every text field in SECOND PERSON — say "you", "your", "you are". NEVER say "the user", "the founder", "the owner", "this person", or any third-person reference. You are speaking directly to this person as their senior strategic advisor.

USER CHALLENGE: "{user_query}"

TASK: Diagnose THE SINGLE root-cause bottleneck that, if fixed, would unlock the most forward progress right now.

DIAGNOSIS PROTOCOL:

STEP 1 — DESIRED OUTCOME: What measurable result are you actually trying to achieve? Strip away the language — identify the core metric: revenue target, conversion rate, retention, time freedom, profitability threshold, market position.

STEP 2 — ISSUE TREE: Map the causal chain from the symptom described to the structural root cause. Most founders describe symptoms ("not enough leads"). Your job is to identify which part of the structure is the binding constraint. Apply the 5 Whys until you reach something the founder can directly change.

STEP 3 — EVIDENCE ANCHOR: What specific detail in the description confirms this root cause? If the input is sparse, explicitly state your inference: "Based on [detail], I am inferring [structural cause] because [reasoning]."

STEP 4 — PATTERN RECOGNITION: Name the business archetype this matches. Example: "This is the classic 'leaky bucket' pattern — you are investing in acquisition while your retention pipeline has a structural break."

STEP 5 — MISDIAGNOSIS TRAP: What would 90% of advisors, courses, and generic content wrongly diagnose? Name it and explain why it is wrong FOR YOUR SPECIFIC SITUATION. This is the insight that separates $50K consulting from free advice.

STEP 6 — 90-DAY COST MODEL: If you ignore this bottleneck for 90 days, model the cost across: (a) direct loss — revenue or customers forfeited, (b) compounding damage — how the problem gets WORSE over time, not just persists, (c) opportunity cost — what you CANNOT build while this consumes capacity. Use calibrated ranges from comparable businesses.

STEP 7 — UNIT ECONOMICS IMPACT: How does this bottleneck distort your unit economics? Connect the root cause to CAC, LTV, payback period, gross margin, or contribution margin.

CRITICAL OUTPUT RULES:
- ONE bottleneck only. Commit. Do not hedge.
- Title names the broken or missing SYSTEM — not a symptom. Good: "No Post-Purchase Activation Sequence to Convert One-Time Buyers". Bad: "Sales Issues".
- Description: EXACTLY 2 sentences, second person, states the structural gap and the pattern archetype. Hard limit: 40 words maximum.
- Consequence: EXACTLY 2 sentences, second person, models 90-day cost with ONE calibrated range. Hard limit: 40 words maximum.
- strategic_priority: ONE sentence, second person, names the specific mechanism. Hard limit: 25 words maximum. Good: "Build a 3-email post-purchase sequence that fires within 24 hours and drives second transaction within 14 days." Bad: "Focus on retention."
- what_to_stop: ONE sentence, second person, names the specific behavior and its cost. Hard limit: 25 words maximum.
- RECOMMENDATION MODE:
  - "single_tool" = ONE AI/SaaS tool directly solves this, no data handoffs needed
  - "automation_stack" = requires MULTIPLE tools in sequence with data handoffs

OUTPUT FORMAT (JSON only, no markdown):
{{
    "primary_bottleneck": {{
        "title": "Specific structural problem title (5-10 words) — names the missing or broken system",
        "description": "2 sentences max 40 words. Structural gap + pattern archetype.",
        "consequence": "2 sentences max 40 words. 90-day cost with one calibrated range."
    }},
    "strategic_priority": "One sentence max 25 words naming the specific mechanism to build this week.",
    "what_to_stop": "One sentence max 25 words naming the specific behavior to eliminate and its cost.",
    "recommendation_mode": "single_tool or automation_stack"
}}"""

        try:
            response = await self._llm(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=800,
            )
            result_text = response.choices[0].message.content.strip()

            if "```json" in result_text:
                result_text = result_text.split("```json")[1].split("```")[0].strip()
            elif "```" in result_text:
                result_text = result_text.split("```")[1].split("```")[0].strip()

            result = _safe_json_loads(result_text)
            logger.info(f"Primary bottleneck: {result['primary_bottleneck']['title']}")
            return result

        except Exception as e:
            logger.error(f"Stage 1 failed: {e}")
            raise

    # =========================================================================
    # STAGE 2: SECONDARY CONSTRAINTS AGENT
    # =========================================================================

    async def _stage2_secondary_constraints(
        self, user_query: str, primary_result: Dict
    ) -> Dict[str, Any]:
        """
        Identify 2-4 secondary constraints.

        Returns:
            {"secondary_constraints": [{"id", "title", "description"}, ...]}
        """
        primary_title = primary_result["primary_bottleneck"]["title"]

        prompt = f"""You are mapping the full constraint landscape around a diagnosed primary bottleneck — the same analysis a McKinsey engagement team produces in their "key issues" workstream, focused on the specific dynamics of a solo founder or small business.

Your job is not to list more problems. It is to identify the forces that create a COMPOUNDING TRAP — the reason this bottleneck is harder to solve than it appears, and why previous attempts to fix it have likely stalled.

PERSONA RULE: Write every text field in SECOND PERSON — say "you", "your". NEVER use third-person references.

USER CHALLENGE: "{user_query}"
PRIMARY BOTTLENECK: "{primary_title}"

TASK: Identify 2-4 secondary constraints that compound your primary bottleneck or structurally block you from solving it.

CONSTRAINT ANALYSIS FRAMEWORK:

1. AMPLIFICATION TEST: Each constraint must make the primary bottleneck measurably WORSE through a specific causal mechanism. Name the mechanism — not just the correlation. "Your shallow onboarding data limits the quality of your diagnosis output, which means users receive generic recommendations, which accelerates their conclusion that the product is not personalized enough to justify returning" — not "your onboarding needs work."

2. DISTINCTNESS TEST: Each constraint must be structurally independent. If removing one would automatically resolve another, merge them. No conceptual overlap with each other or the primary.

3. EVIDENCE TEST: Ground each constraint in something you explicitly described OR a structural consequence that necessarily follows from your situation. If inferring, state the inference chain.

4. CAPACITY DRAIN: At least one constraint should address your CAPACITY to solve the primary bottleneck — the person who must fix the problem is also running every other function. Identify where this capacity drain hits hardest.

5. SEVERITY ORDERING: Rank by compounding velocity — the constraint that accelerates damage fastest goes first.

Return exactly 2 for focused single-dimension challenges. Return 3-4 only when the landscape is genuinely multi-dimensional and each adds a distinct causal pathway.

OUTPUT FORMAT (JSON only, no markdown):
{{
    "secondary_constraints": [
        {{
            "id": 2,
            "title": "Specific constraint name describing a structural gap or behavioral pattern (5-8 words)",
            "description": "2 sentences MAX, strictly 40 words or fewer. Must fit in 4 display lines. Sentence 1: the structural gap and its direct causal mechanism. Sentence 2: the consequence if unaddressed."
        }}
    ]
}}

Number constraints starting at 2 (your primary bottleneck is always #1).
No markdown, no dashes, no bullet prefixes in any text value.
LINE LIMIT RULE: Each description must fit in exactly 4 display lines — 40 words maximum, hard limit. Violating this breaks the UI. Do not exceed 40 words under any circumstance."""

        try:
            response = await self._llm(
                model=self.fast_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.6,
                max_tokens=600,
            )
            result_text = response.choices[0].message.content.strip()

            if "```json" in result_text:
                result_text = result_text.split("```json")[1].split("```")[0].strip()
            elif "```" in result_text:
                result_text = result_text.split("```")[1].split("```")[0].strip()

            result = _safe_json_loads(result_text)
            logger.info(f"Identified {len(result['secondary_constraints'])} secondary constraints")
            return result

        except Exception as e:
            logger.error(f"Stage 2 failed: {e}")
            raise

    async def _attach_toolkit(
        self,
        plan: dict,
        user_query: str,
        used_tool_names: set | None = None,
        plan_index: int = 0,
        total_plans: int = 1,
    ) -> dict:
        """Fetch and attach the best AI tool(s) for a single action plan.

        Design decisions:
        - Semantic search uses ONLY this plan's steps (not the shared user_query)
          so each plan's embedding is genuinely unique and returns different candidates.
        - `used_tool_names` is a shared mutable set — every plan adds its chosen
          tools so no tool can appear in two action cards.
        - The LLM prompt lists already-assigned tools explicitly so the model
          understands the constraint and picks something different.
        - Name matching uses a normalised lookup (case-insensitive, stripped) to
          tolerate minor casing differences between the LLM's output and the DB name.
        """
        if used_tool_names is None:
            used_tool_names = set()

        if not plan.get("needs_ai_tool", False):
            plan["toolkit"] = None
            plan.pop("needs_ai_tool", None)
            return plan

        # Build a clean, joined step string for semantic search
        what_to_do_list: list[str] = (
            plan.get("what_to_do", [])
            if isinstance(plan.get("what_to_do"), list)
            else []
        )
        steps_text = " ".join(what_to_do_list)
        action_description = f"{plan['title']}: {steps_text}"

        # top_k=15: wider candidate pool per plan so different plans can pull
        # genuinely different tools from the database
        tools = await self._search_ai_tools(
            user_query=user_query,
            action_description=action_description,
            top_k=15,
        )

        if not tools:
            plan["toolkit"] = None
            plan.pop("needs_ai_tool", None)
            return plan

        # Build a normalised name map for case-insensitive matching
        # DB name → normalised key
        name_to_canonical: dict[str, str] = {
            t["tool_name"].strip().lower(): t["tool_name"] for t in tools
        }

        # Exclude tools already assigned to another action plan (case-insensitive)
        used_normalised = {n.strip().lower() for n in used_tool_names}
        available_tools = [
            t for t in tools
            if t["tool_name"].strip().lower() not in used_normalised
        ]
        if not available_tools:
            # All candidates already used — allow reuse as last resort
            available_tools = tools
            logger.warning(
                f"All {len(tools)} candidates already used for plan '{plan['title']}' — allowing reuse"
            )

        numbered_steps = "\n".join(
            f"{i+1}. {step}" for i, step in enumerate(what_to_do_list)
        )
        tool_summaries = "\n".join(
            f"{i+1}. {t['tool_name']} (website: {t.get('website') or t.get('url') or 'unknown'}): {t['description'][:180]}"
            for i, t in enumerate(available_tools)
        )

        # Tell the LLM which tools are already taken so it doesn't even try to pick them
        already_used_note = ""
        if used_tool_names:
            already_used_note = (
                f"\nTOOLS ALREADY ASSIGNED TO OTHER ACTION PLANS (do NOT select these):\n"
                + "\n".join(f"  - {n}" for n in sorted(used_tool_names))
                + "\n"
            )

        prompt_tool_selection = f"""You are a specialist in matching AI/SaaS tools to specific business action plans. Each action plan in an analysis is DIFFERENT — your tool selection must reflect that difference.

CONTEXT: This is action plan {plan_index + 1} of {total_plans} in the analysis.
USER BUSINESS CHALLENGE: {user_query}
{already_used_note}
ACTION PLAN TITLE: {plan['title']}

STEPS THE USER MUST COMPLETE FOR THIS PLAN:
{numbered_steps}

CANDIDATE TOOLS (from semantic search — use ONLY names from this list):
{tool_summaries}

YOUR TASK:
1. Read every step of THIS specific action plan carefully.
2. Select the tool(s) whose documented features directly automate or accelerate a named step above.
3. Select UP TO 2 tools ONLY if the steps clearly need two different capabilities (e.g. step 1 needs analytics, step 3 needs email automation). Otherwise select 1.
4. Return an empty array if no tool genuinely addresses a specific step.
5. CRITICAL — "what_it_helps": name the exact step number(s) and describe what the tool does for that step in one concrete sentence. This text will appear on a card next to THIS specific action plan — it must be unique to these steps, not a generic description of the tool.
6. Return the tool's website URL from the candidate list exactly as shown.

OUTPUT FORMAT (JSON only, no markdown, no leading dashes in any text value):
{{
    "toolkits": [
        {{
            "tool_name": "Exact name from the candidate list",
            "website": "Exact URL from the candidate list, or null",
            "what_it_helps": "Step N: one concrete sentence describing what the tool does for that specific step"
        }}
    ]
}}"""

        try:
            tool_response = await self._llm(
                model=self.fast_model,
                messages=[{"role": "user", "content": prompt_tool_selection}],
                temperature=0.3,
                max_tokens=600,
            )
            tool_text = tool_response.choices[0].message.content.strip()
            if "```json" in tool_text:
                tool_text = tool_text.split("```json")[1].split("```")[0].strip()
            elif "```" in tool_text:
                tool_text = tool_text.split("```")[1].split("```")[0].strip()
            tool_selection = _safe_json_loads(tool_text)

            toolkits: list[dict] = tool_selection.get("toolkits", []) or []

            # Normalised lookup: accept LLM output that differs in casing/whitespace
            available_normalised: dict[str, str] = {
                t["tool_name"].strip().lower(): t["tool_name"] for t in available_tools
            }

            validated: list[dict] = []
            for tk in toolkits:
                llm_name = (tk.get("tool_name") or "").strip()
                llm_name_lower = llm_name.lower()
                # Resolve to canonical DB name (case-insensitive)
                canonical = available_normalised.get(llm_name_lower)
                if canonical and canonical.strip().lower() not in used_normalised:
                    # Overwrite with the canonical DB name so storage is consistent
                    tk["tool_name"] = canonical
                    validated.append(tk)
                    if len(validated) == 2:
                        break

            # Register all selected tools before returning
            for tk in validated:
                used_tool_names.add(tk["tool_name"])
                used_normalised.add(tk["tool_name"].strip().lower())

            logger.info(
                f"Plan {plan_index+1}/{total_plans} '{plan['title'][:40]}' → "
                f"toolkit: {[tk['tool_name'] for tk in validated] or 'none'} | "
                f"used pool: {used_tool_names}"
            )

            if len(validated) == 0:
                plan["toolkit"] = None
            elif len(validated) == 1:
                plan["toolkit"] = validated[0]
            else:
                plan["toolkit"] = validated[0]
                plan["additional_toolkits"] = validated[1:]

        except Exception as e:
            logger.warning(f"Toolkit selection failed for plan '{plan['title']}': {e}")
            plan["toolkit"] = None

        plan.pop("needs_ai_tool", None)
        return plan

    # =========================================================================
    # GLOBAL TOOLKIT ASSIGNMENT (replaces per-plan sequential loop)
    # =========================================================================

    # Deterministic pass bar applied in Python (never left to the LLM's
    # discretion) — a candidate tool is only assignable to a step if it
    # clears the bar on at least 4 of these 5 criteria (80%). task_relevance's
    # bar is set far higher than the rest because it is the load-bearing
    # criterion: a tool that doesn't literally perform the step's action has
    # no business being recommended for it, regardless of how cheap or easy
    # to adopt it is. This gate is what makes an assignment defensible under
    # outside scrutiny — the number either clears the bar in code or it doesn't.
    _STEP_TOOL_CRITERIA_BARS = {
        "task_relevance": 90,
        "workflow_integration": 20,
        "expected_impact": 20,
        "ease_of_implementation": 10,
        "cost_efficiency": 10,
    }
    _STEP_TOOL_PASS_THRESHOLD = 4  # of 5 criteria must clear their bar

    async def _assign_tools_to_steps(
        self, action_plans: list[dict], user_query: str
    ) -> list[dict]:
        """
        Assign AI tools to individual action-plan STEPS (not whole plans) in a
        single globally-aware LLM pass, gated by a deterministic scoring rubric.

        Why per-step instead of per-plan:
        - A plan's steps are sequential and often heterogeneous (one step is a
          manual decision, the next is genuinely automatable) — collapsing the
          whole plan into a single tool recommendation misattributes it to
          steps it doesn't actually apply to. Each step gets its own
          independent tool match, or none at all.

        Why a global LLM pass instead of per-step loops:
        - Per-step searches return overlapping candidates (related steps →
          related tools); a single call that sees ALL steps of ALL plans plus
          ALL candidates simultaneously can make globally optimal,
          non-repeating assignments — no two steps in the whole analysis get
          the same tool.

        Flow:
        1. Search for candidates per plan using ONLY that plan's steps (not user_query)
        2. Pool all unique candidates into a single deduplicated catalog
        3. One LLM call proposes up to 2 ranked candidates per step, each with
           the 5 criterion scores (0-100) the tool must earn for that step
        4. Python deterministically gates every proposal against the pass bar
           and enforces global uniqueness — the LLM's scores are advisory
           input, the gate is what actually decides assignment
        """
        # Every plan is searched and scored — there is no upfront LLM opt-out.
        # An earlier version let Stage 3's plan-level "needs_ai_tool" flag skip
        # search+scoring entirely for a plan; that let a single coarse LLM
        # judgment call veto a step before the deterministic criteria gate
        # ever got a chance to evaluate it, which contradicts the gate's whole
        # purpose (the gate, not LLM discretion, decides). The gate is now the
        # only thing standing between a candidate and assignment.
        needy_indices = list(range(len(action_plans)))
        for plan in action_plans:
            plan.pop("needs_ai_tool", None)
            what_to_do_list = plan.get("what_to_do", []) if isinstance(plan.get("what_to_do"), list) else []
            # Always present, same length as what_to_do, so the frontend can
            # zip step text ↔ step tool by index regardless of plan type.
            plan["step_tools"] = [None] * len(what_to_do_list)

        if not needy_indices:
            return action_plans

        # ── Step 1: per-plan semantic search + extract tools cited in steps ──
        all_tools: dict[str, dict] = {}  # canonical tool name → tool record
        plan_candidate_names: dict[int, list[str]] = {}  # plan_idx → tool names from search
        # plan_idx → tool names explicitly named inside what_to_do step text
        plan_step_cited: dict[int, list[str]] = {}

        # Per-plan search + citation-extraction are independent of each other —
        # run them concurrently instead of one plan at a time, so the N
        # sequential LLM round-trips collapse into one overlapped wait.
        async def _gather_plan_data(plan_idx: int):
            plan = action_plans[plan_idx]
            what_to_do_list = plan.get("what_to_do", []) if isinstance(plan.get("what_to_do"), list) else []
            steps_text = " ".join(what_to_do_list)
            action_description = f"{plan['title']}: {steps_text}"
            candidates, cited = await asyncio.gather(
                self._search_ai_tools(
                    user_query=user_query,
                    action_description=action_description,
                    # Wider than the old per-plan search (12) since candidates
                    # now need to cover multiple distinct steps within a plan,
                    # not just the plan as a whole.
                    top_k=15,
                ),
                self._extract_mentioned_tools(what_to_do_list),
            )
            return plan_idx, candidates, cited

        plan_results = await asyncio.gather(*(_gather_plan_data(idx) for idx in needy_indices))

        for plan_idx, candidates, cited in plan_results:
            names_for_plan: list[str] = []
            for t in candidates:
                name = t["tool_name"]
                if name not in all_tools:
                    all_tools[name] = t
                names_for_plan.append(name)
            plan_candidate_names[plan_idx] = names_for_plan
            plan_step_cited[plan_idx] = cited

            # Inject cited tools not found by semantic search as stubs so the
            # LLM can assign them back to this plan
            for cited_name in cited:
                cited_lower = cited_name.lower()
                already_in = any(k.lower() == cited_lower for k in all_tools)
                if not already_in:
                    all_tools[cited_name] = {
                        "tool_name": cited_name,
                        "url": None,
                        "website": None,
                        "description": f"Tool cited by name in plan steps — assign to the plan that mentions it.",
                        "_step_cited": True,
                    }
                    logger.info(f"Injected step-cited tool stub: '{cited_name}' for plan {plan_idx + 1}")

        if not all_tools:
            return action_plans

        # ── Step 2: score each plan's steps against its OWN candidate pool,
        # one LLM call per plan, all run concurrently via asyncio.gather.
        # Cross-plan uniqueness was never actually enforced by the LLM
        # reading everything at once anyway — Step 3 below is the real
        # enforcement: it walks every plan/step in priority order and
        # deterministically rejects/backfills on a collision. That doesn't
        # change here, so splitting the scoring calls doesn't weaken it.
        bars = self._STEP_TOOL_CRITERIA_BARS

        async def _score_plan(plan_idx: int) -> list[dict]:
            plan = action_plans[plan_idx]
            what_to_do_list = plan.get("what_to_do", []) if isinstance(plan.get("what_to_do"), list) else []
            if not what_to_do_list:
                return []
            numbered_steps = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(what_to_do_list))

            plan_tool_names = list(dict.fromkeys(
                plan_candidate_names.get(plan_idx, []) + plan_step_cited.get(plan_idx, [])
            ))
            if not plan_tool_names:
                return []

            tools_section_parts: list[str] = []
            for name in plan_tool_names:
                t = all_tools.get(name)
                if not t:
                    continue
                url = t.get("url") or t.get("website") or "unknown"
                step_flag = " [CITED IN STEPS]" if t.get("_step_cited") else ""
                tools_section_parts.append(
                    f"- {name}{step_flag} | URL: {url}\n  {t.get('description', '')[:200]}"
                )
            tools_section = "\n".join(tools_section_parts)
            if not tools_section:
                return []

            cited_note = ""
            if plan_step_cited.get(plan_idx):
                cited_note = f"\nTools explicitly named in steps (MUST assign to the step that names them): {', '.join(plan_step_cited[plan_idx])}"

            prompt = f"""You are a tool-matching specialist operating with the precision of a technology due diligence team. You match functional capabilities to tools based on verified feature alignment — not keyword similarity, not brand recognition, not marketing claims, not popularity.

Your output appears directly on an action plan card that a solo founder will use to make decisions. Every word must be specific, concrete, and unique to the STEP it accompanies — not the plan as a whole.

CRITICAL: You are matching tools to individual STEPS inside ONE plan, not to the plan as a whole. A plan with {len(what_to_do_list)} steps may end up with 0 or more tools — one per step that genuinely needs one. Steps that are manual, judgment-based, or one-time (e.g. "review the responses and decide", "call your accountant") get NO tool. Do not force a tool onto every step.

PERSONA RULE: Write "what_it_helps" in SECOND PERSON — "you", "your". Never "the user", "the founder", or any third-person reference.

WITHIN-PLAN CONSTRAINT: Do not propose the same tool for two different steps in this plan — each tool you propose should be the best match for exactly one step here.

STEP-CITED OVERRIDE: Any tool marked [CITED IN STEPS] is explicitly named inside this plan's step text. It MUST be assigned to the specific step that names it, as that step's top candidate, regardless of catalog rank.

SCORING RUBRIC — for every candidate you propose, score all 5 criteria honestly on a 0-100 scale. These numbers are checked in code against fixed pass bars, not just read by a person, so inflate nothing:
- task_relevance (bar: {bars['task_relevance']}): does the tool's DOCUMENTED core feature literally perform the action named in THIS step? Score 90+ only if unambiguous. A tool that is merely "in the same category" or "could be adapted" scores well below 90.
- workflow_integration (bar: {bars['workflow_integration']}): does adopting it fit how a solo founder already works, without a heavy migration or new platform lock-in?
- expected_impact (bar: {bars['expected_impact']}): how much of this step's actual outcome does the tool produce versus doing it manually?
- ease_of_implementation (bar: {bars['ease_of_implementation']}): can a non-technical founder start using it the same day, no engineering help required?
- cost_efficiency (bar: {bars['cost_efficiency']}): is there a free tier or low-cost plan sufficient at solo-founder volume?

Only propose a candidate for a step if you would defend task_relevance >= {bars['task_relevance']} under scrutiny from another independent reviewer. If no catalog tool's documented function genuinely performs a step's action, omit that step entirely.

USER BUSINESS CHALLENGE: "{user_query}"

PLAN: {plan['title']}
STEPS:
{numbered_steps}{cited_note}

TOOL CATALOG FOR THIS PLAN (from semantic search + step extraction — names marked [CITED IN STEPS] were found inside the steps themselves):
{tools_section}

MATCHING PROTOCOL:

STEP 1 — FEATURE-LEVEL MATCH, PER STEP
For each numbered step, identify tools in the catalog whose DOCUMENTED CORE FUNCTION directly performs the capability that specific step names. Not "this tool is in the same category" — the tool must have a specific feature that executes the required function for THAT step.

STEP 2 — SPECIFICITY OF DESCRIPTIONS
"what_it_helps" must name the EXACT step number and describe what the tool does for THAT step in one concrete sentence, citing the actual documented feature being used. This text appears on the plan card under that step — it must be unique to that step, not a generic product description.
Good: "Step 2: This tool sends your weekly re-engagement sequence automatically, personalised with each contact's last action, so you never manually write or send a follow-up again."
Bad: "A powerful automation platform."

STEP 3 — CANDIDATES, RANKED
For each step you're confident about, return up to 2 candidates ordered best-first (a second one only if it is also genuinely defensible — never pad with a weak second choice). Most steps will have exactly 1 or 0 candidates.

STEP 4 — ASSIGNMENT RULES
1. Do not propose the same tool for two different steps in this plan.
2. Tools marked [CITED IN STEPS] MUST appear as the top candidate for the step that cites them.
3. Only propose non-cited tools if they directly address that step's named action. Do NOT propose based on general category fit.
4. Return the tool's URL from the catalog exactly as shown (null is acceptable for step-cited stubs).
5. Do NOT start any text value with a dash, bullet, or em dash.

OUTPUT FORMAT (JSON only, no markdown):
{{
  "step_assignments": [
    {{
      "step_index": 2,
      "candidates": [
        {{
          "tool_name": "Exact name from the catalog",
          "website": "Exact URL from the catalog or null",
          "what_it_helps": "Step 2: one concrete sentence in second person naming the documented feature used — unique to this step.",
          "scores": {{"task_relevance": 0, "workflow_integration": 0, "expected_impact": 0, "ease_of_implementation": 0, "cost_efficiency": 0}}
        }}
      ]
    }}
  ]
}}

step_index is 1-based, counting within THIS plan's own step list. Omit any step where no catalog tool genuinely fits its specific action."""

            try:
                response = await self._llm(
                    model=self.fast_model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.2,
                    max_tokens=600,
                )
                raw = response.choices[0].message.content.strip()
                if "```json" in raw:
                    raw = raw.split("```json")[1].split("```")[0].strip()
                elif "```" in raw:
                    raw = raw.split("```")[1].split("```")[0].strip()
                parsed = _safe_json_loads(raw)
                entries = parsed.get("step_assignments", []) or []
                for entry in entries:
                    entry["plan_index"] = plan_idx + 1
                return entries
            except Exception as e:
                logger.warning(f"Step-level tool assignment failed for plan {plan_idx + 1}: {e} — its steps will have no tool")
                return []

        per_plan_results = await asyncio.gather(*(_score_plan(idx) for idx in needy_indices))
        step_assignments_raw: list[dict] = [entry for entries in per_plan_results for entry in entries]

        # ── Step 3: normalise every proposed candidate, then gate it ──
        # canonical_map covers both DB tools and step-cited stubs
        canonical_map: dict[str, str] = {n.strip().lower(): n for n in all_tools}
        assigned_tool_names_lower: set[str] = set()

        def _passes_gate(scores: dict) -> bool:
            """Deterministic 4-of-5 pass bar — the LLM's scores are advisory
            input, this function is what actually decides assignment."""
            passed = 0
            for crit, bar in bars.items():
                try:
                    val = float(scores.get(crit, 0) or 0)
                except (TypeError, ValueError):
                    val = 0.0
                if val >= bar:
                    passed += 1
            return passed >= self._STEP_TOOL_PASS_THRESHOLD

        def _resolve_canonical(llm_name: str, plan_idx: int) -> Optional[str]:
            name = (llm_name or "").strip()
            if not name:
                return None
            canonical = canonical_map.get(name.lower())
            if canonical:
                return canonical
            # LLM returned a name not in the catalog — accept it only if it
            # matches a tool explicitly cited inside this plan's own steps.
            cited_for_plan = [c.lower() for c in plan_step_cited.get(plan_idx, [])]
            if name.lower() in cited_for_plan:
                if name not in all_tools:
                    all_tools[name] = {"tool_name": name, "url": None, "website": None, "description": ""}
                canonical_map[name.lower()] = name
                return name
            return None  # genuinely unknown — reject

        # Group proposals by (plan_idx, step_idx), validating indices against
        # each plan's real what_to_do length so a malformed step_index can't
        # write out of bounds.
        by_step: dict[tuple[int, int], list[dict]] = {}
        for entry in step_assignments_raw:
            plan_idx = int(entry.get("plan_index", 0) or 0) - 1
            step_idx = int(entry.get("step_index", 0) or 0) - 1
            if plan_idx not in needy_indices:
                continue
            what_to_do_list = action_plans[plan_idx].get("what_to_do", [])
            if not isinstance(what_to_do_list, list) or not (0 <= step_idx < len(what_to_do_list)):
                continue
            candidates = entry.get("candidates", []) or []
            if candidates:
                by_step[(plan_idx, step_idx)] = candidates

        # Walk plans/steps in priority order (lower plan number = higher
        # priority, then step order within the plan) so that when two steps
        # compete for the same tool, the higher-priority one wins — same
        # tie-break rule the old plan-level assignment used.
        assigned_steps = 0
        for plan_idx in needy_indices:
            what_to_do_list = action_plans[plan_idx].get("what_to_do", [])
            step_count = len(what_to_do_list) if isinstance(what_to_do_list, list) else 0
            for step_idx in range(step_count):
                # Up to 2 ranked candidates per step (best-first) act as the
                # per-step "backfill": if candidate 1 is already claimed by a
                # higher-priority step or fails the gate, try candidate 2
                # before giving up — no second LLM round trip required.
                for candidate in (by_step.get((plan_idx, step_idx), []) or [])[:2]:
                    canonical = _resolve_canonical(candidate.get("tool_name", ""), plan_idx)
                    if not canonical or canonical.lower() in assigned_tool_names_lower:
                        continue

                    scores = candidate.get("scores", {}) or {}
                    if not _passes_gate(scores):
                        logger.info(
                            f"Rejected '{canonical}' for plan {plan_idx + 1} step {step_idx + 1}: "
                            f"scores {scores} did not clear {self._STEP_TOOL_PASS_THRESHOLD}/5 pass bar"
                        )
                        continue

                    what_it_helps = (candidate.get("what_it_helps") or "").strip()
                    tool_record = all_tools[canonical]
                    if not what_it_helps:
                        is_stub = tool_record.get("_step_cited") or not tool_record.get("url")
                        if is_stub:
                            # No step-specific text from the LLM and no real DB
                            # description to fall back on — showing the internal
                            # stub placeholder would assert an unverifiable claim.
                            continue
                        what_it_helps = tool_record.get("description", "")

                    # Trust the DB's own url over whatever the LLM echoed back;
                    # only step-cited stubs (no DB record) fall through to it.
                    db_url = tool_record.get("url")
                    assigned_tool_names_lower.add(canonical.lower())
                    action_plans[plan_idx]["step_tools"][step_idx] = {
                        "tool_name": canonical,
                        "website": db_url or candidate.get("website") or None,
                        "what_it_helps": what_it_helps,
                        "scores": {crit: scores.get(crit) for crit in bars},
                    }
                    assigned_steps += 1
                    logger.info(
                        f"Assigned '{canonical}' → plan {plan_idx + 1} step {step_idx + 1} "
                        f"'{action_plans[plan_idx]['title'][:40]}'"
                    )
                    break  # step satisfied — don't consider its 2nd candidate

        total_eligible_steps = sum(
            len(action_plans[i].get("what_to_do", []))
            for i in needy_indices
            if isinstance(action_plans[i].get("what_to_do"), list)
        )
        logger.info(
            f"Step-level assignment complete: {assigned_steps}/{total_eligible_steps} "
            f"steps across {len(needy_indices)} plans received a tool"
        )
        return action_plans

    # =========================================================================
    # STAGE 3: ACTION PLANS AGENT
    # =========================================================================

    async def _stage3_action_plans(
        self,
        user_query: str,
        primary_result: Dict,
        secondary_result: Dict,
    ) -> Dict[str, Any]:
        """
        Generate ranked action plans with AI tools matched per-step via semantic search.

        Workflow:
        1. LLM generates action plans and flags which plans need an AI tool
        2. Semantic search retrieves matching tool candidates from DB
        3. LLM proposes a scored candidate per step; a deterministic pass bar
           (see _STEP_TOOL_CRITERIA_BARS) decides whether it's actually assigned

        Returns:
            {
                "action_plans": [
                    {
                        "id", "title", "what_to_do", "why_it_matters", "effort_level",
                        "step_tools": [
                            {"tool_name", "website", "what_it_helps", "scores"} | null,
                            ...  # same length and index alignment as what_to_do
                        ]
                    }
                ],
                "exclusions_note": str
            }
        """
        primary_title = primary_result["primary_bottleneck"]["title"]
        constraints = json.dumps([c["title"] for c in secondary_result["secondary_constraints"]])

        prompt_actions = f"""You are a senior strategist building a ranked action plan with the analytical rigor of BCG's "value creation roadmap" and the execution specificity that a solo founder with 2–3 hours per day actually needs.

Your strategic advantage over traditional consulting: you do not present options and let the client decide. You commit to the highest-leverage sequence and explain why the alternatives are inferior. A $50,000 consulting deck gives you 5 equally-weighted recommendations. This analysis gives you a ranked stack where #1 is the clear priority and the reasoning for the ranking is transparent.

GLOBAL QUALITY STANDARDS — every field must pass ALL of these:
1. PERSONA: Second person only — "you", "your". NEVER "the user", "the founder", "the owner", or any third-person reference.
2. SPECIFICITY: Passes the intern test — a smart person with zero context about your business executes this correctly on first attempt.
3. QUANTIFICATION: Every impact statement names a specific metric, a calibrated directional range (e.g. "15–30%"), and a timeframe ("within 45 days"). No vague language like "significant improvement" or "better results".
4. COMMITMENT: Never hedge with "consider", "might", "could", or "it depends". Commit to the recommendation and state the assumption if context is thin.
5. CONTRARIAN: For every plan, name what conventional advice would tell you to do — and explain in one sentence why that is wrong for your specific situation.
6. FORMATTING: Do NOT start any list item, step, or task with a dash (-), bullet (•), or em dash (—). Write plain complete sentences only.

USER CHALLENGE: "{user_query}"
PRIMARY BOTTLENECK: "{primary_title}"
SECONDARY CONSTRAINTS: {constraints}

TASK: Create 3–5 action plans that directly dismantle your primary bottleneck. Rank by leverage — the plan that produces the fastest measurable business result goes first.

RANKING METHODOLOGY (BCG impact-feasibility matrix, adapted for solo founders):
- IMPACT VELOCITY: How fast does measurable evidence appear? (Not "how big is the eventual impact" — how fast does the SIGNAL appear?)
- LEVERAGE MULTIPLIER: Does completing this make subsequent plans easier, faster, or more valuable? Plans that unlock other plans rank higher.
- EXECUTION INDEPENDENCE: Can you start this alone, this week, with no external dependencies?
- REVERSIBILITY: Can you course-correct cheaply if the first approach needs adjustment?
Rank 1 = highest impact velocity × leverage multiplier, executable independently, with course-correction optionality.
Do NOT rank by effort. A high-effort plan can be rank 1 if it unlocks everything downstream.

PER-PLAN REQUIREMENTS:

"what_to_do" — EXACTLY 3–5 steps (no more than 5) in second person. Each step contains:
(a) The EXACT action to take
(b) The SPECIFIC output or deliverable it produces
(c) The METRIC or observable signal that confirms it is done correctly
Steps are SEQUENTIAL — each builds on the previous and assumes its completion.
Good: "Write a 3-question post-purchase survey targeting the moment of highest engagement — the confirmation page — using a free form tool, and set a 24-hour email trigger to send to every buyer. Review the first 20 responses to identify the top two unmet expectations."
Bad: "Collect customer feedback."

"why_it_matters" — EXACTLY 2–3 impact statements (no more than 3) in second person:
At least one must name a UNIT ECONOMICS metric (CAC, LTV, gross margin, payback period, contribution margin) with a calibrated range and timeframe.
At least one must name a COMPOUNDING effect — what becomes possible only after this action is complete.
Good: "You will increase repeat purchase rate by 18–35% within 60 days, based on patterns in comparable direct-to-consumer businesses with similar first-purchase AOV."
Bad: "You will improve customer loyalty."

"failure_mode" — One sentence in second person naming the single most common reason this plan stalls, written as a direct warning:
"You will be tempted to [specific trap] because [specific reason] — when this happens, [specific corrective action]."

"time_to_first_signal" — The specific leading indicator that confirms this plan is working, with a timeframe and a diagnostic fallback:
"First signal in X days — when you observe [specific measurable event]. If you do not see this signal by day Y, [specific diagnostic action]."

"conventional_vs_this" — One sentence each:
"conventional": what standard advice (blogs, courses, generic consultants) would recommend.
"why_wrong": why that advice is wrong or suboptimal for your specific situation and stage.

OUTPUT FORMAT (JSON only, no markdown):
{{
    "action_plans": [
        {{
            "id": 1,
            "title": "Specific title naming what is being built, fixed, or created (5-10 words) — not a category",
            "what_to_do": [
                "Step 1 in second person: exact action + specific output + completion signal.",
                "Step 2 building on step 1: exact action + specific output + completion signal.",
                "Step 3 building on step 2: exact action + specific output + completion signal."
            ],
            "why_it_matters": [
                "You will [specific unit economics outcome with calibrated range and timeframe].",
                "Once complete, this unlocks [specific compounding effect that was previously blocked].",
                "This shifts your [specific metric] by [specific directional change] within [specific timeframe]."
            ],
            "failure_mode": "You will be tempted to [specific trap] because [reason] — when this happens, [corrective action].",
            "time_to_first_signal": "First signal in X days — when you observe [specific measurable event]. If absent by day Y, [diagnostic action].",
            "conventional_vs_this": {{
                "conventional": "Standard advice would tell you to [common recommendation].",
                "why_wrong": "That is wrong for your situation because [specific reason tied to your stage and context]."
            }},
            "effort_level": "Low or Medium or High"
        }}
    ],
    "exclusions_note": "Strategies considered but excluded: [name each with a concrete reason tied to your specific situation, stage, and constraints]."
}}"""

        try:
            response = await self._llm(
                model=self.fast_model,
                messages=[{"role": "user", "content": prompt_actions}],
                temperature=0.7,
                max_tokens=2500,
            )
            result_text = response.choices[0].message.content.strip()

            if "```json" in result_text:
                result_text = result_text.split("```json")[1].split("```")[0].strip()
            elif "```" in result_text:
                result_text = result_text.split("```")[1].split("```")[0].strip()

            result = _safe_json_loads(result_text)
            action_plans = result["action_plans"]

            # Global assignment: one LLM pass sees all plans + all candidates
            # simultaneously and assigns each tool to exactly one STEP (not
            # plan). This guarantees no tool appears twice anywhere in the
            # analysis even when action plans share semantically similar themes,
            # and every assignment must clear a deterministic scoring gate.
            result["action_plans"] = await self._assign_tools_to_steps(
                action_plans=action_plans,
                user_query=user_query,
            )

            logger.info(f"Generated {len(result['action_plans'])} action plans with per-step tool assignment")
            return result

        except Exception as e:
            logger.error(f"Stage 3 failed: {e}")
            raise

    # =========================================================================
    # STAGE 3B: AUTOMATION STACK AGENT
    # =========================================================================

    async def _enrich_single_stack(
        self, stack: dict, user_query: str, primary_bottleneck: str
    ) -> dict:
        """LLM-enrich a single stack. Returns the stack (modified in place)."""
        tools = stack.get("tools", [])
        if not tools:
            return stack

        allowed_tool_names = [t.get("tool_name", "") for t in tools if t.get("tool_name")]

        tool_context_parts = []
        for tool in tools:
            name = tool.get("tool_name", "")
            desc = (tool.get("description") or "")[:200]
            features_raw = tool.get("key_features") or ""
            integrations_raw = tool.get("compatibility_integration") or ""
            features = features_raw[:200].replace('["', "").replace('"]', "").replace('",', ",")
            integrations = integrations_raw[:200].replace('["', "").replace('"]', "").replace('",', ",")
            tool_context_parts.append(
                f"- {name}: {desc}\n"
                f"  Key Features: {features}\n"
                f"  Integrations: {integrations}"
            )

        tool_context = "\n".join(tool_context_parts)
        allowed_names_str = ", ".join(f'"{n}"' for n in allowed_tool_names)

        prompt = f""" 
OUTPUT FORMAT (JSON only, no markdown fences):
{{
  "stack_name": "Short descriptive name showing the flow (e.g., Tool A → Tool B)",
  "workflow_summary": "2 sentences: what this stack does and why it solves the user's problem",
  "automation_logic": "Step-by-step: how data or tasks flow between the tools (2-3 sentences)",
  "tool_roles": [
    {{
      "tool_name": "exact name from the list above",
      "role": "What this specific tool does in this workflow (1 sentence)",
      "hands_off_to": "What output it passes to the next tool, or 'delivers final output' if last"
    }}
  ],
  "setup_order": [
    {{
      "position": 1,
      "tool_name": "exact name from the list above",
      "why": "Why set this up first / at this step (1 sentence)"
    }}
  ]
}}"""

        try:
            response = await self._llm(
                model=self.fast_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
                max_tokens=700,
            )
            result_text = response.choices[0].message.content.strip()
            if "```json" in result_text:
                result_text = result_text.split("```json")[1].split("```")[0].strip()
            elif "```" in result_text:
                result_text = result_text.split("```")[1].split("```")[0].strip()

            llm_data = _safe_json_loads(result_text)

            validated_tool_roles = [
                tr for tr in llm_data.get("tool_roles", [])
                if tr.get("tool_name") in allowed_tool_names
            ]
            validated_setup_order = [
                so for so in llm_data.get("setup_order", [])
                if so.get("tool_name") in allowed_tool_names
            ]

            stack["stack_name"] = llm_data.get("stack_name", stack.get("stack_name", ""))
            stack["workflow_summary"] = llm_data.get("workflow_summary", stack.get("summary", ""))
            stack["automation_logic"] = llm_data.get("automation_logic", stack.get("automation_logic", ""))
            if validated_tool_roles:
                stack["tool_roles"] = validated_tool_roles
            if validated_setup_order:
                stack["setup_order"] = validated_setup_order

            logger.info(f"LLM enriched stack: {stack.get('stack_name', '?')}")
        except Exception as e:
            logger.warning(f"LLM enrichment failed for stack, keeping base values: {e}")

        return stack

    async def _enrich_stacks_with_llm(
        self,
        stacks: List[Dict],
        user_query: str,
        primary_bottleneck: str,
    ) -> List[Dict]:
        """LLM agent pass: reason about HOW selected DB tools work together. Runs all stacks in parallel."""
        if not stacks:
            return stacks
        results = await asyncio.gather(*[
            self._enrich_single_stack(stack, user_query, primary_bottleneck)
            for stack in stacks
        ])
        return list(results)

    async def _stage3_automation_stacks(
        self,
        user_query: str,
        action_plans_result: Dict[str, Any],
        primary_result: Dict[str, Any],
        secondary_result: Dict[str, Any],
        recommendation_mode: str = "automation_stack",
    ) -> Dict[str, Any]:
        """
        Stage 3B: Compose up to 3 automation stacks (algorithmic only).
        LLM enrichment is deferred to a BackgroundTask in the route layer.
        """
        try:
            if recommendation_mode == "single_tool":
                action_plans_for_tool = action_plans_result.get("action_plans", []) or []
                return await self._recommend_single_tool(user_query, action_plans_for_tool)

            action_plans = action_plans_result.get("action_plans", []) or []
            # Synchronous + potentially slow on a cache miss (full-catalog
            # re-embedding) — see _search_ai_tools for why this must not run
            # directly on the event loop.
            stacks = await asyncio.to_thread(
                recommend_automation_stacks,
                user_query=user_query,
                action_plans=action_plans,
                top_k_stacks=3,
                max_tools_per_stack=4,
                db_session=self.db,
            )

            constraints = secondary_result.get("secondary_constraints", []) or []
            constraint_titles = [
                str(item.get("title", "")).strip()
                for item in constraints
                if item.get("title")
            ]

            valid_stacks = []
            for stack in stacks:
                if not stack.get("tools"):
                    continue
                if constraint_titles:
                    stack["solves"] = f"Helps reduce: {', '.join(constraint_titles[:3])}."
                valid_stacks.append(stack)

            logger.info(f"Built {len(valid_stacks)} raw automation stacks (enrichment deferred)")
            return {"recommended_tool_stacks": valid_stacks, "single_tool_recommendation": None}

        except Exception as e:
            logger.error(f"Stage 3B failed: {e}", exc_info=True)
            try:
                self.db.rollback()
            except Exception:
                pass
            return {"recommended_tool_stacks": [], "single_tool_recommendation": None}

    async def _recommend_single_tool(self, user_query: str, action_plans: list = None) -> Dict[str, Any]:
        """
        Stage 3B (single_tool mode): Return the single best AI tool for the user's query.
        Also generates per-plan descriptions so each action card shows unique context for
        the same tool — no extra LLM call, same request with more structured output.
        """
        from database.pg_models import AITool
        try:
            # See _search_ai_tools — must not block the event loop on a cache miss.
            tools = await asyncio.to_thread(recommend_tools, user_query, top_k=3, db_session=self.db)
            if not tools:
                return {"recommended_tool_stacks": [], "single_tool_recommendation": None}

            tool_summaries = "\n".join(
                f"{i+1}. {t['tool_name']}: {t['description'][:150]}"
                for i, t in enumerate(tools)
            )

            # Build action plans context for per-plan descriptions
            plans_section = ""
            plan_desc_keys = ""
            if action_plans:
                lines = []
                for i, plan in enumerate(action_plans):
                    title = plan.get("title") or plan.get("action_title") or f"Plan {i + 1}"
                    steps = (plan.get("what_to_do") or plan.get("what_to_do_steps")
                             or plan.get("steps") or [])
                    step_preview = "; ".join(str(s) for s in steps[:2])[:120] if steps else ""
                    lines.append(f"Plan {i}: {title}" + (f" — {step_preview}" if step_preview else ""))
                plans_section = "\n".join(lines)
                plan_desc_keys = ", ".join(
                    f'"{i}": "one direct sentence for plan {i}"'
                    for i in range(len(action_plans))
                )

            plan_desc_block = (
                f',\n    "plan_descriptions": {{{plan_desc_keys}}}'
                if action_plans else ""
            )
            plans_block = (
                f"\nACTION PLANS:\n{plans_section}"
                if plans_section else ""
            )
            plan_desc_rule = (
                '\n- "plan_descriptions": dict keyed by plan index (string). Each value is ONE direct sentence explaining what this tool does for THAT plan\'s specific steps. Reference the plan\'s actual task. Second-person voice ("you"). No mention of "user".'
                if action_plans else ""
            )

            selection_prompt = f"""You are selecting the single best AI tool for a business challenge.

BUSINESS CHALLENGE: "{user_query}"{plans_block}

CANDIDATE TOOLS:
{tool_summaries}

Select the ONE tool that most directly addresses this challenge.

RULES:
- "what_it_helps": one direct sentence naming the specific task this tool solves. Second-person voice. No mention of "user" or "the user".{plan_desc_rule}

OUTPUT (JSON only, no markdown):
{{
    "selected_index": 0,
    "what_it_helps": "Direct sentence — what specific task this tool automates or solves"{plan_desc_block}
}}"""

            sel_response = await self._llm(
                model=self.fast_model,
                messages=[{"role": "user", "content": selection_prompt}],
                temperature=0.3,
                max_tokens=500 if action_plans else 250,
            )
            sel_text = sel_response.choices[0].message.content.strip()
            if "```json" in sel_text:
                sel_text = sel_text.split("```json")[1].split("```")[0].strip()
            elif "```" in sel_text:
                sel_text = sel_text.split("```")[1].split("```")[0].strip()

            sel = _safe_json_loads(sel_text)
            idx = int(sel.get("selected_index", 0))
            idx = max(0, min(idx, len(tools) - 1))
            top = tools[idx]
            tool_name = top.get("tool_name", "")

            db_row = self.db.query(AITool).filter(AITool.name == tool_name).first()
            website = db_row.url if db_row else None
            price = db_row.pricing if db_row else None

            single_tool = {
                "tool_name": tool_name,
                "description": sel.get("what_it_helps") or top.get("description", ""),
                "website": website,
                "price": price,
            }

            # Attach per-plan descriptions when available
            plan_descriptions = sel.get("plan_descriptions")
            if isinstance(plan_descriptions, dict) and plan_descriptions:
                single_tool["plan_descriptions"] = plan_descriptions

            return {"recommended_tool_stacks": [], "single_tool_recommendation": single_tool}
        except Exception as e:
            logger.error(f"Single-tool recommendation failed: {e}", exc_info=True)
            try:
                self.db.rollback()
            except Exception:
                pass
            return {"recommended_tool_stacks": [], "single_tool_recommendation": None}

    # =========================================================================
    # STAGE 4: ROADMAP & MOTIVATION AGENT
    # =========================================================================

    async def _stage4_roadmap_and_motivation(
        self, user_query: str, action_plans_result: Dict
    ) -> Dict[str, Any]:
        """
        Create a 7-day sprint execution roadmap and a motivational quote.

        Returns:
            {
                "total_phases": int,
                "estimated_days": 7,
                "execution_roadmap": [{"phase", "days", "title", "tasks"}, ...],
                "motivational_quote": str
            }
        """
        action_titles = [ap["title"] for ap in action_plans_result["action_plans"]]
        action_steps = {
            ap["title"]: ap.get("what_to_do", [])[:2]
            for ap in action_plans_result["action_plans"]
        }
        # Full (unsliced) steps — the prompt only needs the first 2 per plan to
        # stay concise, but the backfill pool below needs every real step
        # available so it doesn't run dry after the LLM already used the first 2.
        action_steps_full = [
            s for ap in action_plans_result["action_plans"]
            for s in (ap.get("what_to_do", []) if isinstance(ap.get("what_to_do"), list) else [])
        ]
        action_list = json.dumps(action_titles)
        action_steps_json = json.dumps(action_steps)

        prompt = f"""You are an execution architect who converts strategic analysis into a daily action schedule that a busy solo founder will actually complete. You understand the psychology of founder execution: momentum compounds, early wins prevent abandonment, visible progress creates motivation, and the biggest risk is not choosing the wrong action — it is doing nothing because the plan felt overwhelming.

The consulting firms produce 80-page implementation roadmaps that sit in a drawer. You produce a 7-day sprint that gets executed because each day has one clear action, one clear output, and one clear "done" signal.

PERSONA RULE: Write every text field in SECOND PERSON — "you", "your". NEVER use third-person. You are speaking directly to the person who will execute this roadmap.

USER CHALLENGE: "{user_query}"
ACTION PLANS (in priority order): {action_list}
FIRST STEPS PER ACTION: {action_steps_json}

TASK: Build a 7-day sprint roadmap that walks through the highest-priority actions in a logical, achievable sequence. Then write a motivational quote.

ROADMAP RULES:
1. Break the 7 days into 2-4 phases — no more than 4
2. Phase names describe what YOU are actively DOING in second person ("Days 1-2: Audit Your Current Funnel and Identify the Three Biggest Leaks" not "Phase 1: Discovery")
3. Each phase contains tasks drawn directly from the action plan steps above — no invented tasks
4. Where a task involves a tool, name the specific first action to take in it
5. Tasks are complete sentences in second person: specific action + specific output. Each task MUST be expressible in 1–2 sentences maximum — no long paragraphs. A reader must be able to read any single task in under 10 seconds.
6. The total span equals exactly 7 days
10. CRITICAL: The total number of tasks across ALL phases MUST equal exactly 7. Count carefully before submitting: tasks_in_phase_1 + tasks_in_phase_2 + ... = 7. Distribute tasks unevenly across phases if needed to hit 7 exactly.
7. Sequence: AUDIT AND DECIDE first → BUILD AND SHIP second → MEASURE AND ADJUST last
8. No task repeats across phases
9. FORMATTING: Do NOT start any task with a dash (-), bullet, or em dash. Write plain complete sentences only.

QUICK WIN — identify the single task from your #1 ranked action plan completable in 90 minutes or less that produces a VISIBLE, TANGIBLE output — something you can screenshot, share, or point to as evidence of progress. Not "research" or "think about." This anchors day 1.

QUOTE RULES:
1. Speaks directly to YOUR specific challenge — not a generic motivational poster
2. Acknowledges the real difficulty — the weight, the doubt, the overwhelm. Do not pretend this is easy.
3. Then grounds it in evidence: you have seen this pattern resolve, you know what the first signal of momentum looks like.
4. Maximum 2 sentences. Sounds like a mentor who has personally watched someone in this exact situation find their way through — not a motivational speaker who has never been in the arena.

OUTPUT FORMAT (JSON only, no markdown):
{{
    "total_phases": 3,
    "estimated_days": 7,
    "execution_roadmap": [
        {{
            "phase": "Days 1-2: Specific phase name",
            "days": 2,
            "title": "What the user achieves this phase",
            "tasks": [
                "Specific task drawn from action plans",
                "Another specific task"
            ]
        }}
    ],
    "motivational_quote": "A quote that speaks directly to this user's situation"
}}"""

        # Flat pool of real action-plan steps, used only as a last-resort backfill
        # source (never fabricated text) if the LLM under-generates after retries.
        # Uses the FULL step lists (not the 2-per-plan slice fed to the prompt) so
        # the pool isn't already exhausted by the time a backfill is needed.
        all_step_pool = action_steps_full

        result = None
        total_tasks = 0
        # 3→2: the real backfill below (using unused action-plan steps, never
        # invented text) makes a 3rd retry mostly wasted latency for a rare case
        # that's already handled safely.
        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            attempt_prompt = prompt
            if attempt > 1:
                attempt_prompt += (
                    f"\n\nRETRY NOTICE: your previous attempt produced {total_tasks} tasks, "
                    f"not exactly 7. Recount before responding: "
                    f"tasks_in_phase_1 + tasks_in_phase_2 + ... MUST equal 7 exactly."
                )
            try:
                response = await self._llm(
                    model=self.fast_model,
                    messages=[{"role": "user", "content": attempt_prompt}],
                    temperature=0.7,   # slightly lower = fewer hallucinations, faster
                    max_tokens=600,    # 800→600: roadmap JSON is typically ~400 tokens
                )
                result_text = response.choices[0].message.content.strip()

                if "```json" in result_text:
                    result_text = result_text.split("```json")[1].split("```")[0].strip()
                elif "```" in result_text:
                    result_text = result_text.split("```")[1].split("```")[0].strip()

                result = _safe_json_loads(result_text)

                # Deduplicate tasks within each phase at the source so the frontend
                # doesn't have to deal with LLM repetitions.
                for phase in result.get("execution_roadmap", []):
                    seen: set = set()
                    deduped = []
                    for t in phase.get("tasks", []):
                        key = str(t).lower().strip()[:60]
                        if key not in seen:
                            seen.add(key)
                            deduped.append(t)
                    phase["tasks"] = deduped

                # Enforce exactly 7 tasks total (missions must equal the 7 days of
                # the week — never fewer, never more). Trim excess deterministically.
                roadmap = result.get("execution_roadmap", [])
                total_tasks = sum(len(p.get("tasks", [])) for p in roadmap)
                if total_tasks > 7:
                    remaining = 7
                    for phase in roadmap:
                        tasks = phase.get("tasks", [])
                        take = min(len(tasks), remaining)
                        phase["tasks"] = tasks[:take]
                        remaining -= take
                        if remaining == 0:
                            for later_phase in roadmap[roadmap.index(phase) + 1:]:
                                later_phase["tasks"] = []
                            break
                    logger.info(f"Trimmed roadmap tasks from {total_tasks} → 7")
                    total_tasks = 7

                if total_tasks == 7:
                    break
                logger.warning(
                    f"Attempt {attempt}/{max_attempts}: roadmap has {total_tasks} tasks (expected 7)"
                )
            except Exception as e:
                logger.error(f"Stage 4 attempt {attempt}/{max_attempts} failed: {e}")
                if attempt == max_attempts:
                    raise

        # If every retry still under-generated, backfill the deficit with real,
        # unused action-plan steps (never invented text) so the count is always 7.
        roadmap = result.get("execution_roadmap", [])
        if total_tasks < 7 and roadmap:
            used = {str(t).lower().strip()[:60] for p in roadmap for t in p.get("tasks", [])}
            pool = [s for s in all_step_pool if str(s).lower().strip()[:60] not in used]
            last_phase = roadmap[-1]
            while total_tasks < 7 and pool:
                candidate = pool.pop(0)
                key = str(candidate).lower().strip()[:60]
                if key in used:
                    continue
                last_phase.setdefault("tasks", []).append(candidate)
                used.add(key)
                total_tasks += 1
            if total_tasks < 7:
                logger.error(
                    f"Roadmap still has only {total_tasks} tasks after backfill — "
                    f"not enough distinct action-plan steps to reach 7"
                )
            else:
                logger.info("Backfilled roadmap to exactly 7 tasks using real action-plan steps")
            result["execution_roadmap"] = roadmap

        logger.info(
            f"Created {result['total_phases']}-phase roadmap ({result['estimated_days']} days)"
        )
        return result

    # =========================================================================
    # CONFIDENCE SCORE
    # =========================================================================

    def _calculate_confidence_score(
        self,
        primary_result: Dict,
        action_plans_result: Dict,
        roadmap_result: Dict,
        automation_stack_result: Optional[Dict] = None,
    ) -> int:
        """
        Dynamic confidence score (62–91) scaled by actual content depth.

        Uses proportional scoring based on text lengths, plan/tool counts,
        and roadmap completeness so identical checks don't always yield 98.
        """
        score = 58

        primary = primary_result.get("primary_bottleneck", {})
        title_len = len(primary.get("title", ""))
        desc_len = len(primary.get("description", ""))

        if title_len >= 25:
            score += 6
        elif title_len >= 12:
            score += 4
        elif title_len > 0:
            score += 1

        if desc_len >= 150:
            score += 7
        elif desc_len >= 60:
            score += 5
        elif desc_len >= 20:
            score += 2

        strategic = primary_result.get("strategic_priority", "")
        strat_len = len(strategic)
        if strat_len >= 100:
            score += 6
        elif strat_len >= 40:
            score += 4
        elif strat_len >= 15:
            score += 2

        action_plans = action_plans_result.get("action_plans", [])
        num_plans = len(action_plans)
        if num_plans >= 4:
            score += 7
        elif num_plans == 3:
            score += 5
        elif num_plans == 2:
            score += 3
        elif num_plans == 1:
            score += 1

        # Depth of action plan steps
        total_steps = sum(len(ap.get("steps", [])) for ap in action_plans)
        if total_steps >= 12:
            score += 5
        elif total_steps >= 6:
            score += 3
        elif total_steps >= 2:
            score += 1

        tools_count = sum(
            1 for ap in action_plans for t in (ap.get("step_tools") or []) if t
        )
        score += min(tools_count * 2, 6)

        stack_count = len((automation_stack_result or {}).get("recommended_tool_stacks", []))
        if stack_count >= 3:
            score += 5
        elif stack_count >= 1:
            score += 3

        roadmap = roadmap_result.get("execution_roadmap", [])
        if len(roadmap) >= 5:
            score += 4
        elif len(roadmap) >= 3:
            score += 3
        elif len(roadmap) >= 1:
            score += 1

        est_days = roadmap_result.get("estimated_days", 0) or 0
        if est_days >= 60:
            score += 3
        elif est_days > 0:
            score += 2

        return max(62, min(score, 91))

    # =========================================================================
    # DATABASE SAVE
    # =========================================================================

    async def _save_to_database(
        self,
        user_id: int,
        user_query: str,
        primary_result: Dict,
        secondary_result: Dict,
        action_plans_result: Dict,
        automation_stack_result: Dict,
        roadmap_result: Dict,
        duration: float,
        confidence_score: int,
        recommendation_mode: str = "automation_stack",
    ) -> int:
        """Persist analysis results to the database."""
        from database.pg_models import BusinessAnalysis

        # Clear any aborted transaction state left by earlier DB operations
        # (e.g., failed tool lookups in _recommend_single_tool or _stage3_automation_stacks).
        # Only read operations happen before this point, so rolling back is safe.
        try:
            self.db.rollback()
        except Exception:
            pass

        try:
            single_tool_recommendation = automation_stack_result.get("single_tool_recommendation")

            analysis = BusinessAnalysis(
                user_id=user_id,
                business_goal=user_query,
                primary_bottleneck=json.dumps(primary_result["primary_bottleneck"]),
                secondary_constraints=json.dumps(secondary_result["secondary_constraints"]),
                what_to_stop=primary_result["what_to_stop"],
                strategic_priority=primary_result["strategic_priority"],
                action_plans=json.dumps(action_plans_result["action_plans"]),
                recommended_tool_stacks=json.dumps(
                    automation_stack_result.get("recommended_tool_stacks", [])
                ),
                total_phases=roadmap_result["total_phases"],
                estimated_days=roadmap_result["estimated_days"],
                execution_roadmap=json.dumps(roadmap_result["execution_roadmap"]),
                exclusions_note=action_plans_result["exclusions_note"],
                motivational_quote=roadmap_result["motivational_quote"],
                confidence_score=confidence_score,
                duration=f"{duration:.1f}s",
                analysis_type="agentic",
                insights_count=len(action_plans_result["action_plans"]),
                recommendations_count=sum(
                    1
                    for ap in action_plans_result["action_plans"]
                    for t in (ap.get("step_tools") or [])
                    if t
                ),
                recommendation_mode=recommendation_mode,
                single_tool_recommendation=single_tool_recommendation,
            )

            self.db.add(analysis)
            self.db.commit()
            self.db.refresh(analysis)

            logger.info(f"Saved analysis ID: {analysis.id}")
            return analysis.id

        except Exception as e:
            logger.error(f"Failed to save analysis: {e}")
            self.db.rollback()
            raise

    # =========================================================================
    # FORMAT FOR FRONTEND
    # =========================================================================

    def _format_for_frontend(
        self,
        analysis_id: int,
        user_query: str,
        primary_result: Dict,
        secondary_result: Dict,
        action_plans_result: Dict,
        automation_stack_result: Dict,
        roadmap_result: Dict,
        recommendation_mode: str = "automation_stack",
    ) -> Dict[str, Any]:
        """Format analysis results for the frontend result page."""
        return {
            "success": True,
            "data": {
                "analysis_id": analysis_id,
                "business_goal": user_query,
                "primary_bottleneck": primary_result["primary_bottleneck"],
                "secondary_constraints": secondary_result["secondary_constraints"],
                "what_to_stop": primary_result["what_to_stop"],
                "strategic_priority": primary_result["strategic_priority"],
                "action_plans": action_plans_result["action_plans"],
                "recommended_tool_stacks": automation_stack_result.get(
                    "recommended_tool_stacks", []
                ),
                "recommendation_mode": recommendation_mode,
                "single_tool_recommendation": automation_stack_result.get("single_tool_recommendation"),
                "total_phases": roadmap_result["total_phases"],
                "estimated_days": roadmap_result["estimated_days"],
                "execution_roadmap": roadmap_result["execution_roadmap"],
                "exclusions_note": action_plans_result["exclusions_note"],
                "motivational_quote": roadmap_result["motivational_quote"],
                "created_at": datetime.now().isoformat(),
                "ai_model": self.model,
            },
        }


def create_analyzer(db_session: Session) -> AgenticAnalyzer:
    """Create an AgenticAnalyzer instance."""
    return AgenticAnalyzer(db_session)
