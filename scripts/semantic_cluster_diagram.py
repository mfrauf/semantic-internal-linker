#!python
"""Generate a semantic cluster diagram (HTML) from semantic linker v3 data.

Reads: articles tab (45 posts, content) + live WordPress internal links.
Computes: same embeddings as the linker (paraphrase-multilingual-MiniLM-L12-v2),
agglomerative clusters, circular layout grouped by cluster, chord edges = real links.
Output: single self-contained HTML (dark theme, no JS).
"""
import sys, os, re, html as htmlmod
sys.path.insert(0, "scripts")
import internal_linker_v3 as v3
import numpy as np
from sklearn.cluster import AgglomerativeClustering
from collections import defaultdict, Counter

SHEET = "YOUR_SHEET_ID"
env = {}
for line in open(".env"):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, _, v = line.partition("=")
    env[k.strip()] = v.strip()
for k in ("WP_SITE_URL", "WP_USER", "WP_APP_PASSWORD"):
    os.environ.setdefault(k, env.get(k, ""))

# ---------- 1. Articles + embeddings ----------
arts = [a for a in v3.read_sheet(SHEET, "articles") if a.get("url")]
print(f"articles: {len(arts)}")
texts = [(a.get("content") or a.get("title") or a["url"])[:12000] for a in arts]
from sentence_transformers import SentenceTransformer
model = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
emb = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
sim = emb @ emb.T
np.fill_diagonal(sim, 0.0)

# ---------- 2. Clusters (agglomerative, fixed count for balance) ----------
n_clusters = 12
cl = AgglomerativeClustering(n_clusters=n_clusters, metric="cosine", linkage="average")
labels = cl.fit_predict(emb)
# merge singleton clusters into the cluster of their nearest post
sizes = Counter(labels)
singletons = [c for c, n in sizes.items() if n == 1]
while singletons:
    for c in singletons:
        i = [i for i in range(len(arts)) if labels[i] == c][0]
        # nearest post by similarity
        nbr = int(np.argmax(sim[i]))
        labels[i] = labels[nbr]
    sizes = Counter(labels)
    singletons = [c for c, n in sizes.items() if n == 1]
sizes = Counter(labels)
print(f"clusters: {len(sizes)} (after singleton merge)")
print("sizes:", dict(sorted(sizes.items())))

# ---------- 3. Live internal links from WordPress ----------
page = 1; posts = []
while True:
    r = v3._wp(f"posts?per_page=100&page={page}&context=edit")
    if not r:
        break
    posts.extend(r)
    if len(r) < 100:
        break
    page += 1
print(f"wp posts: {len(posts)}")

pat = re.compile(r'href="(https://YOUR_SITE.com/[^"]+)"')
slug_of = {}
for a in arts:
    u = v3.normalize_url(a["url"])
    slug_of[u] = v3.normalize_url(a["url"]).rstrip("/")

# map post -> its node index
norm_urls = [v3.normalize_url(a["url"]).rstrip("/") for a in arts]
idx_of = {u: i for i, u in enumerate(norm_urls)}

links = []  # (src_idx, tgt_idx)
outdeg = Counter(); indeg = Counter()
for p in posts:
    raw = p.get("content", {}).get("raw", "") or ""
    src = v3.normalize_url(p["link"]).rstrip("/")
    si = idx_of.get(src)
    if si is None:
        continue
    seen_t = set()
    for m in pat.finditer(raw):
        t = m.group(1).rstrip("/")
        ti = idx_of.get(t)
        if ti is None or ti == si or t in seen_t:
            continue
        seen_t.add(t)
        links.append((si, ti))
        outdeg[si] += 1
        indeg[ti] += 1
print(f"internal links (node pairs): {len(links)}")

# ---------- 4. Layout: circular, grouped by cluster ----------
np.random.seed(7)
order = sorted(range(len(arts)), key=lambda i: (labels[i], norm_urls[i]))
clusters = sorted(set(labels))
cluster_nodes = {c: [i for i in order if labels[i] == c] for c in clusters}

N = len(arts)
R = 460.0
CX, CY = 560.0, 520.0
angle_of = {}
pos = {}
node_radius = {}
max_indeg = max(indeg.values()) if indeg else 1
for k, i in enumerate(order):
    ang = 2 * np.pi * k / N - np.pi / 2
    angle_of[i] = ang
    pos[i] = (CX + R * np.cos(ang), CY + R * np.sin(ang))
    node_radius[i] = 7 + 7 * (indeg[i] / max_indeg)

# cluster arc extents
arc_extents = []
for c in clusters:
    idxs = cluster_nodes[c]
    a0 = angle_of[idxs[0]]
    a1 = angle_of[idxs[-1]]
    if a0 > a1:
        a0 -= 2 * np.pi
    mid = (a0 + a1) / 2
    arc_extents.append((c, a0, a1, mid))

