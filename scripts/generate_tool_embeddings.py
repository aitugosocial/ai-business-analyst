"""
Generate embeddings for AI tools using sentence-transformers and store them in PostgreSQL.
Uses pgvector for efficient similarity search with HNSW index.
Embeddings are generated locally (no API calls needed - completely free!).
"""

import os
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv
from sentence_transformers import SentenceTransformer
from sqlalchemy.orm import Session
from tqdm import tqdm

from database.pg_connections import SessionLocal
from database.pg_models import AITool

# Load environment variables from .env.local
load_dotenv(".env.local")

# Initialize sentence-transformers model (runs locally, no API needed)
print("Loading embedding model...")
model = SentenceTransformer('all-MiniLM-L6-v2')  # 384-dimensional embeddings
print(f"✅ Model loaded: {model.get_sentence_embedding_dimension()} dimensions")

BATCH_SIZE = 50  # Process 50 tools at a time


def generate_embedding_text(tool: AITool) -> str:
    """
    Generate comprehensive text for embedding from tool attributes.

    Combines name, description, category, and key features for better
    semantic representation.
    """
    parts = [
        f"Tool: {tool.name}",
        f"Description: {tool.description}",
    ]

    if tool.summary:
        parts.append(f"Summary: {tool.summary}")

    if tool.main_category:
        parts.append(f"Category: {tool.main_category}")

    if tool.sub_category:
        parts.append(f"Subcategory: {tool.sub_category}")

    if tool.key_features:
        parts.append(f"Features: {tool.key_features}")

    if tool.who_should_use:
        parts.append(f"Use cases: {tool.who_should_use}")

    return " | ".join(parts)


def generate_embeddings_batch(texts: list[str]) -> list[list[float]]:
    """
    Generate embeddings for a batch of texts using sentence-transformers.
    Runs locally - no API calls needed!

    Args:
        texts: List of text strings to embed

    Returns:
        List of embedding vectors (each is 384-dimensional)
    """
    try:
        embeddings = model.encode(texts, show_progress_bar=False)
        return [emb.tolist() for emb in embeddings]
    except Exception as e:
        print(f"❌ Error generating embeddings: {e}")
        raise


def update_tool_embeddings(db: Session, batch_size: int = BATCH_SIZE):
    """
    Generate and store embeddings for all AI tools in the database.

    Args:
        db: Database session
        batch_size: Number of tools to process per batch (Cohere limit: 96)
    """
    # Get all tools without embeddings (or update all if needed)
    tools = db.query(AITool).filter(AITool.embedding.is_(None)).all()

    if not tools:
        print("✅ All tools already have embeddings!")
        return

    print(f"📊 Found {len(tools)} tools without embeddings")
    print(f"🔄 Generating embeddings using sentence-transformers (local, free)...")

    # Process in batches
    for i in tqdm(range(0, len(tools), batch_size), desc="Processing batches"):
        batch = tools[i:i + batch_size]

        # Generate embedding texts
        texts = [generate_embedding_text(tool) for tool in batch]

        # Generate embeddings locally with sentence-transformers (free, private, 384d)
        try:
            embeddings = generate_embeddings_batch(texts)

            # Update each tool with its embedding
            for tool, embedding in zip(batch, embeddings):
                # Convert list to PostgreSQL vector format string: "[0.1, 0.2, 0.3]"
                vector_str = f"[{','.join(map(str, embedding))}]"
                tool.embedding = vector_str

            # Commit batch
            db.commit()
            print(f"  ✅ Processed batch {i//batch_size + 1}: {len(batch)} tools")

        except Exception as e:
            print(f"  ❌ Error processing batch {i//batch_size + 1}: {e}")
            db.rollback()
            continue

    print(f"\n✅ Successfully generated embeddings for {len(tools)} tools!")


def regenerate_all_embeddings(db: Session):
    """
    Regenerate embeddings for ALL tools (even those with existing embeddings).
    Useful when switching embedding models or updating tool descriptions.
    """
    tools = db.query(AITool).all()

    print(f"📊 Regenerating embeddings for {len(tools)} tools...")

    # Clear existing embeddings first
    for tool in tools:
        tool.embedding = None
    db.commit()

    # Generate new embeddings
    update_tool_embeddings(db)


def main():
    """Main execution function."""
    print("🚀 AI Tool Embeddings Generator")
    print("=" * 50)

    db = SessionLocal()
    try:
        # Check total tools
        total_tools = db.query(AITool).count()
        tools_with_embeddings = db.query(AITool).filter(AITool.embedding.isnot(None)).count()

        print(f"📊 Total tools in database: {total_tools}")
        print(f"✅ Tools with embeddings: {tools_with_embeddings}")
        print(f"❌ Tools without embeddings: {total_tools - tools_with_embeddings}")
        print()

        if total_tools == 0:
            print("⚠️  No tools found in database. Please run tool ingestion first.")
            return

        # Generate embeddings for tools without them
        update_tool_embeddings(db)

        print("\n✅ Embedding generation complete!")
        print(f"🔍 You can now use pgvector similarity search with HNSW index")
    finally:
        db.close()


if __name__ == "__main__":
    main()
