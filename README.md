# ANTIBOOK

A static website that lets users search the Project Gutenberg catalog, select a book, and read its *semantic inverse* — a word-for-word replacement where every word is swapped with its polar antonym.

> *"It was the best of times, it was the worst of times…"*
> becomes
> *"It wasn't the worst of places, it wasn't the best of places…"*

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design document.

---

## Quick Start

### Prerequisites

- Python 3.11+
- Node.js 20+ (for the esbuild frontend bundle)

### Install dependencies

```bash
pip install -r requirements.txt
```

### Build the antonym map (one-time)

```bash
python pipeline/build_map.py
# Add --glove for Tier 4 GloVe coverage (requires ~1 GB download)
```

### Ingest books

```bash
# Ingest the top 500 Gutenberg titles
python pipeline/ingest.py --limit 500

# Or specific titles by Gutenberg ID
python pipeline/ingest.py --ids 1342 11 84 1661 98
```

### Transform, chunk, index

```bash
python pipeline/transform.py
python pipeline/chunk.py
python pipeline/index.py
```

### Run the site locally

```bash
# Serve dist/ with any static server
npx serve dist/
# Then open http://localhost:3000
```

Or with Python:

```bash
cd dist && python -m http.server 8000
```

---

## Repository Structure

```
antibook/
├── .github/workflows/
│   ├── build-map.yml       # Antonym map rebuild (manual trigger)
│   ├── build-books.yml     # Transform new titles (weekly + manual)
│   └── deploy.yml          # Bundle frontend + deploy to GitHub Pages
├── data/
│   ├── antonym-map.json    # Master antonym dictionary (built by build_map.py)
│   ├── curated-core.json   # Tier 1 hand-curated pairs (~900 entries)
│   ├── gutenberg-catalog.json
│   └── moby-thesaurus/     # mthesaur.txt goes here (downloaded by CI)
├── pipeline/
│   ├── ingest.py           # Gutenberg download + boilerplate stripping
│   ├── build_map.py        # Tiered antonym map construction
│   ├── transform.py        # Apply map to source texts
│   ├── chunk.py            # Split into serve-ready JSON chunks
│   └── index.py            # Build client-side search catalog
├── site/
│   ├── index.html
│   ├── app.js              # Frontend SPA (Fuse.js search, chunk reader)
│   └── style.css
└── dist/                   # Build output (gitignored)
    ├── catalog.json
    ├── manifest.json
    └── books/{id}/
        ├── meta.json
        └── chunk-{n}.json
```

---

## Pipeline — Antonym Tiers

| Tier | Source | Coverage |
|------|--------|----------|
| 1 | Hand-curated core (`data/curated-core.json`) | ~60–70% of running text by frequency |
| 2 | Princeton WordNet via NLTK | +15–20% |
| 3 | Moby Thesaurus transitive closure | +10–15% |
| 4 | GloVe embedding inversion (opt-in) | +5% |
| 5 | Identity fallback (proper nouns, articles, etc.) | remainder |

---

## CI/CD

All compute is free-tier.

| Workflow | Trigger | Runtime |
|----------|---------|---------|
| `build-map.yml` | Manual | ~10 min |
| `build-books.yml` | Weekly Sunday 04:00 UTC + manual | ~30 min for 500 books |
| `deploy.yml` | On push to `main` (site/) + after books build | < 1 min |

Hosting: GitHub Pages (free, 1 GB limit) + Cloudflare CDN (free).

---

## Initial Seed

For the first full build (500 titles), run the pipeline locally to avoid hitting GitHub Actions' 6-hour job limit, then commit `dist/` to the `gh-pages` branch:

```bash
python pipeline/ingest.py --limit 500
python pipeline/transform.py
python pipeline/chunk.py
python pipeline/index.py

# Push dist/ to gh-pages branch
git subtree push --prefix dist origin gh-pages
```

---

## License

Source code: MIT. Project Gutenberg texts: public domain. Moby Thesaurus: public domain. WordNet: BSD. GloVe vectors: free for research/non-commercial use.