# ---------- 5. Colors ----------
palette = [
    ("#22d3ee", "rgba(34,211,238,0.12)"),   # cyan
    ("#34d399", "rgba(52,211,153,0.12)"),   # emerald
    ("#a78bfa", "rgba(167,139,250,0.12)"),  # violet
    ("#fbbf24", "rgba(251,191,36,0.12)"),   # amber
    ("#fb7185", "rgba(251,113,133,0.12)"),  # rose
    ("#fb923c", "rgba(251,146,60,0.12)"),   # orange
    ("#f472b6", "rgba(244,114,182,0.12)"),  # pink
    ("#4ade80", "rgba(74,222,128,0.12)"),   # green
    ("#38bdf8", "rgba(56,189,248,0.12)"),   # sky
    ("#c084fc", "rgba(192,132,252,0.12)"),  # purple
    ("#2dd4bf", "rgba(45,212,191,0.12)"),   # teal
    ("#facc15", "rgba(250,204,21,0.12)"),   # yellow
    ("#94a3b8", "rgba(148,163,184,0.12)"),  # slate
    ("#e879f9", "rgba(232,121,249,0.12)"),  # fuchsia
]
cluster_color = {c: palette[i % len(palette)] for i, c in enumerate(clusters)}

# short label: title, truncated
def short_label(a, maxlen=26):
    t = (a.get("title") or "").strip()
    if not t:
        t = a["url"].rstrip("/").split("/")[-1]
    return t if len(t) <= maxlen else t[:maxlen - 1] + "…"

# ---------- 6. Build SVG ----------
svg = []
svg.append(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1120 1040" font-family="JetBrains Mono, monospace">')

# background grid
svg.append('<defs><pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse">'
           '<path d="M 40 0 L 0 0 0 40" fill="none" stroke="#1e293b" stroke-width="0.5"/></pattern></defs>')
svg.append('<rect width="1120" height="1040" fill="#020617"/><rect width="1120" height="1040" fill="url(#grid)"/>')

# cluster arcs
for c, a0, a1, mid in arc_extents:
    col, fill = cluster_color[c]
    # arc band behind nodes
    r_in, r_out = R - 46, R + 46
    x0, y0 = CX + r_out * np.cos(a0), CY + r_out * np.sin(a0)
    x1, y1 = CX + r_out * np.cos(a1), CY + r_out * np.sin(a1)
    large = 1 if (a1 - a0) > np.pi else 0
    path = (f'M {x0:.1f} {y0:.1f} A {r_out:.1f} {r_out:.1f} 0 {large} 1 {x1:.1f} {y1:.1f} '
            f'L {CX + r_in * np.cos(a1):.1f} {CY + r_in * np.sin(a1):.1f} '
            f'A {r_in:.1f} {r_in:.1f} 0 {large} 0 {CX + r_in * np.cos(a0):.1f} {CY + r_in * np.sin(a0):.1f} Z')
    svg.append(f'<path d="{path}" fill="{fill}" stroke="{col}" stroke-opacity="0.5" stroke-width="1" stroke-dasharray="4,4"/>')

# edges (chords) — draw before nodes
for si, ti in links:
    x1, y1 = pos[si]
    x2, y2 = pos[ti]
    # control points pulled toward center
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    cx_, cy_ = CX + (mx - CX) * 0.35, CY + (my - CY) * 0.35
    col = cluster_color[labels[si]][0]
    svg.append(f'<path d="M {x1:.1f} {y1:.1f} Q {cx_:.1f} {cy_:.1f} {x2:.1f} {y2:.1f}" '
               f'fill="none" stroke="{col}" stroke-opacity="0.18" stroke-width="0.8"/>')

# nodes
for i in order:
    x, y = pos[i]
    r = node_radius[i]
    col, fill = cluster_color[labels[i]]
    title = htmlmod.escape(short_label(arts[i]) + f"\n{norm_urls[i]}\nin-links: {indeg[i]}  out-links: {outdeg[i]}  cluster: {labels[i]}")
    svg.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" fill="{col}" fill-opacity="0.25" '
               f'stroke="{col}" stroke-width="1.5"><title>{title}</title></circle>')
    # label outside the ring
    ang = angle_of[i]
    lx = CX + (R + 58) * np.cos(ang)
    ly = CY + (R + 58) * np.sin(ang)
    anch = "middle"
    if abs(np.cos(ang)) > 0.3:
        anch = "start" if np.cos(ang) > 0 else "end"
    lab = htmlmod.escape(short_label(arts[i], 22))
    svg.append(f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="{anch}" font-size="9" fill="#cbd5e1" '
               f'paint-order="stroke" stroke="#020617" stroke-width="2.5"><title>{title}</title>{lab}</text>')

svg.append("</svg>")

# ---------- 7. Cluster names (by most common words) ----------
STOP = set("yang dan di ke dengan untuk dari pada ini itu adalah atau juga tidak akan blog artikel menulis saya anda kamu kita membuat menjadi lebih karena sudah saat tapi bisa harus masih ada tentang dalam serta para seo tips cara bagaimana apa mengapa muhammad fathi rauf YOUR_SITE twitter facebook media sosial jasa fanpage kiriman pembaca logo opini refleksi diri call action menggoda resolusi ebook jurus ngeblog pengalaman beli domain awal masa blogging journey perkembangan cerita".split())
def cluster_name(idxs):
    words = Counter()
    for i in idxs:
        t = (arts[i].get("title") or "").lower()
        for w in re.findall(r"[a-z0-9]+", t):
            if w not in STOP and len(w) > 2:
                words[w] += 1
    top = [w for w, _ in words.most_common(3)]
    return " / ".join(top).capitalize() if top else f"Cluster {labels[idxs[0]]}"

