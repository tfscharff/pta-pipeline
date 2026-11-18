"""Build static journal landing pages and a top-level index from the article files in the bucket.

The bucket listing says which articles exist. Their metadata (title, authors, volume/issue/year, pages,
ISSNs) is read from each article's HTML, and from the PDF where there is no usable HTML, by the parsing
profile named in config.json (see profiles/citation_meta.py). Writes:

  <meta>/<slug>.articles.json  parsed article metadata cache (so pages rebuild without re-downloading)
  <meta>/<slug>.pdf.json       metadata read from PDFs
  <meta>/<slug>.json           journal summary used to build the top-level index and the KBART
  <out>/<slug>/index.html      one page per journal
  <out>/index.html             top-level journal list (every summary in <meta>)

With --fetch, each journal's article HTML is synced from S3 into a temporary folder under --tmp,
parsed into the cache, and the temporary folder is removed. Otherwise HTML is read from <src>/<slug>/
when present, falling back to the cache. With --upload, pages go to S3 as STANDARD text/html.
Journals with a cache are skipped by --fetch, so a re-run resumes where it stopped.

With --pdf (needs `pip install pypdf`), articles the HTML gave no title (usually PDF-only ones) are read
from the PDF. --refetch-pdf ignores that cache. With --reparse-html, cached articles that still lack a
title or an issue designation are downloaded and parsed again, which is how an improved profile is
applied without re-reading everything.

Usage:
  python scripts/build_pages.py --slug <journal folder>
  python scripts/build_pages.py --all --fetch --upload --tmp <scratch dir>
  python scripts/build_pages.py --all --pdf --upload --tmp <scratch dir>
"""
import argparse
import html
import json
import re
import shutil
import subprocess
import time
from collections import defaultdict
from pathlib import Path

from common import first, fold, load_config, load_profile, need

# <slug>/<vNNN|preprint>/<issue>/[<id>/]<id>.<ext>  (a profile can supply its own ARTICLE_KEY)
ARTICLE_KEY = re.compile(r'^(?P<slug>[^/]+)/(?P<vol>v[^/]+|preprint)/(?P<issue>[^/]+)/(?:(?P<sub>\d+)/)?(?P<id>\d+)\.(?P<ext>pdf|html)$')
CFG = PROFILE = None


def setup(cfg):
    """Load the configuration and its parsing profile into the module."""
    global CFG, PROFILE, ARTICLE_KEY
    CFG, PROFILE = cfg, load_profile(cfg)
    ARTICLE_KEY = getattr(PROFILE, 'ARTICLE_KEY', ARTICLE_KEY)


def parse_article(path):
    """Metadata for one article page, from the profile. Some ".html" objects are really the article PDF."""
    with open(path, 'rb') as f:
        is_pdf = f.read(5) == b'%PDF-'
    if is_pdf:
        try:
            meta = PROFILE.parse_pdf(path, path.stem)
        except Exception as e:
            meta = {'src': 'pdf', 'title': '', 'error': f'{type(e).__name__}: {e}'}
        return {**meta, 'html_is_pdf': True}
    return PROFILE.parse_article(path)


def tkey(s):
    return re.sub(r'[^a-z0-9]+', '', fold(re.sub(r'\s*\(review\)\s*$', '', s or '', flags=re.I)))


def by_issue(metas):
    out = defaultdict(list)
    for b in metas:
        out[b.rsplit('/', 1)[0]].append(b)
    return out


def blank(m):
    return not m.get('title') and not m.get('missing') and not m.get('html_is_pdf') and (m.get('prev') or m.get('next'))


def neighbour_anchors(metas):
    """Titled articles next to an untitled one whose own previous/next titles are not on file yet."""
    out = []
    for bases in by_issue(metas).values():
        wanted = {tkey(metas[b].get(k)) for b in bases if blank(metas[b]) for k in ('prev', 'next')} - {''}
        out += [b for b in bases if metas[b].get('title') and 'prev' not in metas[b] and metas[b].get('src') != 'pdf'
                and tkey(metas[b]['title']) in wanted]
    return out


