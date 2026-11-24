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
import re
import shutil
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'search'))
import build_pages  # noqa: E402
from common import ROOT, fold, load_config, need  # noqa: E402
from relay import issn_key, title_key  # noqa: E402

SEARCH_JS = """
(function(){
var form=document.getElementById('sform'),status=document.getElementById('status'),list=document.getElementById('results'),
more=document.getElementById('more'),adv=document.getElementById('adv'),next=null,shown=0,req=null,said='';
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
function item(r){var cite=[];if(r.volume)cite.push('Volume '+esc(r.volume));if(r.issue)cite.push('Number '+esc(r.issue));
var where='<i>'+esc(r.journal)+'</i>'+(cite.length?', '+cite.join(', '):'')+(r.year?' ('+esc(r.year)+')':'')+(r.pages?', pages '+esc(r.pages.replace('-','\\u2013')):'')+'.';
return '<li class="art"><a class="t" href="'+esc(r.link)+'">'+esc(r.title||'Untitled article')+'</a><p>'
+(r.authors&&r.authors.length?esc(r.authors.join('; '))+'. ':'')+where+'</p></li>'}
function say(){status.textContent=shown?(shown+' article'+(shown==1?'':'s')+' found in the archive'+(next!=null?' so far.':'.')):
(next!=null?'Searching\\u2026':'No articles in the archive match '+said+'. Try fewer words, other spellings or a wider year range.')}
function load(offset,tries){status.textContent='Searching\\u2026';more.hidden=true;var u=new URLSearchParams(req);u.set('offset',offset);
return fetch('api/search?'+u.toString(),{credentials:'same-origin'})
.then(function(r){return r.json().then(function(d){if(!r.ok)throw new Error(d.error||'The search failed.');return d})})
.then(function(d){var first=null;d.results.forEach(function(r){list.insertAdjacentHTML('beforeend',item(r));if(!first)first=list.lastElementChild});
shown+=d.results.length;next=d.next;
// a page of index results can hold none from the archive; look a little further before giving up
if(!d.results.length&&next!=null&&tries<4)return load(next,tries+1);
more.hidden=next==null;say();return first})
.catch(function(e){status.textContent=e.message||'The search failed. Try again in a minute.';more.hidden=true})}
more.addEventListener('click',function(){load(next,0).then(function(first){if(first)first.querySelector('a').focus()})});
// leave empty fields out of the address
form.addEventListener('submit',function(){[2,3].forEach(function(n){if(!form['t'+n].value.trim()){form['b'+n].disabled=form['f'+n].disabled=form['t'+n].disabled=true}});
['j','from','to'].forEach(function(k){if(!form[k].value.trim())form[k].disabled=true});if(form.f1.value==='kw')form.f1.disabled=true});
window.addEventListener('pageshow',function(){[].forEach.call(form.elements,function(e){e.disabled=false})});
function term(f,t,first){t=t.trim();if(!t)return '';return f&&f!=='kw'?f+':('+t+')':(first?t:'('+t+')')}
var p=new URLSearchParams(location.search),names=['q','f1','b2','f2','t2','b3','f3','t3','j','from','to'],any=false;
names.forEach(function(k){var v=p.get(k);if(v!=null&&form[k]){form[k].value=v;if(k!=='q'&&k!=='f1'&&v)any=true}});
if(any||p.get('adv'))adv.open=true;
var query=term(form.f1.value,form.q.value,true),words=[form.q.value.trim()];
[2,3].forEach(function(n){var x=term(form['f'+n].value,form['t'+n].value);if(!x)return;words.push(form['t'+n].value.trim());
query=query?'('+query+') '+form['b'+n].value+' '+x:(form['b'+n].value==='NOT'?'':x)});
var j=form.j.value,from=form.from.value.trim(),to=form.to.value.trim();
words=words.filter(Boolean);
if(query||j){req={q:query,j:j,from:from,to:to};said=words.map(function(w){return '\\u201c'+w+'\\u201d'}).join(', ')||'your choices';
if(j)said+=' in '+form.j.options[form.j.selectedIndex].text;document.title=(words.join(', ')||'Search')+' | '+document.title;load(0,0)}
else{form.q.focus()}
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


def search_page(cfg, titles):
    sc, site = cfg['search'], cfg['site']
    fields = (('kw', 'Keyword'), ('ti', 'Title'), ('au', 'Author'), ('su', 'Subject'))

    def field(name, label):
        opts = ''.join(f'<option value="{v}">{t}</option>' for v, t in fields)
        return f'<label class="vh" for="{name}">{label}</label><select id="{name}" name="{name}">{opts}</select>'

    def row(n):
        ops = ''.join(f'<option>{o}</option>' for o in ('AND', 'OR', 'NOT'))
        return (f'<div class="row"><label class="vh" for="b{n}">How line {n} combines with the lines above</label>'
                f'<select id="b{n}" name="b{n}">{ops}</select>{field(f"f{n}", f"Search line {n} in")}'
                f'<label class="vh" for="t{n}">Search line {n}</label><input id="t{n}" name="t{n}" type="search" autocomplete="off"></div>')

    journals = sorted(titles['journals'].items(),
                      key=lambda kv: fold(re.sub(r'^(the|a|an)\s+', '', kv[1]['title'], flags=re.I)))
    jopts = '<option value="">All journals</option>' + ''.join(
        f'<option value="{html.escape(slug)}">{html.escape(j["title"])}</option>' for slug, j in journals)
    body = '\n'.join([
        f'<header>\n<h1>{html.escape(sc["title"], quote=False)}</h1>\n</header>',
        '<form class="find" id="sform" role="search" action="search.html" method="get">',
        f'<label for="sq">{html.escape(sc["label"], quote=False)}</label>',
        f'<div class="row">{field("f1", "Search in")}<input id="sq" name="q" type="search" autocomplete="off">'
        '<button type="submit">Search</button></div>',
        '<p>Keyword searches article titles, authors, subjects and abstracts. Put a phrase in quotes. '
        'Each result opens the article in the archive.</p>',
        '<details id="adv"><summary>Advanced search</summary>',
        row(2), row(3),
        f'<div class="row"><label for="j">Journal</label><select id="j" name="j">{jopts}</select></div>',
        '<div class="row"><span id="yl">Years</span><label class="vh" for="from">From year</label>'
        '<input id="from" name="from" class="yr" inputmode="numeric" pattern="[0-9]{4}" placeholder="From" aria-describedby="yl">'
        '<span aria-hidden="true">to</span><label class="vh" for="to">To year</label>'
        '<input id="to" name="to" class="yr" inputmode="numeric" pattern="[0-9]{4}" placeholder="To" aria-describedby="yl"></div>',
        '<p><button type="submit">Search</button></p>',
        '</details>',
        '</form>',
        '<p id="status" role="status" aria-live="polite"></p>',
        '<main id="list">\n<ol id="results"></ol>',
        '<p><button id="more" type="button" hidden>More results</button></p>\n</main>'])
    crumb = f'<p class="crumb"><a href="index.html">{html.escape(site["all_journals"], quote=False)}</a></p>'
    doc = build_pages.page(f'{sc["title"]} | {site["index_title"]}', body, 'articles', crumb, script=SEARCH_JS)
    style = ('.row{display:flex;flex-wrap:wrap;align-items:center;gap:.5rem;max-width:40rem;margin:.6rem 0}'
             '.row input{flex:1 1 14rem;min-width:0;width:auto}.row input.yr{flex:0 0 6rem;width:6rem}'
             '.find select{font:inherit;color:var(--ink);background:var(--bg);border:1px solid var(--muted);border-radius:4px;'
             'padding:.5rem .4rem;max-width:100%}#j{flex:1 1 14rem;min-width:0}.row>label,#yl{font-weight:600;margin:0}'
             '#adv{margin-top:.75rem}#adv summary{cursor:pointer;color:var(--accent);font-weight:600;width:max-content}'
             '.find button,#more{font:inherit;font-weight:600;color:var(--bg);background:var(--accent);border:0;border-radius:4px;'
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
  <p style="margin:.4em 0 0"><a href="{html.escape(prefix + target[:-2])}adv=1" target="_blank" rel="noopener">Advanced search</a></p>
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
    (a.out / 'search.html').write_text(search_page(cfg, titles), encoding='utf-8', newline='\n')
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
