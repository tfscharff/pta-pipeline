"""Unit tests for the pipeline's helpers and edge cases, with made-up data. Standard library only (unittest).

  python tests/unit_test.py

Nothing here touches AWS or the network: the AWS CLI calls in check_restore.py and the Discovery API call in
search/relay.py are replaced by fakes. Files are written only to temporary folders the tests remove.
"""
import contextlib
import csv
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'search'), str(Path(__file__).resolve().parent)]

import build_kbart  # noqa: E402
import build_pages  # noqa: E402
import build_search  # noqa: E402
import check_restore  # noqa: E402
import common  # noqa: E402
import make_manifest  # noqa: E402
import relay  # noqa: E402
from smoke_test import KBART_COLS  # noqa: E402

TMP = None


def setUpModule():
    global TMP
    TMP = tempfile.TemporaryDirectory()
    cfg = Path(TMP.name) / 'config.json'
    cfg.write_text(json.dumps({'base_url': 'https://example.cloudfront.net/', 'site': {'provider': 'Example Provider'}}),
                   encoding='utf-8')
    build_pages.setup(common.load_config(cfg))


def tearDownModule():
    TMP.cleanup()


@contextlib.contextmanager
def quiet():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


def write_config(d, data):
    p = Path(d) / 'config.json'
    p.write_text(json.dumps(data), encoding='utf-8')
    return p


