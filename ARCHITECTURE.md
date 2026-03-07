# ANTIBOOK — Architecture Design Document

**Version:** 0.3 (Design Decisions Resolved)
**Author:** Sean Reid
**Date:** 2026-03-06

---

## 1. Vision

ANTIBOOK is a static website that lets users search the Project Gutenberg catalog, select a book, and read its *semantic inverse* — a word-for-word replacement where every word is swapped with its polar antonym. The result preserves the original's structure (word count, punctuation, paragraph breaks) but inverts its meaning entirely, producing an uncanny, often absurd mirror-text.

> *"It was the best of times, it was the worst of times…"*
> becomes
> *"It wasn't the worst of places, it wasn't the best of places…"*

The site must feel snappy: near-instant search, fast page loads, and no perceptible delay when rendering an AntiBook.

---

## 2. Core Constraints

- **Static site.** No application server at runtime. All compute happens at build time or on the client.
- **No code in this document.** This is a pure architecture and design artifact.
- **Snappy response.** Target sub-200ms for search results, sub-1s for initial AntiBook page render.
- **$0 budget.** Every tool in the pipeline — compute, hosting, NLP data, CI/CD — must be free (open-source, free-tier, or public domain). No paid LLM APIs, no paid hosting, no paid NLP services.
- **Faithful inversion.** Every word in the original maps to exactly one antonym. The AntiBook has the same word count as the source.

---

## 3. Key Definitions

| Term | Meaning |
|---|---|
| **Source Text** | The original Project Gutenberg plain-text file, stripped of headers/footers. |
| **Antonym Map** | A static lookup table mapping each English word to its designated polar antonym. |
| **AntiBook** | The transformed text: same structure, every word replaced via the Antonym Map. |
| **Catalog Index** | A lightweight, pre-built search index of all available Gutenberg titles and metadata. |

---

## 4. High-Level Architecture

The system is split into two phases: an offline **Build Pipeline** that does all the heavy lifting, and a **Static Frontend** that serves pre-computed results.

```
┌──────────────────────────────────────────────────────┐
│                   BUILD PIPELINE (offline)            │
│                                                      │
│  ┌────────────┐   ┌────────────┐   ┌──────────────┐ │
│  │  Gutenberg  │──▶│  Text      │──▶│  Antonym     │ │
│  │  Ingestion  │   │  Processor │   │  Transform   │ │
│  └────────────┘   └────────────┘   └──────┬───────┘ │
│                                           │         │
│  ┌────────────┐                    ┌──────▼───────┐ │
│  │  Catalog    │                    │  Chunk &     │ │
│  │  Indexer    │                    │  Compress    │ │
│  └─────┬──────┘                    └──────┬───────┘ │
│        │                                  │         │
└────────┼──────────────────────────────────┼─────────┘
         │                                  │
         ▼                                  ▼
┌──────────────────────────────────────────────────────┐
│               STATIC HOSTING (CDN)                   │
│                                                      │
│   /index.json          /books/{id}/chunk-{n}.json    │
│   /search-index.bin    /books/{id}/meta.json         │
│                                                      │
└──────────────────────────────────────────────────────┘
         │                                  │
         ▼                                  ▼
┌──────────────────────────────────────────────────────┐
│               STATIC FRONTEND (browser)              │
│                                                      │
│   Search UI  ──▶  Book Viewer  ──▶  Reader Pane     │
│   (client-side     (lazy chunk      (virtual scroll, │
│    fuzzy search)    loading)         dual-pane mode)  │
│                                                      │
└──────────────────────────────────────────────────────┘
```

---

## 5. Build Pipeline

The build pipeline runs offline (CI/CD or local machine) and produces all static assets. It is the only phase that requires compute resources beyond a browser.

### 5.1 Gutenberg Ingestion

- Pull plain-text files from Project Gutenberg mirrors via their bulk download or rsync endpoint.
- Strip boilerplate (license headers, footers) using Gutenberg's well-known delimiters.
- Extract metadata: title, author, language, subject, publication year.
- Filter to English-language works only (initial scope).
- Estimated corpus: ~35,000 English titles.

### 5.2 Antonym Map Construction

This is the heart of the project and its hardest design problem. Every data source used here must be free and open.

**Approach: Tiered Antonym Resolution**

Rather than calling an LLM per word at read-time, the antonym map is built ahead of time as a static dictionary from layered free data sources.

