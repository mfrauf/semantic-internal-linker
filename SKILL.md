---
name: semantic-internal-linking
description: "Use when suggesting SEO internal links via embeddings."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [seo, internal-linking, embeddings, sentence-transformers, google-sheets, n8n, content]
---

# Semantic Internal Linking

Class: use when building or running an internal-linking workflow that suggests
links between articles/pages by **semantic similarity** (embedding + cosine),
with LLM-generated anchor text and a Google Sheets + n8n review/digest loop.
Built live 2026-08-07 for YOUR_CLIENT.example (first client) — the pipeline is client-agnostic.

## USER PREFERENCE — brainstorm-first, never auto-run (enforced 2026-08-07)

Rauf requires: **always brainstorm first, don't execute anything, ask the human
first** (also set globally in `agent.system_prompt` + memory). This is doubly
binding here because v2 **writes to a live production WordPress site**:
- Present the plan + options and get EXPLICIT approval BEFORE running `--insert`
  (or any command that mutates WordPress/Sheets/n8n).
- Read-only exploration (fetch sheets, inspect WP posts, dry-run logic) is fine
  without approval; mutations are not.
- No schedule triggers / cron for this pipeline unless explicitly requested —
  the user rejected the weekly schedule + cron outright.
- If a request is ambiguous about side effects, ask which pieces to run and in
  what order; never chain a mutation onto a read-only request.
- **Reports must land in viewable artifacts, not just chat text.** When the
  user asks "where can I see the report" / "export the html", deliver a durable
  location: a sheet tab (e.g. `v3_report` with a `submitted_at` timestamp
  column), a vault doc (synced via sync_vault.sh), or an exported HTML file —
  then give the link/path. Chat-only summaries are treated as incomplete.
- **If the user says they can't see a sheet tab you just wrote ("you sure?")**:
  do NOT re-write or assume failure. First verify via the API that the tab +
  rows exist (read back the tab list), then give the DIRECT link with the
  `#gid=<sheetId>` anchor (tab id comes from the sheet metadata, not the
  index) and tell them to hard-refresh — sheet tabs are browser-cached and
  an old view won't auto-update. Only if the API read-back also fails is the
  write actually broken.

## Architecture (v1: recommend → human inserts; v2: full-auto WordPress insert; v3: inline contextual insert)

