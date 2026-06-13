"""
LAVOO INTELLIGENCE - Unified Insights & Opportunities Generator
Uses Grok with web search capabilities to produce a single combined feed of
INSIGHTS and OPPORTUNITIES in one JSON response, per the LAVOO INTELLIGENCE
system prompt (verbatim, unmodified below).

Workflow:
1. Grok searches the web following the LAVOO INTELLIGENCE prompt
2. Returns one JSON object: { generated_at, insights: [...], opportunities: [...] }
3. Each list is filtered for duplicates / suspicious or dead URLs
4. Insights are saved via save_insights() into the Insight table
5. Opportunities are saved via save_alerts() into the Alert table
"""

import json
import logging
import os
import hashlib
import requests
from datetime import date
from typing import Dict, List, Tuple

from dotenv import load_dotenv
from sqlalchemy.orm import Session

# Load environment variables
load_dotenv('.env.local')

# Import xAI SDK for Grok API with Agent Tools
try:
    from xai_sdk import Client
    from xai_sdk.chat import user
    from xai_sdk.tools import web_search
    HAS_XAI = True
except ImportError:
    HAS_XAI = False
    print("Warning: xai-sdk package not installed. Install with: uv add xai-sdk")

# Import database models
from database.pg_connections import SessionLocal
from database.pg_models import Insight, Alert

logger = logging.getLogger(__name__)

# Configure logging for standalone script execution
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)


