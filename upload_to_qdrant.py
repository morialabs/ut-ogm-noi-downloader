#!/usr/bin/env python3
"""
Upload Utah OGM coal permit documents to Qdrant for contact extraction.

Usage:
    python upload_to_qdrant.py                    # Upload all documents
    python upload_to_qdrant.py --dry-run          # List files without uploading
    python upload_to_qdrant.py --limit 10         # Upload only 10 documents
    python upload_to_qdrant.py --data-dir output  # Use different input directory
"""

import argparse
import hashlib
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF
from dotenv import load_dotenv
from openai import OpenAI
from qdrant_client import QdrantClient
from qdrant_client.http import models

# Load environment variables
load_dotenv()

# Configuration
DEFAULT_COLLECTION_NAME = "utah-ogm-coal"
DEFAULT_DATA_DIR = "output_coal"
VECTOR_SIZE = 3072  # text-embedding-3-large
EMBEDDING_MODEL = "text-embedding-3-large"
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 200
UPLOAD_BATCH_SIZE = 100
MAX_PAGES = 20  # Only process first N pages of long documents


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


def create_collection(client: QdrantClient, collection_name: str) -> None:
    """Create a Qdrant collection if it doesn't exist."""
    collections = client.get_collections().collections
    if any(c.name == collection_name for c in collections):
        print(f"Collection '{collection_name}' already exists")
        return

    client.create_collection(
        collection_name=collection_name,
        vectors_config=models.VectorParams(
            size=VECTOR_SIZE,
            distance=models.Distance.COSINE,
        ),
    )
    print(f"Created collection '{collection_name}'")

    # Create payload indexes for efficient filtering
    client.create_payload_index(
        collection_name=collection_name,
        field_name="meta_data.mine_name",
        field_schema=models.PayloadSchemaType.KEYWORD,
    )
    client.create_payload_index(
        collection_name=collection_name,
        field_name="meta_data.source",
        field_schema=models.PayloadSchemaType.KEYWORD,
    )
    print(f"Created payload indexes for '{collection_name}'")


def extract_pdf_text(pdf_path: Path, max_pages: int = MAX_PAGES) -> list[tuple[int, str]]:
    """
    Extract text from a PDF file, page by page.

    Args:
        pdf_path: Path to PDF file
        max_pages: Maximum number of pages to extract (default: 20)

    Returns:
        List of (page_number, text) tuples (1-indexed page numbers)
    """
    pages = []
    try:
        doc = fitz.open(pdf_path)
        total_pages = len(doc)
        for page_num, page in enumerate(doc, start=1):
            if page_num > max_pages:
                print(f"  Note: Limiting to first {max_pages} of {total_pages} pages")
                break
            text = page.get_text()
            if text.strip():
                pages.append((page_num, text))
        doc.close()
    except Exception as e:
        print(f"  Warning: Failed to extract text from {pdf_path.name}: {e}")
        return []

    return pages


