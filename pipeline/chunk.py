"""
pipeline/chunk.py

Split each AntiBook into serve-ready JSON chunks and write meta.json.

Output structure (under dist/books/{id}/):
    meta.json           — title, author, word count, chunk count, preview snippet
    chunk-0.json        — { index, word_offset, text }
    chunk-1.json
    ...

Chunk size target: ~20 KB uncompressed (~5 KB gzipped).

Usage:
    python pipeline/chunk.py [--ids 1 2 3 ...] [--chunk-size N] [--force]
"""

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from tqdm import tqdm

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
ANTIBOOK_DIR = DATA_DIR / "antibooks"
CATALOG_PATH = DATA_DIR / "gutenberg-catalog.json"
DIST_DIR = ROOT / "dist" / "books"
MANIFEST_PATH = ROOT / "dist" / "manifest.json"

# Target chunk size in characters (approx 20 KB)
DEFAULT_CHUNK_CHARS = 20_000

WORD_RE = re.compile(r"[a-zA-Z]+(?:'[a-zA-Z]+)?(?:-[a-zA-Z]+)*")


def count_words(text: str) -> int:
    return len(WORD_RE.findall(text))


def get_map_version() -> str:
    """Read antonym-map.json mtime as a version hash proxy."""
    map_path = DATA_DIR / "antonym-map.json"
    if map_path.exists():
        return str(int(map_path.stat().st_mtime))
    return "unknown"


def split_into_chunks(text: str, chunk_chars: int) -> list[dict]:
    """
    Split text into chunks of approximately chunk_chars characters,
    always breaking at paragraph boundaries where possible, then word boundaries.
    Returns list of dicts: {index, word_offset, text}.
    """
    chunks = []
    word_offset = 0
    pos = 0
    chunk_idx = 0
    n = len(text)

    while pos < n:
        end = min(pos + chunk_chars, n)

        if end < n:
            # Try to break at a paragraph boundary (double newline)
            para_break = text.rfind("\n\n", pos, end)
            if para_break != -1 and para_break > pos:
                end = para_break + 2
            else:
                # Fall back to word boundary
                space = text.rfind(" ", pos, end)
                if space != -1 and space > pos:
                    end = space + 1

        chunk_text = text[pos:end]
        chunks.append({
            "index": chunk_idx,
            "word_offset": word_offset,
            "text": chunk_text,
        })

        word_offset += count_words(chunk_text)
        pos = end
        chunk_idx += 1

    return chunks


def make_preview(antibook_text: str, word_limit: int = 200) -> str:
    """Return the first word_limit words as a preview snippet."""
    words = antibook_text.split()
    return " ".join(words[:word_limit])


def chunk_book(book_id: int, meta: dict, chunk_chars: int, map_version: str, force: bool) -> bool:
    out_dir = DIST_DIR / str(book_id)
    meta_path = out_dir / "meta.json"

    # Check manifest for already-processed books
    if not force and meta_path.exists():
        existing_meta = json.loads(meta_path.read_text())
        if existing_meta.get("map_version") == map_version:
            return False

    antibook_path = ANTIBOOK_DIR / f"{book_id}.txt"
    if not antibook_path.exists():
        return False

    antibook_text = antibook_path.read_text(encoding="utf-8", errors="replace")
    chunks = split_into_chunks(antibook_text, chunk_chars)

    out_dir.mkdir(parents=True, exist_ok=True)

    # Write chunk files
    for chunk in chunks:
        chunk_path = out_dir / f"chunk-{chunk['index']}.json"
        chunk_path.write_text(
            json.dumps(chunk, ensure_ascii=False),
            encoding="utf-8",
        )

    # Write meta.json
    total_words = count_words(antibook_text)
    book_meta = {
        "id": book_id,
        "title": meta.get("title", f"Book {book_id}"),
        "author": meta.get("author", "Unknown"),
        "gutenberg_id": book_id,
        "language": meta.get("language", "en"),
        "subjects": meta.get("subjects", []),
        "word_count": total_words,
        "chunk_count": len(chunks),
        "map_version": map_version,
        "preview": make_preview(antibook_text),
        "chunked_at": datetime.now(timezone.utc).isoformat(),
    }
    meta_path.write_text(
        json.dumps(book_meta, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return True


def load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text())
    return {"books": {}, "map_version": None}


def save_manifest(manifest: dict):
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Chunk AntiBooks into serve-ready JSON.")
    parser.add_argument("--ids", type=int, nargs="*", help="Specific book IDs to chunk")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_CHARS,
                        help="Target chunk size in characters (default: 20000)")
    parser.add_argument("--force", action="store_true", help="Re-chunk already-processed books")
    args = parser.parse_args()

    catalog = json.loads(CATALOG_PATH.read_text())
    books = catalog.get("books", {})
    map_version = get_map_version()
    manifest = load_manifest()

    if args.ids:
        book_items = [(str(i), books[str(i)]) for i in args.ids if str(i) in books]
    else:
        book_items = list(books.items())

    new_count = skipped = 0
    for book_id_str, meta in tqdm(book_items, desc="Chunking"):
        book_id = int(book_id_str)
        try:
            result = chunk_book(book_id, meta, args.chunk_size, map_version, args.force)
            if result:
                new_count += 1
                manifest["books"][book_id_str] = {
                    "map_version": map_version,
                    "chunked_at": datetime.now(timezone.utc).isoformat(),
                }
            else:
                skipped += 1
        except Exception as e:
            print(f"  [{book_id_str}] Error: {e}")

    manifest["map_version"] = map_version
    save_manifest(manifest)

    print(f"\nDone. New: {new_count}  Skipped: {skipped}")
    print(f"Output: {DIST_DIR}")


if __name__ == "__main__":
    main()