# Verbatim "LAVOO INTELLIGENCE" system prompt. Do not edit the wording -
# this is the working format the LLM tool is required to use.
LAVOO_INTELLIGENCE_PROMPT = """You are LAVOO INTELLIGENCE: the world's most advanced AI analyst built exclusively for ambitious creators, founders, and business operators who pay a premium to stay ahead.

You fuse the roles of investigative technology journalist, venture capital intelligence analyst, and elite opportunity scout. You think faster than Bloomberg, write sharper than Wired, and spot opportunities earlier than any human analyst team.

Your single mission on every run: sweep the live web, filter ruthlessly, and return only the intelligence that makes a premium subscriber feel the subscription was worth it the moment they read the first item. Every item must pass the "dinner party test", would a smart, ambitious person stop mid-conversation to share this? If not, cut it.

You deliver two types of intelligence in one unified feed:

INSIGHTS — Fresh developments in AI, business, tech, markets, and creator tools that signal where the world is heading before the crowd notices. Subscribers gain a competitive edge by knowing first and thinking faster.

OPPORTUNITIES — Specific, time-bound openings: grants, funding calls, hackathons, accelerators, early API access, AI tool launches with first-mover windows, fellowships, market gaps, events, and commercial channels that reward early movers. Subscribers gain advantage by acting before the window closes.

---

WHO IS READING THIS — KNOW YOUR AUDIENCE:

These are not casual readers. They are:
• Founders building 0 to 1 products or scaling 1 to 10
• Content creators monetizing their expertise and audience
• Freelancers and consultants packaging skills into offers
• Small business operators using AI and automation to compete
• Early-stage investors and accelerator founders spotting trends

They are time-poor, opportunity-hungry, and allergic to waste. They will cancel a premium subscription over three bad items in a row. Every item must earn its place. Speak to them as a trusted insider, not a press release.

---

INTELLIGENCE STANDARDS — NON-NEGOTIABLE:

RECENCY:
• INSIGHTS: published or announced within the last 24 hours. Hard cutoff.
• OPPORTUNITIES: currently open, active, or accepting — future deadlines allowed. Past-closed or expired entries are automatically disqualified. If a deadline is listed, verify it has not already passed against today's date.

ORIGINALITY:
• Rewrite everything in your own voice. Never lift phrases, sentences, or summaries from source articles.
• Every item must sound like an expert analyst processed the raw information and extracted the signal — not like a news aggregator copying headlines.
• If two sources cover the same story, synthesize both into one superior item. Never report the same event twice.

SPECIFICITY:
• Generic claims are disqualified. Every item must contain at least one of: a named company, a named person, a dollar figure, a percentage, a timeframe, a number of users/jobs/tokens/countries affected, or a specific product name.
• "An AI startup raised funding" fails. "Synthesia raised $90M Series C to build AI avatars for enterprise L&D teams targeting a $370B market" passes.

DEPTH OVER BREADTH:
• Surface what most readers won't find or won't connect. The reader already sees headlines. You deliver the layer beneath: what it means, who wins, who loses, and what to do in the next 48 hours.

---

SOURCE QUALITY HIERARCHY (in priority order):

Tier 1 — Primary: Official company announcements, SEC filings, government grant portals, accelerator websites, peer-reviewed preprints (arXiv, Nature, Science)
Tier 2 — Journalistic: TechCrunch, The Verge, Wired, Bloomberg, Reuters, Financial Times, MIT Technology Review, Ars Technica, Fast Company, The Information, Rest of World
Tier 3 — Specialist: Y Combinator blog, a16z publications, Stratechery, Import AI, The Batch, Morning Brew, Axios Pro
Tier 4 — Acceptable: Well-sourced independent newsletters, reputable industry analysts, verified LinkedIn posts from named executives at known companies

REJECT OUTRIGHT:
• Press releases with no third-party verification
• Sponsored content or paid placements disguised as news
• Conference/event promotional copy with no substantive announcement
• Opinion pieces without factual backing
• Any source with a history of inaccurate reporting
• Reddit, Twitter/X, or social media posts as primary sources unless corroborated by Tier 1 to 3

---

URL VALIDATION PROTOCOL (execute before every item enters output):

Step 1: Confirm URL format is structurally valid
Step 2: Cross-reference with known publication domain patterns
Step 3: If confidence is below 100%: search for the official announcement page or primary source and replace
Step 4: If no verified live link can be found: drop the item entirely, do not guess, do not fabricate
Step 5: Check URL against lavoo_opportunity_memory. If previously delivered, skip and find a replacement item

---

IMPACT SCORING RUBRIC — BE PRECISE, NOT GENEROUS:

Score 90–100 (Critical / Viral-worthy): Requires ALL of:
• Affects a major platform, model, or market that touches more than 1 million people
• Changes how creators or entrepreneurs should operate starting today
• Contains a number or stat that stops a reader mid-scroll
• Has a time element that creates urgency: window closing, launch imminent, cutoff approaching
• Cannot be found by casually reading tech headlines

Score 75–89 (High Value / Immediate Opportunity): Requires at least 3 of:
• Specific financial figure, user count, or market size
• Named company, tool, or person with existing credibility
• Clear first-mover or early-adopter advantage exists
• Actionable within 48 hours by a solo operator
• Connects to an ongoing trend the audience already cares about

Score 60–74 (Useful, Moderate Urgency): Requires at least 2 of:
• Relevant to core audience workflows
• Contains at least one specific data point
• Has a clear, simple action attached
• Timely even if not breaking

Score below 60: EXCLUDE. Do not include filler. An empty slot is better than a weak item.

---

OPPORTUNITY-SPECIFIC DETECTION SIGNALS:

Actively hunt for these patterns when scanning for OPPORTUNITIES:
• "applications now open" / "accepting submissions" / "register by [date]"
• "early access" / "beta launch" / "founding member" / "waitlist open"
• "grant deadline" / "funding round open" / "pitch competition"
• New API or SDK released with no dominant tool built on it yet
• Platform algorithm or monetization change creating a gap to fill
• Government or institutional program targeting SMEs, creators, or tech founders
• Accelerator cohort announcement with open applications
• Market demand spike with no established market leader yet

Opportunity urgency scoring:
• High: Deadline within 7 days, or early-mover window confirmed closing
• Medium: Deadline within 30 days, or early-access phase actively open
• Low: Ongoing with no firm deadline — only include if impact score is 85 or above

---

ANTI-AI-SLOP WRITING RULES (apply to every sentence you write):

BANNED PHRASES — never use these:
"game-changer", "game-changing", "paradigm shift", "revolutionary", "groundbreaking", "it's worth noting", "in conclusion", "at the end of the day", "cutting-edge", "world-class", "leveraging", "synergy", "holistic", "robust solution", "in today's fast-paced world", "unprecedented", "transformative" (unless quoting a named source), "exciting times"

// ADDED — FORMATTING PROHIBITION:
BANNED CHARACTER — THE DASH IS STRICTLY FORBIDDEN:
• Never use an em dash (—) anywhere in any field of the output. This includes titles, what_changed, why_it_matters, action_to_take, why_act_now, potential_reward, action_required, or any other field.
• Never use an en dash (–) anywhere in any field of the output.
• Never use a hyphen used as a dash substitute (e.g. " - " with spaces) in any field.
• If you feel the urge to use a dash to connect two ideas, rewrite the sentence. Use a period. Use a colon. Use "and", "but", "so", or "because". Break it into two sentences. A dash is a lazy connector. Replace it with clarity.

WRITING STANDARDS:
• Lead with the most important thing. Never bury the lead.
• Use active voice. "OpenAI launched" not "a launch was made by OpenAI."
• Write short sentences under pressure. Long sentences under explanation.
• Concrete before abstract. Name the thing before explaining it.
• When in doubt: cut the sentence. If it adds nothing, remove it.
• One idea per sentence. One insight per item.
• Write what the smartest person in the room would say, not what sounds impressive.
• Read each item back as if you are the reader seeing it for the first time. Does it feel like real intelligence or filler dressed up? If filler: rewrite or drop.

HEADLINE ENGINEERING RULES:
• Max 80 characters for INSIGHTS, max 70 for OPPORTUNITIES
• Must contain: a specific noun (company, tool, model, dollar amount, person) + a consequence or change
• Avoid vague adjectives: "huge", "massive", "major" — use numbers instead
• Create implied stakes: the reader should feel they are about to learn something that affects them
• Do not exaggerate: credibility is the premium product — overpromising destroys trust
• Never use a dash in a headline under any circumstances

Strong headline patterns:
• [Specific Actor] + [Specific Action] + [Implication for Reader]
  "Anthropic Caps Claude API Pricing: What It Means for Your Stack"
• [Dollar Figure or Number] + [Event] + [Who Benefits]
  "$2M Grant Opens for AI Founders: No Equity, Closes June 15"
• [Tool or Platform] + [Change] + [Reader Outcome]
  "Google's New Ad Format Cuts CPM by 40%: Early Adopters Win"
• [Trend Signal] + [Early-Mover Frame]
  "The AI Agent Market Has a Gap. Three Founders Already Filing"

---

SELF-REVIEW GATE — run this before generating final output:

For each item, answer these four questions internally. If any answer is "no" — rewrite or replace the item:

1. SO WHAT? — Is the implication for creators/entrepreneurs made explicitly clear? Not implied, not assumed — stated.
2. SPECIFIC ENOUGH? — Does this item contain at least one named entity, number, or concrete detail a reader could fact-check?
3. ACTIONABLE? — Could a solo operator take the listed action today with no additional research needed?
4. HUMAN? — Read the item aloud. Does it sound like something a sharp analyst would say to a founder over coffee, or does it sound like a press release?

// ADDED — FINAL FORMATTING CHECK:
5. DASH-FREE? — Scan every single field in every single item. If any em dash (—), en dash (–), or spaced hyphen ( - ) exists anywhere in the output, remove it and rewrite that sentence before returning the JSON. This check is mandatory and must complete before output is returned.

---

CONTENT VOLUME PER RUN:

• INSIGHTS: return 5 to 7 items. Sort highest to lowest impact score.
• OPPORTUNITIES: return 3 to 5 items. Surface the top 2 hottest at positions 1 and 2. Sort remaining highest to lowest.
• Never pad output. 5 strong items beats 7 where 2 are weak.
• If fewer qualifying items are found than the minimum: return what qualifies and flag in the generated_at note — do not lower standards to hit a number.

---

RETURN OUTPUT IN THIS EXACT JSON STRUCTURE. DO NOT DEVIATE:

{
  "generated_at": "ISO 8601 timestamp",
  "insights": [
    {
      "type": "INSIGHT",
      "title": "Irresistible headline (max 80 chars)",
      "category": "AI Technology | Funding | Automation | Business Strategy | Productivity | Marketing | Creator Economy | Networking | Financial Technology",
      "impact_score": 60-100,
      "read_time": "2 min | 3 min | 5 min",
      "what_changed": "What happened, core details, and numbers where possible. (2 to 3 sentences)",
      "why_it_matters": "Strategic implications for creators and entrepreneurs. Opportunities, risks, and competitive edge. (2 to 4 sentences)",
      "action_to_take": "One executable move the reader can take right now. (1 to 2 lines)",
      "source": "Publication name",
      "url": "Verified live link",
      "new_to_memory": true
    }
  ],
  "opportunities": [
    {
      "type": "OPPORTUNITY",
      "title": "Urgency-driven opportunity headline (max 70 chars)",
      "impact_score": 70-100,
      "urgency_level": "High | Medium | Low",
      "category": "Funding | Hackathon | AI Tools | Events | Grants | Partnerships | Scholarships | Markets | Cost Savings",
      "deadline": "DD/MM/YYYY or 'Closes in X days' or 'Ongoing'",
      "why_act_now": "What opened, why timing is critical right now. (2 to 3 sentences, urgency-forward)",
      "potential_reward": "Upside: profit, exposure, adoption, ROI, or network access. Be specific. (2 to 4 sentences)",
      "action_required": "Clear next steps the reader can execute today. (2 to 3 bullet-style instructions)",
      "source": "Source or authority name",
      "url": "Verified live link only",
      "new_to_memory": true
    }
  ]
}

---

BACKEND MEMORY SYSTEM NOTE (for developers):
• On each run, check all URLs against stored list: lavoo_opportunity_memory
• If URL exists in memory, skip and find replacement before output
• If URL is new, include in output with "new_to_memory": true
• Store all flagged URLs immediately after successful output delivery
• Memory should persist across sessions and users — this is a global deduplication layer, not per-user

---

Return ONLY the JSON object. No intro. No outro. No explanation. No meta-commentary. No markdown code fences. Raw JSON only. Every item must earn its place. Every word must earn its sentence. No dashes of any kind anywhere in the output. This is premium intelligence — deliver accordingly."""


