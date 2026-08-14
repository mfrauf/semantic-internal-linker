#!/usr/bin/env python3
"""
Semantic Internal Linker v3 — inline contextual internal linking for WordPress.

Difference from v2: v2 appends a "Baca juga" FOOTER block; v3 inserts the link
INSIDE an existing paragraph by having an LLM rewrite a single paragraph so the
anchor text reads naturally in context ("kinda little bit rewrite").

Pipeline:
  1. Parse WordPress post sitemap (--seed) or read Google Sheets `articles` tab
  2. Fetch article content (from sheet content, or fetch from URL / WP API)
  3. Embed all articles locally (paraphrase-multilingual-MiniLM-L12-v2)
  4. Cosine similarity matrix -> candidate pairs above threshold
  5. DeepSeek anchor text per candidate (--anchor)
  6. Auto-approve pairs >= --auto-threshold, mark rest REVIEW
  7. For approved pairs: INLINE INSERT via LLM paragraph rewrite:
       a. Split source post content into paragraphs
       b. Embed paragraphs, score against target article embedding
       c. Send top-5 most similar paragraphs + target info to LLM
       d. LLM returns: paragraph_index + replacement text + confidence + preserved links
       e. String-replace that one paragraph in the raw content
       f. Backup original to v3_backups tab, then PUT to WordPress
  8. Merge-write candidates back to Google Sheets (preserve user statuses)

Usage:
  # Dry-run (preview diffs, NO writes to WordPress):
  python internal_linker_v3.py \
      --sheet YOUR_SHEET_ID \
      --inline --dry-run --only "URL1,URL2,URL3,URL4,URL5"

  # Commit (with approval): same command without --dry-run

  # Rollback:
  python internal_linker_v3.py --rollback
  python internal_linker_v3.py --rollback "https://YOUR_SITE.com/slug/"

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
    """Clear the tab, then write 2D array (header + data)."""
    _gsheets(f"{sheet_id}/values/{tab}:clear", method="POST", body={})
    body = {"values": rows, "majorDimension": "ROWS"}
    _gsheets(f"{sheet_id}/values/{tab}", method="PUT", body=body,
             query="?valueInputOption=USER_ENTERED")

def append_sheet(sheet_id, tab, rows):
    """Append rows to a tab without touching existing rows."""
    body = {"values": rows, "majorDimension": "ROWS"}
    _gsheets(f"{sheet_id}/values/{tab}:append", method="POST", body=body,
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
            "User-Agent": "Mozilla/5.0 (Hermes Linker v3)"}

def _wp(path, method="GET", body=None):
    base = os.environ["WP_SITE_URL"].rstrip("/")
    req = request.Request(
        f"{base}/wp-json/wp/v2/{path}",
        headers=_wp_headers(), method=method,
        data=json.dumps(body).encode() if body else None)
    with request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())

def wp_find_post(slug):
    """context=edit is MANDATORY — rendered-only content gets silently dropped on PUT."""
    try:
        r = _wp(f"posts?slug={slug}&status=publish,draft&context=edit")
    except error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    return r[0] if r else None

def wp_post_content(post):
    raw = post.get("content", {}).get("raw") or post.get("content", {}).get("rendered") or ""
    return raw

def wp_update_post(post_id, content_html):
    return _wp(f"posts/{post_id}", method="POST", body={"content": content_html})

def post_slug_from_url(url):
    slug = url.rstrip("/").split("/")[-1]
    return slug if slug and "." not in slug.split("/")[-1] else ""

# ---------------------------------------------------------------- LLM helpers
LLM_MODEL = "deepseek-v4-flash"
LLM_BASE = "https://api.deepseek.com/chat/completions"

def _llm(prompt, max_tokens=500, temperature=0.4, retries=2):
    """deepseek-v4-flash is a reasoning model — it burns tokens on
    reasoning_content first, so max_tokens must cover reasoning + answer.
    On empty content, retry with a bigger budget."""
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY not set")
    body = {"model": LLM_MODEL, "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature, "max_tokens": max_tokens}
    req = request.Request(LLM_BASE, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with request.urlopen(req, timeout=60) as resp:
        r = json.loads(resp.read())
    msg = r["choices"][0]["message"]
    content = (msg.get("content") or "").strip()
    if not content and retries > 0:
        # reasoning model ate the budget — retry with 3x tokens
        return _llm(prompt, max_tokens=max_tokens * 3, temperature=temperature, retries=retries - 1)
    return content

def generate_anchor(src_title, src_text, tgt_title, tgt_text):
    prompt = (
        "You are an internal linking assistant for an Indonesian WordPress blog.\n"
        f"SOURCE ARTICLE: {src_title}\n{src_text[:600]}\n\n"
        f"TARGET ARTICLE: {tgt_title}\n{tgt_text[:600]}\n\n"
        'Write ONE natural Indonesian anchor text (2-5 words) that could link from the source '
        'article to the target article. Rules: no keyword stuffing, must read naturally in a '
        'sentence like "baca selengkapnya di <anchor>". Reply with ONLY the anchor text.'
    )
    try:
        return _llm(prompt, max_tokens=150, temperature=0.4)
    except Exception as e:
        print(f"  ⚠ anchor LLM error: {e}", file=sys.stderr)
        return None

# ---------------------------------------------------------------- Inline insert
FOOTER_TEMPLATE = '<p><strong>Baca juga:</strong> <a href="{href}">{anchor}</a></p>'

def contains_link(content_html, target_url):
    norm = target_url.rstrip("/")
    for m in re.finditer(r'href=["\']([^"\']+)["\']', content_html):
        if m.group(1).rstrip("/") == norm:
            return True
    return False

def _html_to_text(html):
    import html as _h
    t = re.sub(r"<[^>]+>", " ", html)
    t = re.sub(r"\s+", " ", t).strip()
    return _h.unescape(t)

def split_paragraphs(content_html):
    """Split raw WP content into (index, html, text) for paragraph-like chunks.
    Two levels: block tags (p/div/li/h2-h4) first; giant <div> blocks are then
    sub-split on <br /> because Blogger-migrated posts keep one sentence per
    <span>...</span><br /> line inside a single div. Only chunks of 50-500
    chars text are kept (short = headings/sign-offs, long = whole sections)."""
    paras = []
    for m in re.finditer(r"<(p|div|li|h[2-4])(?:\s[^>]*)?>.*?</\1>", content_html, re.DOTALL):
        block, tag = m.group(0), m.group(1)
        text = _html_to_text(block)
        if 50 <= len(text) <= 500:
            paras.append({"html": block, "text": text, "tag": tag})
        elif len(text) > 500 and tag == "div":
            # sub-split giant div on <br /> — each span+br line is a sentence.
            # First collapse empty spacer spans (<span ...><br /></span>) so the
            # br-split doesn't tear them apart and leak stray tags.
            cleaned = re.sub(r"<span[^>]*>\s*<br\s*/?>\s*</span>", "<br />", block)
            for ch in re.split(r"<br\s*/?>", cleaned):
                ct = _html_to_text(ch)
                if 50 <= len(ct) <= 500:
                    paras.append({"html": ch.strip(), "text": ct, "tag": tag})
    return paras

def normalize_url(u):
    return u.rstrip("/").replace("http://", "https://")

def inline_rewrite(src_title, paragraphs, tgt_title, tgt_url, anchor):
    """LLM picks ONE paragraph to rewrite with the inline link.
    Returns (index, replacement_text, confidence) or (None, None, 0) if no fit."""
    tgt_norm = normalize_url(tgt_url)
    paras_txt = "\n\n".join(f"[{i}] {p['text']}" for i, p in enumerate(paragraphs))
    prompt = (
        "You are an editor for an Indonesian WordPress blog.\n"
        f"SOURCE ARTICLE TITLE: {src_title}\n\n"
        f"Candidate paragraphs of the source article (indexed):\n{paras_txt}\n\n"
        f"TARGET ARTICLE: {tgt_title}\nTARGET URL: {tgt_norm}\n"
        f'ANCHOR TEXT (use it verbatim as the link text): "{anchor}"\n\n'
        "TASK: Find the single paragraph where a link to the target reads most naturally. "
        "Rewrite ONLY that paragraph so it contains the link. Rules:\n"
        "1. Keep the paragraph's voice, tone, and meaning; integrate the link naturally "
        "into the existing sentence flow. Do NOT add 'Baca juga' or footer text.\n"
        "2. If a paragraph already contains hyperlinks, PRESERVE them verbatim.\n"
        "3. Only use the anchor text given. Link must point to the TARGET URL exactly.\n"
        "4. If NO paragraph is a natural fit, return confidence 0.\n\n"
        "Reply with ONLY a JSON object, no markdown:\n"
        '{"index": <int>, "replacement": "<full rewritten paragraph text, plain text with '
        f'<a href=\\"{tgt_norm}\\">{anchor}</a> inline>", "confidence": <int 1-5>, '
        '"preserved_links": <bool>}'
    )
    try:
        out = _llm(prompt, max_tokens=700, temperature=0.3)
    except Exception as e:
        print(f"  ⚠ inline LLM error: {e}", file=sys.stderr)
        return None, None, 0
    # Strip code fences if any
    out = re.sub(r"^```(json)?", "", out.strip()).strip()
    out = re.sub(r"```$", "", out.strip()).strip()
    try:
        data = json.loads(out)
    except Exception:
        # try to find JSON object in the text
        m = re.search(r"\{.*\}", out, re.DOTALL)
        if not m:
            print(f"  ⚠ inline LLM unparseable: {out[:200]}", file=sys.stderr)
            return None, None, 0
        try:
            data = json.loads(m.group(0))
        except Exception:
            print(f"  ⚠ inline LLM unparseable JSON: {out[:200]}", file=sys.stderr)
            return None, None, 0
    idx = data.get("index")
    repl = data.get("replacement", "")
    conf = int(data.get("confidence", 0) or 0)
    if idx is None or repl is None:
        return None, None, 0
    # validate: link present and points at target
    if not re.search(r'href=["\']' + re.escape(tgt_norm) + r'["\']', repl):
        print(f"  ⚠ LLM returned replacement without target link: {repl[:120]}", file=sys.stderr)
        return None, None, 0
    return idx, repl, conf

def _extract_span_chain(html):
    """Return (open_chain, close_chain) from the dominant span nesting in html.
    Drops orphan tags (e.g. a leading </span> leaked from a previous line)."""
    opens = re.findall(r"<span[^>]*>", html)
    closes = len(re.findall(r"</span>", html))
    if not opens or closes == 0:
        return None, None
    n = min(len(opens), closes)
    return "".join(opens[:n]), "</span>" * n

def build_new_paragraph(old_html, replacement_text):
    """Wrap replacement text in a span chain matching the original chunk's
    dominant nesting, falling back to the plain block tag. Drops orphan tags
    that Blogger-migrated markup leaks across <br /> boundaries."""
    chain_open, chain_close = _extract_span_chain(old_html)
    # block tag wrapping content: <div>...</div>, <p>...</p>, etc.
    m = re.match(r"^(<(p|div|li|h[2-4])(?:\s[^>]*)?>)(.*)(</\2>)$", old_html, re.DOTALL)
    if m:
        if chain_open:
            return m.group(1) + chain_open + replacement_text + chain_close + m.group(4)
        return m.group(1) + replacement_text + m.group(4)
    if chain_open:
        return chain_open + replacement_text + chain_close
    return replacement_text

# ---------------------------------------------------------------- Rollback
FOOTER_RE = re.compile(
    r"\n?\s*<p><strong>Baca juga:</strong>\s*<a href=[\"'][^\"']+[\"']>[^<]*</a></p>")

def strip_footers(sheet_id, only_urls=None, dry_run=False):
    """Remove all 'Baca juga' footer blocks from posts (backup first unless dry-run).
    Returns (posts_touched, footers_removed)."""
    arts = [a for a in read_sheet(sheet_id, "articles") if a.get("url")]
    touched, removed, backups = 0, 0, []
    for a in arts:
        u = normalize_url(a["url"])
        if only_urls and u not in only_urls:
            continue
        try:
            post = wp_find_post(post_slug_from_url(a["url"]))
            if not post:
                continue
            content = wp_post_content(post)
            if "Baca juga" not in content:
                continue
            new_content, n = FOOTER_RE.subn("", content)
            new_content = re.sub(r"\n{2,}", "\n", new_content).rstrip() + "\n"
            if n == 0:
                continue
            if dry_run:
                print(f"  🔸 {a['url']} — would remove {n} footer(s)")
            else:
                backups.append([post["id"], a["url"], a.get("title") or "",
                                content, time.strftime("%Y-%m-%d %H:%M:%S"), "no"])
                wp_update_post(post["id"], new_content)
                print(f"  🔸 {a['url']} — removed {n} footer(s)")
            touched += 1
            removed += n
            time.sleep(0.4)
        except Exception as e:
            print(f"  ✗ strip failed {a['url']}: {e}", file=sys.stderr)
    if not dry_run and backups:
        append_sheet(sheet_id, "v3_backups", backups)
    return touched, removed

def do_rollback(sheet_id, only_url=None):
    if not tab_exists(sheet_id, "v3_backups"):
        print("✗ no v3_backups tab — nothing to roll back")
        return 1
    rows = read_sheet(sheet_id, "v3_backups")
    if not rows or set(rows[0].keys()) != {"post_id", "url", "title", "original_content", "backup_time", "restored"}:
        # tolerate missing/old headers
        pass
    restored = 0
    for r in rows:
        if r.get("restored") == "yes":
            continue
        if only_url and normalize_url(r.get("url", "")) != normalize_url(only_url):
            continue
        post_id = r.get("post_id")
        orig = r.get("original_content", "")
        if not post_id or not orig:
            continue
        try:
            wp_update_post(post_id, orig)
            r["restored"] = "yes"
            restored += 1
            print(f"  ↺ restored #{post_id} ({r.get('url','')})")
            time.sleep(0.4)
        except Exception as e:
            print(f"  ✗ rollback failed #{post_id}: {e}")
    # write back
    HEAD = ["post_id", "url", "title", "original_content", "backup_time", "restored"]
    out = [HEAD] + [[r.get(h, "") for h in HEAD] for r in rows]
    write_sheet(sheet_id, "v3_backups", out)
    print(f"✓ rollback done ({restored} posts restored)")
    return 0

# ---------------------------------------------------------------- Main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet", default="YOUR_SHEET_ID")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--threshold", type=float, default=0.4)
    ap.add_argument("--auto-threshold", type=float, default=0.55)
    ap.add_argument("--top-n", type=int, default=5)
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--anchor", action="store_true")
    ap.add_argument("--inline", action="store_true", help="v3 inline insert mode")
    ap.add_argument("--dry-run", action="store_true", help="preview diffs, no writes")
    ap.add_argument("--only", default="", help="comma-separated source URLs to process (trial)")
    ap.add_argument("--min-confidence", type=int, default=3)
    ap.add_argument("--rollback", nargs="?", const="__ALL__", default=None,
                    help="restore from v3_backups (optional: specific URL)")
    ap.add_argument("--strip-footers", action="store_true",
                    help="remove 'Baca juga' footer blocks from posts (with backup)")
    ap.add_argument("--export-diffs", action="store_true",
                    help="in dry-run mode, write the inline diffs to a v3_diffs sheet tab for review")
    ap.add_argument("--apply-diffs", action="store_true",
                    help="apply APPROVED rows from the v3_diffs tab to WordPress (after review)")
    ap.add_argument("--batch-size", type=int, default=0,
                    help="process at most N candidates per run (batch loop; 0 = all). "
                         "Sheet is the checkpoint — re-run the same command to continue.")
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

    # Rollback mode
    if args.rollback is not None:
        only = None if args.rollback == "__ALL__" else args.rollback
        return do_rollback(args.sheet, only)

    # Strip-footers mode (migrate v2 footers → v3 inline)
    if args.strip_footers:
        ensure_tab(args.sheet, "v3_backups",
                   ["post_id", "url", "title", "original_content", "backup_time", "restored"])
        only_set = None
        if args.only:
            only_set = {normalize_url(u) for u in args.only.split(",") if u.strip()}
        t, r = strip_footers(args.sheet, only_set, dry_run=args.dry_run)
        print(f"✓ footers: {t} posts touched, {r} footer blocks {'would be ' if args.dry_run else ''}removed")
        return 0

    # Apply-diffs mode: commit APPROVED rows from v3_diffs tab
    if args.apply_diffs:
        DIFF_HEADERS = ["from_url", "to_url", "score", "confidence", "original_text", "rewritten_text", "status"]
        if not tab_exists(args.sheet, "v3_diffs"):
            print("✗ no v3_diffs tab — run --dry-run --export-diffs first")
            return 1
        ensure_tab(args.sheet, "v3_backups",
                   ["post_id", "url", "title", "original_content", "backup_time", "restored"])
        diffs = read_sheet(args.sheet, "v3_diffs")
        applied, skipped = 0, 0
        backups = []
        for d in diffs:
            if d.get("status", "").strip() != "APPROVED":
                continue
            src, tgt = d.get("from_url", ""), d.get("to_url", "")
            rewritten = d.get("rewritten_text", "")
            if not src or not tgt or not rewritten:
                skipped += 1
                continue
            try:
                post = wp_find_post(post_slug_from_url(src))
                if not post:
                    print(f"  ✗ post not found: {src}")
                    skipped += 1
                    continue
                content = wp_post_content(post)
                if contains_link(content, tgt):
                    print(f"  ✗ already linked: {src} → {tgt}")
                    skipped += 1
                    continue
                # find the original paragraph by its text prefix and replace it
                orig = d.get("original_text", "")
                # hard guard: never reduce the post's internal-link count
                def _link_count(html):
                    return len(re.findall(r'href="https://YOUR_SITE.com/[^"]+"', html))
                before = _link_count(content)
                if orig and orig.strip() in content:
                    new_content = content.replace(orig.strip(), rewritten, 1)
                elif orig and _html_to_text(orig) in _html_to_text(content):
                    # fall back to replacing by normalized text
                    new_content = content.replace(_html_to_text(orig), _html_to_text(rewritten), 1)
                else:
                    print(f"  ✗ original paragraph not found: {src}")
                    skipped += 1
                    continue
                if new_content == content:
                    print(f"  ✗ no-op replace: {src}")
                    skipped += 1
                    continue
                if _link_count(new_content) < before:
                    print(f"  ✗ REFUSED: replace would drop {before - _link_count(new_content)} existing link(s): {src}")
                    skipped += 1
                    continue
                backups.append([post["id"], src, d.get("from_title", ""),
                                content, time.strftime("%Y-%m-%d %H:%M:%S"), "no"])
                wp_update_post(post["id"], new_content)
                applied += 1
                print(f"  🔗 applied → {src} → {tgt}")
                time.sleep(0.5)
            except Exception as e:
                print(f"  ✗ apply failed {src}: {e}")
                skipped += 1
        if backups:
            append_sheet(args.sheet, "v3_backups", backups)
            print(f"  🗄 {len(backups)} original contents backed up")
        # mark applied rows DONE
        rows = [DIFF_HEADERS]
        for d in diffs:
            status = d.get("status", "").strip() or "REVIEW"
            if status == "APPROVED":
                # check if actually applied (from_url in backups)
                if any(b[1].rstrip("/") == d.get("from_url", "").rstrip("/") for b in backups):
                    status = "DONE"
            rows.append([d.get(h, "") for h in DIFF_HEADERS[:-1]] + [status])
        write_sheet(args.sheet, "v3_diffs", rows)
        print(f"✓ v3_diffs updated: {applied} applied, {skipped} skipped")
        return 0

    ensure_tab(args.sheet, "articles", ["url", "title", "content", "status"])
    ensure_tab(args.sheet, "candidates",
               ["from_url", "from_title", "to_url", "to_title", "score", "anchor_text", "status"])
    if args.inline and not args.dry_run:
        ensure_tab(args.sheet, "v3_backups",
                   ["post_id", "url", "title", "original_content", "backup_time", "restored"])

    # ---- Seed from sitemap
    if args.seed:
        print("→ Seeding articles from post sitemap...")
        base = os.environ["WP_SITE_URL"].rstrip("/")
        req = request.Request(f"{base}/post-sitemap.xml",
                              headers={"User-Agent": "Mozilla/5.0 (Hermes Linker v3)"})
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

    # ---- Restrict to --only list (trial)
    only_set = None
    if args.only:
        only_set = {normalize_url(u) for u in args.only.split(",") if u.strip()}
        picked = [a for a in arts if normalize_url(a["url"]) in only_set]
        print(f"  trial mode: {len(picked)} source posts selected from --only")
        arts_sources = picked
    else:
        arts_sources = arts

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

    # ---- Candidates (no self-pairs, source restricted to arts_sources)
    cand = []
    src_indexes = {id(a): i for i, a in enumerate(arts)}
    for i, a in enumerate(arts):
        if a not in arts_sources:
            continue
        scores = sorted(((j, float(sim[i, j])) for j in range(len(arts)) if j != i),
                        key=lambda x: -x[1])
        for j, s in scores[: args.top_n]:
            if s < args.threshold:
                continue
            if normalize_url(arts[i]["url"]) == normalize_url(arts[j]["url"]):
                continue
            cand.append({"src": arts[i], "tgt": arts[j], "score": round(s, 4)})
    seen = set(); cand2 = []
    for c in cand:
        # dedup by DIRECTED key: (src, tgt). Using a sorted/undirected key
        # reversed the pair (alphabetical order won), which made sheet statuses
        # (REVIEW on one direction, DUPLICATE on the other) unreachable.
        key = (normalize_url(c["src"]["url"]), normalize_url(c["tgt"]["url"]))
        if key in seen:
            continue
        seen.add(key)
        cand2.append(c)
    cand = cand2
    print(f"→ {len(cand)} candidate pairs (threshold {args.threshold}, sources restricted to {len(arts_sources)})")

    # ---- Reuse existing anchors + statuses from the sheet (batch-loop checkpoint)
    existing_cands = read_sheet(args.sheet, "candidates") if tab_exists(args.sheet, "candidates") else []
    known_anchors = {}
    terminal_pairs = set()  # pairs already at a terminal state — never reprocess
    for r in existing_cands:
        if r.get("from_url") and r.get("to_url"):
            key = (normalize_url(r["from_url"]), normalize_url(r["to_url"]))
            if r.get("anchor_text"):
                known_anchors[key] = r["anchor_text"]
            status = (r.get("status") or "").strip().upper()
            if status in ("DONE", "DUPLICATE", "ERROR") or status.startswith("SKIP"):
                terminal_pairs.add(key)
    reused = 0
    for c in cand:
        key = (normalize_url(c["src"]["url"]), normalize_url(c["tgt"]["url"]))
        if key in known_anchors:
            c["anchor"] = known_anchors[key]
            c["anchor_reused"] = True
            reused += 1
        c["terminal"] = key in terminal_pairs
    if reused:
        print(f"  ↻ reused {reused} anchors from sheet (no LLM call)")

    # ---- Batch loop: limit work to the first N candidates needing attention
    if args.batch_size > 0:
        # A candidate needs work only if it is BOTH auto-insert-eligible
        # (score >= auto_threshold) AND not already at a terminal state.
        # Below-threshold pairs are REVIEW-for-human, never auto-inserted,
        # so in bypass mode they count as done (prevents infinite loop).
        todo = [c for c in cand
                if c["score"] >= args.auto_threshold and not c.get("terminal")]
        cand = todo[: args.batch_size]
        print(f"  ↻ batch mode: processing {len(cand)} of {len(todo)} pending candidates (--batch-size {args.batch_size})")

    # ---- Anchors
    if args.anchor:
        print("→ Generating anchor text (deepseek-v4-flash)...")
        for i, c in enumerate(cand):
            c["anchor"] = generate_anchor(
                c["src"].get("title") or c["src"]["url"], c["src"].get("content") or "",
                c["tgt"].get("title") or c["tgt"]["url"], c["tgt"].get("content") or "")
            print(f"  [{i+1}/{len(cand)}] {c['anchor'] or '—'}")
            time.sleep(0.2)
    else:
        for c in cand:
            c["anchor"] = ""

    # ---- Auto-approve + inline insert
    HEADERS = ["from_url", "from_title", "to_url", "to_title", "score", "anchor_text", "status"]
    DIFF_HEADERS = ["from_url", "to_url", "score", "confidence", "original_text", "rewritten_text", "status"]
    inserted, dupes, failed, skipped = 0, 0, 0, 0
    backups = []
    diffs = []  # collected in dry-run mode for --export-diffs

    for c in cand:
        if c["score"] >= args.auto_threshold and args.inline:
            src_norm = normalize_url(c["src"]["url"])
            try:
                post = wp_find_post(post_slug_from_url(c["src"]["url"]))
                if not post:
                    c["status"] = "ERROR: post not found"
                    failed += 1
                    continue
                content = wp_post_content(post)
                if contains_link(content, c["tgt"]["url"]):
                    c["status"] = "DUPLICATE"
                    dupes += 1
                    continue

                # split paragraphs
                paras = split_paragraphs(content)
                target_title = c["tgt"].get("title") or c["tgt"]["url"]
                anchor = c["anchor"] or target_title

                # score paragraphs vs target article embedding (used by tiers 1+2)
                para_sim = None
                if paras:
                    para_texts = [p["text"] for p in paras]
                    para_emb = model.encode(para_texts, normalize_embeddings=True, show_progress_bar=False)
                    tgt_idx = src_indexes[id(c["tgt"])]
                    tgt_emb = emb[tgt_idx][None, :]
                    para_sim = (para_emb @ tgt_emb.T).flatten()

                # ===== TIER 1: inline rewrite (natural contextual link) =====
                tier = "inline"
                new_html = None
                old_html = None
                if paras:
                    top = sorted(range(len(paras)), key=lambda k: -para_sim[k])[:5]
                    # guard paragraph length 50-500 chars
                    top = [k for k in top if 50 <= len(paras[k]["text"]) <= 500]
                    # NEVER rewrite a paragraph that already contains an internal
                    # link — the LLM rewrite would silently drop it (link loss bug).
                    top = [k for k in top if "YOUR_SITE.com" not in paras[k]["html"]]
                    if top:
                        sub = [{"text": paras[k]["text"], "orig_idx": k} for k in top]
                        idx, repl, conf = inline_rewrite(
                            c["src"].get("title") or c["src"]["url"], sub,
                            target_title, c["tgt"]["url"], anchor)
                        if idx is not None and conf >= args.min_confidence:
                            orig_idx = sub[idx]["orig_idx"]
                            old_html = paras[orig_idx]["html"]
                            new_html = build_new_paragraph(old_html, repl)
                            tier = "inline"
                        else:
                            print(f"  ↳ tier1 no natural fit (conf {conf}) — falling to tier2")
                            tier = "mid"

                # ===== TIER 2: mid-article 'Baca juga' after best-related paragraph =====
                if new_html is None and paras and para_sim is not None:
                    # pick the single paragraph most related to the target,
                    # any reasonable length, that has text worth anchoring after
                    best = max(range(len(paras)),
                               key=lambda k: para_sim[k] if len(paras[k]["text"]) >= 20 else -1)
                    if para_sim[best] > 0 and len(paras[best]["text"]) >= 20:
                        old_html = paras[best]["html"]
                        block = FOOTER_TEMPLATE.format(href=c["tgt"]["url"].rstrip("/"), anchor=anchor)
                        # insert block right after that paragraph
                        new_html = old_html + "\n" + block
                        tier = "mid"
                    else:
                        tier = "footer"

                # ===== TIER 3: end-of-article footer =====
                if new_html is None:
                    block = FOOTER_TEMPLATE.format(href=c["tgt"]["url"].rstrip("/"), anchor=anchor)
                    stripped = content.rstrip()
                    new_html = stripped + ("\n" if not stripped.endswith("\n") else "") + block
                    old_html = None  # append mode
                    tier = "footer"

                # dry-run: show diff, don't write
                if args.dry_run:
                    print(f"\n  📄 [{tier}] {c['src']['url']} → {c['tgt']['url']} (score {c['score']})")
                    if old_html:
                        print(f"  - {old_html[:200]}")
                    print(f"  + {new_html[:250]}")
                    c["status"] = "REVIEW"
                    diffs.append([c["src"]["url"], c["tgt"]["url"], c["score"],
                                  5 if tier == "inline" else 3,
                                  old_html or "", new_html, "REVIEW"])
                    continue

                # commit: backup + PUT
                backups.append([post["id"], c["src"]["url"], c["src"].get("title") or "",
                                content, time.strftime("%Y-%m-%d %H:%M:%S"), "no"])
                if old_html:
                    new_content = content.replace(old_html, new_html, 1)
                else:
                    new_content = new_html
                if new_content == content:
                    c["status"] = "ERROR: replace no-op"
                    failed += 1
                    continue
                wp_update_post(post["id"], new_content)
                c["status"] = "DONE"
                inserted += 1
                print(f"  🔗 [{tier}] inserted → {c['src']['url']} → {c['tgt']['url']}")
                time.sleep(0.5)
            except Exception as e:
                c["status"] = f"ERROR: {str(e)[:60]}"
                failed += 1
                print(f"  ✗ insert failed {c['src']['url']} → {c['tgt']['url']}: {e}")
        else:
            c["status"] = "APPROVED" if c["score"] >= args.auto_threshold else "REVIEW"

    if args.inline and not args.dry_run:
        print(f"  ↻ insert results: {inserted} inserted, {dupes} duplicate, {skipped} skipped, {failed} failed")
        if backups:
            append_sheet(args.sheet, "v3_backups", backups)
            print(f"  🗄 {len(backups)} original contents backed up to v3_backups tab")

    # ---- Export dry-run diffs to sheet for review (scales to hundreds of articles)
    if args.dry_run and args.export_diffs:
        ensure_tab(args.sheet, "v3_diffs", DIFF_HEADERS)
        old_diffs = read_sheet(args.sheet, "v3_diffs") if tab_exists(args.sheet, "v3_diffs") else []
        # keep user decisions (APPROVED/DONE/SKIP) from previous exports
        kept = {}
        for r in old_diffs:
            if r.get("status") in ("APPROVED", "DONE", "SKIP"):
                kept[(r.get("from_url", "").rstrip("/"), r.get("to_url", "").rstrip("/"))] = r
        rows = [DIFF_HEADERS]
        seen = set()
        for d in diffs:
            key = (d[0].rstrip("/"), d[1].rstrip("/"))
            seen.add(key)
            if key in kept:
                k = kept[key]
                rows.append([k.get("from_url", ""), k.get("to_url", ""), k.get("score", ""),
                             k.get("confidence", ""), k.get("original_text", ""),
                             k.get("rewritten_text", ""), k.get("status", "REVIEW")])
            else:
                rows.append(d)
        # stale rows (pairs no longer candidates) keep their status
        for key, k in kept.items():
            if key not in seen:
                rows.append([k.get("from_url", ""), k.get("to_url", ""), k.get("score", ""),
                             k.get("confidence", ""), k.get("original_text", ""),
                             k.get("rewritten_text", ""), k.get("status", "REVIEW")])
        write_sheet(args.sheet, "v3_diffs", rows)
        print(f"✓ v3_diffs tab updated: {len(diffs)} new diffs, {len(kept)} preserved user decisions")

    # ---- Merge write: update statuses in place, append new pairs, preserve user decisions
    if not args.dry_run:
        existing_rows = read_sheet(args.sheet, "candidates") if tab_exists(args.sheet, "candidates") else []
        if existing_rows and set(existing_rows[0].keys()) == set(HEADERS):
            old_header, old_data = HEADERS, existing_rows[1:]
        elif existing_rows and all(k in existing_rows[0] for k in ("from_url", "to_url")):
            old_header, old_data = HEADERS, existing_rows
        else:
            old_header, old_data = HEADERS, []
        old_data = [r for r in old_data if (r.get("from_url") or "").strip() and (r.get("to_url") or "").strip()]

        run_status = {}
        for c in cand:
            run_status[(normalize_url(c["src"]["url"]), normalize_url(c["tgt"]["url"]))] = c["status"]

        valid_urls = {normalize_url(a["url"]) for a in arts if a.get("url")}
        old_data = [r for r in old_data
                    if normalize_url(r["from_url"]) in valid_urls and normalize_url(r["to_url"]) in valid_urls]

        USER_LOCKED = {"SKIP", "DONE", "DUPLICATE"}
        rows = [old_header]
        seen_pairs = set()
        for r in old_data:
            key = (normalize_url(r["from_url"]), normalize_url(r["to_url"]))
            seen_pairs.add(key)
            status = r.get("status", "").strip() or "REVIEW"
            if status in USER_LOCKED:
                status = r["status"]
            elif key in run_status:
                status = run_status[key]
            rows.append([r.get(h, "") for h in HEADERS[:-1]] + [status])

        added = 0
        for c in sorted(cand, key=lambda x: -x["score"]):
            key = (normalize_url(c["src"]["url"]), normalize_url(c["tgt"]["url"]))
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
    else:
        print("\n⚠ DRY RUN — no writes to WordPress or Sheets. Review diffs above, then re-run without --dry-run.")

    return 0

if __name__ == "__main__":
    sys.exit(main())