| Tier | Source | Cost | Coverage | Notes |
|---|---|---|---|---|
| 1 — Curated Core | Hand-built list of ~2,000 high-frequency antonym pairs (good/bad, up/down, love/hate, etc.) | Free (manual labor) | ~60–70% of running text by token frequency | Highest quality. Covers function words, common adjectives, verbs, adverbs. Checked into the repo as a version-controlled JSON file. |
| 2 — WordNet Antonyms | Princeton WordNet (open-source, BSD license), accessed via NLTK. Extract antonym relations across all synsets. | Free | +15–20% | Disambiguated by most-common sense (WordNet frequency tags). Falls back to Tier 3 if no antonym exists. |
| 3 — Moby Thesaurus + Heuristic Inversion | Moby Thesaurus (public domain, ~30,000 root words with synonym clusters). For each word missing from Tiers 1–2, find its Moby synonym cluster, then cross-reference those synonyms against WordNet to find a word whose *synonyms* are antonyms of the original. | Free | +10–15% | A poor man's antonym inference: if "happy" and "sad" don't appear as a direct WordNet antonym pair, but "happy" is synonymous with "joyful" and "joyful" has WordNet antonym "sorrowful," then "sorrowful" can serve as the antonym for "happy." This is a transitive closure over the synonym-antonym graph. |
| 4 — Word Embedding Inversion (optional) | Pre-trained GloVe vectors (Stanford, free, public domain dedication). For remaining gaps, find the word whose embedding is most *dissimilar* (cosine distance) within the same part-of-speech. | Free | +5% | Lowest confidence. Only used as a last resort before identity fallback. Results are human-reviewable in a generated audit log. |
| 5 — Identity Fallback | Words with no meaningful antonym (proper nouns, articles with no inverse, technical jargon) map to themselves. | Free | Remainder | Preserves readability. Proper nouns are never replaced. |

**Free NLP toolchain:**
All linguistic processing uses NLTK (MIT license) running in the CI environment. Specifically: WordNet corpus for antonyms and synset frequencies, NLTK's averaged perceptron tagger for POS tagging, and WordNet's Morphy lemmatizer. No paid API calls. No cloud NLP services.

**Map properties:**
- The map is a flat JSON dictionary: `{ "word": "antonym", ... }`.
- Case is normalized at lookup; original casing is restored via a casing transfer function.
- Morphological variants (plurals, verb conjugations) are handled by lemmatizing at lookup, applying the antonym, then re-inflecting to match the original form.
- Estimated vocabulary across all Gutenberg English texts: ~300,000 unique word forms, collapsing to ~80,000 lemmas.
- The final map file (compressed): estimated ~1–3 MB.

**Disambiguation strategy:**
Words with multiple senses (e.g., "light" → weight vs. illumination) are resolved using the *most common sense in general English* (WordNet frequency data). Context-sensitive disambiguation is a non-goal for v1 — it would require per-sentence model inference at build time for every book, which exceeds the free compute budget. The occasional "wrong" antonym is an accepted tradeoff and arguably adds to the surreal charm of the output.

### 5.3 Text Transformation

For each book in the corpus:

1. Tokenize into words while preserving all whitespace, punctuation, and paragraph structure as non-word tokens.
2. For each word token, look up its antonym in the map (lemmatize → lookup → re-inflect → restore casing).
3. Reassemble the full text from the transformed words and the preserved structural tokens.
4. Verify: assert `word_count(source) == word_count(antibook)`.

### 5.4 Chunking and Compression

Whole books are too large to serve as single files (some Gutenberg texts exceed 1 MB). Each AntiBook is split into sequential chunks.

- **Chunk size target:** ~20 KB uncompressed (~5 KB gzipped). This balances initial render speed against request count.
- **Format per chunk:** JSON with fields for chunk index, word offset, and the text content.
- **Metadata file per book** (`meta.json`): title, author, original Gutenberg ID, total word count, total chunk count, and a preview snippet (first 200 words of the AntiBook).

### 5.5 Catalog Index

A client-side search index built at build time using Lunr.js (MIT license, free) or Fuse.js (Apache 2.0, free). The index is serialized to a static file during the CI build.

- Indexed fields: title, author, subject tags.
- Serialized index size target: < 500 KB for the full Gutenberg English catalog.
- Served as a single static binary or JSON file.

---

