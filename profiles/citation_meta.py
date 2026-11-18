"""Default parsing profile: article metadata from citation_* meta tags, PDF metadata from document info.

Most publisher platforms put Highwire-style tags in the head of each article page (citation_title,
citation_author, citation_journal_title, citation_volume and so on), so this profile works for many
collections without changes. When a provider's files need more than that, copy this file, adapt it and
point "profile" in config.json at the copy.

A profile is a Python module with:

  parse_article(path) -> dict
      Metadata for one article HTML file. Keys the page builder reads:
        title, authors (list of names), journal, publisher, type, pages, issn, print_issn
        designation   the issue as text, "Volume 12, Number 3, Fall 2001". Volume, number and year are
                      read back out of this string, and it is shown as the issue heading.
      Optional keys:
        prev, next    titles of the neighbouring articles, when the page names them. An article with no
                      title of its own can then be titled from its neighbours.
        missing       True when the file is the provider's error page with no article behind it. The
                      article is left off the journal page and counted in a note.

  parse_pdf(path, article_id) -> dict
      The same keys for a PDF, plus 'src': 'pdf'. Set 'error' to a message to record a problem.

  REVISION (optional)    string stamped on re-read cache entries. Change it after improving the profile so
                         that --reparse-html re-reads articles that still have no title.
  ARTICLE_KEY (optional) compiled regex for article keys in the bucket, with named groups slug, vol,
                         issue, id, ext and optionally sub. The default layout is
                         <journal>/v<volume>/<issue>/<id>.<pdf|html>, with <journal>/preprint/... for
                         articles not yet in an issue.
  ASSET_DIRS (optional)  folder names (figures, thumbnails) whose .html files are not articles.
"""
import html
import re

from common import first, norm, text

REVISION = '1'

META_TAG = re.compile(r'<meta\b[^>]*>', re.I)
ATTR = re.compile(r'([\w:.-]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\')')


def meta_tags(s):
    """{name: [content, ...]} for every <meta name|property=... content=...> in the document."""
    out = {}
    for tag in META_TAG.findall(s):
        attrs = {k.lower(): (a or b) for k, a, b in ATTR.findall(tag)}
        name = (attrs.get('name') or attrs.get('property') or '').lower()
        if name and 'content' in attrs:
            value = text(html.unescape(attrs['content']))
            if value:
                out.setdefault(name, []).append(value)
    return out


def parse_article(path):
    s = path.read_text(encoding='utf-8', errors='replace')
    tags = meta_tags(s)

    def one(*names):
        return next((tags[n][0] for n in names if tags.get(n)), '')

    title = one('citation_title', 'dc.title', 'og:title') or text(first(r'<title[^>]*>(.*?)</title>', s, re.S | re.I))
    authors = list(dict.fromkeys(tags.get('citation_author') or tags.get('dc.creator') or []))
    volume, issue = one('citation_volume'), one('citation_issue')
    date = one('citation_publication_date', 'citation_date', 'citation_online_date', 'dc.date')
    year = (re.search(r'\b(1[6-9]\d\d|20\d\d)\b', date) or [''])[0]
    designation = ', '.join(x for x in (f'Volume {volume}' if volume else '', f'Number {issue}' if issue else '', year) if x)
    first_page, last_page = one('citation_firstpage'), one('citation_lastpage')
    pages = f'{first_page}-{last_page}' if first_page and last_page and first_page != last_page else first_page
    # The tags do not say which ISSN is the online one. The first is taken as online, a second as print.
    issns = list(dict.fromkeys(tags.get('citation_issn') or []))
    return {
        'journal': one('citation_journal_title', 'citation_journal_abbrev'),
        'designation': designation,
        'publisher': one('citation_publisher', 'dc.publisher'),
        'type': one('citation_article_type'),
        'title': title,
        'authors': authors,
        'pages': pages,
        'issn': issns[0] if issns else '',
        'print_issn': issns[1] if len(issns) > 1 else '',
    }


def parse_pdf(path, article_id):
    from pypdf import PdfReader  # only needed for --pdf
    info = PdfReader(path).metadata or {}
    author = norm(info.get('/Author'))
    return {'src': 'pdf', 'title': norm(info.get('/Title')), 'authors': [author] if author else []}
