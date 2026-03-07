"""
pipeline/ingest.py

Gutenberg ingestion: download plain-text files, strip boilerplate,
extract metadata, and update data/gutenberg-catalog.json.

Usage:
    python pipeline/ingest.py [--limit N] [--ids 1 2 3 ...]

The top-500 most-downloaded English works are fetched by default.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from tqdm import tqdm

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
CATALOG_PATH = DATA_DIR / "gutenberg-catalog.json"

RAW_DIR.mkdir(parents=True, exist_ok=True)

# Gutenberg mirrors (tried in order)
MIRRORS = [
    "https://www.gutenberg.org/files/{id}/{id}-0.txt",
    "https://www.gutenberg.org/files/{id}/{id}.txt",
    "https://gutenberg.pglaf.org/cache/epub/{id}/pg{id}.txt",
    "https://aleph.gutenberg.org/cache/epub/{id}/pg{id}.txt",
]

# Gutenberg catalog RSS / top-100 list
POPULAR_LIST_URL = "https://www.gutenberg.org/browse/scores/top"
CATALOG_RDF_BASE = "https://www.gutenberg.org/ebooks/{id}.rdf"

# The well-known Gutenberg boilerplate delimiters
START_MARKERS = [
    r"\*\*\* ?START OF (THE|THIS) PROJECT GUTENBERG EBOOK",
    r"\*\*\* ?START: FULL LICENSE",
]
END_MARKERS = [
    r"\*\*\* ?END OF (THE|THIS) PROJECT GUTENBERG EBOOK",
    r"End of (the )?Project Gutenberg",
]


def fetch_top_ids(limit: int = 500) -> list[int]:
    """Scrape the Gutenberg top-downloads page to get the most popular English book IDs."""
    print(f"Fetching top {limit} Gutenberg IDs …")
    try:
        resp = requests.get(POPULAR_LIST_URL, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  Warning: could not fetch popular list ({e}); falling back to known top IDs.")
        return _fallback_top_ids()[:limit]

    # IDs appear as /ebooks/NNN links
    ids = list(dict.fromkeys(
        int(m) for m in re.findall(r"/ebooks/(\d+)", resp.text)
        if m.isdigit()
    ))
    print(f"  Found {len(ids)} IDs from popular list.")
    return ids[:limit]


def _fallback_top_ids() -> list[int]:
    """Hard-coded list of canonical top Gutenberg works (used as fallback)."""
    return [
        1342, 11, 84, 1661, 98, 2701, 1952, 76, 74, 43,
        2542, 514, 1080, 345, 2852, 5200, 4300, 2600, 100, 1400,
        2554, 174, 161, 730, 1184, 46, 25344, 4517, 768, 113,
        16328, 1260, 2148, 219, 844, 236, 158, 1232, 135, 996,
        2097, 1727, 2814, 203, 2591, 30254, 105, 45, 244, 1399,
        863, 1998, 17192, 2500, 516, 521, 5740, 7370, 2097, 2800,
        3207, 1251, 4363, 1, 55, 35, 36, 37, 38, 39,
        40, 41, 42, 44, 45, 46, 47, 48, 49, 50,
        51, 52, 53, 54, 56, 57, 58, 59, 60, 61,
    ]


def fetch_metadata_rdf(book_id: int) -> dict:
    """Fetch book metadata from Gutenberg's RDF catalog."""
    url = CATALOG_RDF_BASE.format(id=book_id)
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        text = resp.text
    except requests.RequestException:
        return {}

    meta = {"id": book_id}
    title_m = re.search(r"<dcterms:title>(.*?)</dcterms:title>", text, re.S)
    author_m = re.search(r"<pgterms:name>(.*?)</pgterms:name>", text, re.S)
    lang_m = re.search(r"<dcterms:language>.*?<rdf:value[^>]*>(.*?)</rdf:value>", text, re.S)
    subject_ms = re.findall(r"<dcterms:subject>.*?<rdf:value[^>]*>(.*?)</rdf:value>", text, re.S)

    if title_m:
        meta["title"] = _clean_xml(title_m.group(1))
    if author_m:
        meta["author"] = _clean_xml(author_m.group(1))
    if lang_m:
        meta["language"] = lang_m.group(1).strip()
    if subject_ms:
        meta["subjects"] = [_clean_xml(s) for s in subject_ms]

    return meta