## 6. Static Frontend

### 6.1 Technology Choice

A minimal SPA framework (e.g., Preact, Astro, or vanilla JS) bundled into static HTML/JS/CSS via esbuild (MIT license, zero-config, free). No server-side rendering needed since all content is pre-built.

### 6.2 Search Experience

- On page load, the pre-built search index is fetched and cached.
- User types into a search bar; results appear as-you-type via client-side fuzzy matching against the index.
- Results show: title, author, word count, and the AntiBook preview snippet.
- Target: < 200 ms from keystroke to visible results after initial index load.

### 6.3 Reader Experience

- On book selection, the client fetches `meta.json` and `chunk-0.json` in parallel.
- The reader uses **virtual scrolling** — only chunks in or near the viewport are held in the DOM.
- Subsequent chunks are prefetched 2–3 ahead of the current reading position.
- Optional **dual-pane mode** displays the original Gutenberg text alongside the AntiBook for comparison.

### 6.4 Performance Budget

| Metric | Target |
|---|---|
| First Contentful Paint (home) | < 500 ms |
| Search index load (cold) | < 800 ms |
| Search result render | < 200 ms |
| AntiBook first chunk render | < 500 ms |
| Chunk prefetch (subsequent) | Background, invisible to user |
| Total JS bundle | < 80 KB gzipped |

---

## 7. Hosting and Delivery ($0)

All hosting is free-tier. No paid services.

**Primary option: GitHub Pages + Cloudflare (free tier)**

- Repository lives on GitHub (free for public repos, unlimited storage for repos under the soft ~5 GB limit).
- GitHub Pages serves the static site directly from a branch or the output of a GitHub Actions build. Free for public repos, 1 GB soft limit on published site size, 100 GB/month bandwidth.
- Cloudflare free-tier CDN sits in front of GitHub Pages for edge caching, Brotli compression, and DDoS protection. No cost.
- Custom domain via Cloudflare DNS (free tier) if desired.

**Fallback option: Cloudflare Pages (standalone)**

- Cloudflare Pages offers free static hosting with 500 deploys/month, unlimited bandwidth, and automatic Brotli. No GitHub Pages dependency.
- Build hooks can be triggered from GitHub Actions.

**Asset strategy:**

- JS/CSS bundles use content-hashed filenames with long Cache-Control TTLs (immutable).
- Book chunks are immutable once generated — cache forever.
- The search index and catalog metadata are the only assets that change on rebuild; these use shorter TTLs or cache-busting hashes.

**Storage budget for the MVP (~500 titles):**

| Asset | Estimated Size |
|---|---|
| JS + CSS bundle | ~80 KB gzipped |
| Search index | ~200 KB |
| Catalog metadata (all titles) | ~500 KB |
| Book chunks (~500 books × avg 50 chunks × 5 KB gzipped) | ~125 MB |
| **Total** | **~126 MB** |

This fits comfortably within GitHub Pages' 1 GB limit and Cloudflare's free tier.

---

## 8. CI/CD Pipeline — Detailed Design ($0)

The entire build and deploy lifecycle runs on **GitHub Actions free tier** (2,000 minutes/month for public repos — effectively unlimited for public repos which get unlimited minutes). No paid CI, no paid compute, no paid storage.

### 8.1 Repository Structure

```
antibook/
├── .github/
│   └── workflows/
│       ├── build-map.yml          # Antonym map rebuild (manual trigger)
│       ├── build-books.yml        # Transform new titles (scheduled + manual)
│       └── deploy.yml             # Build frontend + deploy to Pages
├── data/
│   ├── antonym-map.json           # The master antonym dictionary (checked in)
│   ├── curated-core.json          # Tier 1 hand-curated pairs
│   ├── gutenberg-catalog.json     # Cached Gutenberg metadata
│   └── moby-thesaurus/            # Public domain thesaurus data
├── pipeline/                      # Python scripts for the build pipeline
│   ├── ingest.py                  # Gutenberg download + strip boilerplate
│   ├── build_map.py               # Tiered antonym map construction
│   ├── transform.py               # Apply map to source texts → AntiBooks
│   ├── chunk.py                   # Split AntiBooks into serve-ready chunks
│   └── index.py                   # Build the client-side search index
├── site/                          # Static frontend source
│   ├── index.html
│   ├── app.js
│   └── style.css
└── dist/                          # Build output (gitignored, or on gh-pages branch)
    ├── search-index.bin
    └── books/
        └── {id}/
            ├── meta.json
            └── chunk-{n}.json
```