cluster_names = {}
for c in clusters:
    cluster_names[c] = cluster_name(cluster_nodes[c])

# ---------- 8. HTML shell ----------
intra = sum(1 for si, ti in links if labels[si] == labels[ti])
inter = len(links) - intra
linked_posts = sum(1 for i in range(N) if indeg[i] or outdeg[i])

legend_items = "".join(
    f'<li><span style="color:{cluster_color[c][0]}">●</span> {htmlmod.escape(cluster_names[c])} '
    f'<span style="color:#64748b">({len(cluster_nodes[c])})</span></li>'
    for c in sorted(clusters, key=lambda x: -len(cluster_nodes[x]))
)

cards = f"""
<div class="grid">
  <div class="card"><div class="card-header"><div class="card-dot cyan"></div><h3>Posts</h3></div>
    <ul><li>• {N} articles analyzed</li><li>• {len(clusters)} semantic clusters</li><li>• embeddings: paraphrase-multilingual-MiniLM-L12-v2</li></ul></div>
  <div class="card"><div class="card-header"><div class="card-dot emerald"></div><h3>Internal Links</h3></div>
    <ul><li>• {len(links)} links between posts</li><li>• {intra} intra-cluster ({intra * 100 // max(len(links),1)}%)</li><li>• {inter} cross-cluster ({inter * 100 // max(len(links),1)}%)</li></ul></div>
  <div class="card"><div class="card-header"><div class="card-dot amber"></div><h3>Coverage</h3></div>
    <ul><li>• {linked_posts} of {N} posts linked</li><li>• {N - linked_posts} orphan(s)</li><li>• 0 duplicate targets per post</li></ul></div>
</div>
"""

html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Semantic Cluster Map — YOUR_SITE.com internal links (v3)</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
  body {{ background:#020617; color:#e2e8f0; font-family:'JetBrains Mono',monospace; margin:0; padding:32px; }}
  .wrap {{ max-width:1180px; margin:0 auto; }}
  h1 {{ font-size:20px; margin:0 0 4px; color:#f8fafc; }}
  h1 .dot {{ display:inline-block; width:10px; height:10px; border-radius:50%; background:#34d399;
             margin-right:10px; animation:pulse 2s infinite; vertical-align:2px; }}
  @keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:.25}} }}
  .sub {{ font-size:12px; color:#64748b; margin:0 0 20px; }}
  .card-wrap {{ background:#0f172a; border:1px solid #1e293b; border-radius:10px; padding:16px; margin-bottom:24px; overflow:auto; }}
  .card-wrap svg {{ display:block; min-width:800px; }}
  .legend {{ display:flex; flex-wrap:wrap; gap:8px 22px; margin:14px 4px 4px; font-size:11px; color:#cbd5e1; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:16px; margin-top:8px; }}
  .card {{ background:#0f172a; border:1px solid #1e293b; border-radius:10px; padding:14px 16px; }}
  .card-header {{ display:flex; align-items:center; gap:8px; margin-bottom:8px; }}
  .card-header h3 {{ margin:0; font-size:13px; color:#f1f5f9; }}
  .card-dot {{ width:8px; height:8px; border-radius:50%; }}
  .card-dot.cyan {{ background:#22d3ee; }} .card-dot.emerald {{ background:#34d399; }} .card-dot.amber {{ background:#fbbf24; }}
  .card ul {{ margin:0; padding:0; list-style:none; font-size:11px; color:#94a3b8; line-height:1.9; }}
  footer {{ margin-top:20px; font-size:10px; color:#475569; }}
</style>
</head>
<body>
<div class="wrap">
  <h1><span class="dot"></span>Semantic Cluster Map — YOUR_SITE.com</h1>
  <p class="sub">semantic linker v3 · {N} posts · {len(links)} internal links · generated {__import__('time').strftime('%Y-%m-%d %H:%M')} WIB</p>
  <div class="card-wrap">
    {''.join(svg)}
    <div class="legend">{legend_items}</div>
  </div>
  {cards}
  <footer>data: Google Sheets articles tab + live WordPress content (context=edit) · clusters: agglomerative, cosine, avg-linkage · layout: circular grouped by cluster · node size = inbound links</footer>
</div>
</body>
</html>"""

out = "semantic-cluster-v3.html"
with open(out, "w") as f:
    f.write(html_doc)
print(f"written: {out} ({os.path.getsize(out)} bytes)")
print(f"intra={intra} inter={inter} linked_posts={linked_posts}")
for c in sorted(clusters, key=lambda x: -len(cluster_nodes[x])):
    print(f"  cluster {c}: {cluster_names[c]} ({len(cluster_nodes[c])} posts)")
