"""
pipeline/transform.py

Apply the antonym map to source texts and produce AntiBooks.

For each book in the catalog:
  1. Tokenize preserving whitespace and punctuation
  2. POS-tag the words
  3. For each word: lemmatize → lookup antonym → re-inflect → restore casing
  4. Reassemble from tokens
  5. Assert word count invariant

Usage:
    python pipeline/transform.py [--ids 1 2 3 ...] [--force]
"""

import argparse
import json
import re
import sys
from pathlib import Path

import inflect
import nltk
from nltk.corpus import wordnet as wn
from nltk.stem import WordNetLemmatizer
from tqdm import tqdm

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
ANTIBOOK_DIR = DATA_DIR / "antibooks"
CATALOG_PATH = DATA_DIR / "gutenberg-catalog.json"
ANTONYM_MAP_PATH = DATA_DIR / "antonym-map.json"

ANTIBOOK_DIR.mkdir(parents=True, exist_ok=True)

# Part-of-speech constants
POS_MAP = {"J": wn.ADJ, "V": wn.VERB, "N": wn.NOUN, "R": wn.ADV}

# Inflect engine for noun pluralization
_inflect = inflect.engine()

# NLTK setup
_lemmatizer = WordNetLemmatizer()


def ensure_nltk():
    checks = {
        "averaged_perceptron_tagger": "taggers/averaged_perceptron_tagger",
        "averaged_perceptron_tagger_eng": "taggers/averaged_perceptron_tagger_eng",
        "wordnet": "corpora/wordnet",
        "punkt": "tokenizers/punkt",
        "punkt_tab": "tokenizers/punkt_tab",
    }
    for corpus, path in checks.items():
        try:
            nltk.data.find(path)
        except LookupError:
            nltk.download(corpus, quiet=True)


# ---------------------------------------------------------------------------
# Tokenization — preserves all whitespace and punctuation as non-word tokens
# ---------------------------------------------------------------------------

# Matches: contractions (don't), hyphenated words, plain words
WORD_RE = re.compile(r"[a-zA-Z]+(?:'[a-zA-Z]+)?(?:-[a-zA-Z]+)*")
TOKEN_RE = re.compile(r"([a-zA-Z]+(?:'[a-zA-Z]+)?(?:-[a-zA-Z]+)*|[^a-zA-Z]+)")


def tokenize(text: str) -> list[tuple[str, bool]]:
    """
    Returns a list of (token, is_word) pairs.
    Non-word tokens (whitespace, punctuation, numbers) are passed through unchanged.
    """
    return [
        (m.group(), bool(WORD_RE.fullmatch(m.group())))
        for m in TOKEN_RE.finditer(text)
    ]


# ---------------------------------------------------------------------------
# Casing utilities
# ---------------------------------------------------------------------------

def get_casing(word: str) -> str:
    if word.isupper():
        return "upper"
    if word[0].isupper() and word[1:].islower():
        return "title"
    if word[0].isupper():
        return "upper_first"
    return "lower"


def apply_casing(word: str, casing: str) -> str:
    if casing == "upper":
        return word.upper()
    if casing == "title":
        return word.capitalize()
    if casing == "upper_first":
        return word[0].upper() + word[1:] if word else word
    return word.lower()


# ---------------------------------------------------------------------------
# Morphological helpers
# ---------------------------------------------------------------------------

def penn_to_wn(tag: str) -> str:
    return POS_MAP.get(tag[0], wn.NOUN)


def lemmatize(word: str, pos: str) -> str:
    return _lemmatizer.lemmatize(word.lower(), pos)


def get_suffix_type(word: str, tag: str) -> str:
    """Return a morphological descriptor for re-inflection."""
    w = word.lower()
    if tag == "VBG":
        return "VBG"
    if tag in ("VBD", "VBN"):
        return "VBD"
    if tag == "VBZ":
        return "VBZ"
    if tag in ("NNS", "NNPS"):
        return "NNS"
    if tag == "JJR" or tag == "RBR":
        return "CMP"
    if tag == "JJS" or tag == "RBS":
        return "SUP"
    return "BASE"


