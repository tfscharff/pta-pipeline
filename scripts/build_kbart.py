"""Build the WMS KBART for Muse.MuseAll from the journal summaries of what is actually in S3.

Joins the WMS export of Muse.MuseAll (keeps its exact columns and oclc_entry_id) to the per-journal
summaries written by build_pages.py (work/meta/<slug>.json) by ISSN, falling back to normalized title.
For every matched title it writes one row per unbroken run of volumes (a gap in volume numbers starts
a new row), with title_url pointing at the journal's CloudFront index page and ACTION=covoverlay.

Outputs (in --out):
  muse_pta_kbart.txt     rows for every matched title (UTF-8, tab-delimited, WMS column order)
  pilot_kbart.txt        the same rows for --pilot only, to test an upload first
  unmatched_s3.tsv       S3 journals with no Muse.MuseAll row (need a local collection)
  match_report.tsv       every S3 journal with how it matched and its coverage runs

Usage:
  python scripts/build_kbart.py --kbart work/6569_Muse.MuseAll_kbart.txt
"""
import argparse
import csv
import glob
import json
import re
import time
import unicodedata
from pathlib import Path

BASE_URL = 'https://d2cfus91tk2xx.cloudfront.net'
# The collection's own link scheme (muse) sends article links to a muse.jhu.edu search. WMS rejects a blank
# or unlisted value, so use a listed scheme that OCLC's linker resolves to the title_url instead.
LINK_SCHEME = 'atoz'


def issn(s):
    s = (s or '').strip().upper().replace('-', '')
    return s[:4] + '-' + s[4:] if re.fullmatch(r'\d{7}[\dX]', s) else ''


def title_key(s):
    s = unicodedata.normalize('NFKD', s or '').encode('ascii', 'ignore').decode().lower()
    s = re.sub(r'&', ' and ', s)
    s = re.sub(r'[^a-z0-9]+', ' ', s).strip()
    return re.sub(r'^(the|a|an) ', '', s)


def vol_num(issue):
    """Volume as an integer: the designation's volume when numeric, else the folder's (v017 for 'XVII-XVIII').
    MUSE files unnumbered items under volume 0 and special issues under 10000; those are left out of coverage."""
    for v in (issue.get('volume'), issue['folder'].split('/')[0].lstrip('v')):
        m = re.match(r'\d+', v or '')
        if m:
            return int(m.group()) if 0 < int(m.group()) < 10000 else None
    return None


def vol_end(v, issue):
    """Last volume an issue covers: 25 for volume '24-25', 31 for a combined number '30/31' or '45 & 46'
    when the journal numbers issues like volumes (Minnesota Review)."""
    end = v
    vols = [int(n) for n in re.findall(r'\d+', issue.get('volume') or '')]
    if vols and vols[0] == v and max(vols) - v <= 3:
        end = max(vols)
    elif re.fullmatch(r'[IVXLC]+\s*[-/&]\s*[IVXLC]+', (issue.get('volume') or '').strip()):
        end = v + 1  # 'XVII-XVIII'
    nums = [int(n) for n in re.findall(r'\d+', issue.get('number') or '')]
    if nums and nums[0] == v and max(nums) - v <= 3:
        end = max(end, max(nums))
    return end


def is_year(n):
    return 1800 <= n <= int(time.strftime('%Y')) + 1


def guess_year(v, off):
    """Year for an undated issue: the volume itself when it is a year (some issues are filed as v2021),
    else volume + the journal's offset, if that lands on a plausible year."""
    if is_year(v):
        return str(v)
    return str(v + off) if off is not None and is_year(v + off) else ''


def year_offset(keyed):
    """year - volume when a journal publishes about one volume a year (at least 80% of dated issues within
    a year of the most common offset, and at least 3 of them), so undated issues can get a year."""
    offs = [int(i['year']) - v for v, i in keyed if (i.get('year') or '').isdigit() and not is_year(v)]
    if len(offs) < 3:
        return None
    best = max(set(offs), key=offs.count)
    return best if sum(abs(o - best) <= 1 for o in offs) >= 0.8 * len(offs) else None


