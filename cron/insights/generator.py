# decision_engine/content_generator.py
"""
AI Content Generator - Automated Insights & Opportunity Alerts
Uses Grok 4.1 with web search capabilities to find fresh business intelligence.

Workflow:
1. Grok searches the web for latest AI/Business news
2. Filters for relevant, actionable content
3. Formats into Insight or Alert structure
4. Checks for duplicates in database
5. Saves new content only
"""

import json
import logging
import os
import hashlib
import requests
from datetime import datetime, date
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
from sqlalchemy.orm import Session
from sqlalchemy import func

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
from database.pg_connections import SessionLocal, get_db
from database.pg_models import Insight, Alert

logger = logging.getLogger(__name__)

# Configure logging for standalone script execution
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)


class InsightsGenerator:
    """
    AI-powered content generator that:
    1. Uses Grok 4.1 to search the web for fresh business/AI news
    2. Generates formatted Insights and Opportunity Alerts
    3. Avoids duplicates by checking existing content
    4. Stores new content in the database
    """

    def __init__(self, db_session: Session):
        """
        Initialize the content generator.

        Args:
            db_session: SQLAlchemy database session
        """
        self.db = db_session
        # Use Grok 4 Fast with reasoning for web search capabilities
        self.model = "grok-4-fast-reasoning"  # Grok 4 Fast (with reasoning)
        self.today = date.today().strftime("%Y-%m-%d")

        # Initialize xAI SDK client for Grok API with Agent Tools
        if HAS_XAI:
            api_key = os.getenv("XAI_API_KEY")
            if not api_key:
                logger.warning("XAI_API_KEY not set in environment variables")
                self.client = None
            else:
                self.client = Client(api_key=api_key)
                logger.info("xAI Grok client initialized for content generation")
        else:
            self.client = None
            logger.warning("xAI SDK not installed")

    def _get_existing_titles(self, content_type: str) -> set:
        """
        Get existing titles from database to prevent duplicates.

        Args:
            content_type: 'insight' or 'alert'

        Returns:
            Set of existing title hashes
        """
        if content_type == 'insight':
            titles = self.db.query(Insight.title).filter(Insight.is_active == True).all()
        else:
            titles = self.db.query(Alert.title).filter(Alert.is_active == True).all()

        # Create hash of titles for efficient comparison
        return {hashlib.md5(t[0].lower().encode()).hexdigest() for t in titles}

    def _get_existing_title_list(self, content_type: str) -> list:
        """
        Get list of existing titles to include in prompt for duplicate prevention.

        Args:
            content_type: 'insight' or 'alert'

        Returns:
            List of existing titles (most recent first)
        """
        if content_type == 'insight':
            titles = self.db.query(Insight.title).filter(
                Insight.is_active == True
            ).order_by(Insight.created_at.desc()).limit(30).all()
        else:
            titles = self.db.query(Alert.title).filter(
                Alert.is_active == True
            ).order_by(Alert.created_at.desc()).limit(30).all()

        return [t[0] for t in titles]

    def _is_duplicate(self, title: str, existing_hashes: set) -> bool:
        """Check if content with similar title already exists."""
        title_hash = hashlib.md5(title.lower().encode()).hexdigest()
        return title_hash in existing_hashes

    def _get_existing_urls(self, content_type: str) -> set:
        """
        Get set of existing URLs to prevent same article with different titles.

        Args:
            content_type: 'insight' or 'alert'

        Returns:
            Set of normalized URLs
        """
        if content_type == 'insight':
            urls = self.db.query(Insight.url).filter(Insight.is_active == True).all()
        else:
            urls = self.db.query(Alert.url).filter(Alert.is_active == True).all()

        # Normalize URLs (remove trailing slashes, query params) for comparison
        normalized_urls = set()
        for url_tuple in urls:
            url = url_tuple[0]
            if url:  # Skip None URLs
                # Remove trailing slash
                url = url.rstrip('/')
                # Remove query parameters
                url = url.split('?')[0]
                normalized_urls.add(url.lower())

        return normalized_urls

    def _is_duplicate_content(self, title: str, url: str, existing_title_hashes: set, existing_urls: set) -> tuple:
        """
        Check if content is duplicate based on both title AND URL.

        Args:
            title: Content title
            url: Content URL
            existing_title_hashes: Set of existing title hashes
            existing_urls: Set of existing normalized URLs

        Returns:
            Tuple of (is_duplicate: bool, reason: str)
        """
        # Check title duplicate
        title_hash = hashlib.md5(title.lower().encode()).hexdigest()
        if title_hash in existing_title_hashes:
            return (True, "duplicate title")

        # Check URL duplicate (normalize first)
        normalized_url = url.rstrip('/').split('?')[0].lower()
        if normalized_url in existing_urls:
            return (True, "duplicate URL")

        return (False, None)

    def _is_suspicious_url(self, url: str) -> bool:
        """
        Check if URL looks suspicious, fake, or is not a specific article.

        Returns True if URL appears to be fabricated or is a category/homepage.
        """
        url_lower = url.lower()

        # Check for obviously fake domains
        if 'example.com' in url_lower or 'placeholder' in url_lower:
            return True

        # Check for category/landing pages instead of specific articles
        category_indicators = [
            '/category/',
            '/categories/',
            '/tag/',
            '/tags/',
            '/topic/',
            '/topics/',
        ]

        for indicator in category_indicators:
            if indicator in url_lower:
                return True

        # Check if URL ends with just a category (no article slug)
        # e.g., /artificial-intelligence/ or /ai/ or /technology/
        path_parts = url_lower.rstrip('/').split('/')
        if len(path_parts) > 0:
            last_part = path_parts[-1]
            # If last part is very generic and short, likely a category
            generic_categories = ['ai', 'tech', 'technology', 'business', 'news',
                                 'artificial-intelligence', 'machine-learning', 'startup']
            if last_part in generic_categories:
                return True

        return False

    def _validate_url_response(self, url: str) -> bool:
        """
        Actually test if URL is accessible via HTTP request.

        Returns True if URL returns a valid HTTP status (200-399).
        """
        try:
            response = requests.head(url, timeout=5, allow_redirects=True)
            # Accept 200-399 status codes (success and redirects)
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

    async def generate_insights(self, count: int = 3) -> List[Dict]:
        """
        Generate fresh AI/Business insights by searching the web.

        Args:
            count: Number of insights to generate

        Returns:
            List of generated insights
        """
        if not self.client:
            logger.error("Grok client not initialized")
            return []

        existing_hashes = self._get_existing_titles('insight')
        existing_titles = self._get_existing_title_list('insight')
        existing_urls = self._get_existing_urls('insight')
        logger.info(f"Found {len(existing_hashes)} existing insights in database")

        # Calculate date range (24 hours)
        from datetime import timedelta
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        prompt = f"""You are Lavoo's AI Research & Insights Engine, an elite technology journalist, business intelligence analyst, with 20+ years experience across AI, startups, venture funding, automation, productivity and global tech trends.

CRITICAL: You MUST use your web search capabilities to find REAL, LIVE news articles.\nDO NOT hallucinate or make up URLs.\nDO NOT make up fake news.\nYour mission: Quickly scan the web and fetch ONLY the most fresh, high-value, globally relevant news published within the last 24 hours (between {yesterday} and {self.today}). Identify insights creators, entrepreneurs and business operators can act on immediately.

Output 5–7 items per request then pick the top 2 items. Prioritize speed and relevance. Keep responses lightweight, sharp, and insight-rich with minimum fluff.

Strict rules:
• No duplicates: never return a link previously delivered.
• Ignore anything older than 24 hours (between {yesterday} and {self.today}).
• Verify sources: only credible publications.
• Rewrite insights uniquely: NO summaries copied from articles.
• Return ONLY 2 news that feels useful, forward-moving, monetizable or strategic.
• Headlines must trigger curiosity + clicks + urgency.
• Each item MUST have an impact score (60–100) where:
  - 90–100 = Critical / game-changing / viral-worthy
  - 75–89 = High value / immediate opportunity
  - 60–74 = Useful insight, moderate urgency
  - <60 = Do not include
• Prioritize high-scoring stories first.

Your writing style: Clear. Punchy. Smart. No jargon.
Think better than Bloomberg clarity, Wired creativity, and a newsletter voice that feels human, exciting and easy to read.
The reader should feel like they're getting inside information.

Return results in THIS EXACT JSON array format only:
[
  {{
    "title": "Irresistible headline that sparks clicks (max 80 chars)",
    "category": "AI Technology | Funding | Automation | Business Strategy | Productivity | Marketing | Creator Economy | Networking | Financial Technology",
    "impact_score": 60-100,
    "read_time": "2 min | 3 min | 5 min",
    "what_changed": "What happened + core details + numbers where possible (2-3 sentences)",
    "why_it_matters": "Explain implications for creators/entrepreneurs. Highlight opportunities, risks and competitive edge (2-4 sentences)",
    "action_to_take": "One actionable move a reader can take now (1-2 practical lines)",
    "source": "Publication name only",
    "url": "Direct link to original article"
  }}
]

After generating items:
• Sort highest impact_score → lowest.
• Remove any weak, vague or low-value stories.
• Final output should feel like a cheat code for staying ahead of the world.

Tone guideline for headlines: Create FOMO, curiosity, and urgency without exaggerating.
Examples:
• "New AI Model Overtakes GPT-5: Startups Should Pay Attention"
• "$120M Funding Round Signals Huge New Market: Early Founders Win"
• "This AI Tool Cuts Editing Time 80%: Creators Are Switching Fast"
• "E-commerce Automation Just Leveled Up. Here's How to Cash In"

DO NOT CREATE DUPLICATES - These titles already exist:
{chr(10).join(['- ' + t for t in existing_titles[:20]]) if existing_titles else '(No existing insights)'}

Return only the JSON. No commentary, no explanation. Deliver pure gold at speed."""

        try:
            # Create chat with xAI SDK Agent Tools
            chat = self.client.chat.create(
                model=self.model,
                tools=[web_search()],  # Enable web search tool
            )

            # Add system message and user prompt
            chat.append(user(
                "You are a business intelligence analyst with web search capabilities. SEARCH THE WEB FIRST before answering. VERY IMPORTANT: You must NOT hallucinate. Only use real articles and news from the search results. Provide real URLs. You find and summarize the latest business and AI news. Always return valid JSON arrays only."
            ))
            chat.append(user(prompt))

            # Sample response (non-streaming)
            response = chat.sample()

            content = response.content.strip() if response.content else ""

            # Debug: Log raw response
            logger.info(f"Raw API response (first 500 chars): {content[:500]}")

            # Parse JSON response
            # Handle potential markdown code blocks
            if content.startswith("```"):
                parts = content.split("```")
                if len(parts) >= 2:
                    content = parts[1]
                    if content.startswith("json"):
                        content = content[4:]
                    content = content.strip()

            insights = json.loads(content)

            if not isinstance(insights, list):
                insights = [insights]

            # Filter out duplicates and validate URLs
            new_insights = []
            for insight in insights:
                title = insight.get('title', 'Unknown')
                url = insight.get('url', '')

                # Type conversion: Handle string impact scores
                impact_score = insight.get('impact_score', 0)
                if isinstance(impact_score, str):
                    try:
                        impact_score = int(impact_score)
                        insight['impact_score'] = impact_score  # Convert in place
                    except (ValueError, TypeError):
                        impact_score = 0  # Invalid score

                # Enhanced duplicate check (title AND URL)
                is_duplicate, reason = self._is_duplicate_content(
                    title, url, existing_hashes, existing_urls
                )
                if is_duplicate:
                    logger.info(f"Skipping {reason}: {title[:50]}... (URL: {url[:50]}...)")
                    continue

                # Validate URL format
                if not url or not url.startswith('http'):
                    logger.warning(f"Skipping insight with invalid URL format: {title[:50]}... (URL: {url})")
                    continue

                # Quick pattern validation (check if it's obviously fake)
                if self._is_suspicious_url(url):
                    logger.warning(f"Skipping insight with suspicious URL pattern: {title[:50]}... (URL: {url})")
                    continue

                # HTTP validation - actually test if URL works
                if not self._validate_url_response(url):
                    logger.warning(f"Skipping insight with non-accessible URL: {title[:50]}... (URL: {url})")
                    continue

                new_insights.append(insight)
                logger.info(f"✓ Valid insight: {title[:50]}...")

            return new_insights

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse insights JSON: {e}")
            logger.error(f"Raw content: {content[:500]}")
            return []
        except Exception as e:
            logger.error(f"Error generating insights: {e}")
            return []

    def save_insights(self, insights: List[Dict]) -> Tuple[int, int]:
        """
        Save insights to database.

        Args:
            insights: List of insight dictionaries

        Returns:
            Tuple of (saved_count, skipped_count)
        """
        saved = 0
        skipped = 0

        for insight_data in insights:
            try:
                # Validate required fields (impact_score is optional for backwards compatibility)
                required = ['title', 'category', 'what_changed', 'why_it_matters', 'action_to_take']
                if not all(insight_data.get(f) for f in required):
                    logger.warning(f"Skipping insight with missing fields: {insight_data.get('title', 'Unknown')}")
                    skipped += 1
                    continue

                # Handle date field - use impact_score as fallback if date not present
                date_value = insight_data.get('date', self.today)

                insight = Insight(
                    title=insight_data['title'][:255],  # Truncate if needed
                    category=insight_data.get('category', 'General'),
                    read_time=insight_data.get('read_time', '3 min'),
                    date=date_value,
                    source=insight_data.get('source', 'AI Generated'),
                    url=insight_data.get('url', ''),  # URL of the source article
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
                logger.info(f"✅ Saved insight: {insight.title[:50]}...")

            except Exception as e:
                self.db.rollback()
                logger.error(f"Failed to save insight: {e}")
                skipped += 1

        return saved, skipped


async def run_content_generation(insight_count: int = 3) -> dict:
    db = SessionLocal()
    saved, skipped = 0, 0
    try:
        generator = InsightsGenerator(db)
        logger.info("\n📊 Generating Insights...")
        insights = await generator.generate_insights(count=insight_count)
        if insights:
            saved, skipped = generator.save_insights(insights)
            logger.info(f"Insights: {saved} saved, {skipped} skipped")
        else:
            logger.info("No new insights found")
        logger.info("✅ Content Generation Complete")
    except Exception as e:
        logger.error(f"Failed to generate insights: {e}")
    finally:
        db.close()
    return {"insights_saved": saved, "insights_skipped": skipped}
