"""
pipeline/build_map.py

Tiered antonym map construction. Produces data/antonym-map.json and
an audit log at data/audit-log.csv showing which tier resolved each entry.

Tier 1: Curated core (data/curated-core.json)
Tier 2: WordNet antonym pairs via NLTK
Tier 3: Moby Thesaurus + synonym→antonym transitive closure
Tier 4: GloVe embedding cosine-dissimilarity inversion (optional, --glove)
Tier 5: Identity fallback (implicit — words with no mapping keep themselves)

Usage:
    python pipeline/build_map.py [--glove] [--glove-path PATH]
"""

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import nltk
from nltk.corpus import wordnet as wn

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CURATED_CORE_PATH = DATA_DIR / "curated-core.json"
MOBY_PATH = DATA_DIR / "moby-thesaurus" / "mthesaur.txt"
ANTONYM_MAP_PATH = DATA_DIR / "antonym-map.json"
AUDIT_LOG_PATH = DATA_DIR / "audit-log.csv"

GLOVE_DEFAULT = DATA_DIR / "glove" / "glove.6B.300d.txt"


def ensure_nltk():
    for corpus in ("wordnet", "averaged_perceptron_tagger", "omw-1.4"):
        try:
            nltk.data.find(f"corpora/{corpus}")
        except LookupError:
            print(f"  Downloading NLTK corpus: {corpus}")
            nltk.download(corpus, quiet=True)


# ---------------------------------------------------------------------------
# Tier 1: Curated core
# ---------------------------------------------------------------------------

