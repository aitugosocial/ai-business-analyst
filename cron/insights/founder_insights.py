import json
import logging
import os
import hashlib
import urllib.request
import urllib.error
from datetime import datetime
from typing import List, Dict, Optional, Tuple

from dotenv import load_dotenv
from sqlalchemy.orm import Session
from sqlalchemy import text

# Load environment variables
load_dotenv('.env.local')
load_dotenv('.env.production')

from database.pg_connections import SessionLocal
from database.pg_models import FounderInsightCard

logger = logging.getLogger("cron.founder_insights")
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')


class FounderInsightsGenerator:
    """
    AI-powered generator for high-impact positive Founder Insights.
    Uses Grok (xAI API) to search the web for fresh African tech milestones,
    solo founder strategies, and product building metrics.
    """

    def __init__(self, db_session: Session):
        self.db = db_session
        self.api_key = os.getenv("XAI_API_KEY")
        self.model = "grok-4-1-fast-reasoning"
        self.fallback_model = "grok-2-latest"
        try:
            FounderInsightCard.__table__.create(bind=self.db.get_bind(), checkfirst=True)
        except Exception:
            pass

    def _get_existing_hashes(self) -> set:
        """Get MD5 hashes of existing insight texts to prevent duplicates."""
        existing = self.db.query(FounderInsightCard.insight_text).filter(FounderInsightCard.is_active == True).all()
        return {hashlib.md5(item[0].lower().encode()).hexdigest() for item in existing if item[0]}

    def generate_insights(self, count: int = 4) -> List[Dict]:
        """
        Queries Grok AI to research positive founder insights.
        Returns a list of parsed JSON insight dicts.
        """
        if not self.api_key:
            logger.error("XAI_API_KEY not set in environment variables")
            return []

        existing_hashes = self._get_existing_hashes()
        existing_insights = self.db.query(FounderInsightCard.insight_text).order_by(FounderInsightCard.created_at.desc()).limit(20).all()
        existing_list = [item[0] for item in existing_insights if item[0]]

        prompt = f"""You are Lavoo's Founder Intelligence Engine, an elite strategist discovering high-impact startup metrics and inspiring founder insights.

CRITICAL REQUIREMENTS:
1. FOCUS & NARRATIVE BALANCE:
   - 70% WEIGHT: Positive African tech ecosystem narratives, African founder milestones, and West/East/South African product-building statistics (referencing reports/sources like TechCabal, Disrupt Africa, YC African startups like Paystack, Flutterwave, Moniepoint, Chowdeck, or solo African tech builders).
   - 30% WEIGHT: Global product-building research (YC, Harvard Business Review, Indie Hackers) and internal founder activity metrics.

2. CONTENT STRUCTURE:
   - Every insight MUST be a short, punchy 1-SENTENCE statement (max 25 words).
   - Must include an eye-catching stat or multiplier if available (e.g., "42%", "3.5x", "78%", "9 in 10", "60 days").
   - Must include an explicit, authoritative source attribution line (e.g., "Based on TechCabal African Tech Report", "Based on Build Room founder activity").

3. HARD RULES:
   - NO fake or hallucinated stats. Use REAL, verifiable data or well-established founder principles.
   - NO negative news or doom-and-gloom. Focus strictly on positive, actionable, encouraging insights for builders.
   - Return up to {count} insights in a strict JSON array.

Return output in THIS exact JSON array format:
[
  {{
    "highlight_stat": "42%",
    "insight_text": "of solo founders who review decisions weekly ship products twice as fast as unguided teams.",
    "source": "Based on Build Room founder activity",
    "category": "build_room",
    "accent_color": "#e87a02"
  }},
  {{
    "highlight_stat": "3.5x",
    "insight_text": "faster user acquisition is achieved by African B2B startups prioritizing WhatsApp integration over custom portals.",
    "source": "Based on TechCabal African Tech Report",
    "category": "african_tech",
    "accent_color": "#2f7de1"
  }},
  {{
    "highlight_stat": "78%",
    "insight_text": "of successful solo builders in West Africa pre-sell their service before writing their first line of backend code.",
    "source": "Based on Disrupt Africa Founder Survey",
    "category": "african_tech",
    "accent_color": "#7c6cf0"
  }}
]

DO NOT CREATE DUPLICATES — Exclude any titles or stats similar to these recent entries:
{chr(10).join(['- ' + t for t in existing_list]) if existing_list else '(No existing insights)'}

Return ONLY valid JSON array: no intro text, no markdown block wrappers, no commentary."""

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        models_to_try = [self.model, self.fallback_model]
        content = ""

        for model_name in models_to_try:
            try:
                payload = json.dumps({
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": "You are a startup intelligence analyst specializing in positive founder insights and African tech ecosystem research. Return valid JSON arrays only."},
                        {"role": "user", "content": prompt}
                    ],
                    "temperature": 0.4
                }).encode('utf-8')
                req = urllib.request.Request("https://api.x.ai/v1/chat/completions", data=payload, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=45) as resp:
                    if resp.status == 200:
                        res_data = json.loads(resp.read().decode('utf-8'))
                        content = res_data["choices"][0]["message"]["content"].strip()
                        logger.info(f"Successfully generated insights using Grok model: {model_name}")
                        break
            except Exception as err:
                logger.warning(f"Failed calling Grok API model {model_name}: {err}")

        if not content:
            logger.error("Failed to receive output from Grok API")
            return []

        # Parse JSON
        try:
            if "```" in content:
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            content = content.strip()
            items = json.loads(content)
            if not isinstance(items, list):
                logger.error("Parsed response is not a list")
                return []

            valid_items = []
            for item in items:
                text_val = item.get("insight_text", "").strip()
                if not text_val:
                    continue
                item_hash = hashlib.md5(text_val.lower().encode()).hexdigest()
                if item_hash in existing_hashes:
                    logger.info(f"Skipping duplicate insight: {text_val[:40]}...")
                    continue

                valid_items.append({
                    "highlight_stat": item.get("highlight_stat", "").strip(),
                    "insight_text": text_val,
                    "source": item.get("source", "Based on Founder Intelligence").strip(),
                    "category": item.get("category", "african_tech").strip(),
                    "accent_color": item.get("accent_color", "#e87a02").strip() or "#e87a02"
                })
            return valid_items
        except Exception as parse_err:
            logger.error(f"Error parsing Grok response JSON: {parse_err}. Content: {content[:300]}")
            return []

    def save_insights(self, insights: List[Dict]) -> Tuple[int, int]:
        """Saves valid insights to PostgreSQL database."""
        saved, skipped = 0, 0
        for item in insights:
            try:
                card = FounderInsightCard(
                    highlight_stat=item.get("highlight_stat"),
                    insight_text=item["insight_text"],
                    source=item["source"],
                    category=item.get("category", "african_tech"),
                    accent_color=item.get("accent_color", "#e87a02"),
                    is_active=True
                )
                self.db.add(card)
                self.db.commit()
                saved += 1
            except Exception as e:
                self.db.rollback()
                logger.error(f"Failed to save insight: {e}")
                skipped += 1
        return saved, skipped


def run_founder_insights_cron(count: int = 4):
    """Main execution function for 24-hour cron run."""
    db = SessionLocal()
    try:
        logger.info("🚀 Starting Founder Insights 24-Hour Generator...")
        generator = FounderInsightsGenerator(db)
        insights = generator.generate_insights(count=count)
        if insights:
            saved, skipped = generator.save_insights(insights)
            logger.info(f"✅ Founder Insights Cron Complete: {saved} saved, {skipped} skipped.")
        else:
            logger.info("ℹ️ No new founder insights generated.")
    except Exception as err:
        logger.error(f"❌ Founder Insights Cron failed: {err}")
    finally:
        db.close()


if __name__ == "__main__":
    run_founder_insights_cron()
