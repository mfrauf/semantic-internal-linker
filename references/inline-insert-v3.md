# v3 Inline Insert — Build Log & Debugging (2026-08-08)

Session narrative for `internal_linker_v3.py`: upgrading the v2 footer insert to
an inline contextual link (LLM rewrites ONE paragraph). Client: YOUR_SITE.com
(self-hosted WordPress, Indonesian content).

## What changed vs v2

| | v2 (footer) | v3 (inline) |
|---|---|---|
| Placement | End of article, `<p><strong>Baca juga:</strong> <a>...</a></p>` | Inside an existing paragraph, rewritten naturally |
| Content change | One line appended | One paragraph rewritten around the link |
| SEO value | Moderate (footer links de-weighted) | High (contextual inline links) |
| Risk | Near zero | LLM drift — needs dry-run + backups |

## Approved design (user-confirmed decisions)

1. **Paragraph replacement** (not full-article regeneration) — LLM returns
   paragraph index + replacement text; script does a string replace of exactly
   that `<p>` block. Other 95% of article byte-identical.
2. **deepseek-v4-flash** for anchors AND inline rewrites (later: `--model` flag).
3. **Top-5 paragraphs** pre-scored by local embedding vs target article embedding
   (cost control, ~6× cheaper than sending all paragraphs).
4. **Dry-run → review → commit** two-step approval.
5. **`v3_backups` sheet tab** — post_id, url, title, original_content, time,
   restored — written before any PUT; `--rollback` restores.
6. **5 live posts only for trial** (`--only` flag) — rest of the 45 articles
   still participate in embedding for accurate similarity.

## Debugging story

### Bug 1: anchors all returned `—` (empty)

Symptom: 24 anchors printed as `—`, no error lines. Direct `_llm` test with a
tiny prompt worked; `generate_anchor` with the real prompt returned `''`.

Root cause: **`deepseek-v4-flash` is a reasoning model.** It writes
`reasoning_content` first and `content` second, sharing ONE `max_tokens`
budget. With `max_tokens=40`: `finish_reason=length`, `content=''`,
`reasoning_content` consumed all 40 tokens. Silent — no exception, so the
`except` in `generate_anchor` never fired.

Fix (in `_llm`): on empty `content`, retry with `max_tokens * 3` (up to 2
retries). Anchor base budget raised 40 → 150. Inline rewrite budget 700.

Verification recipe (reproduce if anchors ever come back empty again):

```python
body = {"model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 40}
r = json.loads(urllib.request.urlopen(req, timeout=60).read())
msg = r["choices"][0]["message"]
print("finish:", r["choices"][0].get("finish_reason"))  # 'length' = budget eaten
print("content:", repr(msg.get("content")))              # '' = reasoning ate it
print("reasoning:", msg.get("reasoning_content", "")[:300])
```

### Bug 2: trial run produced 0 diffs

Symptom: 24 candidate pairs, anchors fine, but zero `📄` diff blocks in dry-run.

Root cause: **the 5 hand-picked trial posts already had their top candidates
linked as v2 footers.** The v3 duplicate guard (`contains_link`) correctly
skipped every pair → all DUPLICATE. My trial picks were bad, not the code.

Fix: audit live posts for FREE candidates before choosing trial subjects:

```python
# per source post: fetch content, contains_link() against top-5 candidates,
# keep only pairs with score >= 0.55 AND not already linked.
# Then pick 5 sources with the most free candidates.
```

Result: 32 of 45 posts had ≥1 free candidate; best picks (e.g.
`5-tips-seo-untuk-blog-anda` with 4 free, `5-cara-promosi-blog-secara-efektif`
with 5 free). Lesson: **check `free candidates` per source before trialing** —
see SKILL.md pitfall.

## LLM inline-rewrite prompt shape

