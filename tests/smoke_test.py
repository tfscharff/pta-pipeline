"""End-to-end check with made-up data: listing -> journal pages -> KBART. Needs only the standard library.

  python tests/smoke_test.py
"""
import csv
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ARTICLE = """<html><head><title>{title}</title>
<meta name="citation_title" content="{title}">
<meta name="citation_author" content="{author}">
<meta name="citation_journal_title" content="Journal of Sample Studies">
<meta name="citation_publisher" content="Sample University Press">
<meta name="citation_volume" content="{volume}">
<meta name="citation_issue" content="{issue}">
<meta name="citation_publication_date" content="{year}/03/01">
<meta name="citation_firstpage" content="{first}">
<meta name="citation_lastpage" content="{last}">
<meta name="citation_issn" content="1234-5679">
<meta name="citation_issn" content="0000-0019">
</head><body><p>Text.</p></body></html>
"""
# (key without extension, title, author, volume, issue, year, first page, last page)
ARTICLES = [
    ('sample_studies/v001/1/101', 'On Sampling &amp; Its Limits', 'Ada Example', 1, 1, 2001, 1, 20),
    ('sample_studies/v001/1/102', 'A Second Article', 'Ben Example', 1, 1, 2001, 21, 40),
    ('sample_studies/v002/3/201', 'Volume Two Opens', 'Cy Example', 2, 1, 2002, 1, 15),
    ('sample_studies/v005/9/501', 'After the Gap', 'Di Example', 5, 2, 2005, 30, 44),
]
KBART_COLS = ['publication_title', 'print_identifier', 'online_identifier', 'date_first_issue_online',
              'num_first_vol_online', 'num_first_issue_online', 'date_last_issue_online', 'num_last_vol_online',
              'num_last_issue_online', 'title_url', 'first_author', 'title_id', 'embargo_info', 'coverage_depth',
              'coverage_notes', 'publisher_name', 'location', 'title_notes', 'staff_notes', 'vendor_id',
              'oclc_collection_name', 'oclc_collection_id', 'oclc_entry_id', 'oclc_linkscheme', 'oclc_number', 'ACTION']


def run(*args):
    r = subprocess.run([sys.executable, *map(str, args)], capture_output=True, text=True, cwd=ROOT)
    if r.returncode:
        raise SystemExit(f'FAILED: {" ".join(map(str, args))}\n{r.stdout}\n{r.stderr}')


