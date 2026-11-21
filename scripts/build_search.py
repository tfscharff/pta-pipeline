"""Build the article search: the search page, the relay package for AWS Lambda, and a research-guide search box.

Reads the journal summaries in <meta> (written by build_pages.py) and the "search" section of config.json. Writes:

  <out>/search.html              the search page, at the root of the archive next to index.html
  <work>/relay/                  search/relay.py with settings.json and titles.json, ready to zip
  <work>/relay.zip               the same, as the Lambda deployment package
  <work>/widget.html             HTML for a LibGuides (or any web page) box that opens the search through the proxy

With --upload, search.html goes to S3 as STANDARD text/html. Updating the Lambda code is a separate step
(see README, article search), because the journal list it carries changes only when journals are added.

Usage:
  python scripts/build_search.py
  python scripts/build_search.py --upload
"""
import argparse
import html
import json
import shutil
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'search'))
import build_pages  # noqa: E402
from common import ROOT, load_config, need  # noqa: E402
from relay import issn_key, title_key  # noqa: E402

SEARCH_JS = """
(function(){
var form=document.getElementById('sform'),input=document.getElementById('sq'),status=document.getElementById('status'),
list=document.getElementById('results'),more=document.getElementById('more'),next=null,shown=0,query='';
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
function item(r){var cite=[];if(r.volume)cite.push('Volume '+esc(r.volume));if(r.issue)cite.push('Number '+esc(r.issue));
var where='<i>'+esc(r.journal)+'</i>'+(cite.length?', '+cite.join(', '):'')+(r.year?' ('+esc(r.year)+')':'')+(r.pages?', pages '+esc(r.pages.replace('-','\\u2013')):'')+'.';
return '<li class="art"><a class="t" href="'+esc(r.link)+'">'+esc(r.title||'Untitled article')+'</a><p>'
+(r.authors&&r.authors.length?esc(r.authors.join('; '))+'. ':'')+where+'</p></li>'}
function say(){status.textContent=shown?(shown+' article'+(shown==1?'':'s')+' found in the archive'+(next!=null?' so far.':'.')):
(next!=null?'Searching\\u2026':'No articles in the archive match \\u201c'+query+'\\u201d. Try fewer words, or other spellings.')}
function load(offset,tries){status.textContent='Searching\\u2026';more.hidden=true;
return fetch('api/search?q='+encodeURIComponent(query)+'&offset='+offset,{credentials:'same-origin'})
.then(function(r){return r.json().then(function(d){if(!r.ok)throw new Error(d.error||'The search failed.');return d})})
.then(function(d){var first=null;d.results.forEach(function(r){list.insertAdjacentHTML('beforeend',item(r));if(!first)first=list.lastElementChild});
shown+=d.results.length;next=d.next;
// a page of index results can hold none from the archive; look a little further before giving up
if(!d.results.length&&next!=null&&tries<4)return load(next,tries+1);
more.hidden=next==null;say();return first})
.catch(function(e){status.textContent=e.message||'The search failed. Try again in a minute.';more.hidden=true})}
more.addEventListener('click',function(){load(next,0).then(function(first){if(first)first.querySelector('a').focus()})});
var p=new URLSearchParams(location.search);query=(p.get('q')||'').trim();input.value=query;
if(query){document.title=query+' | '+document.title;load(0,0)}else{input.focus()}
})();
"""


def titles_index(meta):
    """The archive's journals for the relay: {journals: {slug: {title, years}}, issn: {ISSN: slug}, title: {key: slug}}."""
    out = {'journals': {}, 'issn': {}, 'title': {}}
    for p in sorted(meta.glob('*.json')):
        if p.name.endswith(('.articles.json', '.pdf.json')):
            continue
        s = json.loads(p.read_text(encoding='utf-8'))
        years = sorted({i['year'] for i in s.get('issues', []) if i.get('year')})
        out['journals'][s['slug']] = {'title': s['journal'], 'years': years}
        for i in (s.get('issn'), s.get('print_issn')):
            if issn_key(i):
                out['issn'][issn_key(i)] = s['slug']
        out['title'].setdefault(title_key(s['journal']), s['slug'])
    return out


