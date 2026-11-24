"""Article search relay: an AWS Lambda function that searches the WorldCat Discovery central index and keeps
only articles from journals in the archive, each linked to its archived copy.

The OCLC WSKey stays here, never in a page. CloudFront sends /api/search?q=...&offset=... to this function's
URL (see README, article search), so the same WAF allow list and EZproxy apply as for the archive.

Bundled next to this file by scripts/build_search.py:
  settings.json   Discovery API limiters from config.json "search": db_ids, item_sub_type, held_by_institution
  titles.json     the archive's journals: ISSNs, folded titles, years held

Environment:
  WSKEY_PARAM     name of an SSM SecureString parameter holding "<key>:<secret>"

Standard library only, plus boto3 (present in the Lambda Python runtime) to read the parameter.
"""
import base64
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOKEN_URL = 'https://oauth.oclc.org/token'
SEARCH_URL = 'https://discovery.api.oclc.org/worldcat-org-ci/search/summary-bibs'
SCOPE = 'WorldCatDiscoveryAPI:view_summary_bib'
PAGE = 50  # the API's maximum

_settings = _titles = _credentials = None
_token = {'value': '', 'expires': 0.0}


def fold(s):
    return ''.join(c for c in unicodedata.normalize('NFKD', (s or '').lower()) if not unicodedata.combining(c))


def title_key(s):
    """Journal title for matching: folded, leading article and punctuation dropped."""
    s = re.sub(r'[^a-z0-9]+', ' ', fold(s)).strip()
    return re.sub(r'^(the|a|an) ', '', s)


def issn_key(s):
    s = re.sub(r'[^0-9xX]', '', s or '').upper()
    return s if len(s) == 8 else ''


def load():
    global _settings, _titles
    if _settings is None:
        _settings = json.loads((HERE / 'settings.json').read_text(encoding='utf-8'))
        _titles = json.loads((HERE / 'titles.json').read_text(encoding='utf-8'))
    return _settings, _titles


def credentials():
    global _credentials
    if _credentials is None:
        import os
        import boto3
        value = boto3.client('ssm').get_parameter(Name=os.environ['WSKEY_PARAM'], WithDecryption=True)['Parameter']['Value']
        _credentials = value.strip().split(':', 1)
    return _credentials


