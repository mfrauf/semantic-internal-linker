#!python
"""Generate an INTERACTIVE semantic cluster map (single self-contained HTML).

Same data pipeline as semantic_cluster_diagram.py (45 posts, live WP links,
agglomerative clusters) but with a clickable vanilla-JS layer:
  - click node  -> side panel: title, URL, in/out links (each clickable)
  - click arc/legend -> filter cluster (highlight, dim rest)
  - search box  -> filter nodes by title/URL
  - toggle links / labels / reset
  - hover tooltip
No CDN, no external deps, works offline.
"""
import sys, os, re, json, html as htmlmod
sys.path.insert(0, "scripts")
import internal_linker_v3 as v3
import numpy as np
from sklearn.cluster import AgglomerativeClustering
from collections import Counter

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
texts = [(a.get("content") or a.get("title") or a["url"])[:12000] for a in arts]
from sentence_transformers import SentenceTransformer
model = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
emb = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
sim = emb @ emb.T
np.fill_diagonal(sim, 0.0)

# ---------- 2. Clusters (same recipe as static map) ----------
n_clusters = 12
cl = AgglomerativeClustering(n_clusters=n_clusters, metric="cosine", linkage="average")
labels = cl.fit_predict(emb)
sizes = Counter(labels)
singletons = [c for c, n in sizes.items() if n == 1]
while singletons:
    for c in singletons:
        i = [i for i in range(len(arts)) if labels[i] == c][0]
        labels[i] = labels[int(np.argmax(sim[i]))]
    sizes = Counter(labels)
    singletons = [c for c, n in sizes.items() if n == 1]
clusters = sorted(set(labels))

# ---------- 3. Live internal links ----------
page = 1; posts = []
while True:
    r = v3._wp(f"posts?per_page=100&page={page}&context=edit")
    if not r:
        break
    posts.extend(r)
    if len(r) < 100:
        break
    page += 1
pat = re.compile(r'href="(https://YOUR_SITE.com/[^"]+)"')
norm_urls = [v3.normalize_url(a["url"]).rstrip("/") for a in arts]
idx_of = {u: i for i, u in enumerate(norm_urls)}
links = []
indeg = Counter(); outdeg = Counter()
for p in posts:
    raw = p.get("content", {}).get("raw", "") or ""
    si = idx_of.get(v3.normalize_url(p["link"]).rstrip("/"))
    if si is None:
        continue
    seen = set()
    for m in pat.finditer(raw):
        t = m.group(1).rstrip("/")
        ti = idx_of.get(t)
        if ti is None or ti == si or t in seen:
            continue
        seen.add(t)
        links.append((si, ti))
        outdeg[si] += 1
        indeg[ti] += 1

# ---------- 4. Layout (circular, grouped by cluster) ----------
np.random.seed(7)
order = sorted(range(len(arts)), key=lambda i: (labels[i], norm_urls[i]))
cluster_nodes = {c: [i for i in order if labels[i] == c] for c in clusters}
N = len(arts)
R = 430.0
CX, CY = 520.0, 470.0
angle_of = {}; pos = {}; node_radius = {}
max_indeg = max(indeg.values()) if indeg else 1
for k, i in enumerate(order):
    ang = 2 * np.pi * k / N - np.pi / 2
    angle_of[i] = ang
    pos[i] = (CX + R * np.cos(ang), CY + R * np.sin(ang))
    node_radius[i] = 8 + 7 * (indeg[i] / max_indeg)

arc_extents = []
for c in clusters:
    idxs = cluster_nodes[c]
    a0 = angle_of[idxs[0]]
    a1 = angle_of[idxs[-1]]
    if a0 > a1:
        a0 -= 2 * np.pi
    arc_extents.append({"c": int(c), "a0": float(a0), "a1": float(a1)})