### 8.2 Workflow: Antonym Map Rebuild

**Trigger:** Manual dispatch (run infrequently — only when improving the map).

```
┌──────────────────────────────────────────────────────────────┐
│  build-map.yml                                               │
│                                                              │
│  1. Checkout repo                                            │
│  2. Install Python + NLTK + NumPy (cached via actions/cache) │
│  3. Download NLTK WordNet + Moby Thesaurus (cached)          │
│  4. Run build_map.py:                                        │
│     a. Load Tier 1 (curated-core.json)                       │
│     b. Extract all WordNet antonym pairs → merge (Tier 2)    │
│     c. For uncovered lemmas, run Moby synonym→antonym        │
│        transitive closure (Tier 3)                           │
│     d. Optionally load GloVe vectors for embedding           │
│        inversion on remaining gaps (Tier 4)                  │
│     e. Write antonym-map.json + audit-log.csv                │
│  5. Commit updated antonym-map.json back to main branch      │
│  6. Log stats: total entries, coverage by tier, new entries   │
│                                                              │
│  Estimated runtime: ~10 minutes (one-time vocabulary scan)   │
│  GloVe download: ~800 MB uncompressed for 6B vectors;        │
│    cached across runs. Falls within Actions' 14 GB RAM.      │
└──────────────────────────────────────────────────────────────┘
```

**Key detail — GloVe on a free runner:** The 6B-token GloVe file (glove.6B.300d) is ~1 GB uncompressed. It fits in the 14 GB RAM of a GitHub-hosted runner. The pipeline loads only the vectors for words actually missing from Tiers 1–3, so peak memory is manageable. The file is cached between runs via `actions/cache` keyed on its checksum.

### 8.3 Workflow: Transform New Books

**Trigger:** Scheduled weekly (`cron: '0 4 * * 0'`) + manual dispatch.

```
┌──────────────────────────────────────────────────────────────┐
│  build-books.yml                                             │
│                                                              │
│  1. Checkout repo (with full dist/ from gh-pages branch      │
│     or artifact cache)                                       │
│  2. Install Python + NLTK                                    │
│  3. Run ingest.py:                                           │
│     a. Fetch Gutenberg catalog RSS / sitemap                 │
│     b. Diff against gutenberg-catalog.json → new title IDs   │
│     c. Download plain-text for new titles only               │
│     d. Strip Gutenberg boilerplate                           │
│     e. Update gutenberg-catalog.json                         │
│  4. Run transform.py on new titles only:                     │
│     a. Load antonym-map.json into memory                     │
│     b. For each new book: tokenize → lemmatize → lookup      │
│        → re-inflect → reassemble                             │
│     c. Assert word count invariant                           │
│  5. Run chunk.py → emit /books/{id}/meta.json + chunks       │
│  6. Run index.py → rebuild search-index.bin (incremental     │
│     append, or full rebuild — fast either way for ~500        │
│     titles)                                                  │
│  7. Upload dist/ as GitHub Actions artifact                  │
│  8. Trigger deploy.yml                                       │
│                                                              │
│  Estimated runtime per new book: ~2–5 seconds                │
│  Full corpus rebuild (500 titles): ~30 minutes               │
│  Full corpus rebuild (35,000 titles): ~12–24 hours           │
│    (split across multiple scheduled runs if needed,          │
│     or run locally for initial seed)                         │
└──────────────────────────────────────────────────────────────┘
```

**Incremental strategy:** The pipeline maintains a manifest file (`dist/manifest.json`) listing every processed book ID and the antonym-map version used. On each run, only books not in the manifest (or built with an outdated map version) are processed. This keeps weekly runs under a few minutes.

**Handling the initial seed:** The first build of 500 books takes ~30 minutes, well within a single Actions run. For the full 35,000-title corpus, the initial build would exceed the 6-hour job limit. The solution is to run the initial seed locally (any machine with Python + NLTK), commit the output to the `gh-pages` branch, and let CI handle incremental updates from that point forward.

### 8.4 Workflow: Deploy

**Trigger:** On completion of `build-books.yml`, or on push to `main` (for frontend changes).

