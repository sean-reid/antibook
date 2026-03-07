/**
 * ANTIBOOK — Frontend Application
 *
 * Architecture:
 *   - On load: fetch catalog.json, init Fuse.js fuzzy search
 *   - Search: as-you-type, results < 200ms after catalog load
 *   - Reader: fetch meta.json + chunk-0.json on book select
 *             IntersectionObserver-driven chunk loading (virtual scroll)
 *             Prefetch 2 chunks ahead
 */

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const CATALOG_URL = "catalog.json";
const BOOKS_BASE = "books";
const FUSE_CDN = "https://cdn.jsdelivr.net/npm/fuse.js@7/dist/fuse.mjs";
const PREFETCH_AHEAD = 2;
const MAX_RESULTS = 40;

// ---------------------------------------------------------------------------
// DOM refs
// ---------------------------------------------------------------------------

const searchScreen = document.getElementById("search-screen");
const readerScreen = document.getElementById("reader-screen");
const searchInput = document.getElementById("search-input");
const searchStatus = document.getElementById("search-status");
const resultsEmpty = document.getElementById("results-empty");
const resultsLoading = document.getElementById("results-loading");
const resultsList = document.getElementById("results-list");
const totalBooksSpan = document.getElementById("total-books");

const backBtn = document.getElementById("back-btn");
const readerTitleEl = document.getElementById("reader-title");
const readerAuthorEl = document.getElementById("reader-author");
const readerProgress = document.getElementById("reader-progress");
const dualPaneBtn = document.getElementById("dual-pane-btn");
const readerBody = document.getElementById("reader-body");
const chunkContainer = document.getElementById("chunk-container");
const readerSpinner = document.getElementById("reader-spinner");
const readerEnd = document.getElementById("reader-end");

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let fuse = null;
let catalog = [];
let searchDebounceTimer = null;

let currentBook = null;    // { meta, loadedChunks, totalChunks, loading }
let prefetchSet = new Set();
let loadedChunkCount = 0;
let intersectionObserver = null;

// ---------------------------------------------------------------------------
// Catalog loading
// ---------------------------------------------------------------------------

async function loadCatalog() {
  show(resultsLoading);
  hide(resultsEmpty);
  setStatus("Loading catalog…");

  try {
    const [Fuse, resp] = await Promise.all([
      importFuse(),
      fetch(CATALOG_URL),
    ]);

    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    catalog = await resp.json();

    fuse = new Fuse(catalog, {
      keys: [
        { name: "title",   weight: 0.6 },
        { name: "author",  weight: 0.3 },
        { name: "subjects", weight: 0.1 },
      ],
      threshold: 0.35,
      includeScore: true,
      ignoreLocation: true,
      minMatchCharLength: 2,
    });

    totalBooksSpan.textContent = catalog.length.toLocaleString();
    hide(resultsLoading);
    show(resultsEmpty);
    setStatus("");
  } catch (err) {
    hide(resultsLoading);
    setStatus(`Could not load catalog: ${err.message}`);
    resultsEmpty.querySelector("p").textContent =
      "Could not load catalog. Check your connection and try reloading.";
    show(resultsEmpty);
  }
}

async function importFuse() {
  // Prefer local bundle (produced by esbuild in CI), fall back to CDN
  try {
    const local = await import("./vendor/fuse.mjs");
    return local.default;
  } catch {
    const mod = await import(FUSE_CDN);
    return mod.default;
  }
}

// ---------------------------------------------------------------------------
// Search
// ---------------------------------------------------------------------------

function onSearchInput() {
  clearTimeout(searchDebounceTimer);
  const q = searchInput.value.trim();

  if (!q) {
    clearResults();
    show(resultsEmpty);
    setStatus("");
    return;
  }

  if (!fuse) {
    setStatus("Catalog not ready yet…");
    return;
  }

  searchDebounceTimer = setTimeout(() => runSearch(q), 60);
}

function runSearch(q) {
  const t0 = performance.now();
  const raw = fuse.search(q, { limit: MAX_RESULTS });
  const elapsed = Math.round(performance.now() - t0);

  clearResults();
  hide(resultsEmpty);

  if (!raw.length) {
    resultsEmpty.querySelector("p").textContent = `No results for "${q}"`;
    show(resultsEmpty);
    setStatus("");
    return;
  }

  const fragment = document.createDocumentFragment();
  for (const { item } of raw) {
    fragment.appendChild(buildResultCard(item));
  }
  resultsList.appendChild(fragment);

  setStatus(`${raw.length} result${raw.length === 1 ? "" : "s"} (${elapsed} ms)`);
}

function buildResultCard(book) {
  const el = document.createElement("div");
  el.className = "result-card";
  el.setAttribute("role", "listitem");
  el.setAttribute("tabindex", "0");

  const wordCount = book.word_count
    ? `${Math.round(book.word_count / 1000)}k words`
    : "";

  el.innerHTML = `
    <div class="result-title">${esc(book.title || "Untitled")}</div>
    <div class="result-author">${esc(book.author || "Unknown")}</div>
    <div class="result-meta">
      ${wordCount ? `<span>${esc(wordCount)}</span>` : ""}
    </div>
    ${book.preview ? `<div class="result-preview">${esc(book.preview.slice(0, 300))}…</div>` : ""}
  `;

  const open = () => openBook(book);
  el.addEventListener("click", open);
  el.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
  });

  return el;
}

function clearResults() {
  resultsList.innerHTML = "";
  hide(resultsEmpty);
}

function setStatus(msg) {
  searchStatus.textContent = msg;
}

// ---------------------------------------------------------------------------
// Reader
// ---------------------------------------------------------------------------

