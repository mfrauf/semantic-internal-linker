# Semantic Internal Linker (v3.1)

A self-hosted SEO pipeline that suggests and inserts **internal links between
your articles by semantic similarity** (sentence-embedding + cosine), with
LLM-generated anchor text and an optional inline-contextual insert mode.

It is client/site-agnostic: point it at your own Google Sheet of articles, your
own WordPress (or any CMS), and your own LLM. Nothing here phones home to the
original author's infrastructure; every ID, host, and domain in this repo is a
`YOUR_*` placeholder you must fill in.

## What it does

1. Read a list of articles (URL + title + body) from a Google Sheet.
2. Embed each article locally (free, offline `sentence-transformers`).
3. Build a cosine-similarity matrix; keep pairs above `--threshold`.
4. (Optional) Generate natural anchor text with an LLM.
5. (Optional) Insert links into WordPress:
   - **v1** — recommend only (write candidates to a Sheet for human review).
   - **v2** — append a "Baca juga" footer to approved posts via WP REST API.
   - **v3 / v3.1** — rewrite ONE paragraph inline so the link reads naturally
     in context (3-tier placement ladder: inline → mid-article → footer).
   - **v3.1** — same as v3 plus `--notify <webhook>` to post a run summary to
     an n8n/Telegram digest after each run.

A cluster-diagram exporter (`semantic_cluster_diagram.py`) and an interactive
HTML map (`interactive_cluster_map.py`) visualize the link graph.

## Architecture

```
Google Sheet `articles` tab (url, title, content, status)
        |  Hermes/Python engine
embeddings (sentence-transformers, local, free) -> cosine matrix
        |  threshold + top-N + existing-link exclusion
candidate pairs (from -> to, score)
        |  LLM anchor text (optional)
Google Sheet `candidates` tab (status = REVIEW)
        |  human approves OR auto-threshold
v2/v3/v3.1 insert into WordPress REST API -> status = DONE
        |  v3.1 --notify
n8n/Telegram digest (your own webhook)
```

## Requirements

- Python 3.10+
- A virtualenv with: `sentence-transformers`, `numpy`, `google-auth`,
  `google-auth-oauthlib`, `requests`
- A Google Cloud OAuth client (`google_token.json`) with Sheets + Drive scopes
- An LLM API key (DeepSeek / OpenAI-compatible) for anchor text + inline rewrites
- (WordPress insert modes) a WordPress site with an Application Password

## Setup

1. Create a virtualenv and install deps:
   ```bash
   python3 -m venv .venv
   . .venv/bin/activate
   pip install sentence-transformers numpy google-auth google-auth-oauthlib requests
   ```
2. Copy `.env.example` to `.env` and fill in your values:
   ```bash
   cp .env.example .env
   ```
   - `GOOGLE_TOKEN_JSON=google_token.json` (path to your OAuth token)
   - `DEEPSEEK_API_KEY=...` (or any OpenAI-compatible key; set `LLM_BASE_URL` too)
   - `WP_SITE_URL=https://YOUR_SITE.com`
   - `WP_USER=YOUR_WP_USER`
   - `WP_APP_PASSWORD=...` (WordPress Application Password, keep spaces)
3. Create the Google Sheet with two tabs: `articles` (columns:
   `url, title, content, status`) and `candidates` (columns:
   `from_url, from_title, to_url, to_title, score, anchor_text, status`).
   Put its ID in `--sheet YOUR_SHEET_ID`.

## Usage

**v1 — recommend only (no WordPress writes):**
```bash
python scripts/internal_linker.py \
  --sheet YOUR_SHEET_ID --threshold 0.4 --top-n 5 --fetch --anchor
```

**v2 — auto-insert "Baca juga" footer (score >= 0.55):**
```bash
python scripts/internal_linker_v2.py \
  --sheet YOUR_SHEET_ID --threshold 0.4 --top-n 5 --fetch --anchor \
  --insert --auto-threshold 0.55
```

**v3 — inline contextual insert (LLM paragraph rewrite):**
```bash
# dry-run first, writes diffs to the v3_diffs sheet tab (no WP writes)
python scripts/internal_linker_v3.py \
  --sheet YOUR_SHEET_ID --inline --dry-run --export-diffs --anchor
# review rows in the sheet, mark APPROVED, then apply
python scripts/internal_linker_v3.py --sheet YOUR_SHEET_ID --apply-diffs
# rollback a post if needed
python scripts/internal_linker_v3.py --sheet YOUR_SHEET_ID --rollback "https://YOUR_SITE.com/slug/"
```

**v3.1 — v3 + run digest to your webhook:**
```bash
python scripts/internal_linker_v31.py \
  --sheet YOUR_SHEET_ID --inline --dry-run --export-diffs --anchor \
  --notify "https://YOUR_N8N_HOST/webhook/internal-linker-digest"
```

**Cluster diagram / interactive map:**
```bash
python scripts/semantic_cluster_diagram.py     # -> semantic-cluster-v3.html
python scripts/interactive_cluster_map.py      # -> semantic-cluster-map-interactive.html
```

## Key design rules (learned the hard way)

- **Per-URL dedup, not per-article:** never link the same target URL twice in
  one article; multiple *different* targets per post are fine and desirable.
- **WordPress REST:** always GET with `context=edit` and PUT `content.raw`
  back with `Content-Type: application/json`, or the write is silently dropped.
- **Reasoning-model token budget:** for reasoning LLMs, budget `max_tokens` for
  both the reasoning pass and the answer (anchors >= 150, inline >= 700).
- **Verify persistence:** re-fetch the post and count `<a href>` after a run;
  a 200 + timestamp bump proves nothing.
- **Blogger-migrated posts** are not `<p>`-based HTML; the paragraph splitter
  sub-splits `<div>` blocks on `<br />` and collapses spacer spans.

## Files

| File | Role |
|---|---|
| `scripts/internal_linker.py` | v1: recommend -> Sheet only |
| `scripts/internal_linker_v2.py` | + WordPress footer auto-insert |
| `scripts/internal_linker_v3.py` | + inline contextual insert (LLM rewrite) |
| `scripts/internal_linker_v31.py` | v3 + `--notify` run digest |
| `scripts/semantic_cluster_diagram.py` | static cluster HTML |
| `scripts/interactive_cluster_map.py` | interactive clickable HTML map |
| `scripts/test_interactive_html.py` | Playwright runtime test for the map |
| `references/wordpress-rest-insert.md` | WP REST silent-failure gotchas |
| `references/inline-insert-v3.md` | v3 inline insert design + debugging |
| `references/google-sheets-api-notes.md` | Sheets/Drive API recipe |

## License

MIT. See `LICENSE`.