v3 (built 2026-08-08, `internal_linker_v3.py`) upgrades the INSERT from a
"Baca juga" footer to an **inline link inside an existing paragraph**: the LLM
rewrites ONE paragraph so the anchor reads naturally in context ("kinda little
bit rewrite"). Full design + gotchas: `references/inline-insert-v3.md`.

```
Google Sheets `articles` tab (url, title, content, status)
        ↓ Hermes engine script
embeddings (sentence-transformers, local, free) → cosine matrix
        ↓ threshold + top-N + existing-link exclusion
candidate pairs (from→to, score)
        ↓ DeepSeek LLM (anchor text, Bahasa)
Google Sheets `candidates` tab (from_url, from_title, to_url, to_title, score, anchor_text, status=REVIEW)
        ↓ n8n workflow (webhook trigger)
Telegram digest (REVIEW rows only)

v2 adds: status=APPROVED (score ≥ --auto-threshold 0.55) →
        Hermes auto-inserts "Baca juga" footer into WordPress REST API →
        status=DONE. Below threshold stays REVIEW for human decision.
```

## Files & IDs (this environment)

- Engine script v1: `internal_linker.py` (recommend → Sheets only; for non-WP CMS like YOUR_CLIENT.example/Framer)
- Engine script v2: `internal_linker_v2.py` (**adds `--seed`, `--insert`, `--auto-threshold`, WordPress REST insert**)
- Engine script v3: `internal_linker_v3.py` (**inline contextual insert via LLM paragraph rewrite; `--inline`, `--dry-run`, `--only`, `--min-confidence`, `--rollback`, `--strip-footers`, `--export-diffs`, `--apply-diffs`, `--batch-size`**)
- Engine script v3.1: `internal_linker_v31.py` (**v3 + `--notify <webhook>`: posts a compact run_report / review_ready / run_error JSON to the n8n digest webhook after each run, so Hermes no longer needs to monitor logs/sheets to report a run. Best-effort: a failed notification never fails the run**). Batch wrapper: `internal_linker_v31_batch.sh` (same loop as v3 but forwards `--notify`). v2 and v3 stay untouched as separate runnable copies.
- Cron wrapper (manual run only — no schedule): `internal_linker_cron.sh`
- Venv: `link-venv` (sentence-transformers + numpy + google-auth + requests; run with `python`)
- Sheet v1 (dedicated since 2026-08-10): "Semantic Internal Linker v1" id `YOUR_SHEET_ID_V1` (tabs: `articles`, `candidates`; v1 is the recommend → Sheets-only version, NO WordPress push)
- Sheet v2/v3/v31 (shared): "Internal Linker" id `YOUR_SHEET_ID` (tabs: `articles`, `candidates`, `v3_diffs`, `v3_backups`, `v3_report`)
- n8n workflow: `internal-linker-digest` id `YOUR_N8N_WORKFLOW_ID`
- Digest webhook: `POST https://YOUR_N8N_HOST/webhook/internal-linker-digest`
- Telegram cred in n8n: "Telegram bot" (id `eDjacpaYvgbErcft`), chat YOUR_TELEGRAM_CHAT_ID
- WordPress (v2, YOUR_SITE.com case): `WP_SITE_URL` / `WP_USER` / `WP_APP_PASSWORD` in `.env` (user `YOUR_WP_USER` authenticated; app password has spaces — keep them)

## Run the engine

```bash
python internal_linker.py \
  --sheet YOUR_SHEET_ID_V1 \
  --threshold 0.4 --top-n 5 --fetch --anchor
```

Flags:
- `--threshold` — minimum cosine similarity (0.4–0.5 typical; tune per site)
- `--top-n` — max candidate targets per article
- `--fetch` — scrape page content for rows missing content
- `--check-existing` — crawl pages to exclude already-linked pairs (slower)
- `--anchor` — generate LLM anchor text (DeepSeek `deepseek-chat`)

Append-only behavior: existing `candidates` rows + their statuses are PRESERVED;
only NEW pairs are added (dedup on from_url+to_url). Re-runs are idempotent.

## v2 — WordPress auto-insert (full-auto mode)

```bash
# Seed articles from the site's post-sitemap.xml (--seed does WP API title lookup)
python internal_linker_v2.py \
  --sheet YOUR_SHEET_ID --seed

# Full auto: fetch → embed → anchor → INSERT into WordPress (score ≥ 0.55)
python internal_linker_v2.py \
  --sheet YOUR_SHEET_ID \
  --threshold 0.4 --top-n 5 --fetch --anchor --insert --auto-threshold 0.55
```

v2 flow: candidates ≥ `--auto-threshold` (0.55) get status APPROVED → the script
finds the source post by slug, appends `<p><strong>Baca juga:</strong> <a
href="{to_url}">{anchor}</a></p>` to `content.raw`, PUTs it back → DONE. Below
threshold stays REVIEW. Status merge on write: user-locked = SKIP/DONE/DUPLICATE;
REVIEW/APPROVED are re-driven by each run (so a fixed re-run retries failures).

### WordPress REST API — the silent-failure gotchas (all cost real debugging)

1. **`context=edit` is MANDATORY on the GET** (`posts?slug=X&status=publish,draft&context=edit`). Without it the API returns `content.rendered` only; PUTting rendered HTML back is **silently dropped** — 200 OK, `modified_gmt` bumps, but content reverts on refetch. Always fetch `content.raw` and PUT raw back.
2. **`Content-Type: application/json` header is MANDATORY on the PUT.** Without it WordPress ignores the JSON body entirely — same silent-200 symptom. (Easy to miss: the auth helper that works for GETs omits it.)
3. **Verify persistence by re-fetching** — a PUT "success" + timestamp change proves nothing. Audit `content.raw` for the marker string after the run.
4. **Homepage URL in the sitemap** (`https://site.com/`) must be excluded from seeding — slug becomes `""` → `post not found` errors.
5. Duplicate guard: scan content for existing `<a href="{target}">` before inserting (normalize trailing slash both sides).

- Full detail on v3 inline insert, its debugging (reasoning-token budget,
  trial-pick audit), prompt shape, rollback, 3-tier ladder and the full-site
  migration run: `references/inline-insert-v3.md`.
- Full debugging narrative + verification recipe for v2 footer insert: `references/wordpress-rest-insert.md`.

## v3 — inline contextual insert (LLM paragraph rewrite)

v3's insert engine differs from v2 in ONE place: instead of appending a footer,
it rewrites a single paragraph inside the article so the link reads naturally.
Approved by Rauf 2026-08-08 for the YOUR_SITE.com case; **user granted
permission for "kinda little bit rewrite" of articles**.

```bash
# Trial mode: only the listed source posts get inline links (others still
# participate in embedding/similarity). Dry-run first — NO writes.
python internal_linker_v3.py \
  --sheet YOUR_SHEET_ID \
  --inline --dry-run --anchor \
  --only "URL1,URL2,URL3,URL4,URL5"

# SCALABLE review loop (hundreds of articles): export diffs to sheet, review
# there, then apply only what you approved.
#   1) generate + review candidates (dry-run, writes diffs to v3_diffs tab):
python internal_linker_v3.py \
  --sheet YOUR_SHEET_ID \
  --inline --dry-run --export-diffs --anchor --only "URL1,URL2,..."
#   2) in the sheet: v3_diffs tab (from, to, score, confidence, original_text,
#      rewritten_text, status) — set rows APPROVED / SKIP
#   3) commit only APPROVED rows:
python internal_linker_v3.py \
  --sheet YOUR_SHEET_ID --apply-diffs
#   4) undo: --rollback [URL] restores originals from the v3_backups tab.
```

v3 insert algorithm (per approved candidate):
1. `contains_link` guard — never double-link a target already in the post.
   **This is the ONLY link-cap rule the user wants.** The old `--max-per-article`
   (1 link per post per run) was REMOVED after user correction (2026-08-10):
   the rule is *"do not link the same URL twice in one article"* — if the target
   URL is not yet linked in the post and score ≥ threshold, insert it, even if
   the post already got other links. Multiple DIFFERENT targets per post are
   fine and desirable.
2. Split source post `content.raw` into paragraph-like chunks
   (`split_paragraphs`): matches `<p>`, `<div>`, `<li>`, `<h2-h4>` blocks;
   giant `<div>` blocks are sub-split on `<br />` because Blogger-migrated
   posts keep one sentence per `<span>...</span><br />` line inside a
   single div. Only chunks of 50–500 chars text survive (short = headings
   /sign-offs, long = whole sections)
3. Embed all chunks locally, score each against the TARGET article's
   embedding, keep top-5 most similar (cost control — don't send all paras)
4. Guard chunk length 50–500 chars
5. **3-TIER PLACEMENT LADDER (user-approved 2026-08-10)** — graceful
   degradation so no good-scoring candidate goes unplaced:
   - **Tier 1 — inline rewrite**: LLM (`deepseek-v4-flash`) picks ONE chunk
     index + returns replacement text (with the `<a href>` inline) +
     confidence 1–5 + preserved_links flag. Validate: replacement contains
     target href; confidence ≥ `--min-confidence` (3).
   - **Tier 2 — mid-article "Baca juga"**: if tier 1 fails (LLM declines /
     no valid paragraph), pick the single paragraph most similar to the
     target (`max(para_sim)` where text ≥ 20 chars) and insert the footer
     block IMMEDIATELY AFTER it (`old_html + "\n" + block`). Zero LLM cost,
     contextually placed — better SEO than a footer, no rewrite drift risk.
   - **Tier 3 — end-of-article footer**: if no usable paragraph exists at
     all, append the block at the end.
   Tier is logged as `[inline]` / `[mid]` / `[footer]` on every insert and
   reported per-pair in the run log so the user sees the placement mix.
6. `--dry-run` → print old/new chunk diff, status REVIEW, no writes
7. Commit → backup original to `v3_backups` tab (post_id, url, original_content,
   time, restored) → string-replace that one chunk (or append) → PUT → DONE

Flags: `--inline` (enable v3 mode), `--dry-run`, `--only` (comma-separated
source URLs, trial), `--min-confidence` (default 3), `--rollback [URL]`,
`--anchor` (LLM anchor text),
`--export-diffs` (dry-run: write diffs to the `v3_diffs` sheet tab for review —
the scalable review path), `--apply-diffs` (commit APPROVED rows from
`v3_diffs` to WordPress), `--strip-footers` (remove "Baca juga" footer blocks
from posts, with backup to v3_backups — used to migrate v2 footers → v3
inline links), `--batch-size N` (process at most N candidates per invocation;
see Batch loop below). Sheet tabs: `articles`, `candidates`, `v3_diffs`
(review queue: REVIEW → APPROVED → DONE), `v3_backups` (rollback source),
`v3_report` (post-level census: url, title, internal_links, target_slugs,
has_footer, submitted_at — the report the user asked to see in the sheet).

### Batch loop — the timeout-proof way to scale (hit live 2026-08-08)

Full-site runs serialize LLM calls (~2-7s each; 155 candidates ≈ 18 min), so a
single invocation blows any foreground timeout. Two mechanisms in the script:

1. **`--batch-size N`**: process at most N *pending* candidates per run. The
   Google Sheets `candidates` tab is the checkpoint — anchors + statuses
   persist, so re-running the SAME command resumes where the last run stopped
   (idempotent).
2. **Anchor reuse**: before generating anchors, the script reads existing
   `anchor_text` from the `candidates` tab and skips the LLM call for any pair
   already anchored (`known_anchors[(from,to)]` map). Repeat runs are nearly
   free — hit live: 154 of 155 anchors reused on a re-run.
3. Driver wrapper: `internal_linker_v3_batch.sh --size 25
   --max-runs N [--dry-run]` loops `--batch-size` invocations and stops when a
   run reports fewer pending candidates than the batch size.

```bash
# run 1: first 25 pending
python internal_linker_v3.py --inline --anchor --batch-size 25
# run 2: resumes (anchors reused, done pairs skipped)
python internal_linker_v3.py --inline --anchor --batch-size 25
```

**Batch-loop pitfall — define "pending" correctly or the loop never terminates**
(hit live 2026-08-09: two loops killed after re-processing the same candidates).
The pending filter must be `score >= auto_threshold AND status NOT terminal`.
Terminal statuses (read from the `candidates` sheet) are: `DONE`, `DUPLICATE`,
`ERROR`, and anything starting with `SKIP`. Two traps:
- **Confidence-skipped candidates** get status `SKIP (confidence N)` in the
  sheet — they ARE terminal for the loop (the LLM already judged them), so a
  pending filter that only checks anchor-reuse keeps them "pending" forever.
- **Below-threshold pairs** (score < auto_threshold) get `REVIEW` — they are
  never auto-inserted by design, so in bypass mode they must count as done,
  or the loop reprocesses them every iteration.
A correct pending query shrinks run over run (live: 129 → 32 → 1) and the
driver exits on "fewer pending than batch size". If `remaining:` in the loop
log repeats the same number, the pending filter is wrong — fix it, don't just
kill the loop.

Cost model (measured, deepseek-v4-flash, 2026-08): ~540 in + ~150 out tokens
per anchor call, ~790 + ~536 per inline rewrite → **~$0.001/article** at
~3 candidates/article. Even 5,000 articles ≈ $4. **Time is the constraint, not
money** — the fix is batching/parallelism, not budget. (DeepSeek rates at the
time: $0.14/M input cache-miss, $0.0028/M cache-hit, $0.28/M output; docs
flagged a planned increase.)

### Pitfall: "already fully linked" sites yield 0 new inserts (and that's correct)

Full-site v3 run on YOUR_SITE.com: 0 inserted, 129 duplicate — every high-score
target was already linked (134 v2 footer links covered the good pairs). The
duplicate guard checks LIVE content, not the sheet, so it's correct. Lesson:
on a mature, already-linked site the remaining value is **footer → inline
migration** (`--strip-footers` then re-run), not new links. v3 delivers its
real value on FRESH client sites with no pre-existing footers. Always audit
pairs per orphaned post before judging a run's success.

### Migration outcome: 3-tier ladder + per-URL rule restored full coverage

Hit live 2026-08-09/10 (YOUR_SITE.com): after `--strip-footers` removed all 134
v2 footer blocks, an early inline-only v3 run added only ~6 links — 38 of 45
posts ended with ZERO internal links (the site became LESS linked than the
footer era; the confidence gate rejected most candidates). **That intermediate
state is NOT the final outcome.** The full-resolution sequence that restored
coverage:

1. User correction: the link-cap rule is *per-URL* ("never the same URL twice
   in one article"), NOT per-article. Removing `--max-per-article` let a post
   receive multiple DIFFERENT target links.
2. Directed-dedup fix (above) surfaced the hidden REVIEW candidates.
3. The 3-tier ladder (inline → mid → footer) placed every remaining
   good-scoring candidate somewhere.

Final live census after the full run: **44 of 45 posts linked, 200 internal
links (39 inline + 161 mid/footer), 0 duplicate targets in any post, 0
failures; 153 inserted in the last pass, 45 duplicate-guard hits.** Only
orphan: `SEO Sekarang Semakin Sulit?` (2025 post, max similarity 0.51 < 0.55
threshold — excluded by design per the user's score rule).

Lessons: (a) report the link count after EVERY migration stage — intermediate
"38 orphans" is a stage, not a verdict; (b) the 3-tier ladder exists precisely
because inline-only linking cannot restore coverage on a mature site;
(c) per-URL (not per-article) is the user's stated dedup rule — encode it in
the guard, not a counter.

### Pitfall: `--strip-footers` only matches the exact footer BLOCK format

The regex targets `<p><strong>Baca juga:</strong> <a href="...">...</a></p>`.
Original 2013-era in-body sentences like "Baca juga tentang pentingnya
Reputasi dalam Bisnis" (written inline in the article, often pointing to dead
`.html` Blogger URLs) are NOT v2 footers and survive stripping — verified
live on `apa-itu-personal-branding-pentingkah`. After a strip run, re-audit
with `count("Baca juga")` per post and expect exactly 0 in footer format;
report any in-body survivors as original content, not v2 leftovers, and ask
the user whether to also fix/remove those dead-link sentences.

### Pitfall: LLM rewrite SILENTLY DROPS existing links in the paragraph (link-loss bug)

Hit live 2026-08-08: a post ended up with its approved 6-tips inline link
missing after an `--apply-diffs` run. Sequence: commit inserted the 6-tips
link into the "Social media" paragraph → a later dry-run export rewrote that
SAME already-linked paragraph for a different candidate → `--apply-diffs`
replaced the paragraph wholesale with the new rewrite, dropping the 6-tips
link. LLM rewrites do NOT preserve existing hyperlinks reliably — the
"preserved_links" flag in the prompt is not a guarantee.

Two guards, both now in the script:
1. **Never send a paragraph that already contains an internal link to the
   LLM** (`top = [k for k in top if "YOUR_SITE.com" not in paras[k]["html"]]`).
2. **Apply-time hard guard**: count internal links before/after the replace
   and REFUSE (skip + report) if the new content has fewer links than before.
   Never trust the LLM's preserved_links claim; count hrefs yourself.
3. When diagnosing missing links, compare against the `v3_backups` rows — each
   backup row is the pre-PUT content. Note backups are append-only and rows
   shift as new ones arrive; match by `post_id` + a content marker, never by
   hardcoded row number.

### Pitfall: Blogger-migrated posts are NOT `<p>`-based HTML

YOUR_SITE.com's 2013-era posts (Blogger-import) structure every sentence as
`<span style="...">text</span><br />` inside ONE giant `<div>`, with headings
as `<h2>` and lots of empty spacer spans `<span><br /></span>`. A naive
`<p>`-only splitter finds 0–1 paragraphs → every candidate silently SKIPPED
(no diff, no error). Fixes baked into `split_paragraphs`:
- match `<p|div|li|h[2-4]>` blocks first; if a div's text > 500 chars,
  sub-split it on `<br />` (each span+br line = one sentence/chunk)
- **collapse spacer spans before the br-split**:
  `re.sub(r"<span[^>]*>\s*<br\s*/?>\s*</span>", "<br />", block)` — otherwise
  the split tears them apart and the next chunk starts with a stray `</span>`
- length guard 50–500 chars text; chunks shorter than 50 (headings, `@MFRauf`
  sign-offs) and longer than 500 (whole sections) are skipped

Replacement must preserve the span chain for font styling: extract the
dominant span nesting (`<span A><span B>...` opens + matching closes) from the
original chunk and wrap the LLM replacement in it (`_extract_span_chain` /
`build_new_paragraph`). Dropping the spans silently loses font-size/family
styling on the rewritten paragraph — "kinda rewrite" should not restyle.

### Pitfall: dedup must be DIRECTED, or sheet statuses become unreachable

Hit live 2026-08-10: 10 REVIEW rows with score ≥ 0.55 sat in the sheet, but the
run reported "1 of 1 pending" — the batch loop silently ignored them. Root
cause: candidate dedup used `key = tuple(sorted([src, tgt]))`, an UNDIRECTED
key. Sorting alphabetically REVERSED the pair: sheet row `menulis→7-langkah`
(REVIEW) became candidate `7-langkah→menulis`, and that reversed direction had
status DUPLICATE in the sheet → marked terminal → the REVIEW row was
unreachable forever. **Dedup key MUST be the directed tuple
`(normalize(src), normalize(tgt))`** — only exact same-direction repeats are
true duplicates. When "pending" undercounts vs the sheet's REVIEW≥threshold
rows, suspect the dedup key first. (The status-merge write already uses
directed keys — the candidate dedup was the offender.)

### Pitfall: trial picks must have FREE (unlinked) targets

First dry-run on 5 hand-picked posts produced **0 diffs** — not a bug. Those
posts already had their top candidates linked as v2 footers, so v3's duplicate
guard correctly skipped everything. Before choosing trial posts, audit which
candidates are actually unlinked: fetch each source post, run `contains_link`
against its top-5 candidates, and pick sources with free targets (score ≥ 0.55).
Posts whose footer already exhausts the top candidates will never produce an
inline diff.

### Pitfall: DeepSeek reasoning models eat the token budget

`deepseek-v4-flash` (and kimi-k2.x/k3) are **reasoning models**: they burn
`max_tokens` on `reasoning_content` BEFORE producing `content`. With
`max_tokens=40`, a call returns `content: ''` and `finish_reason: length` —
silently empty, no error. The retry-in-`_llm` helper in v3 handles this
(empty content → retry with 3× tokens), but hard rule: **for reasoning models,
budget max_tokens for reasoning + answer** (anchors ≥ 150, inline rewrites ≥ 700).
Never assume a small max_tokens answer will come back; check `content` for empty
string, not just for exceptions.

### Model choice for the rewrite LLM

`deepseek-v4-flash` fits: live, cheap, reasoning pass helps placement judgment,
good Indonesian. `deepseek-chat` = cheaper non-reasoning fallback. `kimi-k3`
would be best for Indonesian nuance but was **suspended (insufficient balance)**
as of 2026-08-08 — top-up unlocks it as a one-line swap (LLM_MODEL + base URL).
`gpt-5.6-luna` only reachable via ChatGPT-sub OAuth — not worth wiring into the
script. Vision (for image review) needs `auxiliary.vision.provider` configured —
none of the auto fallbacks worked (OpenRouter no credit, Nous no auth, Kimi
suspended, DeepSeek not vision-capable).

## Model choice for Indonesian content

`paraphrase-multilingual-MiniLM-L12-v2` (384-dim, local, free) beats
`all-MiniLM-L6-v2` for Bahasa. Note: MiniLM similarity is CONCEPTUAL, not
topical — cross-product pages sharing payment vocabulary can outscore two
same-topic articles (hit live: kelas-online B ↔ retail 0.56 > kelas A ↔ B 0.47).
Calibrate threshold against real pairs, don't expect 0.7+ for related pages.

## n8n digest flow

Workflow `internal-linker-digest` (`YOUR_N8N_WORKFLOW_ID`, 10 nodes since 2026-08-10) is a **Switch on `$json.type`** with three formatter branches feeding one Telegram node, plus the legacy path preserved. Manual/webhook triggers only — user explicitly does NOT want schedules.

| Payload `type` | Sent by | Contents |
|---|---|---|
| `run_report` | v3.1 script `--notify` after insert run | inserted/duplicate/skipped/failed + tier counts (inline/mid/footer) + coverage + duration, no article content |
| `review_ready` | dry-run with `--export-diffs` | new diff count + top 3 candidates |
| `run_error` | `__main__` wrapper on fatal exception | stage, error, backup status; exit code preserved |
| (no `type`) | legacy path | Webhook → Read candidates → IF status==REVIEW → Code node → Telegram. Empty REVIEW set → [] → chain ends, no message |

- Payloads must be compact: no HTML, article text, stack traces, or secrets.
- Webhook: `POST https://YOUR_N8N_HOST/webhook/internal-linker-digest`
- **urllib needs a browser User-Agent** — bare `Python-urllib/3.x` gets Cloudflare 403 on the your n8n host host (hit live 2026-08-10); send `User-Agent: Mozilla/5.0 ... Chrome/126.0` in the notify POST.
- Verify delivery via `GET /api/v1/executions?workflowId=YOUR_N8N_WORKFLOW_ID&limit=N` — each payload type should show `status=success, finished=true`. (n8n REST PUT quirks: see `n8n-remote-execution` skill.)

## Pitfalls

- **Google auth scopes must be a LIST**: `Credentials.from_authorized_user_file(path, [SCOPES])` — a bare string is iterated char-by-char → `invalid_scope` with letter fragments.
- **Sheets values:update needs `?valueInputOption=USER_ENTERED`** or HTTP 400.
- **Sheets write MUST clear the tab first** (`values/{tab}:clear` POST then PUT): a PUT that writes fewer rows than the tab holds leaves STALE rows below, which silently re-pollute the next run (hit live: deleted articles reappeared as duplicates → self-links with score 1.0).
- **Trailing-slash variants are different rows**: always `rstrip("/")` on URL keys before dedup, or the same post appears twice (as `.../slug` and `.../slug/`) → duplicate footers / self-links.
- **n8n sheetName `gid=` prefix fails** on v4.5 — use `mode:"name"` + tab title.
- **n8n update node**: `matchingColumns` inside `columns` + `schema` array required; multi-column match flaky → prefer append-only dedup in the engine script over n8n write-back.
- **Full detail on n8n API create/activate/webhook**: see `n8n-remote-execution` skill.
- **Orphaned posts are held at REVIEW, not forced** (hit live: `seo-semakin-sulit`, a 2025 post among 2013-era content). A post whose top similarity sits below `--auto-threshold` gets NO footer and can end up with **zero internal links** (0 in, 0 out) — engine behavior is correct (no forced weak links), but SEO-wise an orphaned fresh post deserves 1-2 links regardless. Two distinct orphan cases: (a) all pairs below threshold (held REVIEW for human override: mark strongest pair APPROVED, re-run `--insert`); (b) distinctive content with NO candidate ≥ threshold at all as a source (receives inbound links, never generates outbound). When auditing a run, list posts WITHOUT footers and check the pair scores before declaring success/failure — low scores here are a review artifact, not a bug.
- **Existing-link exclusion is opt-in** (`--check-existing`) — without it the engine re-suggests links already on the page.
- **Repointing a script to a NEW sheet — the argparse default hides (hit live 2026-08-10)**: when v1 moved to its own dedicated spreadsheet, FOUR places held the old ID: the argparse `--sheet` default, the module docstring usage example, the cron wrapper's `SHEET=` var, and the vault doc. The argparse default is the easy one to miss (`--help` prints it only if the help text contains `%(default)s`). After repointing: grep the old ID across script + wrapper + docs, then verify end-to-end by importing the module and calling its own `read_sheet`/`ensure_tab` against the new ID. Creating the new spreadsheet + tabs is a raw Sheets/Drive API job — recipe in `references/google-sheets-api-notes.md`.

## Pitfalls (verification)

- **Audit by COLUMN, not by index guess**: `candidates` header is
  `from_url, from_title, to_url, to_title, score, anchor_text, status` —
  column 2 is `to_url`, column 3 is `to_title`. A quick audit script that
  compares `row[3]` (a TITLE) against hrefs (URLs) reports every DONE row as
  "false-DONE" (hit live: 43 rows flagged, 0 actually broken). When auditing
  link presence, always normalize both sides (rstrip `/`) and compare `to_url`
  against the post's hrefs. If an audit says "X claims done but isn't linked",
  re-check the column mapping before trusting it.
- **`v3_backups` rows shift** — backups are append-only, so row numbers move as
  new rows arrive. Match by `post_id` (+ a content marker like a link slug),
  never by hardcoded row number, when restoring.
- **Dry-run log vs full-run log can differ** — the LLM is non-deterministic, so
  a dry-run's chosen paragraph may differ from the commit run's. Re-verify the
  live post after commit (re-fetch `content.raw`, count hrefs, check tag
  balance), not just trust the run log.

### "Clicking any internal link redirects to the homepage" — server vs browser

Hit live 2026-08-09 (YOUR_SITE.com): user reported every internal link bounced
to the homepage. Full audit showed the SERVER was clean — the redirect was
client-side (stale browser-cached 301s). This is the checklist to run BEFORE
concluding anything, in order:

1. **Server health (all curl-provable, no browser needed):**
   - `curl -sL -w "%{http_code} %{url_effective}" <link>` → 200, final URL =
     the post itself. One 301 adding a trailing slash is NORMAL WordPress
     (`x-redirect-by: WordPress` header); not the bug.
   - Fetch the final page and check `<title>` = the post's title, and that
     byte-size differs from the homepage — catches SOFT redirects (server
     returns 200 but serves homepage HTML).
   - 404s must stay 404 (no 404→homepage redirect rule in .htaccess/plugins).
   - Scan rendered HTML for client redirect machinery: `location.href` /
     `location.replace` / `window.location`, `onclick`, `addEventListener
     ('click')`, `<meta http-equiv=refresh>`, `navigator.serviceWorker` —
     none present = no JS hijack on the page.
   - Check for nested/malformed anchors: `<a ...>...</a>` containing another
     `<a` breaks link handling in browsers.
2. **If the server is clean, it's the browser** — two candidates:
   - **Cached 301s**: browsers cache 301 permanently. If the site ever had a
     redirect rule (migration era, old footer URLs, pre-fix v2 links), every
     click replays the stale cached 301 → homepage even after the server is
     fixed. THE decisive test: open in **incognito/private window** (no cache).
   - **Extensions**: ad-blockers/redirect-stoppers intercept internal links.
     Test with extensions disabled.
3. **Report the test, not the verdict**: incognito works → browser cache /
   extensions; nothing to fix server-side. Incognito also redirects → get the
   exact link and trace that URL's full chain with fresh eyes.
4. Site-specific note (YOUR_SITE.com): Blogger-era `.html` URLs and
   `http://www.` variants are DEAD (404 / connection timeout) — old in-body
   links to them 404. The footer/inline links v2/v3 wrote (clean
   `https://YOUR_SITE.com/...`) all resolve 200. When a user reports broken
   links, distinguish which URL FORMS they're clicking — clean vs `.html`.

Rule: never tell the user "the site is broken" from a browser report alone
(server may be fine), and never tell them "the site is fine" from curl alone
(browser caching is a real layer). The incognito test separates them in one step.

## Semantic cluster diagram (site visualization, HTML export)

`semantic_cluster_diagram.py` turns the v3 dataset into a
visual cluster map: reads the `articles` tab, embeds with the same MiniLM
model, clusters with agglomerative (cosine, average-linkage), pulls LIVE
internal links from WordPress (`context=edit`), and renders a single
self-contained dark-themed HTML (circular layout grouped by cluster, chord
edges = real links, node radius = inbound-link count, hover tooltips, legend,
summary cards). User wants diagram deliverables **exported as HTML files**,
not just described.

```bash
python semantic_cluster_diagram.py
# → semantic-cluster-v3.html
```

- Cluster-count tuning: a fixed `n_clusters` (12) beats threshold-scanning for
  balance; then MERGE singleton clusters into their nearest post (argmax sim)
  or you get 1-post clusters that pollute the map (live: 12 → 7 clean clusters
  18/8/7/4/3/3/2). If the html file needs label sanity, note the cluster-name
  extractor counts title words minus an Indonesian stoplist; add site-specific
  author-name words to STOP or every cluster is named "Muhammad / fathi / rauf".
- Edge count sanity: `<path>` is self-closing in SVG, so a tag-balance checker
  that expects `</path>` will report MISMATCH — verify circle/text counts
  instead (45 posts → 45 circles + 45 labels; paths = links + cluster arcs).
- Rendering preview without the sandbox-broken browser tool: use the Playwright
  headless shell directly, e.g.
  `chromium-headless-shell --no-sandbox --disable-gpu --screenshot=/tmp/x.png --window-size=1180,1400 file://semantic-cluster-v3.html`
  then verify the PNG is non-blank by decoding IDAT and counting distinct
  sampled colors (>50 = rendered). No vision provider is configured in this
  environment, so pixel-variance is the verification method.

### Interactive HTML artifact (user preference: "like Claude artifacts", clickable, NOT screenshots)

User explicitly wants diagram/artifact deliverables as **interactive clickable
HTML** — "i don't want a screenshot, but interactive clickable html" (2026-08-10).
Screenshots/static SVGs are NOT acceptable as the deliverable; they're only
internal verification aids. Build pattern:

```bash
python interactive_cluster_map.py
# → semantic-cluster-map-interactive.html  (single self-contained file)
```

- Reuses the same data pipeline (articles tab → MiniLM embeddings → agglomerative
  clusters → live WP links) but serializes nodes/links/clusters as JSON in
  `<script type="application/json">` blocks and renders via vanilla JS + SVG.
- Interaction set that satisfied the user: click node → side detail panel with
  in/out links (each clickable to jump), click cluster arc or legend item →
  filter/dim rest, live search box, toggles for links/labels, reset + Esc,
  `/` focuses search, hover tooltips. Indonesian UI labels.
- **Zero dependencies**: no CDN, no external libs — data is embedded as JSON,
  works offline, opens from file:// directly.
- Design playbook: load `claude-design` skill first. Its "surface-first" rule
  applies (this is an **Explore** surface: filters + drill-down, NOT a hero +
  feature cards); run its 10-point slop audit before delivering.

**Verification loop for interactive HTML (mandatory — JS bugs are silent):**
1. JSON blocks parse: `json.loads` each `<script id=... type="application/json">`.
2. JS syntax: extract the last `<script>` block, `node --check file.js`.
3. **Runtime interaction test via Playwright Python API** — playwright lives in
   the HINDSIGHT venv, NOT link-venv:
   `python test_interactive.py`
   (pattern: launch chromium with `--no-sandbox`, attach `console` + `pageerror`
   listeners, then programmatically click a node, click a legend item, fill the
   search box, click reset; assert panel content and `#shown` counter change).
   Reusable copy: `scripts/test_interactive_html.py` in this skill (run via
   `python`, takes the HTML path as argv[1]).
   Pass = zero console/pageerror entries. Live result: 45 nodes, click → panel
   opens, cluster filter → 3 shown, search "seo" → 1, reset → 45, errors NONE.
4. Only after the runtime test passes, deliver the HTML via `MEDIA:` path.

### Pitfall: patching a line with `"""` can silently break the file

Hit live 2026-08-10: a patch whose new_string contained a triple-quoted
`STOP = set("""...""".split())` line came back with only 4 quote chars (one
closing `"""` truncated to `"`) → unterminated string. The resulting
SyntaxError pointed at a DISTANT line (`invalid character '•'` at line 227,
inside a legit f-string) — Python reports the first place the string boundary
breaks, not the actual cause. Fix discipline:
- After ANY patch whose new_string contains `"""` or long strings, run
  `python -m py_compile` (or `node --check`) IMMEDIATELY.
- Count quote chars on the patched line (`grep -o '"' | wc -l`) — a triple-quote
  open needs 3 close quotes (6 total for a balanced `set("""...""")`).
- If the SyntaxError lands somewhere that looks unrelated, suspect an
  unterminated string earlier in the file, not the reported line.

## Related

- `n8n-remote-execution` — workflow create/verify/diagnose patterns (this pipeline's n8n leg)
- Wiki: `seo/seo-framework` (tree-like link hierarchy, impact/effort), `seo/seo-agency-sop` (8-step workflow, approval gates), `product-marketing/hiiboss-case-study` (specificity ladder for link targets)