def _clean_xml(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    return s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").strip()


def download_text(book_id: int) -> str | None:
    """Try each mirror in order; return the raw text or None on failure."""
    for template in MIRRORS:
        url = template.format(id=book_id)
        try:
            resp = requests.get(url, timeout=60)
            if resp.status_code == 200:
                # Gutenberg serves Latin-1 or UTF-8; try both
                for enc in ("utf-8", "latin-1"):
                    try:
                        return resp.content.decode(enc)
                    except UnicodeDecodeError:
                        continue
        except requests.RequestException:
            continue
    return None


def strip_boilerplate(text: str) -> str:
    """Remove Gutenberg header/footer boilerplate."""
    # Find start
    start_pos = 0
    for pattern in START_MARKERS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            # skip to the end of the start-marker line
            line_end = text.find("\n", m.end())
            if line_end != -1:
                start_pos = line_end + 1
            break

    # Find end
    end_pos = len(text)
    for pattern in END_MARKERS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            end_pos = m.start()
            break

    body = text[start_pos:end_pos].strip()
    return body


def is_english(meta: dict) -> bool:
    lang = meta.get("language", "").lower()
    return lang in ("en", "english", "") or lang.startswith("en")


def ingest_book(book_id: int, catalog: dict, force: bool = False) -> bool:
    """
    Download, strip, and save a single book. Returns True if newly processed.
    Skips books already in catalog unless force=True.
    """
    raw_path = RAW_DIR / f"{book_id}.txt"

    if not force and str(book_id) in catalog.get("books", {}):
        # Still re-download if the stripped file is missing (e.g. fresh CI checkout)
        existing_meta = catalog["books"][str(book_id)]
        stripped_path = ROOT / existing_meta.get("stripped_path", "")
        if stripped_path.exists():
            return False

    meta = fetch_metadata_rdf(book_id)
    if not meta:
        meta = {"id": book_id}

    if not is_english(meta):
        return False

    # Download if not already cached locally
    if raw_path.exists():
        raw_text = raw_path.read_text(encoding="utf-8", errors="replace")
    else:
        raw_text = download_text(book_id)
        if raw_text is None:
            print(f"  [{book_id}] Download failed — skipping.")
            return False
        raw_path.write_text(raw_text, encoding="utf-8")

    body = strip_boilerplate(raw_text)
    if len(body) < 1000:
        print(f"  [{book_id}] Body too short after stripping — skipping.")
        return False

    # Count words
    word_count = len(body.split())
    meta["word_count"] = word_count
    meta["raw_path"] = str(raw_path.relative_to(ROOT))
    meta["ingested_at"] = datetime.now(timezone.utc).isoformat()

    # Save stripped body
    stripped_path = RAW_DIR / f"{book_id}_stripped.txt"
    stripped_path.write_text(body, encoding="utf-8")
    meta["stripped_path"] = str(stripped_path.relative_to(ROOT))

    catalog.setdefault("books", {})[str(book_id)] = meta
    return True


def load_catalog() -> dict:
    if CATALOG_PATH.exists():
        return json.loads(CATALOG_PATH.read_text())
    return {"_meta": {}, "books": {}}


def save_catalog(catalog: dict):
    catalog.setdefault("_meta", {})["last_updated"] = datetime.now(timezone.utc).isoformat()
    catalog["_meta"]["total_titles"] = len(catalog.get("books", {}))
    CATALOG_PATH.write_text(json.dumps(catalog, indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="Ingest Project Gutenberg books.")
    parser.add_argument("--limit", type=int, default=500, help="Number of top books to ingest")
    parser.add_argument("--ids", type=int, nargs="*", help="Specific book IDs to ingest")
    parser.add_argument("--force", action="store_true", help="Re-ingest books already in catalog")
    args = parser.parse_args()

    catalog = load_catalog()

    if args.ids:
        book_ids = args.ids
    else:
        book_ids = fetch_top_ids(args.limit)

    new_count = 0
    skipped = 0
    failed = 0

    for book_id in tqdm(book_ids, desc="Ingesting books"):
        try:
            result = ingest_book(book_id, catalog, force=args.force)
            if result:
                new_count += 1
                save_catalog(catalog)  # save incrementally
            else:
                skipped += 1
        except Exception as e:
            print(f"  [{book_id}] Error: {e}")
            failed += 1
        time.sleep(0.1)  # polite rate limiting

    save_catalog(catalog)
    print(f"\nDone. New: {new_count}  Skipped: {skipped}  Failed: {failed}")
    print(f"Total in catalog: {len(catalog.get('books', {}))}")


if __name__ == "__main__":
    main()