def runs(summary):
    """Split a journal's issues into runs of consecutive volume numbers."""
    issues = [i for i in summary['issues'] if not i['folder'].startswith('preprint')]
    keyed = sorted(((vol_num(i), i) for i in issues if vol_num(i) is not None),
                   key=lambda t: (t[0], t[1].get('year') or '', t[1]['folder']))
    off = year_offset(keyed)
    out = []
    for v, i in keyed:
        if out and v - out[-1][-1][2] <= 1:
            out[-1].append((v, i, max(vol_end(v, i), out[-1][-1][2])))
        else:
            out.append([(v, i, vol_end(v, i))])
    result = []
    for r in out:
        years = [i.get('year') or guess_year(v, off) for v, i, _ in r]
        inferred = any(not i.get('year') and y for (_, i, _), y in zip(r, years))
        years = [y for y in years if y]
        first, last = r[0][1], r[-1][1]
        result.append({
            'date_first': min(years) if years else '', 'vol_first': str(r[0][0]),
            'iss_first': first.get('number') or '',
            'date_last': max(years) if years else '', 'vol_last': str(r[-1][0]),
            'iss_last': last.get('number') or '', 'issues': len(r), 'inferred_year': inferred})
    # Issues filed with a year as their volume (v1978) are extras inside the regular run; drop their rows
    # when a regular row already spans those years.
    regular = [x for x in result if not is_year(int(x['vol_first']))]
    return [x for x in result if not is_year(int(x['vol_first'])) or not any(
        y['date_first'] <= x['date_first'] and x['date_last'] <= (y['date_last'] or '9999') for y in regular)]