palette = [
    ("#22d3ee", "rgba(34,211,238,0.14)"),
    ("#34d399", "rgba(52,211,153,0.14)"),
    ("#a78bfa", "rgba(167,139,250,0.14)"),
    ("#fbbf24", "rgba(251,191,36,0.14)"),
    ("#fb7185", "rgba(251,113,133,0.14)"),
    ("#fb923c", "rgba(251,146,60,0.14)"),
    ("#f472b6", "rgba(244,114,182,0.14)"),
    ("#4ade80", "rgba(74,222,128,0.14)"),
]
cluster_color = {c: palette[i % len(palette)] for i, c in enumerate(clusters)}

def short_label(a, maxlen=24):
    t = (a.get("title") or "").strip()
    if not t:
        t = a["url"].rstrip("/").split("/")[-1]
    return t if len(t) <= maxlen else t[:maxlen - 1] + "..."

STOP = set("yang dan di ke dengan untuk dari pada ini itu adalah atau juga tidak akan blog artikel menulis saya anda kamu kita membuat menjadi lebih karena sudah saat tapi bisa harus masih ada tentang dalam serta para seo tips cara bagaimana apa mengapa muhammad fathi rauf YOUR_SITE twitter facebook media sosial jasa fanpage kiriman pembaca logo opini refleksi diri call action menggoda resolusi ebook jurus ngeblog pengalaman beli domain awal masa blogging journey perkembangan cerita".split())

def cluster_name(idxs):
    words = Counter()
    for i in idxs:
        for w in re.findall(r"[a-z0-9]+", (arts[i].get("title") or "").lower()):
            if w not in STOP and len(w) > 2:
                words[w] += 1
    top = [w for w, _ in words.most_common(3)]
    return " / ".join(top).capitalize() if top else "Cluster"

# ---------- 5. Serialize data for the JS layer ----------
nodes = []
for i in order:
    x, y = pos[i]
    nodes.append({
        "id": i,
        "title": arts[i].get("title") or arts[i]["url"].rstrip("/").split("/")[-1],
        "url": arts[i]["url"].rstrip("/"),
        "cluster": int(labels[i]),
        "x": round(x, 1), "y": round(y, 1),
        "r": round(node_radius[i], 1),
        "in": indeg[i], "out": outdeg[i],
    })
edge_list = [{"s": int(s), "t": int(t)} for s, t in links]
clust = [{
    "id": int(c),
    "name": cluster_name(cluster_nodes[c]),
    "color": cluster_color[c][0],
    "n": len(cluster_nodes[c]),
    "a0": next(e["a0"] for e in arc_extents if e["c"] == c),
    "a1": next(e["a1"] for e in arc_extents if e["c"] == c),
} for c in clusters]

DATA = json.dumps({
    "cx": CX, "cy": CY, "r": R,
    "nodes": nodes, "links": edge_list, "clusters": clust,
})
# index for JS: node id -> position of node in nodes array (order is the array index)
IDX = {n["id"]: k for k, n in enumerate(nodes)}
IDX_JSON = json.dumps({str(k): v for k, v in IDX.items()})

# ---------- 6. Build the HTML ----------
intra = sum(1 for s, t in links if labels[s] == labels[t])
inter = len(links) - intra
linked_posts = sum(1 for i in range(N) if indeg[i] or outdeg[i])
gen_time = __import__("time").strftime("%Y-%m-%d %H:%M")

