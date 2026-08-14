#!/usr/bin/env python3
"""
Semantic Internal Linker v2 — full-auto internal linking for WordPress.

Pipeline:
  1. Parse WordPress post sitemap (--seed) or read Google Sheets `articles` tab
  2. Fetch article content (from sheet content, or fetch from URL / WP API)
  3. Embed all articles locally (paraphrase-multilingual-MiniLM-L12-v2)
  4. Cosine similarity matrix → candidate pairs above threshold
  5. DeepSeek anchor text per candidate (--anchor)
  6. Auto-approve pairs >= --auto-threshold, mark rest REVIEW
  7. For approved pairs: INSERT "Baca juga" footer link into the WordPress
     source post (via WP REST API), skip if link already exists, mark DONE
  8. Append new candidates to Google Sheets (existing rows/statuses preserved)

Usage:
  python internal_linker_v2.py \
      --sheet YOUR_SHEET_ID \
      --seed --fetch --anchor --insert --auto-threshold 0.55

Requires in .env:
  WP_SITE_URL, WP_USER, WP_APP_PASSWORD, DEEPSEEK_API_KEY
"""
import argparse
import base64
import json
import os
import re
import sys
import time
from html.parser import HTMLParser
from urllib import request, error

# ---------------------------------------------------------------- Google Sheets
GOOGLE_TOKEN = "google_token.json"
SCOPES = "https://www.googleapis.com/auth/spreadsheets"

def _google_headers():
    import google.auth.transport.requests
    import google.oauth2.credentials
    creds = google.oauth2.credentials.Credentials.from_authorized_user_file(GOOGLE_TOKEN, [SCOPES])
    creds.refresh(google.auth.transport.requests.Request())
    return {"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"}

def _gsheets(path, method="GET", body=None, query=""):
    req = request.Request(
        f"https://sheets.googleapis.com/v4/spreadsheets/{path}{query}",
        headers=_google_headers(), method=method,
        data=json.dumps(body).encode() if body else None)
    with request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())

def read_sheet(sheet_id, tab):
    data = _gsheets(f"{sheet_id}/values/{tab}")
    rows = data.get("values", [])
    if not rows:
        return []
    header = rows[0]
    return [{header[i]: (r[i] if i < len(r) else "") for i in range(len(header))}
            for r in rows[1:]]
def write_sheet(sheet_id, tab, rows):
    """Clear the tab, then write 2D array (header + data).
    Clearing first prevents stale rows below the new data from surviving."""
    _gsheets(f"{sheet_id}/values/{tab}:clear", method="POST", body={})
    body = {"values": rows, "majorDimension": "ROWS"}
    _gsheets(f"{sheet_id}/values/{tab}", method="PUT", body=body,
             query="?valueInputOption=USER_ENTERED")

def tab_exists(sheet_id, tab):
    meta = _gsheets(f"{sheet_id}?fields=sheets.properties(title)")
    return tab in [s["properties"]["title"] for s in meta.get("sheets", [])]

def ensure_tab(sheet_id, tab, headers):
    if tab_exists(sheet_id, tab):
        return False
    _gsheets(f"{sheet_id}:batchUpdate", method="POST", body={
        "requests": [{"addSheet": {"properties": {"title": tab}}}]})
    write_sheet(sheet_id, tab, [headers])
    return True

# ---------------------------------------------------------------- Content fetch
class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0
    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "nav", "footer", "header"):
            self.skip += 1
        if tag in ("p", "h1", "h2", "h3", "li", "br", "div"):
            self.parts.append("\n")
    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "footer", "header") and self.skip:
            self.skip -= 1
    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)

def fetch_text(url, timeout=20):
    req = request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    with request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
    p = _TextExtractor()
    p.feed(raw)
    text = re.sub(r"\n{2,}", "\n", "".join(p.parts))
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()[:12000]

# ---------------------------------------------------------------- WordPress API
def _wp_headers():
    user = os.environ["WP_USER"]
    pw = os.environ["WP_APP_PASSWORD"]
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {token}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Hermes Linker v2)"}

