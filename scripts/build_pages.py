"""Build static journal landing pages and the top-level index from MUSE article HTML.

Reads article HTML downloaded from s3://project.muse/<slug>/ into <src>/<slug>/ (for example with
`aws s3 sync s3://project.muse/<slug>/ work/<slug>/ --exclude "*" --include "*.html"`), plus the
bucket listing so PDF-only articles are included. Writes:

  <out>/<slug>/index.html    one page per journal
  <meta>/<slug>.json         journal summary used to build the top-level index
  <out>/index.html           top-level journal list (every summary in <meta>)

Usage:
  python scripts/build_pages.py --slug ab_autobiography_studies
"""
import argparse
import html
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

ARTICLE_KEY = re.compile(r'^(?P<slug>[^/]+)/(?P<vol>v[^/]+)/(?P<issue>[^/]+)/(?P<id>\d+)\.(?P<ext>pdf|html)$')
TAG = re.compile(r'<[^>]+>')
DETAIL_ROW = re.compile(r'<div class="cell label">\s*(.*?)\s*</div>\s*<div class="cell">\s*(.*?)\s*</div>', re.S)


def text(fragment):
    """Strip tags, unescape entities and collapse whitespace."""
    return re.sub(r'\s+', ' ', html.unescape(TAG.sub(' ', fragment or ''))).strip()


def first(pattern, s, flags=re.S):
    m = re.search(pattern, s, flags)
    return m.group(1) if m else ''


def parse_article(path):
    s = path.read_text(encoding='utf-8', errors='replace')
    # The html_title comment markers use non-breaking spaces, so match the title div itself.
    raw_title = first(r'<div id="article-title"[^>]*>(.*?)<!--\W*/html_title', s)
    main = text(re.sub(r'<span id="subtitle">.*', '', raw_title, flags=re.S))
    sub = text(first(r'<span id="subtitle">(.*?)</span>', raw_title))
    title = main
    if sub:
        title = f'{main} {sub}' if main.endswith((':', '?', '!', '.')) else f'{main}: {sub}'
    front = first(r'<div id="front">(.*?)<div id="body">', s) or s
    authors = []
    for c in re.findall(r'<div class="contrib">(.*?)</div>', front, re.S):
        name = text(re.sub(r'<a class="top_bio_wrap_link".*?</a>', '', c, flags=re.S))
        if name:
            authors.append(name)
    details = {text(k): text(v) for k, v in DETAIL_ROW.findall(s)}
    return {
        'journal': text(first(r'<li class="journal"><span>(.*?)</span>', s)),
        'designation': text(first(r'<li class="designation"><span>(.*?)</span>', s)),
        'publisher': text(first(r'<li class="publisher"><span>(.*?)</span>', s)),
        'type': text(first(r'<li class="type"><span class="[^"]*">(.*?)</span>', s)),
        'title': title or 'Untitled',
        'authors': authors,
        'pages': details.get('Pages', ''),
        'issn': details.get('ISSN', ''),
        'print_issn': details.get('Print ISSN', ''),
    }


def num(s, default=10**9):
    m = re.search(r'\d+', s or '')
    return int(m.group()) if m else default


def parse_designation(d, vol_folder):
    vol = first(r'Volume\s+([^,]+)', d, 0) or vol_folder.lstrip('v').lstrip('0') or vol_folder
    number = first(r'Numbers?\s+([^,]+)', d, 0)
    years = re.findall(r'\b(1[6-9]\d\d|20\d\d)\b', d)
    return vol.strip(), number.strip(), years[-1] if years else ''


def fold(s):
    return ''.join(c for c in unicodedata.normalize('NFKD', s.lower()) if not unicodedata.combining(c))