```
┌──────────────────────────────────────────────────────────────┐
│  deploy.yml                                                  │
│                                                              │
│  1. Checkout main (frontend source)                          │
│  2. Download dist/ artifact from build-books workflow         │
│  3. Build frontend: bundle JS/CSS (esbuild, zero-config,     │
│     no npm install needed — single binary via npx)           │
│  4. Merge frontend bundle + dist/ book assets into            │
│     publish directory                                        │
│  5. Deploy to GitHub Pages via actions/deploy-pages           │
│     (or push to gh-pages branch)                             │
│                                                              │
│  Estimated runtime: < 1 minute                               │
└──────────────────────────────────────────────────────────────┘
```

### 8.5 Caching Strategy

GitHub Actions provides 10 GB of cache storage per repo. The pipeline uses this aggressively:

| Cache Key | Contents | Size | TTL |
|---|---|---|---|
| `nltk-data-{hash}` | WordNet corpus, averaged perceptron tagger | ~50 MB | Until NLTK version changes |
| `glove-6b-300d` | GloVe vectors (only if Tier 4 is active) | ~1 GB | Indefinite |
| `moby-thesaurus-{hash}` | Moby Thesaurus data | ~25 MB | Indefinite |
| `gutenberg-raw-{week}` | Raw downloaded Gutenberg texts for current batch | Variable | 1 week |
| `pip-{hash}` | Python package cache | ~100 MB | Until requirements change |

### 8.6 Full $0 Cost Accounting

| Resource | Provider | Free Tier Limit | ANTIBOOK Usage |
|---|---|---|---|
| CI/CD compute | GitHub Actions | Unlimited for public repos | ~30 min/week steady state |
| Artifact storage | GitHub Actions | 500 MB (artifacts), 10 GB (cache) | ~200 MB artifacts, ~1.2 GB cache |
| Static hosting | GitHub Pages | 1 GB site, 100 GB bandwidth/mo | ~126 MB site (MVP) |
| CDN | Cloudflare (free) | Unlimited bandwidth | Edge cache for all assets |
| DNS | Cloudflare (free) | Unlimited | 1 domain |
| Source control | GitHub (public) | Unlimited | 1 repo |
| NLP data (WordNet) | Princeton / NLTK | Open source (BSD) | Downloaded in CI |
| NLP data (Moby) | Public domain | Free | Checked into repo |
| NLP data (GloVe) | Stanford | Free for research/non-commercial | Downloaded + cached in CI |
| Source texts | Project Gutenberg | Public domain | Bulk download |
| **Total** | | | **$0.00** |

---

## 9. Antonym Quality and Edge Cases

### 9.1 What "Polar Antonym" Means

The design targets **gradable antonyms** (hot/cold, big/small) and **complementary antonyms** (alive/dead, true/false) as first-class replacements. **Relational antonyms** (buy/sell, teacher/student) are included where they exist.

### 9.2 Uninvertible Words

Not every word has an antonym. The strategy for each category:

| Category | Strategy |
|---|---|
| Proper nouns (names, places) | Kept as-is. Detected via NER at build time or capitalization heuristic. |
| Articles and determiners (the, a, an) | Kept as-is. No meaningful antonym. |
| Function words (the, is, was, it, a, an) | Kept as-is. No meaningful antonym; preserves grammatical coherence (see §13, Decision 1). |
| Contractions (don't, can't, won't, isn't) | Replaced with affirmative root: "don't" → "do", "can't" → "can", etc. Explicit Tier 1 table (see §13, Decision 2). |
| Prepositions with spatial antonyms (in/out, up/down, over/under) | Replaced. |
| Prepositions without antonyms (of, for, by) | Kept as-is. |
| Conjunctions (and, but, or) | Kept as-is. |
| Numbers | Kept as-is. |
| Onomatopoeia | Kept as-is. |
| Archaic/rare words | Moby Thesaurus transitive closure (Tier 3), then GloVe embedding inversion (Tier 4); fallback to identity. |

### 9.3 Morphological Fidelity

The lemmatize → antonym → re-inflect pipeline must handle:

- Verb tenses: "running" → lemma "run" → antonym "stop" → re-inflect "stopping"
- Plurals: "enemies" → lemma "enemy" → antonym "friend" → re-inflect "friends"
- Comparatives/superlatives: "darker" → "lighter", "darkest" → "lightest"
- Possessives: "'s" suffix preserved through transformation.

---

## 10. Dual-Pane Mode (Original vs. AntiBook)