def fill_from_neighbours(metas):
    """An article page names the previous and next article of its issue, so an untitled article's title
    can be read from the page before or after it. Fills metas in place and returns the bases it filled."""
    filled = []
    for bases in by_issue(metas).values():
        todo = [b for b in bases if blank(metas[b])]
        while todo:
            titles = defaultdict(list)
            for b in bases:
                if metas[b].get('title'):
                    titles[tkey(metas[b]['title'])].append(b)
            done = []
            for b in todo:
                for mine, theirs in (('prev', 'next'), ('next', 'prev')):
                    hit = titles.get(tkey(metas[b].get(mine)), [])
                    # the neighbour must be the only article of that title, and name exactly one untitled page
                    if len(hit) == 1 and metas[hit[0]].get(theirs) and sum(
                            1 for o in todo if tkey(metas[o].get(mine)) == tkey(metas[b].get(mine))) == 1:
                        t = metas[hit[0]][theirs]
                        review = re.search(r'\s*\(review\)\s*$', t, re.I)
                        metas[b] = {**metas[b], 'title': t[:review.start()] if review else t, 'title_from': 'neighbour',
                                    **({'type': 'Review'} if review and not metas[b].get('type') else {})}
                        done.append(b)
                        break
            if not done:
                break
            filled += done
            todo = [b for b in todo if b not in done]
    return filled


def num(s, default=10**9):
    m = re.search(r'\d+', s or '')
    return int(m.group()) if m else default


def parse_designation(d, vol_folder):
    vol = first(r'Volume\s+([^,]+)', d, 0) or vol_folder.lstrip('v').lstrip('0') or vol_folder
    number = first(r'Numbers?\s+([^,]+)', d, 0)
    years = re.findall(r'\b(1[6-9]\d\d|20\d\d)\b', d)
    return vol.strip(), number.strip(), years[-1] if years else ''


def read_listing(listing, slugs=None):
    """Return {slug: {(vol, issue, id): {'exts': set, 'base': key without extension}}}, one streaming pass.

    slugs=None collects every journal."""
    out = defaultdict(dict)
    with open(listing, encoding='utf-8') as f:
        for line in f:
            key = line.split('\t', 1)[0]
            m = ARTICLE_KEY.match(key)
            if not m or (slugs is not None and m['slug'] not in slugs):
                continue
            if m.groupdict().get('sub') and m['sub'] != m['id']:
                continue
            rec = out[m['slug']].setdefault((m['vol'], m['issue'], m['id']),
                                            {'exts': set(), 'base': key[len(m['slug']) + 1:key.rindex('.')]})
            rec['exts'].add(m['ext'])
    return out


def parse_journal_html(slug, src, holdings):
    """Parse every available article HTML under src/<slug>/ into {base: meta}."""
    metas = {}
    for rec in holdings.values():
        hp = src / slug / f"{rec['base']}.html"
        if 'html' in rec['exts'] and hp.exists():
            try:
                metas[rec['base']] = parse_article(hp)
            except Exception as e:  # one bad file must not stop the journal
                metas[rec['base']] = {'error': str(e)}
    return metas


def fetch_parse_pdfs(slug, bases, tmp, log, chunk=400, ext='pdf'):
    """Download the given article PDFs (or HTML with ext='html') in batches, parse each, delete them.
    Returns ({base: meta}, failures)."""
    import logging
    logging.getLogger('pypdf').setLevel(logging.ERROR)  # font-table warnings are noise
    metas, failures = {}, []
    dest = tmp / slug
    for i in range(0, len(bases), chunk):
        part = bases[i:i + chunk]
        cmd = ['aws', 's3', 'cp', f"s3://{need(CFG, 'bucket')}/{slug}/", str(dest), '--recursive', '--only-show-errors', '--exclude', '*']
        for b in part:
            cmd += ['--include', f'{b}.{ext}']
        rc = subprocess.run(cmd, capture_output=True, text=True)
        for b in part:
            p = dest / f'{b}.{ext}'
            if not p.exists():
                failures.append(b)
                continue
            try:
                metas[b] = PROFILE.parse_pdf(p, b.rsplit('/', 1)[-1]) if ext == 'pdf' else parse_article(p)
            except Exception as e:
                metas[b] = {'src': ext, 'error': f'{type(e).__name__}: {e}'}
        shutil.rmtree(dest, ignore_errors=True)
        if rc.returncode:
            log(f'  {slug}: cp rc={rc.returncode} {rc.stderr.strip()[:300]}')
    return metas, failures