def read_listing(listing, slugs):
    """Return {slug: {(vol, issue, id): set(ext)}} for article-level keys, one streaming pass."""
    out = {slug: defaultdict(set) for slug in slugs}
    with open(listing, encoding='utf-8') as f:
        for line in f:
            key = line.split('\t', 1)[0]
            m = ARTICLE_KEY.match(key)
            if m and m['slug'] in out:
                out[m['slug']][(m['vol'], m['issue'], m['id'])].add(m['ext'])
    return out


CSS = """
:root{--bg:#F4F6F8;--ink:#17202B;--muted:#475263;--rule:#C9D1DB;--accent:#1D3D6E;--rail:#1D3D6E;--mark:#FFE9A8;--focus:#1D3D6E;
--serif:"Iowan Old Style","Palatino Linotype","Book Antiqua",Palatino,"URW Palladio L",P052,Georgia,serif;
--sans:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--bg:#10161D;--ink:#E7ECF2;--muted:#AEB8C6;--rule:#2C3A49;--accent:#A9C8FF;--rail:#8FB3EE;--mark:#4A3B00;--focus:#A9C8FF}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:1.0625rem/1.6 var(--sans)}
a{color:var(--accent);text-underline-offset:.18em;text-decoration-thickness:1px}
a:hover{text-decoration-thickness:2px}
:focus-visible{outline:3px solid var(--focus);outline-offset:3px;border-radius:2px}
.skip{position:absolute;left:-999px;top:0;background:var(--bg);padding:.5rem 1rem}
.skip:focus{left:1rem;top:1rem}
.wrap{max-width:54rem;margin:0 auto;padding:2rem 1rem 4rem}
.crumb{font-size:.9375rem;margin:0 0 2.5rem;color:var(--muted)}
h1{font:600 clamp(2rem,5vw,3rem)/1.1 var(--serif);margin:0 0 1rem;letter-spacing:-.01em;max-width:22ch}
.about{color:var(--muted);margin:0 0 .25rem;max-width:62ch}
.find{margin:2.25rem 0 2.75rem;padding:1.25rem 0;border-top:1px solid var(--rule);border-bottom:1px solid var(--rule)}
.find label{display:block;font-weight:600;margin-bottom:.4rem}
.find input{width:100%;max-width:32rem;font:inherit;color:var(--ink);background:transparent;border:1px solid var(--muted);border-radius:4px;padding:.55rem .7rem}
.find p{margin:.5rem 0 0;color:var(--muted);font-size:.9375rem}
.vol{display:grid;grid-template-columns:7.5rem 1fr;gap:0 1.5rem;padding:1.75rem 0;border-top:1px solid var(--rule)}
main>.vol:first-child,main>.vol[hidden]+.vol{border-top:0;padding-top:0}
.nw{white-space:nowrap}
.vol>h2{margin:0;font-weight:400;align-self:start;position:sticky;top:1rem;color:var(--rail)}
.vlabel{display:block;font:600 .875rem/1.2 var(--sans);color:var(--muted)}
.vnum{display:block;font:400 3.75rem/1 var(--serif);font-variant-numeric:oldstyle-nums;letter-spacing:-.02em}
.vyear{display:block;font:400 1rem/1.4 var(--sans);color:var(--muted);margin-top:.25rem}
.issue{margin:0 0 2rem}
.issue:last-child{margin-bottom:0}
.issue>h3{font:600 1.0625rem/1.4 var(--sans);margin:0 0 .75rem;scroll-margin-top:1rem}
.issue.hit>h3,.art.hit{background:var(--mark)}
ol{list-style:none;margin:0;padding:0}
.art{padding:.35rem 0 .85rem}
.art .t{font:500 1.1875rem/1.4 var(--serif)}
.art p{margin:.15rem 0 0;color:var(--muted);font-size:.9375rem}
.art p a{color:var(--accent)}
.jl li{padding:.9rem 0;border-top:1px solid var(--rule)}
.jl li:first-child{border-top:0;padding-top:0}
.jl .t{font:500 1.25rem/1.35 var(--serif)}
.jl p{margin:.2rem 0 0;color:var(--muted);font-size:.9375rem}
footer{margin-top:3rem;padding-top:1.25rem;border-top:1px solid var(--rule);color:var(--muted);font-size:.9375rem;max-width:62ch}
.vh{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
[hidden]{display:none!important}
@media (max-width:40rem){.vol{grid-template-columns:1fr}.vol>h2{position:static;display:flex;align-items:baseline;gap:.6rem;margin-bottom:1rem}
.vlabel{font:400 1.75rem/1 var(--serif);color:var(--rail)}.vnum{font-size:1.75rem}.vyear{margin:0}}
@media (prefers-reduced-motion:no-preference){html{scroll-behavior:smooth}}
"""