def search_page(cfg):
    sc, site = cfg['search'], cfg['site']
    body = '\n'.join([
        f'<header>\n<h1>{html.escape(sc["title"], quote=False)}</h1>\n</header>',
        '<form class="find" id="sform" role="search" action="search.html" method="get">',
        f'<label for="sq">{html.escape(sc["label"], quote=False)}</label>',
        '<div class="row"><input id="sq" name="q" type="search" autocomplete="off" required>'
        '<button type="submit">Search</button></div>',
        '<p>Searches article titles, authors, subjects and abstracts. Each result opens the article in the archive.</p>',
        '</form>',
        '<p id="status" role="status" aria-live="polite"></p>',
        '<main id="list">\n<ol id="results"></ol>',
        '<p><button id="more" type="button" hidden>More results</button></p>\n</main>'])
    crumb = f'<p class="crumb"><a href="index.html">{html.escape(site["all_journals"], quote=False)}</a></p>'
    doc = build_pages.page(f'{sc["title"]} | {site["index_title"]}', body, 'articles', crumb, script=SEARCH_JS)
    style = ('.row{display:flex;flex-wrap:wrap;gap:.5rem;max-width:38rem}.row input{flex:1 1 14rem;min-width:0}'
             '.row button,#more{font:inherit;font-weight:600;color:var(--bg);background:var(--accent);border:0;border-radius:4px;'
             'padding:.55rem 1.2rem;cursor:pointer}#status{color:var(--muted)}#results .art p i{font-style:italic}</style>')
    return doc.replace('</style>', style, 1)


def widget(cfg):
    sc = cfg['search']
    target = need(cfg, 'base_url') + '/search.html?q='
    prefix = sc['proxy_login']
    label = html.escape(sc['label'], quote=False)
    # The proxy takes everything after "url=" as the address, so the query is appended encoded, by script:
    # a plain GET form would replace the "?url=" part of its action.
    return f"""<form class="pta-search" id="pta-search" role="search">
  <label for="pta-search-q" style="display:block;font-weight:bold;margin-bottom:.4em">{label}</label>
  <div style="display:flex;flex-wrap:wrap;gap:.5em;max-width:36em">
    <input id="pta-search-q" name="q" type="search" required style="flex:1 1 14em;min-width:0;padding:.5em;font:inherit;border:1px solid #767676;border-radius:3px">
    <button type="submit" style="padding:.5em 1.2em;font:inherit;font-weight:bold;color:#fff;background:#1D3D6E;border:1px solid #1D3D6E;border-radius:3px;cursor:pointer">Search</button>
  </div>
</form>
<script>
document.getElementById('pta-search').addEventListener('submit', function (e) {{
  e.preventDefault();
  var q = this.q.value.trim();
  if (q) window.open({json.dumps(prefix + target)} + encodeURIComponent(q), '_blank', 'noopener');
}});
</script>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--config', help='configuration file (default: config.json in the repository root)')
    ap.add_argument('--meta', default='work/meta', type=Path)
    ap.add_argument('--out', default='site', type=Path)
    ap.add_argument('--work', default='work/search', type=Path)
    ap.add_argument('--upload', action='store_true', help='upload search.html to S3 (STANDARD, text/html)')
    a = ap.parse_args()
    cfg = load_config(a.config)
    build_pages.setup(cfg)
    sc = cfg['search']

    titles = titles_index(a.meta)
    if not titles['journals']:
        raise SystemExit(f'no journal summaries in {a.meta}; run build_pages.py first')
    pkg = a.work / 'relay'
    shutil.rmtree(pkg, ignore_errors=True)
    pkg.mkdir(parents=True)
    shutil.copy(ROOT / 'search' / 'relay.py', pkg / 'relay.py')
    settings = {k: sc[k] for k in ('db_ids', 'item_sub_type', 'held_by_institution')}
    (pkg / 'settings.json').write_text(json.dumps(settings, indent=1), encoding='utf-8')
    (pkg / 'titles.json').write_text(json.dumps(titles, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    with zipfile.ZipFile(a.work / 'relay.zip', 'w', zipfile.ZIP_DEFLATED) as z:
        for f in sorted(pkg.iterdir()):
            z.write(f, f.name)

    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / 'search.html').write_text(search_page(cfg), encoding='utf-8', newline='\n')
    (a.work / 'widget.html').write_text(widget(cfg), encoding='utf-8', newline='\n')
    if a.upload:
        build_pages.upload(a.out / 'search.html', 'search.html')
    print(f"{len(titles['journals'])} journals, {len(titles['issn'])} ISSNs in the relay package; "
          f"wrote {a.out / 'search.html'}, {a.work / 'relay.zip'}, {a.work / 'widget.html'}"
          + ('; uploaded search.html' if a.upload else ''))
    if not sc['db_ids'] or not sc['held_by_institution']:
        print('note: search.db_ids or search.held_by_institution is empty, so results are not limited to them')


if __name__ == '__main__':
    main()