System: "editor for an Indonesian WordPress blog". User content: source title,
indexed candidate paragraphs (`[0] text...`), target title/URL, anchor verbatim.
Rules: keep voice/tone/meaning; preserve existing hyperlinks verbatim; no
"Baca juga"; link must point to exact target URL; confidence 0 = no fit.
Output: STRICT JSON only — `{"index": int, "replacement": "...", "confidence":
1-5, "preserved_links": bool}`. Parse defensively (strip ``` fences, regex for
`{...}` if json.loads fails). Validate replacement contains the target href.

## Rollback path

`--rollback` (all) or `--rollback "URL"` (one) reads `v3_backups`, skips rows
already `restored=yes`, PUTs original content back, marks restored. Backups
tab is append-only via `append_sheet` (never rewrite it wholesale).

### Bug 3: Blogger-migrated HTML — 0 diffs again on re-picked posts

Symptom: after re-picking 5 posts with free candidates, dry-run STILL produced
0 diffs, no errors. Anchors fine, 23 candidate pairs, but no `📄` blocks.

Root cause: **the 2013-era posts are Blogger-imports** — every sentence is
`<span style="...">text</span><br />` inside ONE giant `<div>`, NOT `<p>`
blocks. `split_paragraphs` (v1 of the fix, `<p>` only) found 0–1 paragraphs
per post → every candidate skipped as "no valid paragraph" silently.

Fix (two levels in `split_paragraphs`):
1. match `<p|div|li|h[2-4]>` blocks; if a div's text > 500 chars, sub-split
   it on `<br />`
2. **collapse spacer spans first**: `<span><br /></span>` patterns must be
   reduced to `<br />` before splitting, or the br-split tears them and the
   next chunk leaks a stray `</span>` prefix
3. length guard 50–500; `build_new_paragraph` extracts the dominant span
   chain (`_extract_span_chain`) and re-wraps the LLM replacement so
   font-size/family styling survives

After fix: dry-run produced 2 real diffs (`5-tips-seo → 5-hal` score 0.8389
conf 4; `5-cara-promosi → 6-tips-social-media` score 0.7823 conf 5) with span
chains intact. One candidate returned unparseable JSON → safely skipped.

### Bug 4: dry-run didn't honor --max-per-article

Dry-run showed 3 diffs all on the same source post (every candidate for it)
because the `per_article` counter only incremented in the commit branch. Fix:
increment in the dry-run branch too, so the preview matches what commit
would actually insert (1 per source).

### Bug 5: LLM rewrite silently DROPPED an existing inline link (link-loss)

Symptom: after the approved commit (2 links inserted: 5-tips-seo → 5-hal,
5-cara-promosi → 6-tips), running `--apply-diffs` for a later-reviewed row
left `5-cara-promosi` with only the 5-hal link — the 6-tips link was gone.
Recovery: compared live content against `v3_backups` rows, found the pre-PUT
row for post 65 (4458 chars, contains 6-tips), restored it via PUT. Both
posts then verified correct (balanced tags, no link loss).

Root cause chain:
1. Commit inserted 6-tips INTO the "Social media" paragraph of post 65.
2. A later dry-run export (`--export-diffs`) re-ran the pipeline on the now-
   already-linked post and rewrote that SAME paragraph for the 5-hal candidate
   (it was still top-5 similar and length-valid).
3. `--apply-diffs` fetched the sheet row, did `content.replace(original_text,
   rewritten_text)` — the original_text captured the paragraph WITH the 6-tips
   link inside, so the replacement dropped it.

Fixes (both in script now):
1. **Exclude already-linked paragraphs from LLM candidates** in the insert
   path: `top = [k for k in top if "YOUR_SITE.com" not in paras[k]["html"]]`.
2. **Apply-time hard guard** in `--apply-diffs`: count `href="https://YOUR_SITE.com/..."`
   in content before/after the replace; if the count drops, REFUSE the row
   (skip + print "REFUSED: replace would drop N existing link(s)").
3. Diagnostic rule: backups are append-only and rows SHIFT as new ones arrive.
   Never restore by hardcoded row index (`A4:F4`); read the whole tab and
   match `post_id` + a content marker (e.g. "6-tips" in content).

## Scalable review workflow (added for hundreds-of-articles use)

`--export-diffs` (dry-run) writes every generated inline diff to the
`v3_diffs` sheet tab: from_url, to_url, score, confidence, original_text,
rewritten_text, status. User reviews IN THE SHEET, sets rows APPROVED/SKIP;
`--apply-diffs` commits only APPROVED rows (with backup + the link-count
guard), then marks them DONE. Preserves prior user decisions across exports
(APPROVED/DONE/SKIP rows are kept when the tab is rewritten). This replaces
terminal-diff review as the primary path at scale.

## Full-site run performance & cost (measured)

Full 45-post run: ~155 candidate pairs × ~2-7s DeepSeek anchor call each ≈
**18 min** serial. Cost (deepseek-v4-flash, 2026-08 rates): ~540 in + ~150 out
tokens per anchor; ~790 + ~536 per inline rewrite → **~$0.001/article** at
~3 LLM calls/article; 5,000 articles ≈ $4. **Time is the constraint, not
money.** DeepSeek pricing then: $0.14/M input (cache miss), $0.0028/M (cache
hit — ~100× cheaper on repeats), $0.28/M output; docs flagged a planned
increase.

### Batch loop (implemented, solves the timeout)

- `--batch-size N` flag: process at most N PENDING candidates per invocation;
  the `candidates` sheet tab is the checkpoint (anchors + statuses persist).
- **Anchor reuse**: script reads existing `anchor_text` from the sheet and
  skips the LLM for already-anchored pairs — hit live: 154/155 anchors reused
  on a re-run. Repeat runs are nearly free (cache-hit pricing too).
- Driver: `internal_linker_v3_batch.sh --size 25 --max-runs N
  [--dry-run]` loops batches, stops when pending < batch size.
- Full-site re-run result: **0 inserted, 129 duplicate** — a mature site whose
  v2 footers already cover the good pairs. Correct behavior; remaining value
  is footer→inline migration (`--strip-footers`), and v3 shines on fresh
  client sites. Audit pairs per orphan before judging success.

## Final state — full footer→inline migration run (2026-08-09/10)

Supersedes the "full-site run: 0 new inserts" bullet below. Sequence:

1. User deleted all 134 v2 footers (`--strip-footers`, 42 posts; backups in
   `v3_backups`). One survivor correctly left alone: an ORIGINAL 2013 in-body
   sentence "Baca juga tentang pentingnya Reputasi dalam Bisnis" (dead `.html`
   link) — not a v2 footer.
2. Inline-only re-run: ~6 links → 38 orphans. Alarmed the user.
3. **User correction (decisive)**: cap rule is PER-URL, not per-article. The
   `--max-per-article` guard was removed; `contains_link` (live scan) is the
   only dedup guard. Multiple different targets per post are fine.
4. **Directed-dedup fix**: `tuple(sorted([src,tgt]))` → `(normalize(src),
   normalize(tgt))`. Pending jumped 1 → 60 (hidden REVIEW rows surfaced).
5. **3-tier ladder**: inline rewrite → mid-article "Baca juga" right after the
   best-related paragraph (max para_sim, text ≥ 20 chars, zero LLM cost) →
   end footer. Logged `[inline]` / `[mid]` / `[footer]` per insert.
6. Final full pass: **153 inserted, 45 duplicate-guard hits, 0 failed.**

Final live census (audit: fetch all posts context=edit, count
`https://YOUR_SITE.com/...` hrefs, classify inline vs footer by "Baca juga"
within 150 chars before): **44/45 posts linked, 200 total links (39 inline +
161 mid/footer), 0 duplicate targets, 1 orphan** (`SEO Sekarang Semakin
Sulit?`, 0.51 < 0.55 — excluded by design per user's score rule).

Report tabs: `v3_report` = url, title, internal_links, target_slugs,
has_footer, submitted_at (WIB). User wants the report in BOTH the sheet
(per-post census) and the vault doc (narrative + numbers), with a submitted
timestamp on the sheet.


- Script lint-clean; v3 committed 2 live inline links (5-tips-seo → 5-hal
  score 0.8389 conf 4; 5-cara-promosi → 6-tips score 0.7823 conf 5),
  verified live with balanced HTML; backups in `v3_backups` rows 1–4.
- Bug 5 found + fixed during the apply-diffs test; post 65 restored to
  approved state (6-tips) from backup; post 70 correct as-is.
- Vault doc `seo/semantic-internal-linker-v3.md` written + synced; index.md
  updated (v3 linked under SEO).
- `--strip-footers` built but NOT yet tested live — it's the v2→v3 migration
  tool for the remaining ~40 posts (also enables v3 links on posts whose top
  candidates are exhausted by v2 footers).
- Batch loop + anchor reuse + `--batch-size` shipped and verified (154/155
  anchors reused on re-run); `internal_linker_v3_batch.sh` driver written.
- Full-site run: 0 new inserts (129 duplicate — all good targets already
  linked), 43/45 posts have ≥1 internal link, 138 total (4 inline, 134
  footer), 2 orphans held at REVIEW (both below 0.55 threshold).
- Audit lesson: `candidates` col 2 = to_url, col 3 = to_title — a script
  comparing row[3] against hrefs falsely flagged 43 "false-DONE" rows (0
  actually broken). See SKILL.md "Pitfalls (verification)".
- Redirect audit (2026-08-09): user reported internal links bouncing to
  homepage. Server proven clean (all links 200, correct titles, no soft
  redirect, no JS/service-worker redirect, no nested anchors); cause was
  client-side (browser-cached 301s / extension). Full server-vs-browser
  checklist in SKILL.md "Clicking any internal link redirects to the
  homepage" — incognito test separates the layers in one step.
- Vision for image review still blocked (no `auxiliary.vision.provider`;
  OpenRouter no credit, Nous no auth, Kimi suspended) — see SKILL.md model
  section.