FILTER_JS = """
(function(){
var input=document.getElementById('q'),status=document.getElementById('status'),items=[].slice.call(document.querySelectorAll('[data-text]'));
var total=items.length,noun=document.body.getAttribute('data-noun');
function fold(s){return s.toLowerCase().normalize('NFKD').replace(/[\\u0300-\\u036f]/g,'')}
function run(){var terms=fold(input.value).split(/\\s+/).filter(Boolean),shown=0;
items.forEach(function(li){var ok=terms.every(function(t){return li.getAttribute('data-text').indexOf(t)>-1});li.hidden=!ok;if(ok)shown++});
[].forEach.call(document.querySelectorAll('[data-group]'),function(g){g.hidden=!g.querySelector('[data-text]:not([hidden])')});
status.textContent=terms.length?('Showing '+shown+' of '+total+' '+noun+'.'):(total+' '+noun+'.')}
input.addEventListener('input',run);
var p=new URLSearchParams(location.search);
if(p.get('q')){input.value=p.get('q');run()}
var v=p.get('volume'),n=p.get('issue'),y=p.get('year'),a=p.get('article'),target=null;
if(a){target=document.getElementById('a'+a)}
if(!target&&(v||n||y)){[].some.call(document.querySelectorAll('.issue'),function(s){
if((!v||s.dataset.volume===v)&&(!n||s.dataset.number===n)&&(!y||s.dataset.year===y)){target=s;return true}})}
if(target){target.classList.add('hit');var h=target.querySelector('h3')||target;h.setAttribute('tabindex','-1');h.scrollIntoView({block:'start',behavior:'instant'});h.focus({preventScroll:true})}
})();
"""