def chunk_text(content: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """
    Split document content into overlapping chunks.

    Args:
        content: Full document text
        chunk_size: Characters per chunk
        overlap: Overlap between chunks

    Returns:
        List of text chunks
    """
    chunks = []
    start = 0

    while start < len(content):
        end = start + chunk_size
        chunk = content[start:end]

        if chunk.strip():
            chunks.append(chunk)

        start = end - overlap
        if start < 0:
            start = 0
        if end >= len(content):
            break

    return chunks


def chunk_pages(pages: list[tuple[int, str]]) -> list[dict]:
    """
    Chunk pages while preserving page number metadata.

    Returns:
        List of dicts with 'text', 'page_number', 'chunk_index'
    """
    result = []
    chunk_index = 0

    for page_num, text in pages:
        page_chunks = chunk_text(text)
        for chunk in page_chunks:
            result.append({
                "text": chunk,
                "page_number": page_num,
                "chunk_index": chunk_index,
            })
            chunk_index += 1

    return result


def get_embedding(client: OpenAI, text: str) -> list[float]:
    """Generate embedding vector for text using OpenAI."""
    response = client.embeddings.create(
        input=text,
        model=EMBEDDING_MODEL,
    )
    return response.data[0].embedding


def get_embeddings_batch(client: OpenAI, texts: list[str], batch_size: int = 50) -> list[list[float]]:
    """Generate embeddings for multiple texts, batching to avoid token limits."""
    if not texts:
        return []

    all_embeddings = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        response = client.embeddings.create(
            input=batch,
            model=EMBEDDING_MODEL,
        )
        all_embeddings.extend([item.embedding for item in response.data])

    return all_embeddings


def parse_coal_filename(filename: str) -> dict:
    """
    Parse metadata from coal permit filename.

    Expected format: {date}_{description}_from_{from}_to_{to}.pdf

    Returns:
        Dict with file_date, description, doc_from, doc_to
    """
    metadata = {
        "file_date": None,
        "description": None,
        "doc_from": None,
        "doc_to": None,
    }

    # Remove .pdf extension
    name = filename
    if name.lower().endswith(".pdf"):
        name = name[:-4]

    # Try to extract date (YYYY-MM-DD at start)
    date_match = re.match(r"^(\d{4}-\d{2}-\d{2})_(.*)$", name)
    if date_match:
        metadata["file_date"] = date_match.group(1)
        name = date_match.group(2)

    # Try to extract _from_ and _to_
    from_to_match = re.search(r"^(.*)_from_(.*)_to_(.*)$", name, re.IGNORECASE)
    if from_to_match:
        metadata["description"] = from_to_match.group(1).replace("_", " ").strip()
        metadata["doc_from"] = from_to_match.group(2).replace("_", " ").strip()
        metadata["doc_to"] = from_to_match.group(3).replace("_", " ").strip()
    else:
        # Fallback: use entire remaining string as description
        metadata["description"] = name.replace("_", " ").strip()

    return metadata


def generate_chunk_id(source_file: str, chunk_index: int) -> str:
    """Generate a deterministic UUID for a chunk based on file and index."""
    content = f"{source_file}:{chunk_index}"
    hash_bytes = hashlib.md5(content.encode()).digest()
    return str(uuid.UUID(bytes=hash_bytes))


def upload_document(
    qdrant_client: QdrantClient,
    openai_client: OpenAI,
    collection_name: str,
    pdf_path: Path,
    mine_name: str,
    dry_run: bool = False,
) -> int:
    """
    Chunk and upload a single PDF to Qdrant.

    Returns:
        Number of chunks uploaded
    """
    # Extract text from PDF
    pages = extract_pdf_text(pdf_path)
    if not pages:
        print(f"  Skipping {pdf_path.name}: no text extracted")
        return 0

    # Chunk the document
    chunks = chunk_pages(pages)
    if not chunks:
        return 0

    # Parse filename metadata
    file_metadata = parse_coal_filename(pdf_path.name)

    if dry_run:
        print(f"  [DRY-RUN] Would upload {len(chunks)} chunks from {pdf_path.name}")
        return len(chunks)

    # Generate embeddings in batch
    texts = [c["text"] for c in chunks]
    embeddings = get_embeddings_batch(openai_client, texts)

    # Prepare points for upload
    points = []
    for chunk, embedding in zip(chunks, embeddings):
        chunk_id = generate_chunk_id(pdf_path.name, chunk["chunk_index"])

        points.append(models.PointStruct(
            id=chunk_id,
            vector=embedding,
            payload={
                "name": pdf_path.name,  # Agno requires this
                "mine_name": mine_name,  # Root level for Agno compatibility
                "content": chunk["text"],
                "meta_data": {
                    "mine_name": mine_name,
                    "source": "utah_ogm",  # Required for filtering
                    "filename": pdf_path.name,  # Renamed from source_file
                    "page_number": chunk["page_number"],
                    "chunk_index": chunk["chunk_index"],
                    "file_date": file_metadata["file_date"],
                    "description": file_metadata["description"],
                    "doc_from": file_metadata["doc_from"],
                    "doc_to": file_metadata["doc_to"],
                }
            }
        ))

    # Upload in batches
    for i in range(0, len(points), UPLOAD_BATCH_SIZE):
        batch = points[i:i + UPLOAD_BATCH_SIZE]
        qdrant_client.upsert(
            collection_name=collection_name,
            points=batch,
        )

    print(f"  Uploaded {len(points)} chunks from {pdf_path.name}")
    return len(points)


def upload_all_documents(
    collection_name: str,
    data_dir: Path,
    dry_run: bool = False,
    limit: Optional[int] = None,
) -> dict:
    """
    Upload all PDFs from a directory structure to Qdrant.

    Expected structure:
        data_dir/
        ├── mine_name_1/
        │   ├── doc1.pdf
        │   └── doc2.pdf
        └── mine_name_2/
            └── doc3.pdf

    Returns:
        Stats dict with counts
    """
    stats = {"documents": 0, "chunks": 0, "errors": [], "skipped": 0}

    # Get clients
    qdrant_client = get_qdrant_client()
    openai_client = get_openai_client()

    # Create collection if needed
    create_collection(qdrant_client, collection_name)

    # Collect all PDFs
    all_pdfs = []
    for folder in sorted(data_dir.iterdir()):
        if not folder.is_dir() or folder.name == "debug":
            continue

        mine_name = folder.name  # Folder name = mine identifier

        for pdf_path in sorted(folder.glob("*.pdf")):
            all_pdfs.append((pdf_path, mine_name))

    # Apply limit if specified
    if limit:
        all_pdfs = all_pdfs[:limit]

    print(f"Processing {len(all_pdfs)} PDF files...")

    for pdf_path, mine_name in all_pdfs:
        print(f"Processing: {mine_name}/{pdf_path.name}")
        try:
            chunks = upload_document(
                qdrant_client=qdrant_client,
                openai_client=openai_client,
                collection_name=collection_name,
                pdf_path=pdf_path,
                mine_name=mine_name,
                dry_run=dry_run,
            )
            if chunks > 0:
                stats["documents"] += 1
                stats["chunks"] += chunks
            else:
                stats["skipped"] += 1
        except Exception as e:
            error_msg = f"{pdf_path}: {e}"
            stats["errors"].append(error_msg)
            print(f"  Error: {e}")

    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Upload Utah OGM coal permit documents to Qdrant"
    )
    parser.add_argument(
        "--collection-name",
        default=DEFAULT_COLLECTION_NAME,
        help=f"Qdrant collection name (default: {DEFAULT_COLLECTION_NAME})",
    )
    parser.add_argument(
        "--data-dir",
        default=DEFAULT_DATA_DIR,
        help=f"Input directory with PDF files (default: {DEFAULT_DATA_DIR})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List files without uploading",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Process only N documents (for testing)",
    )

    args = parser.parse_args()

    # Resolve data directory
    project_root = Path(__file__).parent
    data_dir = project_root / args.data_dir

    if not data_dir.exists():
        print(f"Error: Data directory not found: {data_dir}")
        sys.exit(1)

    print(f"Collection: {args.collection_name}")
    print(f"Data directory: {data_dir}")
    if args.dry_run:
        print("Mode: DRY RUN (no uploads)")
    if args.limit:
        print(f"Limit: {args.limit} documents")
    print()

    stats = upload_all_documents(
        collection_name=args.collection_name,
        data_dir=data_dir,
        dry_run=args.dry_run,
        limit=args.limit,
    )

    print()
    print("=" * 50)
    print("Summary:")
    print(f"  Documents processed: {stats['documents']}")
    print(f"  Chunks uploaded: {stats['chunks']}")
    print(f"  Documents skipped: {stats['skipped']}")
    if stats["errors"]:
        print(f"  Errors: {len(stats['errors'])}")
        for error in stats["errors"][:5]:
            print(f"    - {error}")
        if len(stats["errors"]) > 5:
            print(f"    ... and {len(stats['errors']) - 5} more")


if __name__ == "__main__":
    main()