def overlap(row, rs):
    """Rank a WMS entry for a title (each title has one entry per coverage range): volumes it shares
    with our runs, then the earliest start, so our coverage lands on the long-standing entry."""
    def num(s, default):
        m = re.match(r'\d+', s or '')
        return int(m.group()) if m else default
    lo, hi = num(row['num_first_vol_online'], 0), num(row['num_last_vol_online'], 10 ** 6)
    shared = sum(max(0, min(hi, int(r['vol_last'])) - max(lo, int(r['vol_first'])) + 1) for r in rs)
    return shared, -num(row['date_first_issue_online'][:4], 9999)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--kbart', required=True, help='WMS export of Muse.MuseAll (UTF-8 tab-delimited)')
    ap.add_argument('--meta', default='work/meta')
    ap.add_argument('--out', default='work/kbart')
    ap.add_argument('--pilot', default='ab_autobiography_studies')
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    with open(a.kbart, encoding='utf-8-sig', newline='') as fh:
        rd = csv.DictReader(fh, delimiter='\t', quoting=csv.QUOTE_NONE)
        cols, kb = rd.fieldnames, list(rd)
    by_issn, by_title = {}, {}
    for row in kb:
        for f in ('print_identifier', 'online_identifier'):
            if issn(row[f]):
                by_issn.setdefault(issn(row[f]), []).append(row)
        by_title.setdefault(title_key(row['publication_title']), []).append(row)

    ocn_titles = {}
    for row in kb:
        if row['oclc_number']:
            ocn_titles.setdefault(row['oclc_number'], set()).add(title_key(row['publication_title']))
    shared_ocn = {o for o, t in ocn_titles.items() if len(t) > 1}

    summaries = [json.load(open(f, encoding='utf-8')) for f in sorted(glob.glob(f'{a.meta}/*.json'))
                 if not f.endswith(('.articles.json', '.pdf.json'))]

    rows, report, unmatched, used = [], [], [], {}
    for s in summaries:
        cands, how = [], ''
        for i in (issn(s.get('issn')), issn(s.get('print_issn'))):
            for r in by_issn.get(i, []) if i else []:
                if r not in cands:
                    cands.append(r)
        if cands:
            how = 'issn'
        else:
            cands, how = by_title.get(title_key(s['journal']), []), 'title'
        rs = runs(s)
        if not cands:
            unmatched.append(s)
            report.append([s['slug'], s['journal'], s.get('issn', ''), s.get('print_issn', ''), 'none', '', '', len(rs), sum(r['inferred_year'] for r in rs), sum(not r['date_first'] for r in rs)])
            continue
        # Prefer a proper entry: a MUSE title_id, and an OCN no other title uses (the export has odd entries,
        # e.g. title_id 'muse' carrying another journal's OCN), then the best coverage overlap.
        kr = max(cands, key=lambda c: (c['title_id'] != 'muse', c['oclc_number'] not in shared_ocn, overlap(c, rs)))
        used.setdefault(kr['oclc_entry_id'], []).append(s['slug'])
        report.append([s['slug'], s['journal'], s.get('issn', ''), s.get('print_issn', ''), how,
                       kr['publication_title'], kr['oclc_entry_id'] + (f' (+{len(cands) - 1} more)' if len(cands) > 1 else ''), len(rs),
                       sum(r['inferred_year'] for r in rs), sum(not r['date_first'] for r in rs)])
        for r in rs:
            new = dict(kr)
            new.update({'date_first_issue_online': r['date_first'], 'num_first_vol_online': r['vol_first'],
                        'num_first_issue_online': r['iss_first'], 'date_last_issue_online': r['date_last'],
                        'num_last_vol_online': r['vol_last'], 'num_last_issue_online': r['iss_last'],
                        'title_url': f"{BASE_URL}/{s['slug']}/index.html", 'coverage_depth': 'fulltext',
                        'ACTION': 'covoverlay'})
            rows.append((s['slug'], new))

    def write(path, items):
        with open(path, 'w', encoding='utf-8', newline='') as fh:
            w = csv.DictWriter(fh, fieldnames=cols, delimiter='\t', quoting=csv.QUOTE_NONE, escapechar='\\',
                               lineterminator='\r\n')
            w.writeheader()
            w.writerows(r for _, r in items)

    write(out / 'muse_pta_kbart.txt', rows)
    write(out / 'pilot_kbart.txt', [x for x in rows if x[0] == a.pilot])
    # covoverlay changes coverage only; the same rows with ACTION=overlay carry title_url and the
    # title-level link scheme (upload with "Update changes only" after the covoverlay file).
    write(out / 'muse_pta_kbart_overlay.txt',
          [(s, {**r, 'ACTION': 'overlay', 'oclc_linkscheme': LINK_SCHEME}) for s, r in rows])
    with open(out / 'match_report.tsv', 'w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh, delimiter='\t')
        w.writerow(['slug', 'journal', 'issn', 'print_issn', 'matched_by', 'kbart_title', 'oclc_entry_id', 'coverage_rows', 'rows_year_inferred', 'rows_undated'])
        w.writerows(report)
    with open(out / 'unmatched_s3.tsv', 'w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh, delimiter='\t')
        w.writerow(['slug', 'journal', 'issn', 'print_issn', 'publisher', 'years'])
        for s in unmatched:
            w.writerow([s['slug'], s['journal'], s.get('issn', ''), s.get('print_issn', ''), s.get('publisher', ''), '-'.join(s.get('years') or [])])

    # S3 journals missing from Muse.MuseAll go in a local collection: standard KBART columns plus oclc_number.
    local_cols = cols[:cols.index('vendor_id')] + ['oclc_number']
    with open(out / 'local_kbart.txt', 'w', encoding='utf-8', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=local_cols, delimiter='\t', quoting=csv.QUOTE_NONE, escapechar='\\',
                           lineterminator='\r\n', extrasaction='ignore')
        w.writeheader()
        for s in unmatched:
            for r in runs(s):
                w.writerow({'publication_title': s['journal'], 'print_identifier': s.get('print_issn', ''),
                            'online_identifier': s.get('issn', ''), 'date_first_issue_online': r['date_first'],
                            'num_first_vol_online': r['vol_first'], 'num_first_issue_online': r['iss_first'],
                            'date_last_issue_online': r['date_last'], 'num_last_vol_online': r['vol_last'],
                            'num_last_issue_online': r['iss_last'], 'title_url': f"{BASE_URL}/{s['slug']}/index.html",
                            'coverage_depth': 'fulltext', 'publisher_name': s.get('publisher', '')})

    shared = {k: v for k, v in used.items() if len(v) > 1}
    by = lambda m: sum(1 for r in report if r[4] == m)
    print(f'KBART titles: {len(kb)}; S3 journals: {len(summaries)}')
    print(f'matched by ISSN: {by("issn")}, by title: {by("title")}, unmatched: {by("none")}')
    print(f'KBART rows written: {len(rows)} for {len(used)} titles; titles with gaps (>1 row): '
          f'{sum(1 for r in report if r[7] and r[7] > 1 and r[4] != "none")}')
    print(f'rows with an inferred year: {sum(r[8] for r in report)}; rows still undated: {sum(r[9] for r in report)}')
    print(f'KBART entries matched by more than one S3 journal: {len(shared)} {list(shared.items())[:10]}')


if __name__ == '__main__':
    main()