def page(title, body, noun, crumb=''):
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{html.escape(title)}</title>
<style>{CSS}</style>
</head>
<body data-noun="{noun}">
<a class="skip" href="#list">Skip to the list</a>
<div class="wrap">
{crumb}
{body}
<footer>
<p>Madeleine Clark Wallace Library, Wheaton College. These are the library's archived copies of journal issues licensed from Project MUSE, for current Wheaton students, faculty and staff.</p>
</footer>
</div>
<script>{FILTER_JS}</script>
</body>
</html>
"""


def range_text(lo, hi):
    return lo if lo == hi else f'{lo}–{hi}'


def build_journal(slug, src, holdings):
    issues = defaultdict(list)
    info = {}
    for (vol_folder, issue_id, art_id), exts in holdings.items():
        meta = {}
        hp = src / slug / vol_folder / issue_id / f'{art_id}.html'
        if 'html' in exts and hp.exists():
            meta = parse_article(hp)
            for k in ('journal', 'publisher', 'issn', 'print_issn'):
                if meta.get(k) and not info.get(k):
                    info[k] = meta[k]
        issues[(vol_folder, issue_id)].append((art_id, exts, meta))

    groups = []
    for (vol_folder, issue_id), arts in issues.items():
        designation = next((m['designation'] for _, _, m in arts if m.get('designation')), '')
        vol, number, year = parse_designation(designation, vol_folder)
        arts.sort(key=lambda a: (num(a[2].get('pages')), int(a[0])))
        groups.append({'vol_folder': vol_folder, 'issue_id': issue_id, 'vol': vol, 'number': number, 'year': year,
                       'designation': designation, 'arts': arts})
    groups.sort(key=lambda g: (num(g['vol']), g['year'], num(g['number']), g['issue_id']))

    by_vol = defaultdict(list)
    for g in groups:
        by_vol[g['vol']].append(g)

    journal = info.get('journal') or slug
    years = sorted(g['year'] for g in groups if g['year'])
    vols = list(by_vol)
    n_articles = sum(len(g['arts']) for g in groups)
    issns = []
    if info.get('issn'):
        issns.append(f"ISSN <span class=\"nw\">{html.escape(info['issn'])}</span> (online)")
    if info.get('print_issn'):
        issns.append(f"<span class=\"nw\">{html.escape(info['print_issn'])}</span> (print)")

    about = []
    if info.get('publisher'):
        about.append(f"Published by {html.escape(info['publisher'])}.")
    if issns:
        about.append(', '.join(issns) + '.')
    span = f"Volumes {range_text(vols[0], vols[-1])}" if len(vols) > 1 else f"Volume {vols[0]}"
    if years:
        span += f", {range_text(years[0], years[-1])}"

    out = [f'<header>\n<h1>{html.escape(journal)}</h1>',
           f'<p class="about">{" ".join(about)}</p>' if about else '',
           f'<p class="about">The library holds {html.escape(span)}. Each article title opens the PDF.</p>\n</header>',
           '<div class="find" role="search">',
           '<label for="q">Find an article by title or author</label>',
           '<input id="q" type="search" autocomplete="off" spellcheck="false">',
           f'<p id="status" role="status" aria-live="polite">{n_articles} articles.</p>',
           '</div>', '<main id="list">']
    for vol, vgroups in by_vol.items():
        vyears = sorted({g['year'] for g in vgroups if g['year']})
        vyear = f'<span class="vyear">{range_text(vyears[0], vyears[-1])}</span>' if vyears else ''
        out.append(f'<section class="vol" data-group aria-labelledby="v-{html.escape(vol)}">')
        out.append(f'<h2 id="v-{html.escape(vol)}"><span class="vlabel">Volume</span> <span class="vnum">{html.escape(vol)}</span>{vyear}</h2>')
        out.append('<div>')
        for g in vgroups:
            heading = g['designation'].split(', ', 1)[1] if g['designation'].startswith('Volume') and ', ' in g['designation'] else (g['designation'] or f"Issue {g['issue_id']}")
            anchor = f"v{g['vol']}-n{g['number'] or g['issue_id']}"
            out.append(f'<section class="issue" data-group id="{html.escape(anchor)}" data-volume="{html.escape(g["vol"])}" '
                       f'data-number="{html.escape(g["number"])}" data-year="{html.escape(g["year"])}">')
            out.append(f'<h3>{html.escape(heading)}</h3>\n<ol>')
            for art_id, exts, m in g['arts']:
                base = f"{g['vol_folder']}/{g['issue_id']}/{art_id}"
                title = m.get('title') or f'Article {art_id}'
                authors = ', '.join(m.get('authors', []))
                bits = []
                if authors:
                    bits.append(html.escape(authors) + '.')
                if m.get('type') and m['type'] != 'Article':
                    bits.append(html.escape(m['type']) + '.')
                if m.get('pages'):
                    bits.append(f"Pages {html.escape(m['pages'].replace('-', '–'))}.")
                if 'pdf' in exts:
                    head = f'<a class="t" href="{base}.pdf">{html.escape(title)}</a>'
                    if 'html' in exts:
                        bits.append(f'<a href="{base}.html">Web version</a>')
                else:
                    head = f'<a class="t" href="{base}.html">{html.escape(title)}</a>'
                    bits.append('Web version only.')
                search = fold(' '.join([title, authors]))
                out.append(f'<li class="art" id="a{art_id}" data-text="{html.escape(search)}">{head}<p>{" ".join(bits)}</p></li>')
            out.append('</ol>\n</section>')
        out.append('</div>\n</section>')
    out.append('</main>')

    crumb = '<p class="crumb"><a href="../index.html">All archived Project MUSE journals</a></p>'
    doc = page(f'{journal} | Wallace Library Project MUSE archive', '\n'.join(x for x in out if x), 'articles', crumb)
    summary = {'slug': slug, 'journal': journal, 'publisher': info.get('publisher', ''), 'issn': info.get('issn', ''),
               'print_issn': info.get('print_issn', ''), 'volumes': [vols[0], vols[-1]],
               'years': [years[0], years[-1]] if years else [], 'articles': n_articles}
    return doc, summary


def build_index(summaries):
    summaries = sorted(summaries, key=lambda s: fold(re.sub(r'^(the|a|an)\s+', '', s['journal'], flags=re.I)))
    out = ['<header>\n<h1>Project MUSE journal archive</h1>',
           f'<p class="about">Back issues of {len(summaries)} journal{"s" if len(summaries) != 1 else ""} the library licensed from Project MUSE. '
           'Choose a journal to see its volumes and articles.</p>\n</header>',
           '<div class="find" role="search">', '<label for="q">Find a journal by title, publisher or ISSN</label>',
           '<input id="q" type="search" autocomplete="off" spellcheck="false">',
           f'<p id="status" role="status" aria-live="polite">{len(summaries)} journal{"s" if len(summaries) != 1 else ""}.</p>', '</div>',
           '<main id="list">\n<ol class="jl">']
    for s in summaries:
        v, y = s['volumes'], s['years']
        span = f"Volumes {range_text(v[0], v[1])}" if v[0] != v[1] else f"Volume {v[0]}"
        if y:
            span += f" ({range_text(y[0], y[1])})"
        meta = ' '.join(x for x in [html.escape(s['publisher']) + '.' if s['publisher'] else '', html.escape(span) + '.',
                                     f"{s['articles']} articles."] if x)
        search = fold(' '.join([s['journal'], s['publisher'], s['issn'], s['print_issn']]))
        out.append(f'<li data-text="{html.escape(search)}"><a class="t" href="{s["slug"]}/index.html">{html.escape(s["journal"])}</a><p>{meta}</p></li>')
    out.append('</ol>\n</main>')
    return page('Project MUSE journal archive | Wallace Library', '\n'.join(out), 'journals')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--slug', action='append', required=True, help='journal folder; repeat for several')
    ap.add_argument('--src', default='work', type=Path, help='folder holding downloaded <slug>/ HTML')
    ap.add_argument('--listing', default='muse_listing.txt')
    ap.add_argument('--out', default='site', type=Path)
    ap.add_argument('--meta', default='work/meta', type=Path)
    a = ap.parse_args()

    a.meta.mkdir(parents=True, exist_ok=True)
    holdings = read_listing(a.listing, a.slug)
    for slug in a.slug:
        if not holdings[slug]:
            raise SystemExit(f'no article-level keys for {slug} in {a.listing}')
        doc, summary = build_journal(slug, a.src, holdings[slug])
        (a.out / slug).mkdir(parents=True, exist_ok=True)
        (a.out / slug / 'index.html').write_text(doc, encoding='utf-8', newline='\n')
        (a.meta / f'{slug}.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding='utf-8')
        print(f"{slug}: {summary['articles']} articles, volumes {summary['volumes']}, years {summary['years']}")
    summaries = [json.loads(p.read_text(encoding='utf-8')) for p in sorted(a.meta.glob('*.json'))]
    (a.out / 'index.html').write_text(build_index(summaries), encoding='utf-8', newline='\n')
    print(f'index: {len(summaries)} journal(s)')


if __name__ == '__main__':
    main()
