"""Run reports: contact sheets (PNG), an interactive page (HTML), summary and low list."""

import html
import json
from collections import Counter, defaultdict

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import ids
from .geometry import svg_d, transform_path

COLORS = [(222, 74, 54), (36, 120, 214), (46, 160, 84), (214, 150, 20), (142, 72, 196), (20, 160, 170)]
OP_NAMES = {"⿰": "lr", "⿱": "tb", "⿲": "lmr", "⿳": "tmb", "⿴": "full", "⿵": "open-b", "⿶": "open-t",
            "⿷": "open-r", "⿸": "ul", "⿹": "ur", "⿺": "ll", "⿻": "overlap"}
LABEL_FONT = "/System/Library/Fonts/Hiragino Sans GB.ttc"


def structure(ids_text):
    return OP_NAMES.get(ids_text[:1], "whole")


def _font(size):
    try:
        return ImageFont.truetype(LABEL_FONT, size)
    except OSError:
        return ImageFont.load_default()


# ---------- PNG ----------

def tile(seg, size=150):
    """Registration | flat | overlay, with a caption."""
    dbg = seg.debug
    if "canvas" not in dbg:
        img = Image.new("RGB", (size * 3 + 8, size + 34), "white")
        ImageDraw.Draw(img).text((4, size + 4), f"{seg.char} empty", fill=(200, 0, 0), font=_font(14))
        return img
    canvas, ink = dbg["canvas"], dbg["ink"]
    res = canvas.res
    panels = []

    reg = np.full((res, res, 3), 255, np.uint8)
    reg[ink] = (210, 210, 210)
    im = Image.fromarray(reg)
    d = ImageDraw.Draw(im)
    for i, line in enumerate(dbg["lines"]):
        c = COLORS[dbg["stroke_node"][i] % len(COLORS)]
        d.line([tuple(p) for p in canvas.to_px(line)], fill=c, width=5)
    panels.append(im)

    flat = np.full((res, res, 3), 255, np.uint8)
    for k, n in enumerate(seg.nodes):
        flat[dbg["flat_masks"][n]] = COLORS[k % len(COLORS)]
    im = Image.fromarray(flat)
    d = ImageDraw.Draw(im)
    for u in seg.units:
        for line in u.cuts:
            d.line([tuple(p) for p in canvas.to_px(line)], fill=(0, 0, 0), width=3)
    panels.append(im)

    over = np.full((res, res, 3), 255.0)
    for k, n in enumerate(seg.nodes):
        m = dbg["overlay_masks"][n]
        over[m] = over[m] * 0.45 + np.array(COLORS[k % len(COLORS)]) * 0.55
    panels.append(Image.fromarray(over.astype(np.uint8)))

    out = Image.new("RGB", (size * 3 + 8, size + 34), "white")
    for i, p in enumerate(panels):
        out.paste(p.resize((size, size), Image.LANCZOS), (i * (size + 4), 0))
    d = ImageDraw.Draw(out)
    ok = seg.status in ("auto", "verified")
    parts = " ".join(ids.leaves(seg.debug["ref"].tree) if not isinstance(seg.debug["ref"].tree, str) else seg.ids)
    d.text((2, size + 1), f"{seg.char} {structure(seg.ids)} {parts}", fill="black", font=_font(14))
    d.text((2, size + 17), f"{seg.status} {seg.confidence:.2f}  cut {seg.metrics.get('cut_units', 0)}/{len(seg.units)}",
           fill=(0, 120, 0) if ok else (200, 0, 0), font=_font(13))
    return out