class IntelligenceGenerator:
    """
    AI-powered content generator that:
    1. Uses Grok with web search to run the LAVOO INTELLIGENCE prompt
    2. Produces a unified feed of INSIGHTS and OPPORTUNITIES in one call
    3. Avoids duplicates by checking existing content (per content type)
    4. Stores new content in the database via the existing Insight/Alert tables
    """

    def __init__(self, db_session: Session):
        self.db = db_session
        # Use Grok 4 Fast with reasoning for web search capabilities
        self.model = "grok-4-fast-reasoning"
        self.today = date.today().strftime("%Y-%m-%d")

        if HAS_XAI:
            api_key = os.getenv("XAI_API_KEY")
            if not api_key:
                logger.warning("XAI_API_KEY not set in environment variables")
                self.client = None
            else:
                self.client = Client(api_key=api_key)
                logger.info("xAI Grok client initialized for Lavoo Intelligence generation")
        else:
            self.client = None
            logger.warning("xAI SDK not installed")

    # ------------------------------------------------------------------
    # Duplicate / memory helpers (mirrors cron/insights/generator.py and
    # cron/alerts/generator.py, parameterized by content_type)
    # ------------------------------------------------------------------

    def _get_existing_titles(self, content_type: str) -> set:
        """Get existing title hashes from database to prevent duplicates."""
        if content_type == 'insight':
            titles = self.db.query(Insight.title).filter(Insight.is_active == True).all()
        else:
            titles = self.db.query(Alert.title).filter(Alert.is_active == True).all()

        return {hashlib.md5(t[0].lower().encode()).hexdigest() for t in titles}

    def _get_existing_title_list(self, content_type: str) -> list:
        """Get list of existing titles to include in the prompt's memory section."""
        if content_type == 'insight':
            titles = self.db.query(Insight.title).filter(
                Insight.is_active == True
            ).order_by(Insight.created_at.desc()).limit(30).all()
        else:
            titles = self.db.query(Alert.title).filter(
                Alert.is_active == True
            ).order_by(Alert.created_at.desc()).limit(30).all()

        return [t[0] for t in titles]

    def _get_existing_urls(self, content_type: str) -> set:
        """Get set of normalized existing URLs to prevent re-delivering the same item."""
        if content_type == 'insight':
            urls = self.db.query(Insight.url).filter(Insight.is_active == True).all()
        else:
            urls = self.db.query(Alert.url).filter(Alert.is_active == True).all()

        normalized_urls = set()
        for url_tuple in urls:
            url = url_tuple[0]
            if url:
                url = url.rstrip('/').split('?')[0]
                normalized_urls.add(url.lower())

        return normalized_urls

    def _is_duplicate_content(self, title: str, url: str, existing_title_hashes: set, existing_urls: set) -> tuple:
        """Check if content is duplicate based on both title AND URL."""
        title_hash = hashlib.md5(title.lower().encode()).hexdigest()
        if title_hash in existing_title_hashes:
            return (True, "duplicate title")

        normalized_url = url.rstrip('/').split('?')[0].lower()
        if normalized_url in existing_urls:
            return (True, "duplicate URL")

        return (False, None)

    def _is_suspicious_url(self, url: str) -> bool:
        """Check if URL looks suspicious, fake, or is not a specific article."""
        url_lower = url.lower()

        if 'example.com' in url_lower or 'placeholder' in url_lower:
            return True

        category_indicators = [
            '/category/', '/categories/', '/tag/', '/tags/', '/topic/', '/topics/',
        ]
        for indicator in category_indicators:
            if indicator in url_lower:
                return True

        path_parts = url_lower.rstrip('/').split('/')
        if len(path_parts) > 0:
            last_part = path_parts[-1]
            generic_categories = ['ai', 'tech', 'technology', 'business', 'news',
                                 'artificial-intelligence', 'machine-learning', 'startup']
            if last_part in generic_categories:
                return True

        return False

    def _validate_url_response(self, url: str) -> bool:
        """Actually test if URL is accessible via HTTP request."""
        try:
            response = requests.head(url, timeout=5, allow_redirects=True)
            return 200 <= response.status_code < 400
        except requests.exceptions.Timeout:
            logger.warning(f"URL validation timeout: {url[:50]}...")
            return False
        except requests.exceptions.RequestException as e:
            logger.warning(f"URL validation failed: {url[:50]}... - {str(e)[:50]}")
            return False
        except Exception as e:
            logger.warning(f"Unexpected error validating URL: {url[:50]}... - {str(e)[:50]}")
            return False

    def _filter_items(self, items: List[Dict], content_type: str, existing_hashes: set, existing_urls: set) -> List[Dict]:
        """Run the shared duplicate/URL validation pipeline over a list of items."""
        valid = []
        for item in items:
            title = item.get('title', 'Unknown')
            url = item.get('url', '')

            # Type conversion: handle string impact scores
            impact_score = item.get('impact_score', 0)
            if isinstance(impact_score, str):
                try:
                    impact_score = int(impact_score)
                    item['impact_score'] = impact_score
                except (ValueError, TypeError):
                    impact_score = 0

            if content_type == 'alert':
                # Backwards-compat field mapping (matches cron/alerts/generator.py)
                if 'urgency_level' in item and 'priority' not in item:
                    item['priority'] = item['urgency_level']
                if 'impact_score' in item and 'score' not in item:
                    item['score'] = item['impact_score']
                if 'deadline' in item and 'time_remaining' not in item:
                    item['time_remaining'] = item['deadline']

            is_duplicate, reason = self._is_duplicate_content(title, url, existing_hashes, existing_urls)
            if is_duplicate:
                logger.info(f"Skipping {reason}: {title[:50]}... (URL: {url[:50]}...)")
                continue

            if not url or not url.startswith('http'):
                logger.warning(f"Skipping {content_type} with invalid URL format: {title[:50]}... (URL: {url})")
                continue

            if self._is_suspicious_url(url):
                logger.warning(f"Skipping {content_type} with suspicious URL pattern: {title[:50]}... (URL: {url})")
                continue

            if not self._validate_url_response(url):
                logger.warning(f"Skipping {content_type} with non-accessible URL: {title[:50]}... (URL: {url})")
                continue

            valid.append(item)
            logger.info(f"\u2713 Valid {content_type}: {title[:50]}...")

        return valid

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------

    async def generate_feed(self) -> Dict[str, List[Dict]]:
        """
        Run the LAVOO INTELLIGENCE prompt once and return a unified feed:
        {"insights": [...], "opportunities": [...]}
        """
        if not self.client:
            logger.error("Grok client not initialized")
            return {"insights": [], "opportunities": []}

        existing_insight_hashes = self._get_existing_titles('insight')
        existing_insight_titles = self._get_existing_title_list('insight')
        existing_insight_urls = self._get_existing_urls('insight')

        existing_alert_hashes = self._get_existing_titles('alert')
        existing_alert_titles = self._get_existing_title_list('alert')
        existing_alert_urls = self._get_existing_urls('alert')

        logger.info(
            f"Found {len(existing_insight_hashes)} existing insights and "
            f"{len(existing_alert_hashes)} existing opportunities in database"
        )

        # The prompt's "lavoo_opportunity_memory" is the existing active
        # Insight/Alert rows already in the database. Append them as a
        # memory section after the unmodified prompt body.
        memory_section = (
            "\n\n---\n\n"
            "LAVOO_OPPORTUNITY_MEMORY (already delivered - check every candidate "
            "item against this list before output; if a title or url below "
            "matches a candidate, skip it and find a replacement; otherwise "
            "mark \"new_to_memory\": true):\n\n"
            "Existing INSIGHT titles:\n"
            + (chr(10).join(['- ' + t for t in existing_insight_titles[:30]]) if existing_insight_titles else '(none)')
            + "\n\nExisting OPPORTUNITY titles:\n"
            + (chr(10).join(['- ' + t for t in existing_alert_titles[:30]]) if existing_alert_titles else '(none)')
            + "\n\nExisting INSIGHT urls:\n"
            + (chr(10).join(['- ' + u for u in list(existing_insight_urls)[:30]]) if existing_insight_urls else '(none)')
            + "\n\nExisting OPPORTUNITY urls:\n"
            + (chr(10).join(['- ' + u for u in list(existing_alert_urls)[:30]]) if existing_alert_urls else '(none)')
        )

        full_prompt = LAVOO_INTELLIGENCE_PROMPT + memory_section

        try:
            chat = self.client.chat.create(
                model=self.model,
                tools=[web_search()],
            )

            chat.append(user(
                "You are LAVOO INTELLIGENCE with web search capabilities. SEARCH THE WEB FIRST "
                "before answering. You must NOT hallucinate. Only use real, verified sources and "
                "live URLs found in your search results. Always return a single valid JSON object "
                "only, with no markdown code fences and no commentary."
            ))
            chat.append(user(full_prompt))

            response = chat.sample()
            content = response.content.strip() if response.content else ""

            logger.info(f"Raw API response (first 500 chars): {content[:500]}")

            if content.startswith("```"):
                parts = content.split("```")
                if len(parts) >= 2:
                    content = parts[1]
                    if content.startswith("json"):
                        content = content[4:]
                    content = content.strip()

            feed = json.loads(content)

            insights_raw = feed.get('insights', []) or []
            opportunities_raw = feed.get('opportunities', []) or []

            insights = self._filter_items(insights_raw, 'insight', existing_insight_hashes, existing_insight_urls)
            opportunities = self._filter_items(opportunities_raw, 'alert', existing_alert_hashes, existing_alert_urls)

            return {"insights": insights, "opportunities": opportunities}

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Lavoo Intelligence feed JSON: {e}")
            logger.error(f"Raw content: {content[:500]}")
            return {"insights": [], "opportunities": []}
        except Exception as e:
            logger.error(f"Error generating Lavoo Intelligence feed: {e}")
            return {"insights": [], "opportunities": []}

    # ------------------------------------------------------------------
    # Persistence (identical save paths to the existing generators)
    # ------------------------------------------------------------------

    def save_insights(self, insights: List[Dict]) -> Tuple[int, int]:
        """Save insights to database."""
        saved = 0
        skipped = 0

        for insight_data in insights:
            try:
                required = ['title', 'category', 'what_changed', 'why_it_matters', 'action_to_take']
                if not all(insight_data.get(f) for f in required):
                    logger.warning(f"Skipping insight with missing fields: {insight_data.get('title', 'Unknown')}")
                    skipped += 1
                    continue

                date_value = insight_data.get('date', self.today)

                insight = Insight(
                    title=insight_data['title'][:255],
                    category=insight_data.get('category', 'General'),
                    read_time=insight_data.get('read_time', '3 min'),
                    date=date_value,
                    source=insight_data.get('source', 'AI Generated'),
                    url=insight_data.get('url', ''),
                    what_changed=insight_data['what_changed'],
                    why_it_matters=insight_data['why_it_matters'],
                    action_to_take=insight_data['action_to_take'],
                    is_active=True,
                    total_views=0,
                    total_shares=0
                )

                self.db.add(insight)
                self.db.commit()
                saved += 1
                logger.info(f"\u2705 Saved insight: {insight.title[:50]}...")

            except Exception as e:
                self.db.rollback()
                logger.error(f"Failed to save insight: {e}")
                skipped += 1

        return saved, skipped

    def save_alerts(self, alerts: List[Dict]) -> Tuple[int, int]:
        """Save opportunities (alerts) to database."""
        saved = 0
        skipped = 0

        for alert_data in alerts:
            try:
                required = ['title', 'category', 'why_act_now', 'potential_reward', 'action_required']
                if not all(alert_data.get(f) for f in required):
                    logger.warning(f"Skipping opportunity with missing fields: {alert_data.get('title', 'Unknown')}")
                    skipped += 1
                    continue

                priority = alert_data.get('priority', alert_data.get('urgency_level', 'Medium'))

                score = alert_data.get('score', alert_data.get('impact_score', 70))
                if isinstance(score, str):
                    score = int(score)
                score = max(1, min(100, score))

                time_remaining = alert_data.get('time_remaining', alert_data.get('deadline', 'Ongoing'))

                alert = Alert(
                    title=alert_data['title'][:255],
                    category=alert_data['category'],
                    priority=priority,
                    score=score,
                    time_remaining=time_remaining,
                    why_act_now=alert_data['why_act_now'],
                    potential_reward=alert_data['potential_reward'],
                    action_required=alert_data['action_required'],
                    source=alert_data.get('source', 'AI Generated'),
                    url=alert_data.get('url', ''),
                    date=alert_data.get('date', self.today),
                    is_active=True,
                    total_views=0,
                    total_shares=0
                )

                self.db.add(alert)
                self.db.commit()
                saved += 1
                logger.info(f"\u2705 Saved opportunity: {alert.title[:50]}...")

            except Exception as e:
                self.db.rollback()
                logger.error(f"Failed to save opportunity: {e}")
                skipped += 1

        return saved, skipped


async def run_content_generation():
    """Run the unified Lavoo Intelligence generation: one feed, two save paths."""
    logger.info("=" * 60)
    logger.info("\U0001f680 Starting LAVOO INTELLIGENCE Generation")
    logger.info("=" * 60)

    db = SessionLocal()
    try:
        generator = IntelligenceGenerator(db)

        feed = await generator.generate_feed()

        insights = feed.get('insights', [])
        if insights:
            saved, skipped = generator.save_insights(insights)
            logger.info(f"Insights: {saved} saved, {skipped} skipped")
        else:
            logger.info("No new insights found")

        opportunities = feed.get('opportunities', [])
        if opportunities:
            saved, skipped = generator.save_alerts(opportunities)
            logger.info(f"Opportunities: {saved} saved, {skipped} skipped")
        else:
            logger.info("No new opportunities found")

        logger.info("=" * 60)
        logger.info("\u2705 LAVOO INTELLIGENCE Generation Complete")
        logger.info("=" * 60)

    except Exception as e:
        logger.error(f"Lavoo Intelligence generation failed: {e}")
        raise
    finally:
        db.close()
