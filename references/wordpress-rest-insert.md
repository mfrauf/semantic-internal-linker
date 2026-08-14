# WordPress REST auto-insert — debugging narrative & verification recipe

Hit live 2026-08-07 on YOUR_SITE.com (45 posts, Semantic Internal Linker v2).
The two silent-failure modes below cost ~6 debugging iterations because the API
*looks* successful while doing nothing. Recorded here so the next session goes
straight to the fix.

## Symptom timeline

1. First `--insert` run logged "135 inserted" via `wp_update_post` returning 200
   with a bumped `modified_gmt`. Audit of `content.raw` showed only **2 of 45**
   posts actually contained the footer.
2. Root cause A: the post-lookup GET (`posts?slug=X&status=publish,draft`) was
   called WITHOUT `context=edit`, so the response carried `content.rendered`
   (filtered HTML), not `content.raw`. Appending the footer to rendered HTML and
   PUTting it back → WordPress silently discarded it. 200 OK + timestamp bump,
   content reverted on refetch.
3. Fix A: add `&context=edit` to the GET. Retest on one post → still dropped.
4. Root cause B: the `_wp_headers()` helper that worked for authenticated GETs
   omitted `Content-Type: application/json`. WordPress ignores the JSON body
   without it — same silent-200. The earlier manual test that "worked" had set
   that header explicitly.
5. Fix B: add the header. Retest → content persisted (raw len grew, footer
   present in raw, rendered, and public view).

## The rule

- **GET with `context=edit`** → `content.raw` (what you must modify and PUT back).
- **PUT with `Content-Type: application/json`** + `Authorization: Basic`.
- **Verify by re-fetching** — never trust the PUT response or the timestamp.

## Verification recipe (audit after any insert run)

```python
# fetch ALL posts with context=edit
# for each: raw = p["content"]["raw"]
#   count "Baca juga" markers
#   extract hrefs to the site domain, normalize rstrip("/"), detect duplicates
# expected: N posts with footer, 0 duplicate internal links
```

Live result after fixes: 43/45 posts with footer, 136 internal links, 0
duplicates, 0 failures. The 2 without footers were CORRECT behavior (2025 post
below 0.55 auto-threshold → REVIEW; one post had no source candidates ≥0.4).

## Related gotchas hit in the same session

- Sitemap includes the homepage URL → slug becomes `""` → `post not found`.
  Exclude `https://site.com/` when seeding.
- Stale sheet rows: a write that doesn't clear the tab first resurrects deleted
  article rows → duplicate URLs → score-1.0 self-link candidates.
- Application Password string contains spaces — keep them verbatim in .env.
