#!/usr/bin/env python3
"""
Verify Utah OGM documents in Qdrant collection.

Usage:
    python verify_qdrant.py                        # Show collection stats
    python verify_qdrant.py --search "contact"     # Test search query
    python verify_qdrant.py --list-mines           # List all mine names
    python verify_qdrant.py --sample 5             # Show sample documents
"""

import argparse
import os
import sys

from dotenv import load_dotenv
from openai import OpenAI
from qdrant_client import QdrantClient

# Load environment variables
load_dotenv()

DEFAULT_COLLECTION_NAME = "utah-ogm-coal"
EMBEDDING_MODEL = "text-embedding-3-large"


def get_qdrant_client() -> QdrantClient:
    """Create and return a Qdrant client."""
    url = os.getenv("QDRANT_URL")
    api_key = os.getenv("QDRANT_API_KEY")

    if not url or not api_key:
        print("Error: QDRANT_URL and QDRANT_API_KEY must be set in .env file")
        sys.exit(1)

    return QdrantClient(url=url, api_key=api_key)


def get_openai_client() -> OpenAI:
    """Create and return an OpenAI client."""
    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        print("Error: OPENAI_API_KEY must be set in .env file")
        sys.exit(1)

    return OpenAI(api_key=api_key)


def get_embedding(client: OpenAI, text: str) -> list[float]:
    """Generate embedding vector for text using OpenAI."""
    response = client.embeddings.create(
        input=text,
        model=EMBEDDING_MODEL,
    )
    return response.data[0].embedding


def show_collection_stats(client: QdrantClient, collection_name: str) -> None:
    """Display collection statistics."""
    try:
        info = client.get_collection(collection_name)
        print(f"Collection: {collection_name}")
        print(f"  Points: {info.points_count}")
        print(f"  Status: {info.status}")
    except Exception as e:
        print(f"Error: Could not get collection info: {e}")
        sys.exit(1)


def get_unique_values(client: QdrantClient, collection_name: str, field: str) -> set:
    """Get all unique values for a payload field."""
    values = set()
    offset = None

    while True:
        result = client.scroll(
            collection_name=collection_name,
            limit=100,
            offset=offset,
            with_payload=[field],
            with_vectors=False,
        )
        points, next_offset = result
        if not points:
            break
        for point in points:
            if field in point.payload and point.payload[field]:
                values.add(point.payload[field])
        offset = next_offset
        if offset is None:
            break

    return values


def list_mines(client: QdrantClient, collection_name: str) -> None:
    """List all unique mine names in the collection."""
    mines = get_unique_values(client, collection_name, "mine_name")
    print(f"\nUnique mine names ({len(mines)}):")
    for mine in sorted(mines):
        print(f"  - {mine}")


def show_sample(client: QdrantClient, collection_name: str, count: int) -> None:
    """Show sample documents from the collection."""
    result = client.scroll(
        collection_name=collection_name,
        limit=count,
        with_payload=True,
        with_vectors=False,
    )
    points, _ = result

    print(f"\nSample documents ({len(points)}):")
    for i, point in enumerate(points, 1):
        payload = point.payload
        meta = payload.get("meta_data", {})
        print(f"\n--- Document {i} ---")
        print(f"  Mine: {payload.get('mine_name', 'N/A')}")
        print(f"  Source: {meta.get('source', 'N/A')}")
        print(f"  File: {meta.get('filename', 'N/A')}")
        print(f"  Page: {meta.get('page_number', 'N/A')}")
        print(f"  Chunk: {meta.get('chunk_index', 'N/A')}")
        print(f"  Date: {meta.get('file_date', 'N/A')}")
        desc = meta.get('description', 'N/A') or 'N/A'
        print(f"  Description: {desc[:50]}...")
        content = payload.get("content", "")
        preview = content[:150].replace("\n", " ") + "..." if len(content) > 150 else content.replace("\n", " ")
        print(f"  Content: {preview}")


def search_documents(
    qdrant_client: QdrantClient,
    openai_client: OpenAI,
    collection_name: str,
    query: str,
    limit: int = 5,
) -> None:
    """Search documents using semantic search."""
    print(f"\nSearching for: '{query}'")

    # Generate embedding for query
    query_embedding = get_embedding(openai_client, query)

    # Search using query_points (newer API)
    results = qdrant_client.query_points(
        collection_name=collection_name,
        query=query_embedding,
        limit=limit,
    )

    print(f"Found {len(results.points)} results:\n")
    for i, result in enumerate(results.points, 1):
        payload = result.payload
        meta = payload.get("meta_data", {})
        print(f"--- Result {i} (score: {result.score:.4f}) ---")
        print(f"  Mine: {payload.get('mine_name', 'N/A')}")
        print(f"  File: {meta.get('filename', 'N/A')}")
        print(f"  Page: {meta.get('page_number', 'N/A')}")
        content = payload.get("content", "")
        preview = content[:300].replace("\n", " ") + "..." if len(content) > 300 else content.replace("\n", " ")
        print(f"  Content: {preview}")
        print()


def search_by_mine(
    client: QdrantClient,
    collection_name: str,
    mine_name: str,
    limit: int = 10,
) -> None:
    """List documents for a specific mine."""
    results = client.scroll(
        collection_name=collection_name,
        scroll_filter={"must": [{"key": "mine_name", "match": {"value": mine_name}}]},
        limit=limit,
        with_payload=["meta_data"],
        with_vectors=False,
    )
    points, _ = results

    print(f"\nDocuments for mine '{mine_name}' ({len(points)} chunks):")
    seen_files = set()
    for point in points:
        meta = point.payload.get("meta_data", {})
        source = meta.get("filename", "N/A")
        if source not in seen_files:
            seen_files.add(source)
            print(f"  - {source}")


def main():
    parser = argparse.ArgumentParser(
        description="Verify Utah OGM documents in Qdrant"
    )
    parser.add_argument(
        "--collection-name",
        default=DEFAULT_COLLECTION_NAME,
        help=f"Qdrant collection name (default: {DEFAULT_COLLECTION_NAME})",
    )
    parser.add_argument(
        "--search",
        metavar="QUERY",
        help="Search documents with a query",
    )
    parser.add_argument(
        "--list-mines",
        action="store_true",
        help="List all unique mine names",
    )
    parser.add_argument(
        "--mine",
        metavar="NAME",
        help="Show documents for a specific mine",
    )
    parser.add_argument(
        "--sample",
        type=int,
        metavar="N",
        help="Show N sample documents",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Number of results for search/mine queries (default: 5)",
    )

    args = parser.parse_args()

    qdrant_client = get_qdrant_client()

    # Always show stats
    show_collection_stats(qdrant_client, args.collection_name)

    # Get unique counts
    mines = get_unique_values(qdrant_client, args.collection_name, "mine_name")
    print(f"  Unique mines: {len(mines)}")

    # Optional operations
    if args.list_mines:
        list_mines(qdrant_client, args.collection_name)

    if args.mine:
        search_by_mine(qdrant_client, args.collection_name, args.mine, args.limit)

    if args.sample:
        show_sample(qdrant_client, args.collection_name, args.sample)

    if args.search:
        openai_client = get_openai_client()
        search_documents(
            qdrant_client,
            openai_client,
            args.collection_name,
            args.search,
            args.limit,
        )


if __name__ == "__main__":
    main()
