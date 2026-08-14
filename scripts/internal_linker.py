#!/usr/bin/env python3
"""
Internal Linker — semantic similarity engine for article internal linking.

Pipeline:
  1. Read articles from Google Sheets tab `articles` (url, title, content?, status)
  2. Fetch missing content from URLs (optional, --fetch)
  3. Embed all articles with sentence-transformers (multilingual, local, free)
  4. Cosine similarity matrix → candidate pairs above threshold
  5. Exclude already-existing links (optional, --check-existing)
  6. LLM anchor text generation per candidate (DeepSeek, optional, --anchor)
  7. Write candidates to Google Sheets tab `candidates`

Usage:
  python internal_linker.py \
      --sheet YOUR_SHEET_ID_V1 \
      --threshold 0.45 --top-n 5 --anchor

Requires: google_token.json (Google OAuth), DEEPSEEK_API_KEY in .env
"""
import argparse
import json
import os
import re
import sys
import time
from html.parser import HTMLParser
from urllib import request, error

import numpy as np

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

def read_sheet(sheet_id, tab="articles"):
    """Return list of dicts from the tab's header row + data rows."""
    data = _gsheets(f"{sheet_id}/values/{tab}")
    rows = data.get("values", [])
    if not rows:
        return []
    header = rows[0]
    out = []
    for r in rows[1:]:
        out.append({header[i]: (r[i] if i < len(r) else "") for i in range(len(header))})
    return out

def write_sheet(sheet_id, tab, rows):
    """Write 2D array (header + data) to a tab."""
    body = {"values": rows, "majorDimension": "ROWS"}
    _gsheets(f"{sheet_id}/values/{tab}", method="PUT", body=body,
             query="?valueInputOption=USER_ENTERED")

def tab_exists(sheet_id, tab):
    meta = _gsheets(f"{sheet_id}?fields=sheets.properties(title)")
    return tab in [s["properties"]["title"] for s in meta.get("sheets", [])]

def ensure_tab(sheet_id, tab, headers):
    """Create tab + header row if missing. Returns True if created."""
    meta = _gsheets(f"{sheet_id}?fields=sheets.properties(title)")
    titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if tab in titles:
        return False
    _gsheets(f"{sheet_id}:batchUpdate", method="POST", body={
        "requests": [{"addSheet": {"properties": {"title": tab}}}]
    })
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

def fetch_links(url, timeout=20):
    """Extract all internal hrefs from a page (for existing-link exclusion)."""
    req = request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
    base = url.rstrip("/")
    hrefs = set()
    for m in re.finditer(r'href=["\']([^"\'#]+)["\']', raw):
        h = m.group(1)
        if h.startswith("/"):
            h = base + h
        if h.startswith("http") and base.split("//")[1].split("/")[0] in h:
            hrefs.add(h.rstrip("/"))
    return hrefs

# ---------------------------------------------------------------- LLM anchor
def generate_anchor(src_title, src_text, tgt_title, tgt_text, model="deepseek-chat"):
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return None
    prompt = (
        "You are an internal linking assistant for an Indonesian content site.\n"
        f"SOURCE ARTICLE: {src_title}\n{src_text[:600]}\n\n"
        f"TARGET ARTICLE: {tgt_title}\n{tgt_text[:600]}\n\n"
        'Write ONE natural Indonesian anchor text (2-5 words) that could link from the source '
        'article to the target article. Rules: no keyword stuffing, must read naturally in a '
        'sentence like "baca selengkapnya di <anchor>". Reply with ONLY the anchor text.'
    )
    body = {
        "model": model, "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.4, "max_tokens": 40,
    }
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