def double_final_consonant(word: str) -> bool:
    """Return True if the final consonant should be doubled before -ing/-ed."""
    vowels = "aeiou"
    if len(word) < 2:
        return False
    if word[-1] in vowels or word[-1] in "wxy":
        return False
    if word[-2] not in vowels:
        return False
    # Avoid doubling for two-vowel stems (e.g. "rain")
    if len(word) >= 3 and word[-3] in vowels:
        return False
    return True


def apply_ing(lemma: str) -> str:
    """Apply present-participle morphology to an antonym lemma."""
    w = lemma.lower()
    if w.endswith("ie"):
        return w[:-2] + "ying"
    if w.endswith("e") and not w.endswith("ee"):
        return w[:-1] + "ing"
    if double_final_consonant(w):
        return w + w[-1] + "ing"
    return w + "ing"


def apply_ed(lemma: str) -> str:
    """Apply past-tense morphology to an antonym lemma."""
    w = lemma.lower()
    if w.endswith("e"):
        return w + "d"
    if w.endswith("y") and len(w) > 1 and w[-2] not in "aeiou":
        return w[:-1] + "ied"
    if double_final_consonant(w):
        return w + w[-1] + "ed"
    return w + "ed"


def apply_vbz(lemma: str) -> str:
    """Apply 3rd-person singular present morphology."""
    w = lemma.lower()
    if w.endswith(("s", "sh", "ch", "x", "z", "o")):
        return w + "es"
    if w.endswith("y") and len(w) > 1 and w[-2] not in "aeiou":
        return w[:-1] + "ies"
    return w + "s"


def apply_comparative(lemma: str) -> str:
    w = lemma.lower()
    if w.endswith("e"):
        return w + "r"
    if w.endswith("y") and len(w) > 1 and w[-2] not in "aeiou":
        return w[:-1] + "ier"
    if double_final_consonant(w):
        return w + w[-1] + "er"
    return w + "er"


def apply_superlative(lemma: str) -> str:
    w = lemma.lower()
    if w.endswith("e"):
        return w + "st"
    if w.endswith("y") and len(w) > 1 and w[-2] not in "aeiou":
        return w[:-1] + "iest"
    if double_final_consonant(w):
        return w + w[-1] + "est"
    return w + "est"


def apply_plural(lemma: str) -> str:
    plural = _inflect.plural(lemma)
    return plural if plural else lemma + "s"


def reinflect(antonym_lemma: str, suffix_type: str) -> str:
    """Re-inflect the antonym lemma to match the original word's morphology."""
    if suffix_type == "VBG":
        return apply_ing(antonym_lemma)
    if suffix_type == "VBD":
        return apply_ed(antonym_lemma)
    if suffix_type == "VBZ":
        return apply_vbz(antonym_lemma)
    if suffix_type == "NNS":
        return apply_plural(antonym_lemma)
    if suffix_type == "CMP":
        return apply_comparative(antonym_lemma)
    if suffix_type == "SUP":
        return apply_superlative(antonym_lemma)
    return antonym_lemma


# ---------------------------------------------------------------------------
# Proper-noun detection
# ---------------------------------------------------------------------------

ALWAYS_PRESERVE = frozenset([
    "i", "a", "an", "the",
    "and", "but", "or", "nor", "for", "yet", "so",
    "of", "in", "on", "at", "to", "by", "as", "if",
    "it", "its", "this", "that", "these", "those",
    "he", "she", "they", "we", "you", "me", "him", "her", "us", "them",
    "his", "hers", "their", "our", "your", "my",
    "who", "what", "which", "when", "where", "how", "why",
    "be", "is", "am", "are", "was", "were", "been", "being",
    "have", "has", "had", "do", "does", "did",
    "will", "would", "shall", "should", "may", "might", "must", "can", "could",
    "not", "no",
])


def is_proper_noun(word: str, tag: str, position: int) -> bool:
    """Detect proper nouns: NNP/NNPS tag, or initial-cap not at sentence start."""
    if tag in ("NNP", "NNPS"):
        return True
    # Capitalised words not at position 0 are likely proper nouns
    if position > 0 and word[0].isupper() and word.lower() not in ALWAYS_PRESERVE:
        return True
    return False