The reader optionally displays the source text alongside the AntiBook. This requires:

- Storing (or hot-fetching from Gutenberg) the original text, chunked to the same boundaries.
- Synchronized scrolling between panes.
- Word-level highlighting on hover: mousing over an AntiBook word highlights the corresponding original word and vice versa.

This feature is deferred to v1.1 but the chunk boundary design accommodates it from the start by including word offsets in chunk metadata.

---

## 11. Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Antonym quality is poor for polysemous words | Nonsensical output reduces charm, becomes annoying | Accept for v1; WordNet + Moby transitive closure covers most cases. Plan context-aware disambiguation in v2 if a free local model becomes practical. |
| Corpus size leads to long build times | Initial seed exceeds GitHub Actions 6-hour job limit | Run initial seed locally; CI handles incremental updates only. Parallelize with matrix jobs if needed. |
| Gutenberg mirror availability | Build pipeline stalls | Cache raw texts locally and in Actions cache; use multiple mirrors. |
| GitHub free-tier limits change | Build or hosting breaks | Cloudflare Pages as fallback host; pipeline is portable to any CI with Python. |
| GloVe license restricts commercial use | Legal risk if project is monetized later | GloVe is free for research/non-commercial. If commercializing, swap to fastText (Apache 2.0 licensed, similar quality). |
| Antonym map grows unmanageably large | Longer CI builds | Map stays on build side only; client never touches it. Map is < 3 MB even for the full vocabulary. |
| Copyright concerns | Gutenberg texts are public domain, but derivative works (AntiBooks) need review | Public domain source + mechanical transformation = low risk; add attribution per Gutenberg license. |

---

## 12. Future Directions (Out of Scope for v1)

- **Context-aware antonyms (v2):** Run a small open-weight model (e.g., Phi, Gemma, Qwen — free weights, runs on consumer hardware) locally to disambiguate polysemous words before antonym selection. Still $0 if run on your own machine; could also leverage free-tier GPU notebooks (Kaggle, Colab) for batch processing.
- **User-suggested corrections:** Allow readers to flag and suggest better antonyms; feed corrections back into the curated core (Tier 1). This is the highest-leverage quality improvement and costs nothing.
- **Other languages:** Extend to French, Spanish, German Gutenberg collections with language-specific antonym maps.
- **AntiBook "difficulty" slider:** Let users choose between strict antonyms (polar opposites only) and loose semantic inversion (broader reinterpretation).
- **Audio AntiBooks:** TTS rendering of the inverted text using browser-native SpeechSynthesis API (free, client-side) or pre-rendered with Piper TTS (open-source, free).
- **API access:** Expose the antonym transform as a public API for arbitrary input text. (Note: this would require a server, breaking the static/$0 model. Could be implemented as a client-side WASM module instead.)

---

## 13. Resolved Design Decisions

1. **Function words (the, is, was, it) are preserved.** These words lack meaningful polar antonyms, and attempting to invert them (e.g., "is" → "isn't") would introduce contractions that violate the word-count invariant. Function words pass through the transform unchanged. This also keeps the output grammatically coherent — inverting content words alone produces the right level of surreal without becoming unreadable.

2. **Contractions invert by dropping the negation.** A contraction like "don't" is treated as a single token whose antonym is its affirmative root: "don't" → "do", "can't" → "can", "won't" → "will", "isn't" → "is". This preserves the one-word-in, one-word-out invariant. The antonym map includes an explicit contraction table (~50 entries) in the curated core (Tier 1) so these never fall through to heuristic tiers.

3. **Negation stacking is acceptable.** If the original text says "not bad," the pipeline produces "not good" — which pragmatically preserves the original's meaning through double inversion. This is a known artifact of word-level (vs. phrase-level) transformation and is accepted for v1. Detecting and resolving negation scope would require syntactic parsing that adds complexity for marginal quality gain, and the occasional semantic preservation is part of the project's charm.

4. **MVP launches with the top 500 titles.** The initial corpus is the 500 most-downloaded English-language works on Project Gutenberg (Gutenberg publishes download rankings). This covers the canonical works most users will search for (Shakespeare, Austen, Dickens, Twain, etc.), keeps the initial build under 30 minutes on a GitHub Actions runner, and fits comfortably in the ~126 MB GitHub Pages budget. The full 35,000-title corpus is a post-launch scaling target.
