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
        """Semantic search for relevant AI tools from the database."""
        try:
            search_query = f"{user_query} {action_description}"
            tools = recommend_tools(search_query, top_k=top_k, db_session=self.db)
            logger.info(
                f"Found {len(tools)} tools via semantic search for: {action_description[:50]}..."
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
        prompt = f"""You are an elite business consultant analyzing a user's business challenge.

USER QUERY: "{user_query}"

Your task: Identify THE SINGLE most critical bottleneck blocking their success.

CRITICAL RULES:
1. Identify ONLY ONE primary bottleneck (not 3-5, just ONE)
2. The bottleneck must be the ROOT CAUSE, not a symptom
3. Describe what's ACTUALLY broken in simple terms
4. Explain the consequence if they ignore this (be specific and impactful)
5. Identify the strategic priority (what they should focus on)
6. Identify ONE critical action they must STOP doing (wastes time/resources)
7. RECOMMENDATION MODE: Set "recommendation_mode" to "single_tool" if the bottleneck can be directly addressed by adopting ONE focused AI tool (writing assistant, scheduling tool, analytics dashboard) — the workflow does NOT require tools to hand off to each other. Set "automation_stack" if the bottleneck requires MULTIPLE tools working in sequence (triggers, data flow between apps, cross-platform automation). One tool alone cannot solve it.

OUTPUT FORMAT (JSON):
{{
    "primary_bottleneck": {{
        "title": "Clear, concise title (5-10 words)",
        "description": "What's actually broken (2-3 sentences, be direct)",
        "consequence": "Specific consequence if ignored (1-2 sentences, make it real)"
    }},
    "strategic_priority": "The ONE thing they should focus on (1 sentence)",
    "what_to_stop": "The ONE action they must stop immediately (1 sentence, be direct)",
    "recommendation_mode": "single_tool or automation_stack"
}}

Think like a consultant who charges $500/hour. Be brutally honest, specific, and actionable."""

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

            result = json.loads(result_text)
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

        prompt = f"""You are an elite business consultant analyzing secondary issues.

USER QUERY: "{user_query}"
PRIMARY BOTTLENECK: "{primary_title}"

Your task: Identify 2-4 SECONDARY constraints that compound the primary issue.

CRITICAL RULES:
1. These are NOT as critical as the primary bottleneck
2. They should be related but distinct issues
3. Keep descriptions brief and actionable
4. Order by impact (most impactful first)
5. If the user's situation is simple, return only 2 constraints
6. Maximum 4 constraints

OUTPUT FORMAT (JSON):
{{
    "secondary_constraints": [
        {{
            "id": 1,
            "title": "Brief title (5-8 words)",
            "description": "What's the issue (1-2 sentences)"
        }},
        {{
            "id": 2,
            "title": "Brief title (5-8 words)",
            "description": "What's the issue (1-2 sentences)"
        }}
    ]
}}

Be specific and practical."""

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

            result = json.loads(result_text)
            logger.info(f"Identified {len(result['secondary_constraints'])} secondary constraints")
            return result

        except Exception as e:
            logger.error(f"Stage 2 failed: {e}")
            raise

    async def _attach_toolkit(self, plan: dict, user_query: str) -> dict:
        """Fetch and attach the best AI tool for a single action plan (runs in parallel)."""
        if not plan.get("needs_ai_tool", False):
            plan["toolkit"] = None
            plan.pop("needs_ai_tool", None)
            return plan

        tools = await self._search_ai_tools(
            user_query=user_query,
            action_description=f"{plan['title']} - {plan.get('what_to_do', '')}",
            top_k=3,
        )

        if not tools:
            plan["toolkit"] = None
            plan.pop("needs_ai_tool", None)
            return plan

        tool_names = [f"{t['tool_name']}: {t['description'][:100]}" for t in tools]
        prompt_tool_selection = f"""You are selecting the best AI tool for a specific action.

ACTION: {plan['title']}
WHAT TO DO: {plan.get('what_to_do', '')}

AVAILABLE TOOLS (from semantic search):
{chr(10).join([f"{i+1}. {t}" for i, t in enumerate(tool_names)])}

Your task: Pick the BEST tool for this action, or return null if none are good fits.

OUTPUT FORMAT (JSON):
{{
    "selected_tool_index": 0 or null,
    "toolkit": {{
        "tool_name": "Selected tool name",
        "what_it_helps": "What it specifically helps with for this action (1 sentence)",
        "why_this_tool": "Why this tool is best for this action (1 sentence)"
    }} or null
}}

Only recommend if it genuinely adds value."""

        try:
            tool_response = await self._llm(
                model=self.fast_model,
                messages=[{"role": "user", "content": prompt_tool_selection}],
                temperature=0.6,
                max_tokens=300,
            )
            tool_text = tool_response.choices[0].message.content.strip()
            if "```json" in tool_text:
                tool_text = tool_text.split("```json")[1].split("```")[0].strip()
            elif "```" in tool_text:
                tool_text = tool_text.split("```")[1].split("```")[0].strip()
            tool_selection = json.loads(tool_text)
            plan["toolkit"] = tool_selection.get("toolkit")
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

        prompt_actions = f"""You are an elite business strategist creating an action plan.

USER QUERY: "{user_query}"
PRIMARY BOTTLENECK: "{primary_title}"
SECONDARY CONSTRAINTS: {constraints}

Your task: Create 3-5 RANKED action plans that solve the primary bottleneck.

CRITICAL RULES:
1. Order by LEVERAGE (highest impact first, not chronological)
2. Each action must directly address the primary bottleneck
3. "what_to_do" = a LIST of clear, executable steps (complete, meaningful sentences)
4. "why_it_matters" = a LIST of business impact/reasoning points (complete, meaningful sentences)
5. "effort_level" = Low, Medium, or High
6. "needs_ai_tool" = true if AI automation could significantly help, false otherwise
7. Maximum 5 action plans
8. Include an "exclusions_note" explaining what you intentionally excluded

OUTPUT FORMAT (JSON):
{{
    "action_plans": [
        {{
            "id": 1,
            "title": "Action title (5-10 words)",
            "what_to_do": ["Step 1", "Step 2"],
            "why_it_matters": ["Impact 1"],
            "effort_level": "Low",
            "needs_ai_tool": false
        }}
    ],
    "exclusions_note": "What strategies you excluded and why (2-3 sentences)"
}}

Be practical and specific."""

        try:
            response = await self._llm(
                model=self.fast_model,
                messages=[{"role": "user", "content": prompt_actions}],
                temperature=0.7,
                max_tokens=1500,
            )
            result_text = response.choices[0].message.content.strip()

            if "```json" in result_text:
                result_text = result_text.split("```json")[1].split("```")[0].strip()
            elif "```" in result_text:
                result_text = result_text.split("```")[1].split("```")[0].strip()

            result = json.loads(result_text)
            action_plans = result["action_plans"]

            # Attach toolkits to all plans in parallel
            action_plans_with_toolkits = await asyncio.gather(*[
                self._attach_toolkit(plan, user_query) for plan in action_plans
            ])
            result["action_plans"] = list(action_plans_with_toolkits)

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

            llm_data = json.loads(result_text)

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
            tools = recommend_tools(user_query, top_k=1, db_session=self.db)
            if not tools:
                return {"recommended_tool_stacks": [], "single_tool_recommendation": None}
            top = tools[0]
            tool_name = top.get("tool_name", "")
            if not tool_name:
                return {"recommended_tool_stacks": [], "single_tool_recommendation": None}
            db_row = self.db.query(AITool).filter(AITool.name == tool_name).first()
            website = db_row.url if db_row else None
            price = db_row.pricing if db_row else None
            single_tool = {
                "tool_name": tool_name,
                "description": top.get("description", ""),
                "why_this_tool": "Directly addresses your bottleneck with focused AI assistance",
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
        action_list = json.dumps(action_titles)

        prompt = f"""You are an execution strategist creating a realistic timeline.

USER QUERY: "{user_query}"
ACTION PLANS: {action_list}

Your task: Create a strict 7-Day Sprint execution roadmap AND generate a motivational quote.

CRITICAL RULES FOR ROADMAP:
1. Break into 2-4 phases (not more than 4)
2. Each phase = specific day range (e.g., "Days 1-3", "Days 4-7")
3. Each phase has 2-4 concrete tasks
4. Total timeline must be exactly 7 days (a 7-Day Sprint)
5. Order phases logically (setup → execute → optimize)

CRITICAL RULES FOR QUOTE:
1. Generate a UNIQUE motivational quote based on their specific challenge
2. Should be encouraging but realistic
3. 1-2 sentences maximum
4. Reference their specific situation (not generic)

OUTPUT FORMAT (JSON):
{{
    "total_phases": 3,
    "estimated_days": 7,
    "execution_roadmap": [
        {{
            "phase": "Days 1-3: The Fix",
            "days": 3,
            "title": "Research & Planning",
            "tasks": [
                "Task 1 description",
                "Task 2 description"
            ]
        }}
    ],
    "motivational_quote": "You're not behind - you're just early in the sequence. Focus on the bottleneck, and everything else becomes noise."
}}

Be practical and encouraging."""

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

            result = json.loads(result_text)

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