def sheets(tiles, path_prefix, cols=4, rows=10):
    if not tiles:
        return []
    tw, th = tiles[0].size
    per = cols * rows
    paths = []
    for page in range(0, len(tiles), per):
        chunk = tiles[page:page + per]
        n_rows = -(-len(chunk) // cols)
        sheet = Image.new("RGB", (cols * (tw + 14), n_rows * (th + 10)), "white")
        for i, t in enumerate(chunk):
            sheet.paste(t, ((i % cols) * (tw + 14), (i // cols) * (th + 10)))
        p = f"{path_prefix}-{page // per + 1:02d}.png"
        sheet.save(p)
        paths.append(p)
    return paths


# ---------- HTML ----------

def page_data(seg, font):
    """Per-character data for the HTML page: SVG paths per node and view, font units."""
    views = {}
    for view in ("flat", "overlay"):
        views[view] = {ids.fmt(n): svg_d(transform_path(p, font.from_em)) for n, p in seg.pieces(view).items()}
    cuts = []
    for u in seg.units:
        for line in u.cuts:
            pts = [font.from_em(*p) for p in line]
            cuts.append("M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in pts))
    return {
        "char": seg.char, "ids": seg.ids, "structure": structure(seg.ids), "status": seg.status,
        "confidence": seg.confidence, "nodes": [ids.fmt(n) for n in seg.nodes], "views": views, "cuts": cuts,
        "metrics": seg.metrics, "issues": seg.issues,
        "faces": [{"nodes": [ids.fmt(n) for n in f.nodes], "flat": ids.fmt(f.flat), "rule": f.rule,
                   "at": [round(v, 1) for v in font.from_em(*f.point)]}
                  for u in seg.units for f in u.faces if len(f.nodes) > 1],
    }


def write_html(path, title, entries, upm, descender):
    colors = ["#%02x%02x%02x" % c for c in COLORS]
    data = json.dumps(entries, ensure_ascii=False, separators=(",", ":"))
    box = f"0 {-(upm + descender)} {upm} {upm}"
    doc = HTML_TEMPLATE.replace("__TITLE__", html.escape(title)).replace("__DATA__", data) \
        .replace("__COLORS__", json.dumps(colors)).replace("__VIEWBOX__", box)
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)


HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#fff;--fg:#1d1d1f;--muted:#6e6e73;--card:#f5f5f7;--line:#d2d2d7;--bad:#c62828;--good:#2e7d32}
@media (prefers-color-scheme:dark){:root{--bg:#161618;--fg:#f2f2f2;--muted:#9a9aa0;--card:#232326;--line:#3a3a3e;--bad:#ef6b6b;--good:#6cc070}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 -apple-system,"PingFang SC",sans-serif}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;flex-wrap:wrap;gap:12px;align-items:center;z-index:1}
header h1{font-size:16px;margin:0 12px 0 0}
select,button,input{font:inherit;color:inherit;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:3px 8px}
#grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:12px;padding:16px}
.card{background:var(--card);border-radius:10px;padding:8px;cursor:pointer}
.card svg{width:100%;aspect-ratio:1;display:block;background:var(--bg);border-radius:6px}
.cap{display:flex;justify-content:space-between;margin-top:4px;gap:6px}
.low{color:var(--bad)}.auto,.verified{color:var(--good)}
.muted{color:var(--muted);font-size:12px}
dialog{max-width:min(720px,94vw);background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:12px}
dialog svg{width:min(420px,80vw);aspect-ratio:1;background:var(--card);border-radius:8px}
pre{white-space:pre-wrap;font-size:12px;background:var(--card);padding:8px;border-radius:6px;max-height:40vh;overflow:auto}
.chips button{margin:0 4px 4px 0}
</style></head><body>
<header><h1>__TITLE__</h1>
<label>视图 <select id="view"><option value="flat">拍平</option><option value="overlay">叠加</option></select></label>
<label>状态 <select id="status"><option value="">全部</option><option>low</option><option>auto</option><option>verified</option></select></label>
<label>结构 <select id="struct"><option value="">全部</option></select></label>
<label>排序 <select id="sort"><option value="conf">置信度↑</option><option value="order">原顺序</option></select></label>
<label><input type="checkbox" id="cuts" checked> 切线</label>
<span class="muted" id="count"></span></header>
<div id="grid"></div>
<dialog id="dlg"><div id="detail"></div><form method="dialog"><button>关闭</button></form></dialog>
<script>
const DATA=__DATA__, COLORS=__COLORS__, VB="__VIEWBOX__";
const $=s=>document.querySelector(s);
const structs=[...new Set(DATA.map(d=>d.structure))].sort();
structs.forEach(s=>$('#struct').insertAdjacentHTML('beforeend',`<option>${s}</option>`));
function svg(d,view,only,showCuts){
  const flip='transform="scale(1,-1)"';
  let body='';
  d.nodes.forEach((n,i)=>{
    if(only!=null&&only!==n)return;
    const op=view==='overlay'&&only==null?0.6:1;
    body+=`<path d="${d.views[view][n]}" fill="${COLORS[i%COLORS.length]}" fill-opacity="${op}"/>`;
  });
  if(showCuts&&only==null)d.cuts.forEach(c=>body+=`<path d="${c}" fill="none" stroke="currentColor" stroke-width="4"/>`);
  return `<svg viewBox="${VB}"><g ${flip}>${body}</g></svg>`;
}
function render(){
  const view=$('#view').value,st=$('#status').value,sc=$('#struct').value,cuts=$('#cuts').checked;
  let rows=DATA.map((d,i)=>({d,i})).filter(({d})=>(!st||d.status===st)&&(!sc||d.structure===sc));
  if($('#sort').value==='conf')rows.sort((a,b)=>a.d.confidence-b.d.confidence);
  $('#count').textContent=`${rows.length} / ${DATA.length}`;
  $('#grid').innerHTML=rows.map(({d,i})=>`<div class="card" data-i="${i}">${svg(d,view,null,cuts)}
    <div class="cap"><span>${d.char} <span class="muted">${d.ids}</span></span><span class="${d.status}">${d.confidence.toFixed(2)}</span></div></div>`).join('');
}
function detail(i){
  const d=DATA[i];let only=null;
  const draw=()=>{
    const v=$('#view').value;
    $('#detail').innerHTML=`<h2>${d.char} <span class="muted">${d.ids} · ${d.structure} · ${d.status} ${d.confidence.toFixed(3)}</span></h2>
    <div class="chips"><button data-n="">全部</button>${d.nodes.map((n,k)=>`<button data-n="${n}" style="border-color:${COLORS[k%COLORS.length]}">节点 ${n}</button>`).join('')}</div>
    ${svg(d,only==null?v:'overlay',only,$('#cuts').checked)}
    <p class="muted">单独显示某个节点时使用叠加视图（完整部件）。</p>
    <pre>${JSON.stringify({faces:d.faces,metrics:d.metrics,issues:d.issues},null,1)}</pre>`;
    $('#detail').querySelectorAll('button[data-n]').forEach(b=>b.onclick=()=>{only=b.dataset.n||null;draw();});
  };
  draw();$('#dlg').showModal();
}
['#view','#status','#struct','#sort','#cuts'].forEach(s=>$(s).onchange=render);
$('#grid').onclick=e=>{const c=e.target.closest('.card');if(c)detail(+c.dataset.i);};
render();
</script></body></html>
"""


# ---------- text ----------

def summary(segs, records, threshold):
    by_struct = defaultdict(list)
    for s in segs:
        by_struct[structure(s.ids)].append(s)
    n = len(segs)
    auto = sum(s.status in ("auto", "verified") for s in segs)
    ious = [v["iou"] for s in segs for v in s.metrics.get("nodes", {}).values()]
    lines = [
        "# Run summary", "",
        f"- characters: {n}",
        f"- auto (confidence >= {threshold}): {auto} ({auto / max(n, 1):.1%})",
        f"- low: {sum(s.status == 'low' for s in segs)}",
        f"- units cut: {sum(s.metrics.get('cut_units', 0) for s in segs)} in "
        f"{sum(bool(s.metrics.get('cut_units')) for s in segs)} characters",
        f"- mean IoU with the warped reference: {np.mean(ious):.3f} (style-limited; read as a trend)" if ious else "",
        f"- junction rules: {dict(Counter(r for s in segs for r, c in s.metrics.get('junctions', {}).items() for _ in range(c)))}",
        "", "| structure | n | auto | mean conf | mean IoU |", "|---|---:|---:|---:|---:|",
    ]
    for k in sorted(by_struct):
        g = by_struct[k]
        gi = [v["iou"] for s in g for v in s.metrics.get("nodes", {}).values()]
        lines.append(f"| {k} | {len(g)} | {sum(s.status in ('auto', 'verified') for s in g)} | "
                     f"{np.mean([s.confidence for s in g]):.3f} | {np.mean(gi) if gi else 0:.3f} |")
    return "\n".join(l for l in lines if l is not None) + "\n"


def low_list(segs):
    rows = ["char\tids\tconfidence\tweakest\tcut_units\tjunctions\tissues"]
    for s in sorted(segs, key=lambda s: s.confidence):
        if s.status != "low":
            continue
        nodes = s.metrics.get("nodes", {})
        weakest = min(nodes.items(), key=lambda kv: min(kv[1]["median_in"], kv[1]["purity"]), default=(None, None))
        w = f"{weakest[0]}:in={weakest[1]['median_in']},purity={weakest[1]['purity']}" if weakest[0] is not None else ""
        rows.append("\t".join([s.char, s.ids, f"{s.confidence:.3f}", w, str(s.metrics.get("cut_units", 0)),
                               json.dumps(s.metrics.get("junctions", {})), "; ".join(s.issues)]))
    return "\n".join(rows) + "\n"