def sync_html(slug, dest):
    """Download a journal's article-level HTML (not figure pages) from S3."""
    # --force-glacier-transfer: the CLI otherwise skips GLACIER/DEEP_ARCHIVE objects even when restored.
    cmd = ['aws', 's3', 'sync', f"s3://{need(CFG, 'bucket')}/{slug}/", str(dest / slug), '--only-show-errors',
           '--force-glacier-transfer', '--exclude', '*', '--include', '*.html']
    for d in getattr(PROFILE, 'ASSET_DIRS', ()):
        cmd += ['--exclude', f'*/{d}/*']
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, r.stderr.strip()


def upload(local, key):
    cmd = ['aws', 's3', 'cp', str(local), f"s3://{need(CFG, 'bucket')}/{key}", '--only-show-errors', '--storage-class', 'STANDARD',
           '--content-type', 'text/html; charset=utf-8', '--cache-control', 'max-age=300']
    subprocess.run(cmd, check=True)


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
var v=p.get('volume')||p.get('quickLinkVolume'),n=p.get('issue')||p.get('quickLinkIssue'),y=p.get('year')||p.get('quickLinkYear'),a=p.get('article'),target=null;
if(a){target=document.getElementById('a'+a)}
// Article links from WorldCat Discovery arrive with the article title in Title= (see README, link scheme).
// One exact match opens that article; several are listed; a near match is highlighted; else say so.
var T=p.get('Title')||p.get('atitle');
function key(s){return fold(s).replace(/\\(review\\)\\s*$/,'').replace(/[^a-z0-9]+/g,' ').trim()}
if(!target&&T){var want=key(T),exact=[],near=null,best=0,words=want.split(' ').filter(function(w){return w.length>2});
items.forEach(function(li){var link=li.querySelector('a.t');if(!link)return;var t=key(link.textContent);if(!t)return;
if(t===want){exact.push(li);return}
var hit=0;words.forEach(function(w){if((' '+t+' ').indexOf(' '+w+' ')>-1)hit++});
var score=words.length?hit/Math.max(words.length,t.split(' ').filter(function(w){return w.length>2}).length):0;
if(score>best){best=score;near=li}});
if(exact.length===1){location.replace(exact[0].querySelector('a.t').href);return}
if(exact.length>1){var set=exact;items.forEach(function(li){li.hidden=set.indexOf(li)<0});
[].forEach.call(document.querySelectorAll('[data-group]'),function(g){g.hidden=!g.querySelector('[data-text]:not([hidden])')});
status.textContent=exact.length+' '+noun+' have the title you followed. Clear the search box to see everything.';input.value=T;v=n=y=null}
else if(near&&best>=0.8){target=near}
else{status.textContent='We could not match the article you followed ('+T+'). Browse by volume or search below.';}}
if(!target&&(v||n||y)){[].some.call(document.querySelectorAll('.issue'),function(s){
if((!v||s.dataset.volume===v)&&(!n||s.dataset.number===n)&&(!y||s.dataset.year===y)){target=s;return true}})}
if(target){target.classList.add('hit');var h=target.querySelector('h3')||target;h.setAttribute('tabindex','-1');h.scrollIntoView({block:'start',behavior:'instant'});h.focus({preventScroll:true})}
})();
"""


def page(title, body, noun, crumb=''):
    footer = html.escape(CFG['site']['footer'], quote=False)
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
<p>{footer}</p>
</footer>
</div>
<script>{FILTER_JS}</script>
</body>
</html>
"""


def range_text(lo, hi):
    return lo if lo == hi else f'{lo}–{hi}'


def vol_span(vols):
    vols = [v for v in vols if v]
    if not vols:
        return ''
    return f"Volumes {range_text(vols[0], vols[-1])}" if vols[0] != vols[-1] else f"Volume {vols[0]}"