class Common(unittest.TestCase):
    def test_overlays_one_level_and_keeps_other_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = common.load_config(write_config(d, {'base_url': 'https://x.example//', 'site': {'footer': 'F'},
                                                      'extra': 1, 'kbart': {'pilot': 'p'}}))
        self.assertEqual(cfg['base_url'], 'https://x.example')
        self.assertEqual(cfg['site']['footer'], 'F')
        self.assertEqual(cfg['site']['provider'], common.DEFAULTS['site']['provider'])
        self.assertEqual(cfg['kbart']['pilot'], 'p')
        self.assertEqual(cfg['kbart']['avoid_title_ids'], [])
        self.assertEqual(cfg['extra'], 1)

    def test_load_does_not_change_the_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = common.load_config(write_config(d, {'site': {'footer': 'changed'}}))
            cfg['kbart']['avoid_title_ids'].append('x')
            again = common.load_config(write_config(d, {}))
        self.assertEqual(common.DEFAULTS['site']['footer'], again['site']['footer'])
        self.assertNotEqual(again['site']['footer'], 'changed')
        self.assertEqual(again['kbart']['avoid_title_ids'], [])

    def test_missing_named_config_and_unset_key_stop(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(SystemExit):
                common.load_config(Path(d) / 'nope.json')
            cfg = common.load_config(write_config(d, {}))
        with self.assertRaises(SystemExit):
            common.need(cfg, 'bucket')
        self.assertEqual(common.need({'bucket': 'b'}, 'bucket'), 'b')

    def test_malformed_config_raises_json_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'config.json'
            p.write_text('{"bucket": ', encoding='utf-8')
            with self.assertRaises(json.JSONDecodeError):
                common.load_config(p)

    def test_text_helpers(self):
        self.assertEqual(common.text('<p>A&amp;B\n  <i>c</i></p>'), 'A&B c')
        self.assertEqual(common.text(None), '')
        self.assertEqual(common.fold('Éclair Ünïon'), 'eclair union')
        self.assertEqual(common.norm('é  x '), 'é x')
        self.assertEqual(common.first(r'id=(\d+)', 'a id=42 b'), '42')
        self.assertEqual(common.first(r'id=(\d+)', 'none'), '')


class Profile(unittest.TestCase):
    """The default parsing profile, profiles/citation_meta.py, on unusual pages."""

    def parse(self, data):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / '1.html'
            p.write_bytes(data)
            return build_pages.PROFILE.parse_article(p)

    def test_title_fallback_attribute_styles_and_duplicates(self):
        m = self.parse(b"""<HTML><HEAD><TITLE>Fallback &amp; Title</TITLE>
<META NAME='citation_author' CONTENT='Eve Example'>
<meta content="Eve Example" name="citation_author">
<meta name="citation_issn" content="1111-1111"><meta name="citation_issn" content="1111-1111">
<meta name="citation_firstpage" content="7"><meta name="citation_lastpage" content="7">
<meta name="citation_date" content="Spring 1998">
<meta name="citation_volume" content="  12 ">
<meta name="citation_issue">
</HEAD><body>x""")
        self.assertEqual(m['title'], 'Fallback & Title')
        self.assertEqual(m['authors'], ['Eve Example'])
        self.assertEqual((m['issn'], m['print_issn']), ('1111-1111', ''))
        self.assertEqual(m['pages'], '7')
        self.assertEqual(m['designation'], 'Volume 12, 1998')
        self.assertEqual(m['journal'], '')

    def test_markup_in_content_bad_bytes_and_og_title(self):
        m = self.parse(b'\xff\xfe<meta property="og:title" content="Open Graph Name">'
                       b'<meta name="citation_title" content="&lt;i&gt;Hamlet&lt;/i&gt; Revisited">'
                       b'<meta name="citation_firstpage" content="3"><meta name="citation_lastpage" content="9">')
        self.assertEqual(m['title'], 'Hamlet Revisited')
        self.assertEqual(m['pages'], '3-9')
        self.assertEqual(m['designation'], '')
        m = self.parse(b'<meta property="og:title" content="Open Graph Name">')
        self.assertEqual(m['title'], 'Open Graph Name')

    def test_html_file_that_is_a_pdf(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / '5.html'
            p.write_bytes(b'%PDF-1.4 not really a pdf')
            with quiet():  # pypdf, when installed, logs its complaint
                m = build_pages.parse_article(p)
        self.assertTrue(m['html_is_pdf'])
        self.assertEqual((m['src'], m['title']), ('pdf', ''))
        self.assertIn('error', m)


class Pages(unittest.TestCase):
    def test_read_listing(self):
        lines = ['jx/v001/1/10.pdf', 'jx/v001/1/10.html', 'jx/v001/1/full/fig.html', 'jx/v001/1/11/11.html',
                 'jx/v001/1/11/12.html', 'jx/preprint/p/20.pdf', 'jy/v002/3/30.pdf', 'jx/v001/1/13.txt']
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'listing.txt'
            p.write_bytes((''.join(f'{k}\tGLACIER_IR\t1\r\n' for k in lines) + 'not-a-key\r\n').encode('utf-8'))
            only = build_pages.read_listing(p, {'jx'})
            every = build_pages.read_listing(p)
        self.assertEqual(only, {'jx': {
            ('v001', '1', '10'): {'exts': {'pdf', 'html'}, 'base': 'v001/1/10'},
            ('v001', '1', '11'): {'exts': {'html'}, 'base': 'v001/1/11/11'},
            ('preprint', 'p', '20'): {'exts': {'pdf'}, 'base': 'preprint/p/20'}}})
        self.assertEqual(sorted(every), ['jx', 'jy'])

    def test_parse_designation(self):
        pd = build_pages.parse_designation
        self.assertEqual(pd('Volume 12, Number 3, Fall 2001', 'v012'), ('12', '3', '2001'))
        self.assertEqual(pd('Volume XVII-XVIII, Numbers 1/2, 1990–1991', 'v017'), ('XVII-XVIII', '1/2', '1991'))
        self.assertEqual(pd('Spring 2003', 'v005'), ('5', '', '2003'))
        self.assertEqual(pd('', 'v000'), ('v000', '', ''))  # then left out of KBART coverage by vol_num

    def test_fill_from_neighbours(self):
        metas = {'v1/1/1': {'title': 'First Piece', 'next': 'Middle Piece (Review)'},
                 'v1/1/2': {'title': '', 'prev': 'First Piece'},
                 'v1/1/3': {'title': 'Last Piece', 'prev': 'Middle Piece (Review)'},
                 # two untitled articles that name the same neighbour: neither can be titled
                 'v1/2/1': {'title': 'Alone', 'next': 'Who Knows'},
                 'v1/2/2': {'title': '', 'prev': 'Alone'},
                 'v1/2/3': {'title': '', 'prev': 'Alone'}}
        self.assertEqual(build_pages.fill_from_neighbours(metas), ['v1/1/2'])
        self.assertEqual(metas['v1/1/2'], {'title': 'Middle Piece', 'prev': 'First Piece', 'title_from': 'neighbour',
                                           'type': 'Review'})
        self.assertEqual(metas['v1/2/2']['title'], '')
        self.assertEqual(build_pages.neighbour_anchors({'a/1': {'title': 'T'}, 'a/2': {'prev': 'T'}}), ['a/1'])

    def test_build_journal_edge_cases(self):
        def h(base, *exts):
            v, i, a = base.split('/')
            return (v, i, a), {'exts': set(exts), 'base': base}
        holdings = dict([h('v002/20/201', 'pdf', 'html'), h('v002/20/202', 'pdf', 'html'), h('v002/20/203', 'pdf'),
                         h('v002/20/204', 'html'), h('v002/20/205', 'html', 'pdf'), h('v003/30/301', 'html'),
                         h('v003/31/311', 'html'), h('preprint/x/901', 'pdf')])
        des = 'Volume 2, Number 1, Spring 2002'
        metas = {
            'v002/20/201': {'journal': 'Invented Quarterly', 'publisher': 'Nowhere Press', 'issn': '1111-2222',
                            'print_issn': '3333-4444', 'designation': des, 'title': 'Early Pages',
                            'authors': ['A. One', 'B. Two'], 'pages': '9-12', 'type': 'Article'},
            'v002/20/202': {'designation': des, 'title': 'Tags <b> & Ampersands', 'pages': '100-110', 'type': 'Review'},
            'v002/20/204': {'missing': True, 'title': 'Error Page'},
            'v002/20/205': {'missing': True, 'title': 'Rescued by PDF'},
            'v003/30/301': {'title': 'Web Only Piece'},
            'v003/31/311': {'title': 'Secretly a PDF', 'html_is_pdf': True},
            'preprint/x/901': {'src': 'pdf', 'title': 'Ahead of Print', 'journal': 'Name From PDF'},
        }
        doc, s = build_pages.build_journal('invented_quarterly', holdings, metas)
        self.assertEqual((s['journal'], s['issn'], s['print_issn'], s['volumes'], s['years'], s['articles']),
                         ('Invented Quarterly', '1111-2222', '3333-4444', ['2', '3'], ['2002', '2002'], 7))
        self.assertEqual(s['quality'], {'no_metadata': 1, 'no_designation': 2, 'parse_errors': 0, 'from_pdf': 1,
                                        'preprints': 1, 'missing': 1})
        self.assertEqual([(i['volume'], i['number'], i['folder']) for i in s['issues']],
                         [('2', '1', 'v002/20'), ('3', '', 'v003/30'), ('3', '', 'v003/31')])
        self.assertIn('<h1>Invented Quarterly</h1>', doc)
        self.assertIn('The library holds Volumes 2–3, 2002.', doc)
        self.assertIn('1 article is not listed because Example Provider supplied an error page', doc)
        self.assertIn('Tags &lt;b&gt; &amp; Ampersands', doc)
        self.assertNotIn('<b>', doc)
        self.assertIn('<h3>Number 1, Spring 2002</h3>', doc)
        self.assertIn('<h3>Issue 30</h3>', doc)
        self.assertNotIn('id="a204"', doc)
        # ordered by first page, then untitled and unpaged articles by id
        order = [doc.index(t) for t in ('Early Pages', 'Tags &lt;b&gt;', 'Untitled article (no title on file)', 'Rescued by PDF')]
        self.assertEqual(order, sorted(order))

        def li(art):
            return re.search(rf'<li class="art" id="a{art}"[^>]*>.*?</li>', doc).group()
        self.assertIn('A. One, B. Two. Pages 9–12. <a href="v002/20/201.html">Web version</a>', li(201))
        self.assertIn('data-text="early pages a. one, b. two"', li(201))
        self.assertIn('Review.', li(202))
        self.assertIn('href="v002/20/205.pdf"', li(205))
        self.assertNotIn('Web version', li(205))  # the HTML is an error page
        self.assertIn('href="v003/30/301.html"', li(301))
        self.assertIn('Web version only.', li(301))
        self.assertNotIn('Web version only.', li(311))
        self.assertIn('Early release', doc)
        self.assertIn('href="preprint/x/901.pdf"', doc)

    def test_build_journal_with_nothing_parsed(self):
        holdings = {('v004', '7', '1'): {'exts': {'pdf'}, 'base': 'v004/7/1'}}
        doc, s = build_pages.build_journal('some_odd_journal', holdings, {})
        self.assertEqual((s['journal'], s['volumes'], s['years']), ('Some Odd Journal', ['4', '4'], []))
        self.assertIn('The library holds Volume 4. ', doc)

    def test_build_index(self):
        def summary(slug, journal, volumes=('1', '1'), years=()):
            return {'slug': slug, 'journal': journal, 'publisher': 'P & Co', 'issn': '', 'print_issn': '',
                    'volumes': list(volumes), 'years': list(years)}
        doc = build_pages.build_index([summary('z', 'The Zebra Review'), summary('a', 'Ápple Studies', ('1', '9'), ('1990', '1999')),
                                       summary('m', 'Middle Journal')])
        order = [doc.index(f'href="{s}/index.html"') for s in ('a', 'm', 'z')]
        self.assertEqual(order, sorted(order))
        self.assertIn('Volumes 1–9 (1990–1999).', doc)
        self.assertIn('<p>Volume 1.</p>', doc)
        self.assertIn('P &amp; Co.', doc)
        self.assertIn('3 journals.', doc)
        self.assertNotIn('id="aq"', doc)
        cfg = json.loads(json.dumps(build_pages.CFG))
        cfg['site'].update(show_index_heading=False, index_intro='')
        cfg['search']['enabled'] = True
        with mock.patch.object(build_pages, 'CFG', cfg):
            doc = build_pages.build_index([summary('m', 'Middle Journal')])
        self.assertIn('<h1 class="vh">', doc)
        self.assertNotIn('class="about"', doc)
        self.assertIn('id="aq"', doc)
        self.assertIn('1 journal.', doc)


class KbartHelpers(unittest.TestCase):
    def test_issn_and_title_key(self):
        self.assertEqual(build_kbart.issn('1234567x'), '1234-567X')
        self.assertEqual(build_kbart.issn(' 1234-5679 '), '1234-5679')
        self.assertEqual(build_kbart.issn('1234-56789'), '')
        self.assertEqual(build_kbart.issn(None), '')
        self.assertEqual(build_kbart.title_key('The Journal of Ünusual & Odd.'), 'journal of unusual and odd')
        self.assertEqual(build_kbart.title_key('An Anthology'), 'anthology')

    def test_volume_numbers(self):
        vn, ve = build_kbart.vol_num, build_kbart.vol_end
        self.assertEqual(vn({'volume': 'XVII-XVIII', 'folder': 'v017/5'}), 17)
        self.assertEqual(vn({'volume': 'Special', 'folder': 'v012/1'}), 12)
        self.assertIsNone(vn({'volume': '0', 'folder': 'v000/1'}))
        self.assertIsNone(vn({'volume': '', 'folder': 'v10001/1'}))
        self.assertEqual(ve(24, {'volume': '24-25'}), 25)
        self.assertEqual(ve(24, {'volume': '24-40'}), 24)
        self.assertEqual(ve(17, {'volume': 'XVII-XVIII'}), 18)
        self.assertEqual(ve(30, {'volume': '30', 'number': '30/31'}), 31)
        self.assertEqual(ve(5, {'volume': '5', 'number': '1-2'}), 5)

    def test_years(self):
        self.assertEqual(build_kbart.guess_year(2021, None), '2021')
        self.assertEqual(build_kbart.guess_year(5, 2000), '2005')
        self.assertEqual(build_kbart.guess_year(5, None), '')
        self.assertEqual(build_kbart.guess_year(5, -10), '')
        iss = lambda v, y: (v, {'year': y})
        self.assertEqual(build_kbart.year_offset([iss(1, '2001'), iss(2, '2002')]), None)
        self.assertEqual(build_kbart.year_offset([iss(1, '2001'), iss(2, '2002'), iss(4, '2005')]), 2000)
        self.assertEqual(build_kbart.year_offset([iss(1, '2001'), iss(2, '2010'), iss(3, '1990')]), None)

    def test_runs(self):
        def i(volume, year, folder, number='1'):
            return {'volume': volume, 'number': number, 'year': year, 'folder': folder}
        s = {'issues': [i('1', '2001', 'v001/1'), i('2', '2002', 'v002/1'), i('3', '', 'v003/1', ''),
                        i('5', '2005', 'v005/2', '2'), i('', '2004', 'preprint/x'),
                        i('2002', '2002', 'v2002/9'),   # filed under its year, inside the regular run: dropped
                        i('2015', '2015', 'v2015/1')]}  # filed under its year, outside any run: kept
        self.assertEqual(build_kbart.runs(s), [
            {'date_first': '2001', 'vol_first': '1', 'iss_first': '1', 'date_last': '2003', 'vol_last': '3',
             'iss_last': '', 'issues': 3, 'inferred_year': True},
            {'date_first': '2005', 'vol_first': '5', 'iss_first': '2', 'date_last': '2005', 'vol_last': '5',
             'iss_last': '2', 'issues': 1, 'inferred_year': False},
            {'date_first': '2015', 'vol_first': '2015', 'iss_first': '1', 'date_last': '2015', 'vol_last': '2015',
             'iss_last': '1', 'issues': 1, 'inferred_year': False}])


class KbartOutput(unittest.TestCase):
    """build_kbart.py end to end: entry choice, title matching, unmatched journals and the file format."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        t = cls.dir = Path(cls.tmp.name)
        meta = t / 'meta'
        meta.mkdir()

        def summary(slug, journal, issn, issues):
            (meta / f'{slug}.json').write_text(json.dumps({
                'slug': slug, 'journal': journal, 'publisher': 'Invented Press', 'issn': issn, 'print_issn': '',
                'years': [], 'issues': [{'volume': v, 'number': '1', 'year': y, 'folder': f'v{v}/1'} for v, y in issues]}),
                encoding='utf-8')
        summary('alpha', 'Alpha Letters', '1234-5679', [('1', '2001'), ('2', '2002')])
        summary('beta', 'The Beta & Gamma Review', '', [('10', '2010')])
        summary('gamma', 'Gamma Unheard', '9999-9991', [('1', '1999')])
        (meta / 'alpha.articles.json').write_text('{}', encoding='utf-8')  # caches are not summaries

        def row(**kw):
            r = dict.fromkeys(KBART_COLS, '')
            r.update(kw)
            return r
        export = [
            row(publication_title='Alpha Letters', online_identifier='1234-5679', title_id='odd-entry', oclc_number='100',
                oclc_entry_id='E1'),
            row(publication_title='Alpha Letters', print_identifier='12345679', title_id='alpha', oclc_number='200',
                oclc_entry_id='E2', oclc_linkscheme='publisher', coverage_notes='Formerly "Alpha"', title_notes='a\\b',
                publisher_name='Éditions Inventées'),
            row(publication_title='Alpha Letters', online_identifier='1234-5679', title_id='alpha2', oclc_number='300',
                oclc_entry_id='E3'),
            row(publication_title='Beta and Gamma Review', oclc_number='400', oclc_entry_id='E4', num_first_vol_online='1'),
            row(publication_title='Other Thing', oclc_number='300', oclc_entry_id='E5'),
        ]
        # plain tab-separated text with a byte order mark, as Collection Manager exports it (no quoting)
        lines = ['\t'.join(KBART_COLS)] + ['\t'.join(r[c] for c in KBART_COLS) for r in export]
        (t / 'export.txt').write_bytes(('\r\n'.join(lines) + '\r\n').encode('utf-8-sig'))
        cfg = write_config(t, {'base_url': 'https://example.cloudfront.net/', 'kbart': {'avoid_title_ids': ['odd-entry'], 'pilot': 'beta'}})
        r = subprocess.run([sys.executable, 'scripts/build_kbart.py', '--config', str(cfg), '--kbart', str(t / 'export.txt'),
                            '--meta', str(meta), '--out', str(t / 'kbart')], capture_output=True, text=True, cwd=ROOT)
        if r.returncode:
            raise AssertionError(f'build_kbart failed\n{r.stdout}\n{r.stderr}')

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def raw(self, name):
        return (self.dir / 'kbart' / name).read_bytes()

    def rows(self, name):
        lines = self.raw(name).decode('utf-8').split('\r\n')
        head = lines[0].split('\t')
        return [dict(zip(head, line.split('\t'))) for line in lines[1:] if line]

    def test_file_format(self):
        for name in ('kbart_coverage.txt', 'kbart_overlay.txt', 'pilot_kbart.txt'):
            data = self.raw(name)
            self.assertFalse(data.startswith(b'\xef\xbb\xbf'), name)
            self.assertTrue(data.startswith('\t'.join(KBART_COLS).encode() + b'\r\n'), name)
            self.assertNotIn(b'\n', data.replace(b'\r\n', b''), name)
        self.assertIn('Éditions Inventées'.encode('utf-8'), self.raw('kbart_coverage.txt'))

    def test_entry_choice_and_coverage(self):
        cov = self.rows('kbart_coverage.txt')
        self.assertEqual([(r['oclc_entry_id'], r['num_first_vol_online'], r['num_last_vol_online'],
                           r['date_first_issue_online'], r['date_last_issue_online']) for r in cov],
                         [('E2', '1', '2', '2001', '2002'), ('E4', '10', '10', '2010', '2010')])
        self.assertEqual([r['title_url'] for r in cov], ['https://example.cloudfront.net/alpha/index.html',
                                                         'https://example.cloudfront.net/beta/index.html'])
        self.assertTrue(all(r['ACTION'] == 'covoverlay' and r['coverage_depth'] == 'fulltext' for r in cov))
        self.assertEqual([r['oclc_entry_id'] for r in self.rows('pilot_kbart.txt')], ['E4'])

    def test_overlay_without_link_scheme_keeps_export_linking(self):
        over = self.rows('kbart_overlay.txt')
        self.assertEqual([(r['ACTION'], r['oclc_linkscheme'], r['title_id']) for r in over],
                         [('overlay', 'publisher', 'alpha'), ('overlay', '', '')])

    def test_escaping(self):
        r = self.rows('kbart_coverage.txt')[0]
        # CURRENT BEHAVIOUR (suspected bug): the writer uses QUOTE_NONE with escapechar '\', so every '"' and
        # '\' copied from the export gets a backslash added. A plain KBART file would keep them as they are.
        self.assertEqual(r['coverage_notes'], 'Formerly \\"Alpha\\"')
        self.assertEqual(r['title_notes'], 'a\\\\b')

    def test_unmatched_and_report(self):
        local = self.rows('local_kbart.txt')
        self.assertEqual(list(local[0]), KBART_COLS[:KBART_COLS.index('vendor_id')] + ['oclc_number'])
        self.assertEqual([(r['publication_title'], r['online_identifier'], r['num_first_vol_online'],
                           r['date_first_issue_online'], r['title_url'], r['publisher_name']) for r in local],
                         [('Gamma Unheard', '9999-9991', '1', '1999', 'https://example.cloudfront.net/gamma/index.html',
                           'Invented Press')])
        with open(self.dir / 'kbart' / 'unmatched_s3.tsv', encoding='utf-8', newline='') as fh:
            self.assertEqual([r['slug'] for r in csv.DictReader(fh, delimiter='\t')], ['gamma'])
        with open(self.dir / 'kbart' / 'match_report.tsv', encoding='utf-8', newline='') as fh:
            rep = {r['slug']: r for r in csv.DictReader(fh, delimiter='\t')}
        self.assertEqual((rep['alpha']['matched_by'], rep['alpha']['oclc_entry_id']), ('issn', 'E2 (+2 more)'))
        self.assertEqual((rep['beta']['matched_by'], rep['beta']['kbart_title']), ('title', 'Beta and Gamma Review'))
        self.assertEqual(rep['gamma']['matched_by'], 'none')


class Manifest(unittest.TestCase):
    LISTING = ['j/v1/1/a b.pdf\tDEEP_ARCHIVE\t100', 'j/v1/1/x,y.PDF\tDEEP_ARCHIVE\t200',
               'j/v1/1/é.html\tDEEP_ARCHIVE\t300', 'j/v1.2/noext\tDEEP_ARCHIVE\t400',
               'k/v1/1/z.pdf\tGLACIER_IR\t500', 'broken line without tabs', 'j/v1/1/q.pdf\tDEEP_ARCHIVE\t600\textra']

    def make(self, *args):
        with tempfile.TemporaryDirectory() as d:
            listing = Path(d) / 'listing.txt'
            listing.write_bytes(('\r\n'.join(self.LISTING) + '\r\n').encode('utf-8'))
            out = Path(d) / 'm' / 'manifest.csv'
            argv = ['make_manifest.py', '--config', str(write_config(d, {})), '--listing', str(listing),
                    '--bucket', 'b', '--out', str(out), *args]
            with mock.patch.object(sys, 'argv', argv), quiet() as (so, se):
                self.assertEqual(make_manifest.main(), 0)
            data = out.read_bytes()
        self.assertNotIn(b'\r', data)
        return data.decode('utf-8').splitlines(), so.getvalue(), se.getvalue()

    def test_storage_class_encoding_and_malformed_lines(self):
        lines, out, err = self.make('--storage-class', 'DEEP_ARCHIVE')
        self.assertEqual(lines, ['b,j/v1/1/a%20b.pdf', 'b,j/v1/1/x%2Cy.PDF', 'b,j/v1/1/%C3%A9.html', 'b,j/v1.2/noext'])
        self.assertIn('4 objects, 0.00 GB', out)
        self.assertEqual(err.count('skip malformed line'), 2)

    def test_extension_and_prefix_filters(self):
        self.assertEqual(self.make('--ext', '.PDF')[0], ['b,j/v1/1/a%20b.pdf', 'b,j/v1/1/x%2Cy.PDF', 'b,k/v1/1/z.pdf'])
        self.assertEqual(self.make('--exclude-ext', 'pdf', '--prefix', 'j/')[0], ['b,j/v1/1/%C3%A9.html', 'b,j/v1.2/noext'])


class Restore(unittest.TestCase):
    STATES = {'k1': {}, 'k2': {'Restore': 'ongoing-request="true"'},
              'k3': {'Restore': 'ongoing-request="false", expiry-date="Mon, 04 Jan 2027 00:00:00 GMT"'},
              'k4': {'Restore': 'ongoing-request="false", expiry-date="Fri, 26 Feb 2027 00:00:00 GMT"'},
              'k5': {'_error': 'An error occurred (404)'}}

    def check(self, keys, *args):
        seen = []

        def head(bucket, key):
            seen.append((bucket, key))
            return self.STATES.get(key, {})
        with tempfile.TemporaryDirectory() as d:
            listing = Path(d) / 'listing.txt'
            listing.write_text(''.join(f'{k}\tGLACIER\t1\n' for k in keys), encoding='utf-8')
            argv = ['check_restore.py', '--config', str(write_config(d, {})), '--listing', str(listing), '--bucket', 'b', *args]
            with mock.patch.object(sys, 'argv', argv), mock.patch.object(check_restore, 'head', head), quiet() as (so, se):
                check_restore.main()
        return seen, so.getvalue(), se.getvalue()

    def test_counts_each_state(self):
        seen, out, err = self.check(list(self.STATES), '--sample', '10')
        self.assertEqual(seen, [('b', k) for k in self.STATES])
        counts = dict(re.findall(r'^\s*([a-z ]+): +(\d+)', out, re.M))
        self.assertEqual(counts, {'not requested': '1', 'in progress': '1', 'restored': '2', 'error': '1'})
        self.assertIn('ERROR k5: An error occurred (404)', err)
        # CURRENT BEHAVIOUR (suspected bug): expiry dates are compared as text ("Fri, ..." < "Mon, ..."), so
        # the "earliest" is the 26 February one, not 4 January.
        self.assertIn('earliest expiry: Fri, 26 Feb 2027', out)

    def test_reservoir_sample(self):
        keys = [f'j/v1/1/{n}.pdf' for n in range(50)]
        seen, _, _ = self.check(keys, '--sample', '5', '--seed', '7')
        self.assertEqual(len(seen), 5)
        self.assertEqual(len(set(seen)), 5)
        self.assertTrue(all(k in keys for _, k in seen))
        self.assertEqual(seen, self.check(keys, '--sample', '5', '--seed', '7')[0])

    def test_empty_listing(self):
        # CURRENT BEHAVIOUR (suspected bug): an empty listing divides by a sample size of zero.
        with self.assertRaises(ZeroDivisionError):
            self.check([], '--sample', '5')


class Search(unittest.TestCase):
    TITLES = {'journals': {'alpha': {'title': 'Alpha Letters', 'years': ['2001', '2005']},
                           'noissn': {'title': 'No ISSN Quarterly', 'years': []}},
              'issn': {'12345679': 'alpha'}, 'title': {'alpha letters': 'alpha', 'no issn quarterly': 'noissn'}}

    def test_titles_index(self):
        with tempfile.TemporaryDirectory() as d:
            meta = Path(d)
            for slug, journal, issn in (('a1', 'The Same Name', '1234-567x'), ('a2', 'Same Name', '')):
                (meta / f'{slug}.json').write_text(json.dumps({'slug': slug, 'journal': journal, 'issn': issn, 'print_issn': None,
                    'issues': [{'year': '2003'}, {'year': ''}, {'year': '2001'}, {'year': '2003'}]}), encoding='utf-8')
            (meta / 'a1.pdf.json').write_text('{}', encoding='utf-8')
            t = build_search.titles_index(meta)
        self.assertEqual(t['journals']['a1'], {'title': 'The Same Name', 'years': ['2001', '2003']})
        self.assertEqual(t['issn'], {'1234567X': 'a1'})
        self.assertEqual(t['title'], {'same name': 'a1'})  # the first journal with a title keeps it

    def test_search_page_escapes_journal_names(self):
        titles = {'journals': {'s1': {'title': 'Arts <&> Letters', 'years': []}}, 'issn': {}, 'title': {}}
        doc = build_search.search_page(build_pages.CFG, titles)
        self.assertIn('<option value="s1">Arts &lt;&amp;&gt; Letters</option>', doc)

    def test_parts(self):
        self.assertEqual(relay.parts({'relatedParts': ['Vol. 7, no. 10 (2012), p. 1-9']}), ('7', '10', '1-9'))
        self.assertEqual(relay.parts({'volumes': ['4'], 'issues': ['2'], 'pageRanges': ['5 - 9'], 'relatedParts': ['Vol. 9']}),
                         ('4', '2', '5-9'))
        self.assertEqual(relay.parts({}), ('', '', ''))
        # CURRENT BEHAVIOUR (suspected bug): the page pattern tries "p" before "pp" (and "s" before "str"),
        # so "pp. 45-60" gives the pages "p".
        self.assertEqual(relay.parts({'relatedParts': ['Vol. 12, issue 3, pp. 45-60']}), ('12', '3', 'p'))

    def test_limits(self):
        self.assertEqual(relay.limits('', 'noissn', '', '', self.TITLES), ('(so:"No ISSN Quarterly")', ''))
        self.assertEqual(relay.limits('x', 'unknown', '', '2010', self.TITLES), ('x', '1500-2010'))
        self.assertEqual(relay.limits('x', 'alpha', '2003', '', self.TITLES), ('(x) AND (in:1234-5679)', '2003-2099'))

    def test_result_and_paging(self):
        recs = [{'title': 'Outside our years', 'date': '2003', 'source': {'sourceIssn': '1234-5679'}},
                {'title': 'Undated :', 'source': {'sourceTitle': 'The Alpha Letters'}}]
        got = relay.search('q', 100, {}, self.TITLES, fetch=lambda q, o, s: {'numberOfRecords': 150, 'briefRecords': recs})
        self.assertEqual([r['title'] for r in got['results']], ['Undated'])
        self.assertEqual(got['results'][0]['link'], 'alpha/index.html?Title=Undated')
        self.assertIsNone(got['next'])
        got = relay.search('q', 0, {}, self.TITLES, fetch=lambda q, o, s: {'numberOfRecords': 1000, 'briefRecords': []})
        self.assertIsNone(got['next'])

    def test_handler(self):
        calls = []

        def fake(q, offset, settings, titles, journal='', lo='', hi=''):
            calls.append((q, offset, journal, lo, hi))
            return {'results': []}

        def failing(err):
            def f(*a, **k):
                raise err
            return f
        http = lambda code: urllib.error.HTTPError('u', code, 'm', {}, io.BytesIO(b'detail'))
        with mock.patch.object(relay, '_settings', {}), mock.patch.object(relay, '_titles', self.TITLES):
            self.assertEqual(relay.handler({})['statusCode'], 400)
            self.assertEqual(relay.handler({'queryStringParameters': {'j': 'unknown'}})['statusCode'], 400)
            with mock.patch.object(relay, 'search', fake):
                r = relay.handler({'queryStringParameters': {'j': 'alpha', 'offset': 'abc'}})
                relay.handler({'queryStringParameters': {'q': ' ' + 'w' * 1200, 'offset': '99999', 'from': '1990', 'to': '2000'}})
                relay.handler({'queryStringParameters': {'q': 'x', 'offset': '-5'}})
            self.assertEqual(r['statusCode'], 200)
            self.assertEqual(r['headers']['Cache-Control'], 'no-store')
            self.assertEqual(calls, [('', 0, 'alpha', '', ''), ('w' * 1000, 5000, '', '1990', '2000'), ('x', 0, '', '', '')])
            with quiet():
                with mock.patch.object(relay, 'search', failing(http(400))):
                    bad = relay.handler({'queryStringParameters': {'q': 'x'}})
                with mock.patch.object(relay, 'search', failing(http(503))):
                    down = relay.handler({'queryStringParameters': {'q': 'x'}})
                with mock.patch.object(relay, 'search', failing(OSError('timed out'))):
                    other = relay.handler({'queryStringParameters': {'q': 'x'}})
        self.assertEqual(bad['statusCode'], 400)
        self.assertIn('could not be understood', json.loads(bad['body'])['error'])
        self.assertEqual((down['statusCode'], other['statusCode']), (502, 502))


if __name__ == '__main__':
    unittest.main()
