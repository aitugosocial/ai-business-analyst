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
    from openai import OpenAI
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

        self.client = OpenAI(
            api_key=api_key,
            base_url="https://api.x.ai/v1",
            timeout=120.0,
        )
        logger.info("xAI Grok client initialized for agentic analysis")

    async def _llm(self, **kwargs):
        """Run a blocking OpenAI chat completion in a thread so the event loop stays free."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self.client.chat.completions.create(**kwargs))

    # =========================================================================
    # SEMANTIC TOOL SEARCH (used by Stage 3)
    # =========================================================================

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
            tools = recommend_tools(action_description, top_k=top_k, db_session=self.db)
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
        prompt = f"""You are a safety filter for a business analysis engine.
Your job: determine whether the following user query is a legitimate business challenge.

USER QUERY: "{user_query}"

A query is UNSAFE if it is asking for help with:
- Illegal activity (fraud, tax evasion, money laundering, bribery, counterfeiting)
- Hacking, data theft, or system intrusion
- Harm to individuals, groups, or competitors
- Weapons, drugs, or controlled substance trade
- Exploitation of workers, children, or vulnerable people
- Any clearly criminal or unethical purpose

A query is SAFE if it is:
- A real business challenge, even if the business is struggling
- Questions about marketing, operations, growth, hiring, finance, tech
- Questions about competitive strategy, pivoting, or recovery
- Questions that sound edgy but have a legitimate business interpretation

OUTPUT FORMAT (JSON, no markdown):
{{
    "is_safe": true or false,
    "reason": "One sentence explaining why it is or isn't safe",
    "suggestions": ["Alternative framing 1", "Alternative framing 2"]
}}

Only return is_safe: false if you are CERTAIN the query is harmful or illegal.
When in doubt, return is_safe: true."""

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
        prompt = f"""You are a senior business strategist with deep expertise across industries — retail, SaaS, services, manufacturing, healthcare, e-commerce, fintech, hospitality, agriculture, and more.

USER CHALLENGE: "{user_query}"

Your task: Diagnose THE SINGLE root-cause bottleneck that, if fixed, would unlock the most progress for this person.

DIAGNOSIS FRAMEWORK (apply in order):
1. What is the user actually trying to achieve? What result do they want?
2. What is the REAL constraint — not the symptom they described, but the underlying cause behind it?
3. What hard evidence or pattern in their description points to this root cause?
4. What are the measurable consequences of leaving this bottleneck unaddressed for 90 days?
5. What is the ONE high-leverage action that addresses the root cause directly?
6. What is one common but wasteful behavior this person should immediately stop?

CRITICAL OUTPUT RULES:
- ONE bottleneck only. Not a list, not "it could be X or Y". Commit to the single most important one.
- The bottleneck title must name the specific business problem (e.g. "No Repeatable Lead Generation System", not "Marketing Issues")
- Description must identify the gap between current state and required state. Be concrete.
- Consequence must quantify the business impact (lost revenue, churn, missed opportunities) in specific terms.
- strategic_priority must name a specific, actionable focus area — not a platitude.
- what_to_stop must name a specific behavior or activity to eliminate — not a vague instruction.
- RECOMMENDATION MODE RULE:
  - "single_tool" = bottleneck can be directly solved by ONE AI/SaaS tool being adopted (e.g. a CRM, writing assistant, analytics tool). Tools do NOT need to pass data between each other.
  - "automation_stack" = bottleneck requires MULTIPLE tools working in sequence with data handoffs (e.g. CRM → email automation → analytics). One tool alone is insufficient.