def main():
    with tempfile.TemporaryDirectory() as d:
        t = Path(d)
        for base, title, author, vol, issue, year, first, last in ARTICLES:
            p = t / 'src' / f'{base}.html'
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(ARTICLE.format(title=title, author=author, volume=vol, issue=issue, year=year, first=first, last=last),
                         encoding='utf-8')
        (t / 'listing.txt').write_text(''.join(f'{a[0]}.html\tSTANDARD\t500\n' for a in ARTICLES)
                                       + 'sample_studies/v001/1/full/fig1.jpg\tSTANDARD\t900\n', encoding='utf-8')
        cfg = {'bucket': 'unused', 'base_url': 'https://example.cloudfront.net', 'listing': str(t / 'listing.txt'),
               'kbart': {'link_scheme': 'example.scheme', 'title_id_template': '{host}/{slug}/index.html?x=', 'pilot': 'sample_studies'}}
        (t / 'config.json').write_text(json.dumps(cfg), encoding='utf-8')

        run('scripts/build_pages.py', '--config', t / 'config.json', '--slug', 'sample_studies', '--src', t / 'src',
            '--out', t / 'site', '--meta', t / 'meta', '--log', t / 'pages.log')
        page = (t / 'site' / 'sample_studies' / 'index.html').read_text(encoding='utf-8')
        summary = json.loads((t / 'meta' / 'sample_studies.json').read_text(encoding='utf-8'))
        assert 'On Sampling &amp; Its Limits' in page and 'Ada Example' in page, 'article title or author missing from the page'
        assert '<h1>Journal of Sample Studies</h1>' in page, 'journal title missing'
        assert 'href="v001/1/101.html"' in page, 'article link missing'
        assert summary['articles'] == 4 and summary['years'] == ['2001', '2005'], summary
        assert summary['issn'] == '1234-5679' and summary['quality']['no_metadata'] == 0, summary
        assert 'sample_studies/index.html' in (t / 'site' / 'index.html').read_text(encoding='utf-8'), 'journal missing from the index'

        row = dict.fromkeys(KBART_COLS, '')
        row.update(publication_title='Journal of Sample Studies', print_identifier='0000-0019', online_identifier='1234-5679',
                   date_first_issue_online='1990', num_first_vol_online='1', title_url='https://publisher.example/jss',
                   title_id='jss', coverage_depth='fulltext', oclc_entry_id='12345', oclc_linkscheme='publisher', oclc_number='999')
        with open(t / 'export.txt', 'w', encoding='utf-8', newline='') as fh:
            w = csv.DictWriter(fh, fieldnames=KBART_COLS, delimiter='\t')
            w.writeheader()
            w.writerow(row)
        run('scripts/build_kbart.py', '--config', t / 'config.json', '--kbart', t / 'export.txt', '--meta', t / 'meta', '--out', t / 'kbart')

        def rows(name):
            with open(t / 'kbart' / name, encoding='utf-8', newline='') as fh:
                return list(csv.DictReader(fh, delimiter='\t'))
        cov, over = rows('kbart_coverage.txt'), rows('kbart_overlay.txt')
        # volumes 1-2 and volume 5 are separate runs, so the title gets two coverage rows
        assert [(r['num_first_vol_online'], r['num_last_vol_online'], r['date_first_issue_online'], r['date_last_issue_online'])
                for r in cov] == [('1', '2', '2001', '2002'), ('5', '5', '2005', '2005')], cov
        assert all(r['ACTION'] == 'covoverlay' and r['oclc_entry_id'] == '12345' for r in cov), cov
        assert all(r['title_url'] == 'https://example.cloudfront.net/sample_studies/index.html' for r in cov), cov
        assert all(r['ACTION'] == 'overlay' and r['oclc_linkscheme'] == 'example.scheme'
                   and r['title_id'] == 'example.cloudfront.net/sample_studies/index.html?x=' for r in over), over
        assert rows('pilot_kbart.txt') == cov and not rows('local_kbart.txt')

        # article search: page, relay package and research-guide box
        cfg['search'] = {'enabled': True, 'db_ids': [99], 'held_by_institution': '1', 'proxy_login': 'https://proxy.example/login?url='}
        (t / 'config.json').write_text(json.dumps(cfg), encoding='utf-8')
        run('scripts/build_search.py', '--config', t / 'config.json', '--meta', t / 'meta', '--out', t / 'site', '--work', t / 'search')
        assert 'id="sq"' in (t / 'site' / 'search.html').read_text(encoding='utf-8'), 'search page missing its box'
        assert 'https://proxy.example/login?url=https://example.cloudfront.net/search.html?q=' in \
            (t / 'search' / 'widget.html').read_text(encoding='utf-8'), 'search box does not go through the proxy'
        sys.path.insert(0, str(t / 'search' / 'relay'))
        import relay
        settings, titles = relay.load()
        assert settings['db_ids'] == [99] and titles['issn']['12345679'] == 'sample_studies', titles
        fake = {'numberOfRecords': 120, 'briefRecords': [
            {'title': 'On Sampling & Its Limits /', 'creator': 'Ada Example', 'date': '2001',
             'source': {'sourceTitle': 'Journal of sample studies', 'sourceIssn': '1234-5679', 'relatedParts': ['Vol. 1, no. 1 (2001), p. 1-20']}},
            {'title': 'Held elsewhere', 'date': '2001', 'source': {'sourceTitle': 'Other Journal', 'sourceIssn': '9999-9999'}},
            {'title': 'Outside our years', 'date': '2003', 'source': {'sourceTitle': 'The Journal of Sample Studies'}},
            # the live API returns source as a list of partial objects, and contributors as name objects
            {'title': 'Volume Two Opens', 'date': '2002', 'sourceIssns': ['0000-0019'],
             'contributors': [{'firstName': {'text': 'Cy'}, 'secondName': {'text': 'Example'}}],
             'source': [{'relatedParts': ['v2 n1 (2002): 1-15'], 'sourceTitle': 'Other title'}, {'volumes': ['2'], 'issues': ['1']}]}]}
        got = relay.search('sampling', 0, settings, titles, fetch=lambda q, o, s: fake)
        assert [r['title'] for r in got['results']] == ['On Sampling & Its Limits', 'Volume Two Opens'], got
        second = got['results'][1]
        assert (second['volume'], second['issue'], second['pages'], second['authors']) == ('2', '1', '1-15', ['Cy Example']), second
        first = got['results'][0]
        assert (first['volume'], first['issue'], first['pages']) == ('1', '1', '1-20'), first
        assert first['link'] == 'sample_studies/index.html?Title=On+Sampling+%26+Its+Limits&volume=1&issue=1&year=2001', first
        assert got['next'] == 50 and got['checked'] == 4, got
        assert relay.handler({'queryStringParameters': {}})['statusCode'] == 400
        # advanced search: a journal limit becomes its ISSNs and, with no years given, the years held
        assert relay.limits('ti:(sampling)', 'sample_studies', '', '', titles) == \
            ('(ti:(sampling)) AND (in:0000-0019 OR in:1234-5679)', '2001-2005')
        assert relay.limits('x', '', '2010', '2001', titles) == ('x', '2001-2010')
        assert relay.limits('x', '', '19', '', titles) == ('x', '')
        assert 'search.html?adv=1' in (t / 'search' / 'widget.html').read_text(encoding='utf-8')
    print('smoke test passed')


if __name__ == '__main__':
    main()