def token():
    if _token['value'] and time.time() < _token['expires'] - 60:
        return _token['value']
    key, secret = credentials()
    req = urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode({'grant_type': 'client_credentials', 'scope': SCOPE}).encode(),
                                 headers={'Authorization': 'Basic ' + base64.b64encode(f'{key}:{secret}'.encode()).decode(),
                                          'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as r:
        got = json.load(r)
    _token['value'] = got['access_token']
    _token['expires'] = time.time() + int(got.get('expires_in', 1200))
    return _token['value']


def discovery(q, offset, settings):
    params = {'q': q, 'offset': offset, 'limit': PAGE, 'orderBy': 'bestMatch'}
    if settings.get('db_ids'):
        params['dbIds'] = ','.join(map(str, settings['db_ids']))
    if settings.get('item_sub_type'):
        params['itemSubType'] = settings['item_sub_type']
    if settings.get('held_by_institution'):
        params['heldByInstitutionID'] = settings['held_by_institution']
    req = urllib.request.Request(f'{SEARCH_URL}?{urllib.parse.urlencode(params)}',
                                 headers={'Authorization': f'Bearer {token()}', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def source(rec):
    """The record's source fields. The API returns a list of partial objects (the spec says one object)."""
    src = rec.get('source') or {}
    if isinstance(src, dict):
        return src
    out = {}
    for s in src:
        for k, v in (s or {}).items():
            out.setdefault(k, v)
    return out


def authors(rec):
    names = []
    for c in rec.get('contributors') or []:
        if isinstance(c, str):
            names.append(c)
        elif isinstance(c, dict):
            n = ' '.join(x.get('text', '') for x in (c.get('firstName') or {}, c.get('secondName') or {}) if isinstance(x, dict)).strip()
            if n:
                names.append(n)
    if rec.get('creator') and not names:
        names.append(rec['creator'])
    return names


def parts(src):
    """Volume, issue and pages of an article, from the structured fields or the "Vol. 7, no. 10 (2012), p. 1-9" text."""
    vol = (src.get('volumes') or [''])[0]
    issue = (src.get('issues') or [''])[0]
    pages = (src.get('pageRanges') or [''])[0]
    text = ' '.join(src.get('relatedParts') or [])
    if not vol:
        m = re.search(r'\bv(?:ol)?\.?\s*(\w+)', text, re.I)
        vol = m.group(1) if m else ''
    if not issue:
        m = re.search(r'\b(?:no|nr|n|issue)\.?\s*(\d[\w/-]*)', text, re.I)
        issue = m.group(1) if m else ''
    if not pages:  # "p. 1-20" or "v38 n2 (2015): 267-272"
        m = re.search(r'\b(?:p|pp|s|str)\.?\s*(\w+\s*-\s*\w+|\w+)', text, re.I) or re.search(r':\s*(\w+\s*-\s*\w+)', text)
        pages = m.group(1) if m else ''
    return vol.strip(), issue.strip(), re.sub(r'\s+', '', pages)


def match(rec, titles):
    """The archive journal an article belongs to, or None."""
    src = source(rec)
    for i in [src.get('sourceIssn', ''), *(rec.get('sourceIssns') or []), *(rec.get('issns') or [])]:
        slug = titles['issn'].get(issn_key(i))
        if slug:
            return slug
    return titles['title'].get(title_key(src.get('sourceTitle', '')))


def result(rec, titles):
    """A display record linked into the archive, or None when the article is not held there."""
    slug = match(rec, titles)
    if not slug:
        return None
    j = titles['journals'][slug]
    year = (re.search(r'\b(1[6-9]\d\d|20\d\d)\b', rec.get('date') or '') or [''])[0]
    if year and j['years'] and year not in j['years']:
        return None
    vol, issue, pages = parts(source(rec))
    title = re.sub(r'\s*[/:;,.]\s*$', '', rec.get('title') or '').strip()
    query = {k: v for k, v in (('Title', title), ('volume', vol), ('issue', issue), ('year', year)) if v}
    return {'title': title, 'authors': authors(rec), 'journal': j['title'], 'volume': vol, 'issue': issue, 'year': year,
            'pages': pages, 'link': f"{slug}/index.html?{urllib.parse.urlencode(query)}"}


def search(q, offset, settings, titles, fetch=discovery):
    data = fetch(q, offset, settings)
    recs = data.get('briefRecords') or []
    out = [r for r in (result(rec, titles) for rec in recs) if r]
    total = int(data.get('numberOfRecords') or 0)
    nxt = offset + PAGE if offset + PAGE < total and recs else None
    return {'query': q, 'total': total, 'offset': offset, 'next': nxt, 'checked': len(recs), 'results': out}


def reply(status, body):
    return {'statusCode': status, 'headers': {'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store'},
            'body': json.dumps(body, ensure_ascii=False)}


def handler(event, context=None):
    p = event.get('queryStringParameters') or {}
    q = (p.get('q') or '').strip()[:500]
    if not q:
        return reply(400, {'error': 'Type something to search for.'})
    try:
        offset = max(0, min(int(p.get('offset') or 0), 5000))
    except ValueError:
        offset = 0
    settings, titles = load()
    try:
        return reply(200, search(q, offset, settings, titles))
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', 'replace')[:500]
        print(f'OCLC HTTP {e.code}: {detail}')
        return reply(400 if e.code == 400 else 502,
                     {'error': 'The search could not be understood. Try fewer or simpler words.' if e.code == 400
                      else 'The article index did not answer. Try again in a minute.'})
    except Exception as e:  # network, token, parameter
        print(f'relay error: {type(e).__name__}: {e}')
        return reply(502, {'error': 'The article index did not answer. Try again in a minute.'})