def _wp(path, method="GET", body=None):
    base = os.environ["WP_SITE_URL"].rstrip("/")
    req = request.Request(
        f"{base}/wp-json/wp/v2/{path}",
        headers=_wp_headers(), method=method,
        data=json.dumps(body).encode() if body else None)
    with request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())

def wp_find_post(slug):
    """Find a post by its slug. Returns dict or None.
    Uses context=edit so content.raw is returned (rendered-only content
    cannot be re-saved reliably — WordPress drops it silently)."""
    try:
        r = _wp(f"posts?slug={slug}&status=publish,draft&context=edit")
    except error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    return r[0] if r else None

def wp_post_content(post):
    """Raw HTML content, prefers raw over rendered (strips Gutenberg comments)."""
    raw = post.get("content", {}).get("raw") or post.get("content", {}).get("rendered") or ""
    return raw

def wp_update_post(post_id, content_html):
    return _wp(f"posts/{post_id}", method="POST", body={"content": content_html})

def post_slug_from_url(url):
    slug = url.rstrip("/").split("/")[-1]
    return slug if slug and "." not in slug.split("/")[-1] else ""

# ---------------------------------------------------------------- LLM anchor
def generate_anchor(src_title, src_text, tgt_title, tgt_text, model="deepseek-chat"):
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return None
    prompt = (
        "You are an internal linking assistant for an Indonesian WordPress blog.\n"
        f"SOURCE ARTICLE: {src_title}\n{src_text[:600]}\n\n"
        f"TARGET ARTICLE: {tgt_title}\n{tgt_text[:600]}\n\n"
        'Write ONE natural Indonesian anchor text (2-5 words) that could link from the source '
        'article to the target article. Rules: no keyword stuffing, must read naturally in a '
        'sentence like "baca selengkapnya di <anchor>". Reply with ONLY the anchor text.'
    )
    body = {"model": model, "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.4, "max_tokens": 40}
    req = request.Request(
        "https://api.deepseek.com/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=30) as resp:
            r = json.loads(resp.read())
        return r["choices"][0]["message"]["content"].strip().strip('"')
    except Exception as e:
        print(f"  ⚠ anchor LLM error: {e}", file=sys.stderr)
        return None

# ---------------------------------------------------------------- Insert engine
FOOTER_TEMPLATE = '<p><strong>Baca juga:</strong> <a href="{href}">{anchor}</a></p>'

def contains_link(content_html, target_url):
    """True if content already links to target_url (any href variant)."""
    norm = target_url.rstrip("/")
    for m in re.finditer(r'href=["\']([^"\']+)["\']', content_html):
        h = m.group(1).rstrip("/")
        if h == norm:
            return True
    return False

def insert_footer(content_html, target_url, anchor):
    """Append 'Baca juga' footer block if not already present for this target."""
    if contains_link(content_html, target_url):
        return content_html, "duplicate"
    block = FOOTER_TEMPLATE.format(href=target_url, anchor=anchor)
    # Insert before trailing </body> or closing tags if present; else append
    stripped = content_html.rstrip()
    if stripped.endswith("</p>"):
        return stripped + "\n" + block, "inserted"
    return stripped + "\n" + block, "inserted"

# ---------------------------------------------------------------- Main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet", default="YOUR_SHEET_ID")
    ap.add_argument("--seed", action="store_true", help="seed articles tab from WP post sitemap")
    ap.add_argument("--threshold", type=float, default=0.4, help="similarity floor for candidates")
    ap.add_argument("--auto-threshold", type=float, default=0.55, help="score to auto-approve+insert")
    ap.add_argument("--top-n", type=int, default=5)
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--anchor", action="store_true")
    ap.add_argument("--insert", action="store_true", help="auto-insert approved links into WordPress")
    args = ap.parse_args()

    # Load .env
    env = {}
    with open(".env") as f:
        for line in f:
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    for k in ("WP_SITE_URL", "WP_USER", "WP_APP_PASSWORD", "DEEPSEEK_API_KEY"):
        os.environ.setdefault(k, env.get(k, ""))

    ensure_tab(args.sheet, "articles", ["url", "title", "content", "status"])
    ensure_tab(args.sheet, "candidates",
               ["from_url", "from_title", "to_url", "to_title", "score", "anchor_text", "status"])

    # ---- Seed from sitemap
    if args.seed:
        print("→ Seeding articles from post sitemap...")
        base = os.environ["WP_SITE_URL"].rstrip("/")
        req = request.Request(f"{base}/post-sitemap.xml",
                              headers={"User-Agent": "Mozilla/5.0 (Hermes Linker v2)"})
        with request.urlopen(req, timeout=30) as resp:
            sm = resp.read().decode("utf-8", errors="ignore")
        urls = re.findall(r"<loc>([^<]+)</loc>", sm)
        arts = read_sheet(args.sheet, "articles")
        existing = {a["url"].rstrip("/") for a in arts if a.get("url")}
        rows = [["url", "title", "content", "status"]]
        for a in arts:
            rows.append([a["url"], a.get("title") or "", a.get("content") or "",
                         a.get("status") or ("ready" if a.get("content") else "")])
        added = 0
        for u in urls:
            if u.rstrip("/") in existing:
                continue
            # Title from WP API (cheap) or fallback to slug
            title = ""
            try:
                p = wp_find_post(post_slug_from_url(u))
                if p:
                    title = p.get("title", {}).get("rendered", "")
            except Exception:
                pass
            rows.append([u, title, "", ""])
            added += 1
        write_sheet(args.sheet, "articles", rows)
        print(f"  {len(urls)} URLs in sitemap, {added} new rows added")
        return 0

    # ---- Read articles
    print("→ Reading articles from Google Sheets...")
    arts = [a for a in read_sheet(args.sheet, "articles") if a.get("url")]
    if not arts:
        print("✗ No articles. Run with --seed first, or add rows to the `articles` tab.")
        return 1
    print(f"  {len(arts)} articles loaded")

    # ---- Fetch missing content
    if args.fetch:
        for a in arts:
            if not a.get("content"):
                try:
                    a["content"] = fetch_text(a["url"])
                    a["status"] = "ready"
                except Exception:
                    a["status"] = "error"
                time.sleep(0.2)
        rows = [["url", "title", "content", "status"]]
        for a in arts:
            rows.append([a["url"], a.get("title") or "", a.get("content") or "",
                         a.get("status") or ("ready" if a.get("content") else "")])
        write_sheet(args.sheet, "articles", rows)
        print(f"  ↻ articles tab updated ({len(arts)} rows)")

    # ---- Embed + cosine
    print("→ Embedding articles...")
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
    texts = [(a.get("content") or a.get("title") or a["url"])[:12000] for a in arts]
    emb = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    sim = emb @ emb.T
    print(f"  {emb.shape[0]}×{emb.shape[1]} similarity matrix")

    # ---- Candidates (no self-pairs)
    cand = []
    for i in range(len(arts)):
        scores = sorted(((j, float(sim[i, j])) for j in range(len(arts)) if j != i),
                        key=lambda x: -x[1])
        for j, s in scores[: args.top_n]:
            if s < args.threshold:
                continue
            if arts[i]["url"].rstrip("/") == arts[j]["url"].rstrip("/"):
                continue  # self-link guard
            cand.append({"src": arts[i], "tgt": arts[j], "score": round(s, 4)})
    seen = set(); cand2 = []
    for c in cand:
        key = tuple(sorted([c["src"]["url"], c["tgt"]["url"]]))
        if key in seen:
            continue
        seen.add(key)
        cand2.append(c)
    cand = cand2
    print(f"→ {len(cand)} candidate pairs (threshold {args.threshold})")

    # ---- Anchors
    if args.anchor:
        print("→ Generating anchor text (DeepSeek)...")
        for i, c in enumerate(cand):
            c["anchor"] = generate_anchor(
                c["src"].get("title") or c["src"]["url"], c["src"].get("content") or "",
                c["tgt"].get("title") or c["tgt"]["url"], c["tgt"].get("content") or "")
            print(f"  [{i+1}/{len(cand)}] {c['anchor'] or '—'}")
            time.sleep(0.2)
    else:
        for c in cand:
            c["anchor"] = ""

    # ---- Auto-approve + insert
    HEADERS = ["from_url", "from_title", "to_url", "to_title", "score", "anchor_text", "status"]
    inserted, dupes, failed = 0, 0, 0
    for c in cand:
        if c["score"] >= args.auto_threshold and args.insert:
            c["status"] = "DONE"
            try:
                post = wp_find_post(post_slug_from_url(c["src"]["url"]))
                if not post:
                    c["status"] = "ERROR: post not found"
                    failed += 1
                    continue
                content = wp_post_content(post)
                new_content, result = insert_footer(content, c["tgt"]["url"].rstrip("/"), c["anchor"] or c["tgt"].get("title") or c["tgt"]["url"])
                if result == "duplicate":
                    c["status"] = "DUPLICATE"
                    dupes += 1
                else:
                    wp_update_post(post["id"], new_content)
                    c["status"] = "DONE"
                    inserted += 1
                    print(f"  🔗 inserted → {c['src']['url']} → {c['tgt']['url']}")
                time.sleep(0.5)
            except Exception as e:
                c["status"] = f"ERROR: {str(e)[:60]}"
                failed += 1
                print(f"  ✗ insert failed {c['src']['url']} → {c['tgt']['url']}: {e}")
        else:
            c["status"] = "APPROVED" if c["score"] >= args.auto_threshold else "REVIEW"
    if args.insert:
        print(f"  ↻ insert results: {inserted} inserted, {dupes} duplicate, {failed} failed")

    # ---- Merge write: update statuses in place, append new pairs, preserve user decisions
    existing_rows = read_sheet(args.sheet, "candidates") if tab_exists(args.sheet, "candidates") else []
    if existing_rows and set(existing_rows[0].keys()) == set(HEADERS):
        old_header, old_data = HEADERS, existing_rows[1:]
    elif existing_rows and all(k in existing_rows[0] for k in ("from_url", "to_url")):
        old_header, old_data = HEADERS, existing_rows
    else:
        old_header, old_data = HEADERS, []
    old_data = [r for r in old_data if (r.get("from_url") or "").strip() and (r.get("to_url") or "").strip()]

    # status map from this run: key=(from,to) -> status
    run_status = {}
    for c in cand:
        run_status[(c["src"]["url"].rstrip("/"), c["tgt"]["url"].rstrip("/"))] = c["status"]

    # Valid article URLs this run — drop stale rows referencing removed articles
    valid_urls = {a["url"].rstrip("/") for a in arts if a.get("url")}
    old_data = [r for r in old_data
                if r["from_url"].rstrip("/") in valid_urls and r["to_url"].rstrip("/") in valid_urls]

    # Rewrite: existing rows keep user-set SKIP and terminal states DONE/DUPLICATE;
    # REVIEW and APPROVED are driven by this run (a successful --insert run moves
    # APPROVED→DONE, a re-run retries failed/REVIEW rows).
    USER_LOCKED = {"SKIP", "DONE", "DUPLICATE"}
    rows = [old_header]
    seen_pairs = set()
    for r in old_data:
        key = (r["from_url"].rstrip("/"), r["to_url"].rstrip("/"))
        seen_pairs.add(key)
        status = r.get("status", "").strip() or "REVIEW"
        if status in USER_LOCKED:
            status = r["status"]  # keep user's decision
        elif key in run_status:
            status = run_status[key]
        rows.append([r.get(h, "") for h in HEADERS[:-1]] + [status])

    added = 0
    for c in sorted(cand, key=lambda x: -x["score"]):
        key = (c["src"]["url"].rstrip("/"), c["tgt"]["url"].rstrip("/"))
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        rows.append([c["src"]["url"], c["src"].get("title") or "",
                     c["tgt"]["url"], c["tgt"].get("title") or "",
                     c["score"], c["anchor"], c["status"]])
        added += 1
    write_sheet(args.sheet, "candidates", rows)
    print(f"✓ candidates tab rewritten: {len(old_data)} existing (statuses merged), {added} new")

    print(f"  Sheet: https://docs.google.com/spreadsheets/d/{args.sheet}/edit")
    return 0

if __name__ == "__main__":
    sys.exit(main())