def build_journal(slug, holdings, metas):
    issues = defaultdict(list)
    preprints = []
    info, pdf_info = {}, {}
    missing = 0
    for (vol_folder, issue_id, art_id), rec in holdings.items():
        meta = metas.get(rec['base']) or {}
        if meta.get('missing') and 'pdf' not in rec['exts']:  # an error page and nothing else
            missing += 1
            continue
        into = pdf_info if meta.get('src') == 'pdf' else info  # prefer journal details from article HTML
        for k in ('journal', 'publisher', 'issn', 'print_issn'):
            if meta.get(k) and not into.get(k):
                into[k] = meta[k]
        item = (art_id, rec['exts'], meta, rec['base'])
        if vol_folder == 'preprint':
            preprints.append(item)
        else:
            issues[(vol_folder, issue_id)].append(item)

    info = {**pdf_info, **info}
    groups = []
    for (vol_folder, issue_id), arts in issues.items():
        designation = next((m['designation'] for _, _, m, _ in arts if m.get('designation')), '')
        vol, number, year = parse_designation(designation, vol_folder)
        arts.sort(key=lambda a: (num(a[2].get('pages')), int(a[0])))
        groups.append({'vol_folder': vol_folder, 'issue_id': issue_id, 'vol': vol, 'number': number, 'year': year,
                       'designation': designation, 'arts': arts})
    groups.sort(key=lambda g: (num(g['vol']), g['year'], num(g['number']), g['issue_id']))

    by_vol = defaultdict(list)
    for g in groups:
        by_vol[g['vol']].append(g)

    journal = info.get('journal') or slug.replace('_', ' ').title()
    years = sorted(g['year'] for g in groups if g['year'])
    vols = list(by_vol) or ['']
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
    span = vol_span(vols)
    if years:
        span += f", {range_text(years[0], years[-1])}"
    holds = f'The library holds {html.escape(span)}. ' if span else ''
    site = CFG['site']
    gone = (f" {missing} article{' is' if missing == 1 else 's are'} not listed because "
            f"{html.escape(site['provider'], quote=False)} supplied an error page in place of the article.") if missing else ''

    out = [f'<header>\n<h1>{html.escape(journal)}</h1>',
           f'<p class="about">{" ".join(about)}</p>' if about else '',
           f'<p class="about">{holds}Each article title opens the PDF.{gone}</p>\n</header>',
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
            out.extend(article_li(*a) for a in g['arts'])
            out.append('</ol>\n</section>')
        out.append('</div>\n</section>')
    if preprints:
        preprints.sort(key=lambda a: int(a[0]))
        out.append('<section class="vol" data-group aria-labelledby="v-early">')
        out.append('<h2 id="v-early"><span class="vlabel">Early release</span></h2>\n<div>')
        out.append('<section class="issue" data-group id="early"><h3>Articles published online before their issue</h3>\n<ol>')
        out.extend(article_li(*a) for a in preprints)
        out.append('</ol>\n</section>\n</div>\n</section>')
    out.append('</main>')

    crumb = f'<p class="crumb"><a href="../index.html">{html.escape(site["all_journals"], quote=False)}</a></p>'
    doc = page(f"{journal} | {site['journal_title_suffix']}", '\n'.join(x for x in out if x), 'articles', crumb)
    all_arts = [a for g in groups for a in g['arts']] + preprints
    summary = {'slug': slug, 'journal': journal, 'publisher': info.get('publisher', ''), 'issn': info.get('issn', ''),
               'print_issn': info.get('print_issn', ''), 'volumes': [vols[0], vols[-1]],
               'years': [years[0], years[-1]] if years else [], 'articles': len(all_arts),
               'issues': [{'volume': g['vol'], 'number': g['number'], 'year': g['year'], 'designation': g['designation'],
                           'folder': f"{g['vol_folder']}/{g['issue_id']}", 'articles': len(g['arts'])} for g in groups],
               'quality': {'no_metadata': sum(1 for a in all_arts if not a[2].get('title')),
                           'no_designation': sum(1 for g in groups if not g['designation']),
                           'parse_errors': sum(1 for a in all_arts if a[2].get('error')),
                           'from_pdf': sum(1 for a in all_arts if a[2].get('src') == 'pdf' and a[2].get('title')),
                           'preprints': len(preprints), 'missing': missing}}
    return doc, summary


def article_li(art_id, exts, m, base):
    title = m.get('title') or 'Untitled article (no title on file)'
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
        if 'html' in exts and not m.get('missing'):
            bits.append(f'<a href="{base}.html">Web version</a>')
    else:
        head = f'<a class="t" href="{base}.html">{html.escape(title)}</a>'
        if not m.get('html_is_pdf'):
            bits.append('Web version only.')
    search = fold(' '.join([title, authors]))
    return f'<li class="art" id="a{art_id}" data-text="{html.escape(search)}">{head}<p>{" ".join(bits)}</p></li>'


def build_index(summaries):
    summaries = sorted(summaries, key=lambda s: fold(re.sub(r'^(the|a|an)\s+', '', s['journal'], flags=re.I)))
    site = CFG['site']
    count = f'{len(summaries)} journal{"s" if len(summaries) != 1 else ""}'
    intro = site['index_intro'].format(count=count).strip()  # an empty index_intro leaves the line out
    h1_class = '' if site['show_index_heading'] else ' class="vh"'  # hidden, but kept for screen readers
    out = [f'<header>\n<h1{h1_class}>{html.escape(site["archive_name"], quote=False)}</h1>'
           + (f'\n<p class="about">{html.escape(intro, quote=False)}</p>' if intro else '') + '\n</header>',
           '<div class="find" role="search">', '<label for="q">Find a journal by title, publisher or ISSN</label>',
           '<input id="q" type="search" autocomplete="off" spellcheck="false">',
           f'<p id="status" role="status" aria-live="polite">{len(summaries)} journal{"s" if len(summaries) != 1 else ""}.</p>', '</div>',
           '<main id="list">\n<ol class="jl">']
    for s in summaries:
        v, y = s['volumes'], s['years']
        span = vol_span(v)
        if y:
            span += f" ({range_text(y[0], y[1])})"
        # two lines under the title: publisher, then coverage
        meta = ''.join(f'<p>{html.escape(x)}.</p>' for x in (s['publisher'], span) if x)
        search = fold(' '.join([s['journal'], s['publisher'], s['issn'], s['print_issn']]))
        out.append(f'<li data-text="{html.escape(search)}"><a class="t" href="{s["slug"]}/index.html">{html.escape(s["journal"])}</a>{meta}</li>')
    out.append('</ol>\n</main>')
    return page(site['index_title'], '\n'.join(out), 'journals')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sel = ap.add_mutually_exclusive_group(required=True)
    sel.add_argument('--slug', action='append', help='journal folder; repeat for several')
    sel.add_argument('--all', action='store_true', help='every journal in the listing')
    ap.add_argument('--src', default='work', type=Path, help='folder holding downloaded <slug>/ HTML (without --fetch)')
    ap.add_argument('--fetch', action='store_true', help='sync HTML from S3 into --tmp, parse, then remove it')
    ap.add_argument('--refetch', action='store_true', help='with --fetch, re-download journals that already have a cache')
    ap.add_argument('--pdf', action='store_true',
                    help='for articles the HTML gave no title (PDF-only), read title etc. from the PDF (needs pypdf)')
    ap.add_argument('--refetch-pdf', action='store_true', help='with --pdf, redo journals that already have a PDF cache')
    ap.add_argument('--reparse-html', action='store_true',
                    help='re-download and re-parse cached articles that lack a title or an issue designation '
                         '(use after improving the parsing profile)')
    ap.add_argument('--tmp', type=Path, help='temporary download folder for --fetch / --pdf / --reparse-html')
    ap.add_argument('--upload', action='store_true', help='upload pages to S3 (STANDARD, text/html)')
    ap.add_argument('--config', help='configuration file (default: config.json in the repository root)')
    ap.add_argument('--listing', help='bucket listing (default: "listing" in the config)')
    ap.add_argument('--out', default='site', type=Path)
    ap.add_argument('--meta', default='work/meta', type=Path)
    ap.add_argument('--log', default='logs/build_pages.log', type=Path)
    a = ap.parse_args()
    if (a.fetch or a.pdf) and not a.tmp:
        ap.error('--fetch and --pdf need --tmp')

    setup(load_config(a.config))
    a.listing = a.listing or CFG['listing']
    rev = getattr(PROFILE, 'REVISION', '1')
    a.meta.mkdir(parents=True, exist_ok=True)
    a.log.parent.mkdir(parents=True, exist_ok=True)

    def log(msg):
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        print(line, flush=True)
        with open(a.log, 'a', encoding='utf-8') as f:
            f.write(line + '\n')

    holdings = read_listing(a.listing, None if a.all else set(a.slug))
    slugs = sorted(holdings) if a.all else a.slug
    log(f'start: {len(slugs)} journal(s), fetch={a.fetch}, upload={a.upload}')
    for i, slug in enumerate(slugs, 1):
        if not holdings.get(slug):
            log(f'[{i}/{len(slugs)}] {slug}: SKIP, no article-level keys in listing')
            continue
        cache = a.meta / f'{slug}.articles.json'
        if a.fetch and (a.refetch or not cache.exists()):
            t0 = time.time()
            rc, err = sync_html(slug, a.tmp)
            # rc 2 = some files skipped. Skips for storage class (object not restored) are failures: no cache is
            # written, so a re-run retries the journal.
            if rc not in (0, 2) or 'storage class' in err or 'restore the object' in err:
                log(f'[{i}/{len(slugs)}] {slug}: SYNC FAILED rc={rc} {err[:500]}')
                shutil.rmtree(a.tmp / slug, ignore_errors=True)
                continue
            metas = parse_journal_html(slug, a.tmp, holdings[slug])
            cache.write_text(json.dumps(metas, ensure_ascii=False), encoding='utf-8')
            shutil.rmtree(a.tmp / slug, ignore_errors=True)
            log(f'[{i}/{len(slugs)}] {slug}: fetched and parsed {len(metas)} HTML in {time.time() - t0:.0f}s'
                + (f' (sync rc=2: {err[:300]})' if rc == 2 else ''))
        elif not a.fetch and (a.src / slug).exists():
            metas = parse_journal_html(slug, a.src, holdings[slug])
            cache.write_text(json.dumps(metas, ensure_ascii=False), encoding='utf-8')
        elif cache.exists():
            metas = json.loads(cache.read_text(encoding='utf-8'))
        else:
            metas = {}
        if a.reparse_html:
            # Gaps a better profile may fill. An article with no title is retried once per profile
            # REVISION; one with a title but no journal or issue designation is retried once. Re-read
            # entries are stamped with the revision.
            want = [b for b, m in metas.items() if not m.get('error') and (
                (m.get('title') and not m.get('journal') and not m.get('designation') and not m.get('reparsed'))
                or (not m.get('title') and m.get('reparsed') != rev))]
            if want:
                t0 = time.time()
                got, fails = fetch_parse_pdfs(slug, want, a.tmp, log, ext='html')
                for b, m in got.items():
                    # keep the old entry only when the page itself could not be parsed
                    metas[b] = {**(metas[b] if m.get('src') == 'html' else m), 'reparsed': rev}
                cache.write_text(json.dumps(metas, ensure_ascii=False), encoding='utf-8')  # failures retry next run
                log(f'[{i}/{len(slugs)}] {slug}: re-parsed {len(got)} HTML in {time.time() - t0:.0f}s'
                    + (f', HTML FETCH FAILED for {len(fails)}, e.g. {fails[0]}' if fails else ''))
        # PDF-derived metadata fills articles the HTML could not (cached in <slug>.pdf.json).
        pdf_cache = a.meta / f'{slug}.pdf.json'
        pdf_metas = json.loads(pdf_cache.read_text(encoding='utf-8')) if pdf_cache.exists() else {}
        if a.pdf and (a.refetch_pdf or not pdf_cache.exists()):
            want = [rec['base'] for rec in holdings[slug].values()
                    if 'pdf' in rec['exts'] and not (metas.get(rec['base']) or {}).get('title')]
            if want:
                t0 = time.time()
                pdf_metas, fails = fetch_parse_pdfs(slug, want, a.tmp, log)
                if fails:  # no cache, so a re-run retries this journal
                    log(f'[{i}/{len(slugs)}] {slug}: PDF FETCH FAILED for {len(fails)} of {len(want)}, e.g. {fails[0]}')
                else:
                    pdf_cache.write_text(json.dumps(pdf_metas, ensure_ascii=False), encoding='utf-8')
                log(f'[{i}/{len(slugs)}] {slug}: parsed {len(pdf_metas)} PDFs in {time.time() - t0:.0f}s')
        if a.reparse_html:
            # Articles whose HTML has no title (mostly reviews) but which have a PDF: read the PDF cover.
            want = [rec['base'] for rec in holdings[slug].values() if 'pdf' in rec['exts']
                    and rec['base'] in metas and not metas[rec['base']].get('title') and rec['base'] not in pdf_metas]
            if want:
                t0 = time.time()
                got, fails = fetch_parse_pdfs(slug, want, a.tmp, log)
                pdf_metas.update(got)
                pdf_cache.write_text(json.dumps(pdf_metas, ensure_ascii=False), encoding='utf-8')
                log(f'[{i}/{len(slugs)}] {slug}: parsed {len(got)} PDFs for untitled HTML in {time.time() - t0:.0f}s'
                    + (f', PDF FETCH FAILED for {len(fails)}, e.g. {fails[0]}' if fails else ''))
        html_metas = dict(metas)
        for b, pm in pdf_metas.items():
            m = metas.get(b) or {}
            if not m.get('title'):  # keep whatever the HTML did give (designation, pages, type)
                metas[b] = {**pm, **{k: v for k, v in m.items() if v}}
        if a.reparse_html:
            want = [b for b in neighbour_anchors(metas) if b in html_metas]
            if want:
                got, fails = fetch_parse_pdfs(slug, want, a.tmp, log, ext='html')
                for b, m in got.items():
                    extra = {'prev': m.get('prev', ''), 'next': m.get('next', '')}
                    metas[b], html_metas[b] = {**metas[b], **extra}, {**html_metas[b], **extra}
            filled = fill_from_neighbours(metas)
            for b in filled:
                html_metas[b] = {**html_metas[b], **{k: metas[b][k] for k in ('title', 'title_from', 'type') if k in metas[b]}}
            if want or filled:
                cache.write_text(json.dumps(html_metas, ensure_ascii=False), encoding='utf-8')
                log(f'[{i}/{len(slugs)}] {slug}: read {len(want)} neighbouring pages, titled {len(filled)} from them')
        doc, summary = build_journal(slug, holdings[slug], metas)
        (a.out / slug).mkdir(parents=True, exist_ok=True)
        page_path = a.out / slug / 'index.html'
        page_path.write_text(doc, encoding='utf-8', newline='\n')
        (a.meta / f'{slug}.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding='utf-8')
        if a.upload:
            upload(page_path, f'{slug}/index.html')
        q = summary['quality']
        log(f"[{i}/{len(slugs)}] {slug}: {summary['articles']} articles, volumes {summary['volumes']}, "
            f"years {summary['years']}, no_metadata={q['no_metadata']}, no_designation={q['no_designation']}, "
            f"errors={q['parse_errors']}, preprints={q['preprints']}, from_pdf={q['from_pdf']}{', uploaded' if a.upload else ''}")
    summaries = [json.loads(p.read_text(encoding='utf-8')) for p in sorted(a.meta.glob('*.json'))
                 if not p.name.endswith(('.articles.json', '.pdf.json'))]
    (a.out / 'index.html').write_text(build_index(summaries), encoding='utf-8', newline='\n')
    if a.upload:
        upload(a.out / 'index.html', 'index.html')
    log(f'done: index lists {len(summaries)} journal(s)')


if __name__ == '__main__':
    main()