# ---------------------------------------------------------------- Main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet", default="YOUR_SHEET_ID_V1")
    ap.add_argument("--threshold", type=float, default=0.45)
    ap.add_argument("--top-n", type=int, default=5, help="max candidate targets per article")
    ap.add_argument("--fetch", action="store_true", help="fetch content for rows missing content")
    ap.add_argument("--check-existing", action="store_true", help="fetch pages to exclude existing links")
    ap.add_argument("--anchor", action="store_true", help="generate LLM anchor text per candidate")
    args = ap.parse_args()

    # Load env keys
    env = {}
    with open(".env") as f:
        for line in f:
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    os.environ.setdefault("DEEPSEEK_API_KEY", env.get("DEEPSEEK_API_KEY", ""))

    print("→ Reading articles from Google Sheets...")
    ensure_tab(args.sheet, "articles", ["url", "title", "content", "status"])
    ensure_tab(args.sheet, "candidates",
               ["from_url", "from_title", "to_url", "to_title", "score", "anchor_text", "status"])
    arts = [a for a in read_sheet(args.sheet) if a.get("url")]
    if not arts:
        print("✗ No articles in the `articles` tab. Add url/title rows first.")
        return 1
    print(f"  {len(arts)} articles loaded")

    # Fetch missing content
    if args.fetch:
        for i, a in enumerate(arts):
            if not a.get("content"):
                try:
                    a["content"] = fetch_text(a["url"])
                    a["status"] = "ready"
                    print(f"  ✓ fetched {a['url']} ({len(a['content'])} chars)")
                except Exception as e:
                    a["status"] = "error"
                    print(f"  ✗ fetch failed {a['url']}: {e}")
                time.sleep(0.3)

    # Persist fetched content + status back to the `articles` tab (source of truth)
    # so the sheet is never left blank and re-runs skip already-fetched rows.
    persisted = False
    if args.fetch:
        rows = [["url", "title", "content", "status"]]
        for a in arts:
            status = a.get("status") or ("ready" if a.get("content") else "")
            rows.append([a["url"], a.get("title") or "", a.get("content") or "", status])
        write_sheet(args.sheet, "articles", rows)
        persisted = True
        print(f"  ↻ articles tab updated ({len(arts)} rows, content+status persisted)")

    # Existing links map
    existing = {}
    if args.check_existing:
        for a in arts:
            try:
                existing[a["url"].rstrip("/")] = fetch_links(a["url"])
            except Exception:
                existing[a["url"].rstrip("/")] = set()
        print(f"  existing-link map for {len(existing)} pages")

    # Embed
    print("→ Embedding articles (paraphrase-multilingual-MiniLM-L12-v2)...")
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
    texts = [(a.get("content") or a.get("title") or a["url"])[:12000] for a in arts]
    emb = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    sim = emb @ emb.T
    print(f"  {emb.shape[0]}×{emb.shape[1]} similarity matrix computed")

    # Candidates
    cand = []
    for i in range(len(arts)):
        scores = [(j, float(sim[i, j])) for j in range(len(arts)) if j != i]
        scores.sort(key=lambda x: -x[1])
        for j, s in scores[: args.top_n]:
            if s < args.threshold:
                continue
            src, tgt = arts[i], arts[j]
            if args.check_existing:
                if src["url"].rstrip("/") in existing and tgt["url"].rstrip("/") in existing[src["url"].rstrip("/")]:
                    continue
            cand.append({"src": src, "tgt": tgt, "score": round(s, 4)})

    # Dedupe: keep the higher-score direction when A→B and B→A both qualify
    seen = set(); deduped = []
    for c in cand:
        key = tuple(sorted([c["src"]["url"], c["tgt"]["url"]]))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)
    cand = deduped

    print(f"→ {len(cand)} candidate pairs after threshold {args.threshold}")

    # Anchor text
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

    # Write candidates — append-only, robust:
    #   - keep the header + ALL existing rows (statuses intact)
    #   - add only pairs that are not already present
    HEADERS = ["from_url", "from_title", "to_url", "to_title", "score", "anchor_text", "status"]
    existing_rows = read_sheet(args.sheet, "candidates") if tab_exists(args.sheet, "candidates") else []
    if existing_rows and set(existing_rows[0].keys()) == set(HEADERS):
        # first dict is the header row — keep it, rest are data
        old_header, old_data = HEADERS, existing_rows[1:]
    elif existing_rows and all(k in existing_rows[0] for k in ("from_url", "to_url")):
        # header missing/corrupted — rebuild it, treat all as data
        old_header, old_data = HEADERS, existing_rows
    else:
        old_header, old_data = HEADERS, []

    seen_pairs = set()
    for r in old_data:
        if r.get("from_url") and r.get("to_url"):
            seen_pairs.add((r["from_url"].rstrip("/"), r["to_url"].rstrip("/")))

    new_cands = [c for c in cand
                 if (c["src"]["url"].rstrip("/"), c["tgt"]["url"].rstrip("/")) not in seen_pairs]
    if not new_cands:
        print("✓ No new candidate pairs (all already in the sheet)")
        return 0

    rows = [old_header]
    for r in old_data:  # preserve every existing row verbatim
        rows.append([r.get(h, "") for h in HEADERS])
    for c in sorted(new_cands, key=lambda x: -x["score"]):
        rows.append([c["src"]["url"], c["src"].get("title") or "",
                     c["tgt"]["url"], c["tgt"].get("title") or "",
                     c["score"], c["anchor"], "REVIEW"])
    write_sheet(args.sheet, "candidates", rows)
    print(f"✓ {len(new_cands)} NEW candidates appended (existing pairs preserved)")
    print(f"  Sheet: https://docs.google.com/spreadsheets/d/{args.sheet}/edit")
    return 0

if __name__ == "__main__":
    sys.exit(main())
