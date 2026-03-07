"""
pipeline/index.py

Build the client-side search catalog from all generated meta.json files.

Outputs:
    dist/catalog.json   — array of book metadata objects (used by Fuse.js on the client)
    dist/index-stats.json — build stats

Usage:
    python pipeline/index.py
"""

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = ROOT / "dist"
BOOKS_DIR = DIST_DIR / "books"
CATALOG_OUT = DIST_DIR / "catalog.json"
STATS_OUT = DIST_DIR / "index-stats.json"


def build_catalog() -> list[dict]:
    """Collect all meta.json files and assemble a flat catalog array."""
    entries = []
    if not BOOKS_DIR.exists():
        print(f"No books directory found at {BOOKS_DIR}")
        return entries

    for meta_path in sorted(BOOKS_DIR.glob("*/meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            # Lean catalog entry — only fields needed for search + display
            entry = {
                "id": meta["id"],
                "title": meta.get("title", ""),
                "author": meta.get("author", ""),
                "subjects": meta.get("subjects", []),
                "word_count": meta.get("word_count", 0),
                "chunk_count": meta.get("chunk_count", 0),
                "preview": meta.get("preview", ""),
            }
            entries.append(entry)
        except Exception as e:
            print(f"  Warning: could not read {meta_path}: {e}")

    return entries


def write_stats(entries: list[dict]):
    stats = {
        "total_books": len(entries),
        "total_words": sum(e.get("word_count", 0) for e in entries),
        "built_at": datetime.now(timezone.utc).isoformat(),
    }
    STATS_OUT.write_text(json.dumps(stats, indent=2), encoding="utf-8")
    return stats


def main():
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    print("Building search catalog …")
    entries = build_catalog()

    if not entries:
        print("No entries found. Run chunk.py first.")
        return

    # Sort by title for deterministic output
    entries.sort(key=lambda e: e.get("title", "").lower())

    CATALOG_OUT.write_text(
        json.dumps(entries, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    stats = write_stats(entries)

    catalog_size_kb = CATALOG_OUT.stat().st_size / 1024
    print(f"  Books indexed:   {stats['total_books']:,}")
    print(f"  Total words:     {stats['total_words']:,}")
    print(f"  catalog.json:    {catalog_size_kb:.1f} KB")
    print(f"  Output:          {CATALOG_OUT}")


if __name__ == "__main__":
    main()