# ---------------------------------------------------------------------------
# Core transformation
# ---------------------------------------------------------------------------

def transform_text(text: str, antonym_map: dict[str, str]) -> str:
    tokens = tokenize(text)
    words_only = [(i, tok) for i, (tok, is_w) in enumerate(tokens) if is_w]

    if not words_only:
        return text

    word_strings = [tok for _, tok in words_only]
    tags = nltk.pos_tag(word_strings)

    for list_idx, (tok_idx, original_word) in enumerate(words_only):
        tag = tags[list_idx][1]
        casing = get_casing(original_word)

        # Preserve proper nouns
        if is_proper_noun(original_word, tag, list_idx):
            continue

        word_lower = original_word.lower()

        # Preserve always-pass-through function words
        if word_lower in ALWAYS_PRESERVE:
            continue

        # Look up contraction first (exact match)
        if word_lower in antonym_map:
            antonym = antonym_map[word_lower]
            tokens[tok_idx] = (apply_casing(antonym, casing), True)
            continue

        # Lemmatize, look up, re-inflect
        wn_pos = penn_to_wn(tag)
        lemma = lemmatize(word_lower, wn_pos)
        suffix_type = get_suffix_type(original_word, tag)

        antonym_lemma = antonym_map.get(lemma)
        if antonym_lemma is None:
            # Try the original word form too
            antonym_lemma = antonym_map.get(word_lower)

        if antonym_lemma is None:
            # No mapping — keep original
            continue

        antonym_inflected = reinflect(antonym_lemma, suffix_type)
        tokens[tok_idx] = (apply_casing(antonym_inflected, casing), True)

    return "".join(tok for tok, _ in tokens)


def count_words(text: str) -> int:
    return len(WORD_RE.findall(text))


# ---------------------------------------------------------------------------
# Book-level transform
# ---------------------------------------------------------------------------

def transform_book(book_id: int, meta: dict, antonym_map: dict, force: bool = False) -> bool:
    out_path = ANTIBOOK_DIR / f"{book_id}.txt"
    if not force and out_path.exists():
        return False

    stripped_path = ROOT / meta.get("stripped_path", "")
    if not stripped_path.exists():
        print(f"  [{book_id}] Stripped text not found — skipping.")
        return False

    source_text = stripped_path.read_text(encoding="utf-8", errors="replace")
    original_word_count = count_words(source_text)

    antibook_text = transform_text(source_text, antonym_map)
    antibook_word_count = count_words(antibook_text)

    if original_word_count != antibook_word_count:
        print(
            f"  [{book_id}] Word count mismatch: "
            f"original={original_word_count}, antibook={antibook_word_count}"
        )
        # Not fatal — log and continue

    out_path.write_text(antibook_text, encoding="utf-8")
    return True


def main():
    ensure_nltk()

    parser = argparse.ArgumentParser(description="Transform books into AntiBooks.")
    parser.add_argument("--ids", type=int, nargs="*", help="Specific book IDs to transform")
    parser.add_argument("--force", action="store_true", help="Re-transform already-processed books")
    args = parser.parse_args()

    catalog = json.loads(CATALOG_PATH.read_text())
    antonym_map = json.loads(ANTONYM_MAP_PATH.read_text())
    books = catalog.get("books", {})

    if args.ids:
        book_items = [(str(i), books[str(i)]) for i in args.ids if str(i) in books]
    else:
        book_items = list(books.items())

    print(f"Antonym map loaded: {len(antonym_map):,} entries")
    print(f"Books to transform: {len(book_items)}")

    new_count = skipped = failed = 0
    for book_id_str, meta in tqdm(book_items, desc="Transforming"):
        try:
            result = transform_book(int(book_id_str), meta, antonym_map, force=args.force)
            if result:
                new_count += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"  [{book_id_str}] Error: {e}")
            failed += 1

    print(f"\nDone. New: {new_count}  Skipped: {skipped}  Failed: {failed}")


if __name__ == "__main__":
    main()