html_doc = """<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Peta Cluster Semantik - YOUR_SITE.com (v3)</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #020617; --surface: #0f172a; --surface2: #111c2f;
    --ink: #f1f5f9; --muted: #94a3b8; --dim: #475569; --line: #1e293b;
    --accent: #22d3ee; --accent2: #34d399;
  }
  * { box-sizing: border-box; }
  body { background: var(--bg); color: var(--ink); font-family: 'JetBrains Mono', monospace;
         margin: 0; padding: 20px 24px 40px; }
  .wrap { max-width: 1240px; margin: 0 auto; }
  h1 { font-size: 18px; margin: 0 0 2px; letter-spacing: -0.01em; }
  h1 .dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%;
            background: var(--accent2); margin-right: 10px; animation: pulse 2.4s infinite; vertical-align: 2px; }
  @keyframes pulse { 0%,100% { opacity: 1 } 50% { opacity: .2 } }
  .sub { font-size: 11px; color: var(--dim); margin: 0 0 14px; }

  .controls { display: flex; flex-wrap: wrap; gap: 10px 14px; align-items: center;
              margin-bottom: 12px; font-size: 11px; }
  .controls input[type=text] { background: var(--surface); border: 1px solid var(--line);
              color: var(--ink); font-family: inherit; font-size: 11px; padding: 7px 10px;
              border-radius: 6px; width: 210px; outline: none; }
  .controls input[type=text]:focus { border-color: var(--accent); }
  .controls label { display: inline-flex; align-items: center; gap: 5px; color: var(--muted);
              cursor: pointer; user-select: none; }
  .controls button { background: var(--surface); border: 1px solid var(--line); color: var(--muted);
              font-family: inherit; font-size: 11px; padding: 7px 12px; border-radius: 6px;
              cursor: pointer; }
  .controls button:hover { border-color: var(--accent); color: var(--ink); }
  .stats { margin-left: auto; color: var(--dim); font-size: 11px; white-space: nowrap; }

  .layout { display: grid; grid-template-columns: minmax(0, 1fr) 320px; gap: 14px; }
  @media (max-width: 900px) { .layout { grid-template-columns: 1fr; } }

  .map-card { background: var(--surface); border: 1px solid var(--line); border-radius: 10px;
              padding: 8px; overflow: auto; }
  .map-card svg { display: block; width: 100%; height: auto; min-width: 720px; }

  #panel { background: var(--surface); border: 1px solid var(--line); border-radius: 10px;
           padding: 14px 16px; height: fit-content; max-height: 640px; overflow: auto; font-size: 11px; }
  #panel h3 { margin: 0 0 10px; font-size: 13px; line-height: 1.45; color: var(--ink); }
  #panel .meta { color: var(--dim); margin-bottom: 12px; line-height: 1.8; }
  #panel .meta b { color: var(--muted); font-weight: 400; }
  #panel .lbl { color: var(--dim); text-transform: uppercase; letter-spacing: 0.08em;
                font-size: 9px; margin: 14px 0 6px; }
  #panel ul { list-style: none; margin: 0; padding: 0; }
  #panel li { padding: 4px 0; border-bottom: 1px solid #16213a; }
  #panel li:last-child { border-bottom: none; }
  #panel a { color: var(--accent); text-decoration: none; }
  #panel a:hover { text-decoration: underline; }
  #panel .count { color: var(--dim); }
  #panel .close { float: right; background: none; border: none; color: var(--dim);
                  font-family: inherit; font-size: 14px; cursor: pointer; padding: 0 2px; }
  #panel .close:hover { color: var(--ink); }
  #panel .empty { color: var(--dim); padding: 24px 0; text-align: center; line-height: 2; }

  .legend { display: flex; flex-wrap: wrap; gap: 8px 18px; margin-top: 12px; font-size: 10px; }
  .legend .lg { display: inline-flex; align-items: center; gap: 6px; cursor: pointer;
                color: var(--muted); padding: 3px 7px; border-radius: 5px; border: 1px solid transparent; }
  .legend .lg:hover { background: var(--surface2); }
  .legend .lg.off { opacity: 0.35; text-decoration: line-through; }
  .legend .sw { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
  .legend .n { color: var(--dim); }

  svg text { paint-order: stroke; }
  footer { margin-top: 18px; font-size: 10px; color: var(--dim); line-height: 1.8; }
</style>
</head>
<body>
<div class="wrap">
  <h1><span class="dot"></span>Peta Cluster Semantik - YOUR_SITE.com</h1>
  <p class="sub">semantic linker v3 · __N__ artikel · __L__ tautan internal · data: __T__ WIB</p>

  <div class="controls">
    <input type="text" id="search" placeholder="cari judul atau URL..." autocomplete="off">
    <label><input type="checkbox" id="tglLinks" checked> tautan</label>
    <label><input type="checkbox" id="tglLabels" checked> label</label>
    <button id="reset">reset</button>
    <span class="stats"><span id="shown">__N__</span>/__N__ artikel · __LINKED__ terhubung</span>
  </div>

  <div class="layout">
    <div class="map-card">
      <svg id="map" viewBox="0 0 1040 940" xmlns="http://www.w3.org/2000/svg" role="img"
           aria-label="Peta cluster semantik artikel YOUR_SITE.com">
      </svg>
      <div class="legend" id="legend"></div>
    </div>
    <div id="panel">
      <div class="empty">Klik salah satu titik untuk melihat detail artikel.<br>
      Klik busur cluster atau legenda untuk memfilter.<br>
      Ketik di kolom cari untuk menemukan artikel.</div>
    </div>
  </div>

  <footer>
    data: Google Sheets (tab articles) + konten WordPress live (context=edit) · __N__ artikel,
    __L__ tautan internal (__INTRA__ intra-cluster, __INTER__ cross-cluster) · cluster: agglomerative
    cosine avg-linkage · ukuran titik = jumlah tautan masuk · klik titik / busur / legenda untuk interaksi
  </footer>
</div>

<script id="data" type="application/json">__DATA__</script>
<script id="idx" type="application/json">__IDX__</script>
<script>
"use strict";
const DATA = JSON.parse(document.getElementById("data").textContent);
const IDX = JSON.parse(document.getElementById("idx").textContent);
const svg = document.getElementById("map");
const panel = document.getElementById("panel");
const legendEl = document.getElementById("legend");
const shownEl = document.getElementById("shown");

const state = { activeNode: null, activeCluster: null, search: "",
                showLinks: true, showLabels: true };

// ---------- geometry ----------
function pt(ang) {
  return [DATA.cx + DATA.r * Math.cos(ang), DATA.cy + DATA.r * Math.sin(ang)];
}
function arcPath(c, rIn, rOut) {
  const a0 = c.a0, a1 = c.a1;
  const p0 = pt(a0), p1 = pt(a1);
  const large = (a1 - a0) > Math.PI ? 1 : 0;
  const q0 = pt(a1), q1 = pt(a0);
  const o = rOut / DATA.r, i = rIn / DATA.r;
  const P0 = [DATA.cx + (p0[0]-DATA.cx)*o, DATA.cy + (p0[1]-DATA.cy)*o];
  const P1 = [DATA.cx + (p1[0]-DATA.cx)*o, DATA.cy + (p1[1]-DATA.cy)*o];
  const Q1 = [DATA.cx + (q0[0]-DATA.cx)*i, DATA.cy + (q0[1]-DATA.cy)*i];
  const Q0 = [DATA.cx + (q1[0]-DATA.cx)*i, DATA.cy + (q1[1]-DATA.cy)*i];
  return `M ${P0[0].toFixed(1)} ${P0[1].toFixed(1)} A ${(rOut).toFixed(1)} ${(rOut).toFixed(1)} 0 ${large} 1 ${P1[0].toFixed(1)} ${P1[1].toFixed(1)} L ${Q1[0].toFixed(1)} ${Q1[1].toFixed(1)} A ${(rIn).toFixed(1)} ${(rIn).toFixed(1)} 0 ${large} 0 ${Q0[0].toFixed(1)} ${Q0[1].toFixed(1)} Z`;
}

// ---------- filter state ----------
function nodeVisible(n) {
  if (state.activeCluster !== null && n.cluster !== state.activeCluster) return false;
  if (state.search) {
    const q = state.search.toLowerCase();
    if (!n.title.toLowerCase().includes(q) && !n.url.toLowerCase().includes(q)) return false;
  }
  return true;
}
function nodeDimmed(n) {
  if (state.activeNode !== null && n.id !== state.activeNode) return true;
  return false;
}

// ---------- render ----------
function render() {
  svg.innerHTML = "";

  // grid
  const defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
  defs.innerHTML = `<pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse"><path d="M 40 0 L 0 0 0 40" fill="none" stroke="#16213a" stroke-width="0.5"/></pattern>`;
  svg.appendChild(defs);
  const bg = document.createElementNS("http://www.w3.org/2000/svg", "rect");
  bg.setAttribute("width", 1040); bg.setAttribute("height", 940);
  bg.setAttribute("fill", "url(#grid)");
  svg.appendChild(bg);

  // cluster arcs (clickable)
  const arcR = DATA.r + 46;
  for (const c of DATA.clusters) {
    if (state.activeNode !== null) continue; // hide arcs when inspecting a node
    const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
    g.style.cursor = "pointer";
    const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
    p.setAttribute("d", arcPath(c, DATA.r - 46, arcR));
    p.setAttribute("fill", c.color + "22");
    p.setAttribute("stroke", c.color);
    p.setAttribute("stroke-opacity", 0.55);
    p.setAttribute("stroke-dasharray", "4,4");
    p.setAttribute("stroke-width", 1);
    p.setAttribute("data-cluster", c.id);
    const isOff = state.activeCluster !== null && state.activeCluster !== c.id;
    if (isOff) p.setAttribute("opacity", 0.12);
    g.appendChild(p);
    g.addEventListener("click", () => toggleCluster(c.id));
    svg.appendChild(g);
  }

  // links (chords)
  if (state.showLinks) {
    for (const e of DATA.links) {
      const n1 = DATA.nodes[IDX[e.s]];
      const n2 = DATA.nodes[IDX[e.t]];
      if (!nodeVisible(n1) || !nodeVisible(n2)) continue;
      const x1 = n1.x, y1 = n1.y, x2 = n2.x, y2 = n2.y;
      const mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
      const cx2 = DATA.cx + (mx - DATA.cx) * 0.35, cy2 = DATA.cy + (my - DATA.cy) * 0.35;
      const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
      p.setAttribute("d", `M ${x1.toFixed(1)} ${y1.toFixed(1)} Q ${cx2.toFixed(1)} ${cy2.toFixed(1)} ${x2.toFixed(1)} ${y2.toFixed(1)}`);
      p.setAttribute("fill", "none");
      const ccol = DATA.clusters.find(c => c.id === n1.cluster).color;
      const active = state.activeNode !== null && (n1.id === state.activeNode || n2.id === state.activeNode);
      const dim = state.activeNode !== null && !active;
      p.setAttribute("stroke", ccol);
      p.setAttribute("stroke-opacity", active ? 0.65 : 0.16);
      p.setAttribute("stroke-width", active ? 1.6 : 0.8);
      if (dim) p.setAttribute("opacity", 0.06);
      svg.appendChild(p);
    }
  }

  // nodes
  for (const n of DATA.nodes) {
    if (!nodeVisible(n)) continue;
    const dim = nodeDimmed(n);
    const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
    g.style.cursor = "pointer";
    g.setAttribute("opacity", dim ? 0.15 : 1);
    const c = DATA.clusters.find(x => x.id === n.cluster).color;
    const circ = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    circ.setAttribute("cx", n.x); circ.setAttribute("cy", n.y); circ.setAttribute("r", n.r);
    circ.setAttribute("fill", c); circ.setAttribute("fill-opacity", 0.28);
    circ.setAttribute("stroke", c); circ.setAttribute("stroke-width", 1.6);
    const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
    title.textContent = `${n.title}\n${n.url}\nmasuk: ${n.in}  keluar: ${n.out}`;
    circ.appendChild(title);
    g.appendChild(circ);

    if (state.showLabels) {
      const ang = Math.atan2(n.y - DATA.cy, n.x - DATA.cx);
      const lx = DATA.cx + (DATA.r + 58) * Math.cos(ang);
      const ly = DATA.cy + (DATA.r + 58) * Math.sin(ang);
      const txt = document.createElementNS("http://www.w3.org/2000/svg", "text");
      txt.setAttribute("x", lx.toFixed(1)); txt.setAttribute("y", ly.toFixed(1));
      txt.setAttribute("font-size", 8.5);
      txt.setAttribute("fill", "#cbd5e1");
      txt.setAttribute("stroke", "#020617"); txt.setAttribute("stroke-width", 2.5);
      txt.setAttribute("text-anchor", Math.abs(Math.cos(ang)) > 0.3 ? (Math.cos(ang) > 0 ? "start" : "end") : "middle");
      const label = n.title.length > 22 ? n.title.slice(0, 21) + "..." : n.title;
      txt.textContent = label;
      g.appendChild(txt);
    }
    g.addEventListener("click", () => selectNode(n.id));
    svg.appendChild(g);
  }

  // stats
  let shown = 0;
  for (const n of DATA.nodes) if (nodeVisible(n)) shown++;
  shownEl.textContent = shown;
  renderLegend();
}

function renderLegend() {
  legendEl.innerHTML = "";
  for (const c of DATA.clusters) {
    const el = document.createElement("span");
    el.className = "lg" + (state.activeCluster !== null && state.activeCluster !== c.id ? " off" : "");
    el.innerHTML = `<span class="sw" style="background:${c.color}"></span>${escapeHtml(c.name)} <span class="n">(${c.n})</span>`;
    el.addEventListener("click", () => toggleCluster(c.id));
    legendEl.appendChild(el);
  }
}

// ---------- panel ----------
function selectNode(id) {
  state.activeNode = (state.activeNode === id) ? null : id;
  renderPanel(); render();
}
function toggleCluster(id) {
  state.activeCluster = (state.activeCluster === id) ? null : id;
  renderPanel(); render();
}

function renderPanel() {
  if (state.activeNode !== null) {
    const n = DATA.nodes[IDX[state.activeNode]];
    const c = DATA.clusters.find(x => x.id === n.cluster);
    const inLinks = DATA.links.filter(e => e.t === n.id);
    const outLinks = DATA.links.filter(e => e.s === n.id);
    let html = `<button class="close" onclick="selectNode(${n.id})" title="tutup">x</button>`;
    html += `<h3>${escapeHtml(n.title)}</h3>`;
    html += `<div class="meta">`;
    html += `<b>URL:</b> <a href="${n.url}" target="_blank" rel="noopener">${escapeHtml(n.url)}</a><br>`;
    html += `<b>Cluster:</b> <span style="color:${c.color}">${escapeHtml(c.name)}</span> (${c.id})<br>`;
    html += `<b>Masuk:</b> ${n.in} tautan · <b>Keluar:</b> ${n.out} tautan</div>`;
    html += `<div class="lbl">Artikel yang menautkan ke sini (${inLinks.length})</div><ul>`;
    if (inLinks.length === 0) html += `<li style="color:var(--dim)">tidak ada</li>`;
    for (const e of inLinks) {
      const src = DATA.nodes[IDX[e.s]];
      html += `<li><a href="#" data-jump="${e.s}" class="jump">${escapeHtml(src.title)}</a> <span class="count">· cluster ${src.cluster}</span></li>`;
    }
    html += `</ul><div class="lbl">Artikel yang ditautkan dari sini (${outLinks.length})</div><ul>`;
    if (outLinks.length === 0) html += `<li style="color:var(--dim)">tidak ada</li>`;
    for (const e of outLinks) {
      const tgt = DATA.nodes[IDX[e.t]];
      html += `<li><a href="#" data-jump="${e.t}" class="jump">${escapeHtml(tgt.title)}</a> <span class="count">· cluster ${tgt.cluster}</span></li>`;
    }
    html += `</ul>`;
    panel.innerHTML = html;
    panel.querySelectorAll(".jump").forEach(a => {
      a.addEventListener("click", ev => { ev.preventDefault(); selectNode(parseInt(a.dataset.jump)); });
    });
  } else if (state.activeCluster !== null) {
    const c = DATA.clusters.find(x => x.id === state.activeCluster);
    const members = DATA.nodes.filter(n => n.cluster === c.id);
    const inSum = members.reduce((s, n) => s + n.in, 0);
    const outSum = members.reduce((s, n) => s + n.out, 0);
    let html = `<button class="close" onclick="toggleCluster(${c.id})" title="tutup">x</button>`;
    html += `<h3 style="color:${c.color}">${escapeHtml(c.name)}</h3>`;
    html += `<div class="meta"><b>Anggota:</b> ${members.length} artikel<br><b>Tautan masuk:</b> ${inSum} · <b>Tautan keluar:</b> ${outSum}</div>`;
    html += `<div class="lbl">Artikel (${members.length})</div><ul>`;
    for (const n of members) {
      html += `<li><a href="#" data-jump="${n.id}" class="jump">${escapeHtml(n.title)}</a> <span class="count">· in ${n.in} / out ${n.out}</span></li>`;
    }
    html += `</ul>`;
    panel.innerHTML = html;
    panel.querySelectorAll(".jump").forEach(a => {
      a.addEventListener("click", ev => { ev.preventDefault(); selectNode(parseInt(a.dataset.jump)); });
    });
  } else {
    panel.innerHTML = `<div class="empty">Klik salah satu titik untuk melihat detail artikel.<br>
    Klik busur cluster atau legenda untuk memfilter.<br>
    Ketik di kolom cari untuk menemukan artikel.</div>`;
  }
}

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// ---------- events ----------
document.getElementById("search").addEventListener("input", ev => {
  state.search = ev.target.value.trim();
  render();
});
document.getElementById("tglLinks").addEventListener("change", ev => {
  state.showLinks = ev.target.checked; render();
});
document.getElementById("tglLabels").addEventListener("change", ev => {
  state.showLabels = ev.target.checked; render();
});
document.getElementById("reset").addEventListener("click", () => {
  state.activeNode = null; state.activeCluster = null; state.search = "";
  document.getElementById("search").value = "";
  renderPanel(); render();
});
document.addEventListener("keydown", ev => {
  if (ev.key === "Escape") { state.activeNode = null; state.activeCluster = null; renderPanel(); render(); }
  if (ev.key === "/" && document.activeElement !== document.getElementById("search")) {
    ev.preventDefault(); document.getElementById("search").focus();
  }
});

render(); renderPanel();
</script>
</body>
</html>
"""

# substitute data + numbers
html_doc = (html_doc
    .replace("__DATA__", DATA)
    .replace("__IDX__", IDX_JSON)
    .replace("__N__", str(N))
    .replace("__L__", str(len(links)))
    .replace("__INTRA__", str(intra))
    .replace("__INTER__", str(inter))
    .replace("__LINKED__", str(linked_posts))
    .replace("__T__", gen_time))

out = "semantic-cluster-map-interactive.html"
with open(out, "w") as f:
    f.write(html_doc)
print(f"written: {out} ({os.path.getsize(out)} bytes)")
print(f"nodes={N} links={len(links)} clusters={len(clusters)} intra={intra} inter={inter} linked={linked_posts}")
for c in sorted(clusters, key=lambda x: -Counter(labels)[x]):
    print(f"  cluster {c}: {cluster_name(cluster_nodes[c])} ({Counter(labels)[c]})")