def load_tier1() -> dict[str, str]:
    raw = json.loads(CURATED_CORE_PATH.read_text())
    # Skip _meta key
    return {k: v for k, v in raw.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# Tier 2: WordNet antonym pairs
# ---------------------------------------------------------------------------

def extract_wordnet_antonyms() -> dict[str, str]:
    """
    For every lemma in WordNet that has a direct antonym relation,
    record the most common sense antonym.
    """
    antonyms: dict[str, list[tuple[str, int]]] = defaultdict(list)

    for synset in wn.all_synsets():
        for lemma in synset.lemmas():
            word = lemma.name().lower().replace("_", " ")
            for ant_lemma in lemma.antonyms():
                ant_word = ant_lemma.name().lower().replace("_", " ")
                # Use lemma count as a proxy for frequency
                freq = ant_lemma.count() + 1
                antonyms[word].append((ant_word, freq))

    # Pick the highest-frequency antonym for each word
    result = {}
    for word, candidates in antonyms.items():
        best = max(candidates, key=lambda x: x[1])
        result[word] = best[0]

    return result


# ---------------------------------------------------------------------------
# Tier 3: Moby Thesaurus transitive closure
# ---------------------------------------------------------------------------

def load_moby_thesaurus() -> dict[str, list[str]]:
    """
    Parse mthesaur.txt format:
        root_word,synonym1,synonym2,...
    Returns a dict: word -> list of synonyms (including the root).
    """
    if not MOBY_PATH.exists():
        print(f"  Moby Thesaurus not found at {MOBY_PATH} — skipping Tier 3.")
        return {}

    thesaurus: dict[str, list[str]] = {}
    with MOBY_PATH.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = [p.strip().lower() for p in line.strip().split(",")]
            if parts:
                root = parts[0]
                synonyms = parts[1:]
                thesaurus[root] = synonyms
    return thesaurus


def build_tier3(tier1: dict, tier2: dict, thesaurus: dict) -> dict[str, str]:
    """
    For words not covered by Tier 1 or Tier 2:
    Find word's Moby synonym cluster, then check if any synonym has a
    Tier1/Tier2 antonym whose own synonyms overlap with our word's cluster.

    This is the transitive closure: if happy~joyful and joyful->sorrowful,
    then happy->sorrowful (if no direct antonym was found earlier).
    """
    if not thesaurus:
        return {}

    known = {**tier1, **tier2}
    result = {}

    # Build reverse index: synonym -> root words it belongs to
    synonym_to_roots: dict[str, list[str]] = defaultdict(list)
    for root, syns in thesaurus.items():
        for syn in syns:
            synonym_to_roots[syn].append(root)
        synonym_to_roots[root].append(root)

    for word, synonyms in thesaurus.items():
        if word in known:
            continue

        syn_cluster = set(synonyms) | {word}

        # For each synonym of `word`, check if that synonym has a direct antonym
        for syn in synonyms:
            if syn in known:
                candidate = known[syn]
                # Skip if the candidate is the word itself or one of its synonyms
                # (that would be a circular or identity mapping)
                if candidate != word and candidate not in syn_cluster:
                    result[word] = candidate
                    break

    return result


# ---------------------------------------------------------------------------
# Tier 4: GloVe embedding inversion
# ---------------------------------------------------------------------------

def load_glove(glove_path: Path, vocab: set[str]) -> dict[str, list[float]]:
    """Load GloVe vectors only for words in vocab (to limit memory)."""
    print(f"  Loading GloVe vectors from {glove_path} (this may take a minute)…")
    vectors = {}
    with glove_path.open(encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip().split(" ")
            word = parts[0]
            if word in vocab:
                vectors[word] = [float(x) for x in parts[1:]]
    return vectors


def cosine_similarity(a: list[float], b: list[float]) -> float:
    import math
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def build_tier4(remaining: set[str], known: dict[str, str], glove_path: Path,
                thesaurus: dict[str, list[str]] | None = None) -> dict[str, str]:
    """
    For remaining words: use GloVe to find the best antonym candidate.

    Strategy: for each word, collect the antonyms of its Moby synonyms as
    candidates (same logic as Tier 3, but use embedding similarity to pick
    the *best* candidate rather than just the first). Only accept mappings
    where cosine similarity is genuinely negative (truly dissimilar vectors).

    Falls back to searching all known antonym values only when no synonym-based
    candidates exist, but still requires negative cosine similarity.
    """
    if not glove_path.exists():
        print(f"  GloVe file not found at {glove_path} — skipping Tier 4.")
        return {}

    import numpy as np

    vocab = remaining | set(known.keys()) | set(known.values())
    vectors = load_glove(glove_path, vocab)

    result = {}

    for word in remaining:
        if word not in vectors:
            continue

        vec = np.array(vectors[word])
        norm = np.linalg.norm(vec)
        if norm == 0:
            continue
        vec_normalized = vec / norm

        # Build candidate antonyms from synonyms' known antonyms
        candidates: set[str] = set()
        if thesaurus and word in thesaurus:
            syn_cluster = set(thesaurus[word]) | {word}
            for syn in thesaurus[word]:
                if syn in known and known[syn] not in syn_cluster:
                    candidates.add(known[syn])
        # Fallback: all known antonym values (filtered below by similarity)
        if not candidates:
            candidates = set(known.values())

        candidates.discard(word)
        candidate_list = [c for c in candidates if c in vectors]
        if not candidate_list:
            continue

        cand_vecs = np.array([vectors[c] for c in candidate_list])
        cand_norms = np.linalg.norm(cand_vecs, axis=1, keepdims=True)
        cand_normalized = cand_vecs / (cand_norms + 1e-9)

        sims = cand_normalized @ vec_normalized
        idx = int(np.argmin(sims))

        # Only accept if similarity is genuinely negative (opposite direction)
        if sims[idx] < 0.0:
            result[word] = candidate_list[idx]

    return result


# ---------------------------------------------------------------------------
# Map assembly
# ---------------------------------------------------------------------------

def assemble_map(
    tier1: dict, tier2: dict, tier3: dict, tier4: dict
) -> tuple[dict[str, str], list[dict]]:
    """
    Merge tiers in priority order. Return (final_map, audit_rows).
    """
    final_map: dict[str, str] = {}
    audit: list[dict] = []

    def add(word: str, antonym: str, tier: int):
        if word not in final_map:
            final_map[word] = antonym
            audit.append({"word": word, "antonym": antonym, "tier": tier})

    for word, ant in tier1.items():
        add(word, ant, 1)
    for word, ant in tier2.items():
        add(word, ant, 2)
    for word, ant in tier3.items():
        add(word, ant, 3)
    for word, ant in tier4.items():
        add(word, ant, 4)

    return final_map, audit


def write_audit_log(audit: list[dict]):
    with AUDIT_LOG_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["word", "antonym", "tier"])
        writer.writeheader()
        writer.writerows(sorted(audit, key=lambda r: (r["tier"], r["word"])))


def print_stats(audit: list[dict], final_map: dict):
    from collections import Counter
    tier_counts = Counter(r["tier"] for r in audit)
    total = len(final_map)
    print(f"\nAntonym map statistics:")
    print(f"  Total entries:  {total:,}")
    for tier, label in [
        (1, "Tier 1 (curated core)"),
        (2, "Tier 2 (WordNet)"),
        (3, "Tier 3 (Moby transitive)"),
        (4, "Tier 4 (GloVe inversion)"),
    ]:
        count = tier_counts.get(tier, 0)
        pct = 100 * count / total if total else 0
        print(f"  {label}: {count:,}  ({pct:.1f}%)")


def main():
    parser = argparse.ArgumentParser(description="Build the tiered antonym map.")
    parser.add_argument("--glove", action="store_true", help="Enable Tier 4 GloVe inversion")
    parser.add_argument("--glove-path", type=Path, default=GLOVE_DEFAULT,
                        help="Path to glove.6B.300d.txt")
    args = parser.parse_args()

    print("Ensuring NLTK corpora …")
    ensure_nltk()

    print("Tier 1: Loading curated core …")
    tier1 = load_tier1()
    print(f"  {len(tier1):,} entries")

    print("Tier 2: Extracting WordNet antonyms …")
    tier2 = extract_wordnet_antonyms()
    print(f"  {len(tier2):,} entries")

    print("Tier 3: Moby Thesaurus transitive closure …")
    thesaurus = load_moby_thesaurus()
    tier3 = build_tier3(tier1, tier2, thesaurus)
    print(f"  {len(tier3):,} entries")

    tier4 = {}
    if args.glove:
        print("Tier 4: GloVe embedding inversion …")
        known = {**tier1, **tier2, **tier3}
        all_words = set(thesaurus.keys()) | set(tier2.keys())
        remaining = all_words - set(known.keys())
        tier4 = build_tier4(remaining, known, args.glove_path, thesaurus)
        print(f"  {len(tier4):,} entries")
    else:
        print("Tier 4: Skipped (pass --glove to enable)")

    print("\nAssembling final map …")
    final_map, audit = assemble_map(tier1, tier2, tier3, tier4)

    ANTONYM_MAP_PATH.write_text(json.dumps(final_map, indent=2, ensure_ascii=False, sort_keys=True))
    write_audit_log(audit)
    print_stats(audit, final_map)

    print(f"\nWrote: {ANTONYM_MAP_PATH}")
    print(f"Wrote: {AUDIT_LOG_PATH}")


if __name__ == "__main__":
    main()