OUTPUT FORMAT (JSON only, no markdown):
{{
    "primary_bottleneck": {{
        "title": "Specific problem title (5-10 words)",
        "description": "What is broken and why — be specific about the gap (2-3 sentences)",
        "consequence": "What happens in 90 days if this isn't fixed — name the business cost (1-2 sentences)"
    }},
    "strategic_priority": "The single most important thing to focus on this month (1 specific sentence)",
    "what_to_stop": "The specific wasteful action to eliminate immediately (1 direct sentence)",
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

        prompt = f"""You are a senior business strategist mapping the constraint landscape around a diagnosed bottleneck.

USER CHALLENGE: "{user_query}"
PRIMARY BOTTLENECK: "{primary_title}"

Your task: Identify the 2-4 secondary constraints that make the primary bottleneck WORSE or prevent the user from fixing it.

CONSTRAINT IDENTIFICATION RULES:
1. Secondary constraints COMPOUND the primary — they either feed into it or reduce the user's capacity to solve it
2. Each constraint must be DISTINCT — no overlaps with each other or with the primary bottleneck
3. Each constraint must be grounded in something the user described or that is a predictable consequence of their situation
4. Order by impact severity (most damaging to least)
5. If the situation is straightforward, return exactly 2. If complex, return 3-4.
6. Each description must explain: (a) what the constraint IS, and (b) how it makes the primary bottleneck harder to solve

OUTPUT FORMAT (JSON only, no markdown):
{{
    "secondary_constraints": [
        {{
            "id": 1,
            "title": "Specific constraint name (5-8 words)",
            "description": "What the constraint is and how it compounds the primary bottleneck (2 sentences)"
        }}
    ]
}}"""

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
6. CRITICAL — "why_this_tool": name the specific feature or function that makes this the right choice for these steps. One sentence, no generic claims.
7. Return the tool's website URL from the candidate list exactly as shown.

OUTPUT FORMAT (JSON only, no markdown, no leading dashes in any text value):
{{
    "toolkits": [
        {{
            "tool_name": "Exact name from the candidate list",
            "website": "Exact URL from the candidate list, or null",
            "what_it_helps": "Step N: one concrete sentence describing what the tool does for that specific step",
            "why_this_tool": "One sentence naming the specific feature that makes this tool the right fit"
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
    # STAGE 3: ACTION PLANS AGENT
    # =========================================================================

    async def _stage3_action_plans(
        self,
        user_query: str,
        primary_result: Dict,
        secondary_result: Dict,
    ) -> Dict[str, Any]:
        """
        Generate ranked action plans with AI tools matched via semantic search.

        Workflow:
        1. LLM generates action plans and flags which need an AI tool
        2. Semantic search retrieves matching tool candidates from DB
        3. LLM selects the best match and attaches it as a toolkit

        Returns:
            {
                "action_plans": [
                    {
                        "id", "title", "what_to_do", "why_it_matters",
                        "effort_level", "toolkit": {"tool_name", "what_it_helps", "why_this_tool"} | null
                    }
                ],
                "exclusions_note": str
            }
        """
        primary_title = primary_result["primary_bottleneck"]["title"]
        constraints = json.dumps([c["title"] for c in secondary_result["secondary_constraints"]])

        prompt_actions = f"""You are a senior business strategist building a ranked action plan. You think in first principles and prioritize leverage over effort.

USER CHALLENGE: "{user_query}"
PRIMARY BOTTLENECK: "{primary_title}"
SECONDARY CONSTRAINTS: {constraints}

Your task: Create 3-5 action plans that directly dismantle the primary bottleneck. Rank them by leverage — the plan that will produce the fastest measurable result goes first.

ACTION PLAN RULES:
1. RANK by leverage, not by chronological order or alphabetical order
2. Every action must address the PRIMARY BOTTLENECK directly — not a secondary constraint
3. "what_to_do" must be a LIST of 3-5 specific, executable steps the user can act on today. Each step is a complete sentence. Do NOT be vague (no "research options" or "consider doing X"). Name the exact action.
4. "why_it_matters" must be a LIST of 2-3 business impact statements. Each statement names a specific outcome: revenue gained, cost saved, churn reduced, speed increased, risk eliminated. Complete sentences.
5. "effort_level" = one of: Low (days), Medium (1-2 weeks), or High (weeks to months)
6. "needs_ai_tool" = true ONLY if an AI or SaaS tool would meaningfully accelerate or automate a specific step in what_to_do. false if this is purely a human/process action.
7. Keep action titles specific (e.g. "Build a Weekly Referral Outreach System" not "Improve Marketing")
8. Maximum 5 action plans. Minimum 3.
9. "exclusions_note" must name the strategies you considered but excluded, and give a concrete reason for each exclusion
10. FORMATTING: Do NOT start any list item with a dash (-), bullet, or em dash. Write plain complete sentences only.

OUTPUT FORMAT (JSON only, no markdown):
{{
    "action_plans": [
        {{
            "id": 1,
            "title": "Specific action title (5-10 words)",
            "what_to_do": [
                "Step 1: specific, actionable, named action (no dashes, no bullet prefixes)",
                "Step 2: specific, actionable, named action (no dashes, no bullet prefixes)"
            ],
            "why_it_matters": [
                "Specific business impact with a named outcome"
            ],
            "effort_level": "Low",
            "needs_ai_tool": false
        }}
    ],
    "exclusions_note": "Name 2-3 strategies you excluded and exactly why each was deprioritized"
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

            # Attach toolkits sequentially so the shared `used_tool_names` set
            # correctly prevents any tool from appearing in two action cards.
            # plan_index and total_plans are passed so the LLM prompt can
            # reference its position in the analysis ("plan 2 of 3") and reason
            # about what has already been assigned.
            used_tool_names: set = set()
            action_plans_with_toolkits = []
            total_plans = len(action_plans)
            for plan_index, plan in enumerate(action_plans):
                enriched = await self._attach_toolkit(
                    plan, user_query, used_tool_names,
                    plan_index=plan_index, total_plans=total_plans,
                )
                action_plans_with_toolkits.append(enriched)
            result["action_plans"] = action_plans_with_toolkits

            logger.info(f"Generated {len(result['action_plans'])} action plans with semantic tool matching")
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

        prompt = f"""You are an automation workflow expert. A semantic search engine selected these tools from a live database to match a user's business problem. Explain HOW they work together as a workflow.

STRICT RULE: You MUST ONLY reference these exact tool names from the database: {allowed_names_str}
Do NOT mention, suggest, or invent any other tools.

USER QUERY: "{user_query}"
PRIMARY BOTTLENECK: "{primary_bottleneck}"

TOOLS SELECTED FROM DATABASE:
{tool_context}

Explain how these {len(tools)} tool(s) form an automation workflow for this user.

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
                return await self._recommend_single_tool(user_query)

            action_plans = action_plans_result.get("action_plans", []) or []
            stacks = recommend_automation_stacks(
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
            return {"recommended_tool_stacks": [], "single_tool_recommendation": None}

    async def _recommend_single_tool(self, user_query: str) -> Dict[str, Any]:
        """
        Stage 3B (single_tool mode): Return the single best AI tool for the user's query.
        Used when recommendation_mode == "single_tool" — one focused tool, no stack needed.
        """
        from database.pg_models import AITool
        try:
            tools = recommend_tools(user_query, top_k=3, db_session=self.db)
            if not tools:
                return {"recommended_tool_stacks": [], "single_tool_recommendation": None}

            # Use LLM to select the best of the top-3 matches and generate
            # a specific, action-relevant reason — not a generic fallback.
            tool_summaries = [
                f"{i+1}. {t['tool_name']}: {t['description'][:150]}"
                for i, t in enumerate(tools)
            ]
            selection_prompt = f"""You are selecting the single best AI tool for a user's business challenge.

USER CHALLENGE: "{user_query}"

CANDIDATE TOOLS (from semantic search):
{chr(10).join(tool_summaries)}

Select the ONE tool that most directly helps solve this specific challenge.

OUTPUT FORMAT (JSON only, no markdown):
{{
    "selected_index": 0,
    "what_it_helps": "Explain what specific part of the user's challenge this tool addresses (1 concrete sentence — name the actual task or problem it solves)",
    "why_this_tool": "Name the specific capability that makes this tool the right choice for this user's situation (1 sentence — be specific, not generic)"
}}"""

            sel_response = await self._llm(
                model=self.fast_model,
                messages=[{"role": "user", "content": selection_prompt}],
                temperature=0.3,
                max_tokens=250,
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
                "why_this_tool": sel.get("why_this_tool") or top.get("description", ""),
                "website": website,
                "price": price,
            }
            return {"recommended_tool_stacks": [], "single_tool_recommendation": single_tool}
        except Exception as e:
            logger.error(f"Single-tool recommendation failed: {e}", exc_info=True)
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
        action_list = json.dumps(action_titles)
        action_steps_json = json.dumps(action_steps)

        prompt = f"""You are an execution sprint planner who turns strategy into a concrete 7-day action schedule.

USER CHALLENGE: "{user_query}"
ACTION PLANS (in priority order): {action_list}
FIRST STEPS PER ACTION: {action_steps_json}

Your task: Build a 7-Day Sprint roadmap that walks the user through the highest-priority actions in a logical, achievable sequence. Then write a motivational quote.

ROADMAP RULES:
1. Break the 7 days into 2-4 phases — no more than 4
2. Phase names must describe what the user is DOING (e.g. "Days 1-2: Diagnose and Decide", not "Phase 1")
3. Each phase must contain 2-4 tasks drawn directly from the action plan steps above
4. Tasks must be specific and actioned ("Set up your CRM pipeline with 3 stages" not "Do CRM work")
5. The total span must equal exactly 7 days
6. Order: foundation/diagnosis first, execution second, review/optimize last
7. Do NOT repeat the same task across phases
8. FORMATTING: Do NOT start any task with a dash (-), bullet, or em dash. Write plain complete sentences only.

QUOTE RULES:
1. Write a quote that speaks directly to the user's specific challenge — not a generic business platitude
2. It must be encouraging AND grounded in reality (acknowledge the difficulty)
3. Maximum 2 sentences
4. Sound like a mentor who has seen this situation before and knows they can get through it

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

        try:
            response = await self._llm(
                model=self.fast_model,
                messages=[{"role": "user", "content": prompt}],
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

            logger.info(
                f"Created {result['total_phases']}-phase roadmap ({result['estimated_days']} days)"
            )
            return result

        except Exception as e:
            logger.error(f"Stage 4 failed: {e}")
            raise

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
        Dynamic confidence score (75–98) based on analysis completeness.

        Factors: bottleneck quality, action plan count, tool recommendations,
        automation stack count, roadmap completeness.
        """
        score = 75

        primary = primary_result.get("primary_bottleneck", {})
        if primary.get("title") and len(primary.get("title", "")) > 10:
            score += 5
        if primary.get("description") and len(primary.get("description", "")) > 20:
            score += 5

        if primary_result.get("strategic_priority") and len(primary_result.get("strategic_priority", "")) > 15:
            score += 5

        action_plans = action_plans_result.get("action_plans", [])
        num_plans = len(action_plans)
        if num_plans >= 2:
            score += 4
        if 3 <= num_plans <= 4:
            score += 2

        tools_count = len([ap for ap in action_plans if ap.get("toolkit")])
        if tools_count > 0:
            score += min(tools_count * 2, 5)

        stack_count = len((automation_stack_result or {}).get("recommended_tool_stacks", []))
        if stack_count > 0:
            score += min(stack_count * 2, 5)

        roadmap = roadmap_result.get("execution_roadmap", [])
        if len(roadmap) >= 2:
            score += 3
        if roadmap_result.get("estimated_days", 0) > 0:
            score += 2

        return min(score, 98)

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
                recommendations_count=len(
                    [ap for ap in action_plans_result["action_plans"] if ap.get("toolkit")]
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
