"""Shared helpers: configuration, the parsing profile loader and small text utilities."""
import html
import importlib.util
import json
import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    'bucket': '',                     # S3 bucket that holds the content
    'ops_bucket': '',                 # private bucket for Batch Operations manifests and reports
    'batch_role': 'pta-batchops',     # IAM role assumed by S3 Batch Operations
    'base_url': '',                   # https://<distribution>.cloudfront.net (no trailing slash)
    'listing': 'listing.txt',         # tab-separated Key, StorageClass, Size
    'profile': 'profiles/citation_meta.py',
    'site': {
        'archive_name': 'Journal archive',          # heading of the top-level index
        'index_title': 'Journal archive',           # <title> of the top-level index
        'journal_title_suffix': 'Journal archive',  # "<journal> | <suffix>" in each journal page <title>
        'show_index_heading': True,                 # false hides the heading visually but keeps it for screen readers
        'index_intro': 'Back issues of {count} held by the library. Choose a journal to see its volumes and articles.',  # set to '' to leave it out
        'all_journals': 'All archived journals',    # link from a journal page back to the index
        'provider': 'the provider',                 # who supplied the files, used in notes about missing articles
        'footer': "These are the library's archived copies of licensed journal issues, for authorized users only.",
    },
    'kbart': {
        'link_scheme': '',            # title-level oclc_linkscheme for overlay rows; blank leaves linking alone
        'title_id_template': '',      # e.g. "{host}/{slug}/index.html?x=" (see README, article links)
        'avoid_title_ids': [],        # title_id values that mark odd knowledge base entries to avoid
        'pilot': '',                  # journal folder written to pilot_kbart.txt for a first test upload
    },
}


def load_config(path=None):
    """Defaults overlaid with config.json (or the given file). A missing default file is not an error."""
    p = Path(path) if path else ROOT / 'config.json'
    cfg = json.loads(json.dumps(DEFAULTS))
    if p.exists():
        user = json.loads(p.read_text(encoding='utf-8'))
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    elif path:
        raise SystemExit(f'config file not found: {p}')
    cfg['base_url'] = cfg['base_url'].rstrip('/')
    return cfg


def need(cfg, key):
    if not cfg.get(key):
        raise SystemExit(f'"{key}" is not set. Copy config.example.json to config.json and fill it in.')
    return cfg[key]


def load_profile(cfg):
    """Import the parsing profile named in the config (a path relative to the repository root)."""
    p = Path(cfg['profile'])
    p = p if p.is_absolute() else ROOT / p
    spec = importlib.util.spec_from_file_location('pta_profile', p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TAG = re.compile(r'<[^>]+>')


def text(fragment):
    """Strip tags, unescape entities and collapse whitespace."""
    return re.sub(r'\s+', ' ', html.unescape(TAG.sub(' ', fragment or ''))).strip()


def first(pattern, s, flags=re.S):
    m = re.search(pattern, s, flags)
    return m.group(1) if m else ''


def norm(s):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFC', s or '')).strip()


def fold(s):
    return ''.join(c for c in unicodedata.normalize('NFKD', s.lower()) if not unicodedata.combining(c))