async function openBook(book) {
  // Transition to reader screen
  searchScreen.classList.remove("active");
  searchScreen.classList.add("hidden");
  readerScreen.classList.remove("hidden");

  // Reset reader state
  chunkContainer.innerHTML = "";
  hide(readerEnd);
  hide(readerSpinner);
  if (intersectionObserver) { intersectionObserver.disconnect(); intersectionObserver = null; }
  prefetchSet.clear();
  loadedChunkCount = 0;

  readerTitleEl.textContent = book.title || "Untitled";
  readerAuthorEl.textContent = book.author || "";
  readerProgress.textContent = "";

  currentBook = {
    id: book.id,
    totalChunks: book.chunk_count || 0,
    loading: false,
  };

  // Fetch meta.json and first chunk in parallel
  show(readerSpinner);
  try {
    const [meta, chunk0] = await Promise.all([
      fetchJSON(`${BOOKS_BASE}/${book.id}/meta.json`),
      fetchJSON(`${BOOKS_BASE}/${book.id}/chunk-0.json`),
    ]);

    currentBook.totalChunks = meta.chunk_count;
    appendChunk(chunk0);
    hide(readerSpinner);
    updateProgress();

    // Set up sentinel for infinite scroll
    setupScrollSentinel();
    // Prefetch ahead
    prefetchChunks(1);
  } catch (err) {
    hide(readerSpinner);
    chunkContainer.innerHTML = `<p style="color:var(--red);padding:40px 0">Could not load book: ${esc(err.message)}</p>`;
  }
}

function appendChunk(chunk) {
  const div = document.createElement("div");
  div.className = "chunk";
  div.dataset.chunkIndex = chunk.index;
  div.textContent = chunk.text;
  chunkContainer.appendChild(div);
  loadedChunkCount = chunk.index + 1;
}

async function loadNextChunk() {
  if (!currentBook) return;
  if (currentBook.loading) return;
  if (loadedChunkCount >= currentBook.totalChunks) {
    show(readerEnd);
    hide(readerSpinner);
    return;
  }

  currentBook.loading = true;
  show(readerSpinner);

  try {
    const chunk = await fetchJSON(
      `${BOOKS_BASE}/${currentBook.id}/chunk-${loadedChunkCount}.json`
    );
    appendChunk(chunk);
    updateProgress();
    prefetchChunks(loadedChunkCount + 1);
  } catch (err) {
    console.warn("Chunk load failed:", err);
  } finally {
    currentBook.loading = false;
    if (loadedChunkCount < currentBook.totalChunks) {
      hide(readerSpinner);
    }
  }
}

function prefetchChunks(fromIndex) {
  if (!currentBook) return;
  for (let i = fromIndex; i < fromIndex + PREFETCH_AHEAD; i++) {
    if (i >= currentBook.totalChunks) break;
    if (prefetchSet.has(i)) continue;
    prefetchSet.add(i);
    fetch(`${BOOKS_BASE}/${currentBook.id}/chunk-${i}.json`).catch(() => {
      prefetchSet.delete(i);
    });
  }
}

function setupScrollSentinel() {
  // Remove old sentinel
  const old = document.getElementById("scroll-sentinel");
  if (old) old.remove();

  const sentinel = document.createElement("div");
  sentinel.id = "scroll-sentinel";
  sentinel.style.height = "1px";
  chunkContainer.after(sentinel);

  intersectionObserver = new IntersectionObserver(
    (entries) => {
      if (entries[0].isIntersecting) {
        loadNextChunk();
      }
    },
    { rootMargin: "400px" }
  );
  intersectionObserver.observe(sentinel);
}

function updateProgress() {
  if (!currentBook || !currentBook.totalChunks) return;
  const pct = Math.round((loadedChunkCount / currentBook.totalChunks) * 100);
  readerProgress.textContent = `${pct}%`;
}

// ---------------------------------------------------------------------------
// Dual-pane mode
// ---------------------------------------------------------------------------

let dualPaneActive = false;
let originalPane = null;

dualPaneBtn.addEventListener("click", () => {
  dualPaneActive = !dualPaneActive;
  dualPaneBtn.setAttribute("aria-pressed", String(dualPaneActive));

  if (dualPaneActive) {
    readerBody.classList.add("dual");
    if (!originalPane) {
      originalPane = document.createElement("div");
      originalPane.id = "original-pane";
      originalPane.className = "reader-pane";
      originalPane.innerHTML = `<div class="chunk-container" style="color:var(--text-dim)">
        <p style="font-family:var(--font-serif);font-style:italic;color:var(--text-faint);padding:40px 0;text-align:center">
          Original text loading is deferred to v1.1.<br>
          Word-level alignment requires an original-text chunk store.
        </p>
      </div>`;
    }
    readerBody.appendChild(originalPane);
  } else {
    readerBody.classList.remove("dual");
    if (originalPane) originalPane.remove();
  }
});

// ---------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------

backBtn.addEventListener("click", goBack);

function goBack() {
  readerScreen.classList.add("hidden");
  searchScreen.classList.remove("hidden");
  searchScreen.classList.add("active");
  currentBook = null;
  if (intersectionObserver) { intersectionObserver.disconnect(); intersectionObserver = null; }
}

// Handle browser back button
window.addEventListener("popstate", () => {
  if (!readerScreen.classList.contains("hidden")) {
    goBack();
  }
});

// ---------------------------------------------------------------------------
// Search input wiring
// ---------------------------------------------------------------------------

searchInput.addEventListener("input", onSearchInput);
searchInput.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    searchInput.value = "";
    clearResults();
    show(resultsEmpty);
  }
});

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function show(el) { el.classList.remove("hidden"); }
function hide(el) { el.classList.add("hidden"); }

function esc(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function fetchJSON(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`HTTP ${resp.status} fetching ${url}`);
  return resp.json();
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

loadCatalog();
