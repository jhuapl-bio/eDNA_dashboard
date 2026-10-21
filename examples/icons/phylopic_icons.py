#!/usr/bin/env python3
"""
phylopic_icons.py — organism silhouettes (PhyloPic) for the MBON eDNA reports
=============================================================================

Same idea as jhuapl-bio/mytax (src/services/phylopic.js): a prebuilt, offline
PhyloPic index maps scientific names to silhouette images; a taxon is resolved
by its own name first, then progressively broader names. The SVGs themselves
are downloaded once into ``svg/`` next to this file and embedded into the HTML
report by ``build_mbon_html.py`` (or by ``inject`` below), so the report works
offline and never calls PhyloPic from the browser.

Folder layout (this folder, outside the eDNA_processing repo)::

    icons/
      phylopic_icons.py     this script (stdlib only)
      phylopic_index.json   name -> image index (copied from mytax; `refresh-index` rebuilds it)
      svg/<uuid>.svg        downloaded silhouettes
      manifest.json         per-image attribution / licence + NCBI taxid -> image cache
      overrides.tsv         your manual fixes: name <TAB> uuid | none
      matches.csv           what every organism in the last report matched (review this)

Resolution order for one organism (name, group, taxid, lineage):

  1. overrides.tsv (exact name, then genus)            how = "override"
  2. exact name / cleaned name / binomial in the index  how = "species"
  3. NCBI taxid resolved through the PhyloPic API       how = "taxid"   (cached in manifest.json)
  4. genus (index title, clade title, or a congener)    how = "genus"
  5. Kraken2 lineage, deepest rank first                how = "lineage"
  6. the report's group name (e.g. Diatoms -> Diatomea) how = "group"   (a representative stand-in)

Commands::

    python3 phylopic_icons.py fetch  --report ../eDNA_processing/reports/mbon.html --inject
    python3 phylopic_icons.py fetch  --bundle ~/Downloads/mbon_phylopic_svgs_bundle.json --report ... --inject
    python3 phylopic_icons.py inject --report ../eDNA_processing/reports/mbon.html
    python3 phylopic_icons.py match  "Mola mola" "Vibrio sp." --group Fish
    python3 phylopic_icons.py refresh-index

``fetch`` needs internet access to images.phylopic.org / api.phylopic.org (run it
in your own Terminal). ``inject`` and the report builder only read this folder.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
API = 'https://api.phylopic.org'
IMG = 'https://images.phylopic.org'
API_HEADERS = {'Accept': 'application/vnd.phylopic.v2+json',
               'User-Agent': 'mbon-edna-icons/1.0 (+https://github.com/jhuapl-bio/eDNA_processing)'}
DATA_START, DATA_END = '/*@@MBON_DATA_START@@*/', '/*@@MBON_DATA_END@@*/'

# Report group label -> PhyloPic clade titles to try (first hit wins). Groups
# not listed are tried by their own name. Edit freely; overrides.tsv also takes
# "group:<Group name>" rows.
GROUP_CLADES = {
    'Diatoms': ['Bacillariophyta', 'Diatomea'],
    'Crustaceans': ['Crustacea', 'Copepoda'],
    'Copepods': ['Copepoda'],
    'Fish': ['Actinopterygii', 'Teleostei'],
    'Sharks & rays': ['Elasmobranchii', 'Chondrichthyes'],
    'Cnidarians': ['Cnidaria'],
    'Green algae': ['Chlorophyta', 'Viridiplantae', 'Mamiellophyceae'],
    'Dinoflagellates': ['Dinoflagellata'],
    'Ciliates': ['Ciliophora'],
    'Worms': ['Annelida', 'Polychaeta'],
    'Molluscs': ['Mollusca'],
    'Fungi': ['Fungi'],
    'Heterokont algae': ['Ochrophyta', 'Phaeophyceae'],
    'Bacilli': ['Bacilli'],
    'Gammaproteobacteria': ['Gammaproteobacteria'],
    'Betaproteobacteria': ['Betaproteobacteria'],
    'Alphaproteobacteria': ['Alphaproteobacteria'],
    'Mammals': ['Mammalia'], 'Mammalia': ['Mammalia'],
    'Birds': ['Aves'], 'Reptiles': ['Reptilia', 'Testudines'],
    'Insects': ['Insecta'],
    'Bryozoans': ['Bryozoa', 'Ectoprocta', 'Gymnolaemata'],
    'Cryptomonads': ['Cryptophyceae', 'Cryptophyta'],
    'Plants': ['Embryophyta'],
    'Saccharomycetes': ['Saccharomycetes'],
    'Echinoderms': ['Echinodermata'],
    'Sponges': ['Porifera'],
    'Oomycetes': ['Oomycota'],
    'Tunicates': ['Tunicata', 'Ascidiacea', 'Appendicularia'],
    'Malacostraca': ['Malacostraca', 'Decapoda'],
    'Mites and ticks': ['Acari'],
    'Rotifers': ['Rotifera'],
    'Ribbon worms': ['Nemertea'],
    'Amoebae': ['Amoebozoa'],
    'Comb jellies': ['Ctenophora'],
    'Haptophytes': ['Haptophyta'],
    'Red algae': ['Rhodophyta'],
}
NO_ICON_GROUPS = {'other', 'unknown', 'unclassified', ''}

# ------------------------------------------------------------------ names
def norm(name) -> str:
    """Same normalisation as mytax (letters + single spaces, lower case)."""
    s = str(name or '').replace('_', ' ')
    s = re.sub(r'[^A-Za-z\s]', ' ', s)
    return re.sub(r'\s+', ' ', s).strip().lower()


_QUALIFIERS = re.compile(r'\b(subsp|ssp|str|strain|var|serovar|cf|aff|nr|f|forma|isolate|clone|'
                         r'environmental|uncultured)\b.*$', re.I)


def clean_name(name) -> str:
    """'Salmonella enterica subsp. enterica str. LT2' -> 'salmonella enterica';
    'Nephtys sp. RA03' -> 'nephtys'; 'Clade Ib (genus)' -> 'clade ib'."""
    s = re.sub(r'\s*\([^)]*\)\s*$', '', str(name or ''))      # Kraken2 "(genus)" suffix
    s = re.split(r'\s+sp{1,2}\.?(?:\s|$)', s)[0]              # "Genus sp. XYZ"
    s = _QUALIFIERS.sub('', s)
    return norm(s)


def name_candidates(name):
    """Most specific first, de-duplicated: full, cleaned, binomial."""
    out = []
    full, c = norm(name), clean_name(name)
    parts = c.split(' ')
    for k in (full, c, ' '.join(parts[:2]) if len(parts) >= 2 else ''):
        if k and k not in out:
            out.append(k)
    return out


def genus_of(name) -> str:
    c = clean_name(name)
    return c.split(' ')[0] if c else ''


# ------------------------------------------------------------------ storage
class Store:
    def __init__(self, folder=HERE):
        self.dir = Path(folder)
        self.svg_dir = self.dir / 'svg'
        self.index_path = self.dir / 'phylopic_index.json'
        self.manifest_path = self.dir / 'manifest.json'
        self.overrides_path = self.dir / 'overrides.tsv'
        self._index = None
        self.manifest = self._load_json(self.manifest_path, {'images': {}, 'taxids': {}})
        self.manifest.setdefault('images', {})
        self.manifest.setdefault('taxids', {})
        self.overrides = self._load_overrides()

    @staticmethod
    def _load_json(p, default):
        try:
            return json.loads(Path(p).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return default

    def _load_overrides(self):
        out = {}
        if not self.overrides_path.exists():
            return out
        for line in self.overrides_path.read_text(encoding='utf-8').splitlines():
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            parts = re.split(r'\t|\s{2,}', line.strip(), maxsplit=1)
            if len(parts) != 2:
                continue
            key, val = parts[0].strip(), parts[1].strip()
            k = 'group:' + norm(key[6:]) if key.lower().startswith('group:') else norm(key)
            out[k] = None if val.lower() in ('none', '-', '') else val.lower()
        return out

    def save_manifest(self):
        tmp = self.manifest_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.manifest, indent=1, sort_keys=True), encoding='utf-8')
        tmp.replace(self.manifest_path)

    # -------- index: normalized title -> entry (specific titles), clade titles, genus -> congener
    @property
    def index(self):
        if self._index is None:
            self._index = self._build_index()
        return self._index

    def _build_index(self):
        raw = self._load_json(self.index_path, {})
        orgs = raw.get('organisms') or {}
        if not orgs and isinstance(raw.get('map'), dict):       # mytax "phylopic-index.json" shape
            orgs = {t: {'UUID': re.search(r'images/([0-9a-f-]{36})/', v.get('sourceFile') or '').group(1),
                        'svg': v.get('vectorFile') or v.get('sourceFile')}
                    for t, v in raw['map'].items() if re.search(r'images/([0-9a-f-]{36})/', v.get('sourceFile') or '')}
        spec, clade, congener, by_uuid = {}, {}, {}, {}
        as_list = lambda v: v if isinstance(v, list) else [v]
        for title, val in orgs.items():
            for e in as_list(val):
                if not e or not e.get('UUID'):
                    continue
                by_uuid.setdefault(e['UUID'], dict(e, title=title))
                k = norm(title)
                if k and k not in spec:
                    spec[k] = e['UUID']
        for title, val in orgs.items():
            for e in as_list(val):
                if not e or not e.get('UUID'):
                    continue
                g = (e.get('generalNode') or {}).get('title')
                k = norm(g)
                if k and k not in spec and k not in clade:
                    clade[k] = e['UUID']
        for k, u in spec.items():
            parts = k.split(' ')
            if len(parts) >= 2:
                congener.setdefault(parts[0], u)
        return {'spec': spec, 'clade': clade, 'congener': congener, 'by_uuid': by_uuid}

    def entry(self, uuid):
        return self.index['by_uuid'].get(uuid) or {}

    def has_svg(self, uuid):
        return (self.svg_dir / f'{uuid}.svg').exists()

    # -------- matching
    def _lookup(self, key, allow_congener=False):
        ix = self.index
        if key in ix['spec']:
            return ix['spec'][key]
        if key in ix['clade']:
            return ix['clade'][key]
        if allow_congener and ' ' not in key and key in ix['congener']:
            return ix['congener'][key]
        return None

    def match(self, name, group=None, taxid=None, lineage=None):
        """-> dict(uuid, how, key) or None. Never touches the network."""
        cands = name_candidates(name)
        genus = genus_of(name)
        # 1. overrides
        for k in cands + ([genus] if genus else []):
            if k in self.overrides:
                u = self.overrides[k]
                return {'uuid': u, 'how': 'override', 'key': k} if u else None
        # 2. species / binomial
        for k in cands:
            u = self._lookup(k)
            if u:
                return {'uuid': u, 'how': 'species' if ' ' in k else 'genus', 'key': k}
        # 3. taxid (resolved online by `fetch`, cached in manifest)
        if taxid not in (None, ''):
            u = self.manifest['taxids'].get(str(taxid))
            if u:
                return {'uuid': u, 'how': 'taxid', 'key': f'taxid:{taxid}'}
        # 4. genus
        if genus:
            u = self._lookup(genus, allow_congener=True)
            if u:
                return {'uuid': u, 'how': 'genus', 'key': genus}
        # 5. lineage (deepest first, skipping the name itself)
        for anc in reversed(list(lineage or [])):
            k = norm(anc)
            if not k or k in cands or k == genus or k in ('root', 'cellular organisms'):
                continue
            u = self._lookup(k)
            if u:
                return {'uuid': u, 'how': 'lineage', 'key': k}
        # 6. group
        return self.match_group(group)

    def match_group(self, group):
        g = norm(group)
        if g in NO_ICON_GROUPS:
            return None
        if 'group:' + g in self.overrides:
            u = self.overrides['group:' + g]
            return {'uuid': u, 'how': 'group', 'key': g} if u else None
        clades = next((v for k, v in GROUP_CLADES.items() if norm(k) == g), [])
        for c in clades + [group]:
            k = norm(c)
            u = self._lookup(k) if k else None
            if u:
                return {'uuid': u, 'how': 'group', 'key': k}
        return None


# ------------------------------------------------------------------ network (fetch only)
def _http(url, accept_json=False, timeout=30):
    headers = dict(API_HEADERS) if accept_json else {'User-Agent': API_HEADERS['User-Agent']}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b''
    except Exception as e:      # SSL cert problems on python.org builds of Python -> try curl
        if shutil.which('curl'):
            cmd = ['curl', '-sSL', '--max-time', str(timeout), '-w', '\n%{http_code}']
            for k, v in headers.items():
                cmd += ['-H', f'{k}: {v}']
            p = subprocess.run(cmd + [url], capture_output=True)
            if p.returncode == 0:
                body, _, code = p.stdout.rpartition(b'\n')
                return int(code or 0), body
        print(f'  ! {url}: {e}', file=sys.stderr)
        return 0, b''


def _api_json(url):
    code, body = _http(url, accept_json=True)
    if code != 200:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def current_build():
    j = _api_json(f'{API}/') or {}
    return j.get('build')


def resolve_taxid(taxid, build):
    """NCBI taxid -> PhyloPic primary image uuid (or None)."""
    url = (f'{API}/resolve/ncbi.nlm.nih.gov/taxid/{taxid}'
           f'?build={build}&embed_primaryImage=true')
    j = _api_json(url)
    if not j:
        return None
    emb = (j.get('_embedded') or {}).get('primaryImage') or {}
    if emb.get('uuid'):
        return emb['uuid']
    href = ((j.get('_links') or {}).get('primaryImage') or {}).get('href') or ''
    m = re.search(r'/images/([0-9a-f-]{36})', href)
    return m.group(1) if m else None


def image_info(uuid, build=None):
    j = _api_json(f'{API}/images/{uuid}' + (f'?build={build}' if build else '')) or {}
    links = j.get('_links') or {}
    return {'license': (links.get('license') or {}).get('href'),
            'attribution': j.get('attribution'),
            'title': (links.get('self') or {}).get('title')}


def download_svg(store, uuid):
    dest = store.svg_dir / f'{uuid}.svg'
    if dest.exists() and dest.stat().st_size > 100:
        return True
    for kind in ('vector.svg', 'source.svg'):
        code, body = _http(f'{IMG}/images/{uuid}/{kind}')
        if code == 200 and b'<svg' in body[:4000]:
            dest.write_bytes(body)
            return True
    return False


# ------------------------------------------------------------------ report payloads
def read_report_payload(html_path):
    s = Path(html_path).read_text(encoding='utf-8')
    a, b = s.find(DATA_START), s.find(DATA_END)
    if a < 0 or b < 0:
        raise SystemExit(f'{html_path}: no {DATA_START} ... {DATA_END} data slot')
    return json.loads(s[a + len(DATA_START):b]), s, a, b


def taxa_of_payload(payload):
    """Unique organisms across all runs: name -> {group, taxid, lineage}."""
    out = {}
    for run in payload.get('runs') or []:
        for t in run.get('taxa') or []:
            nm = t.get('species')
            if not nm:
                continue
            rec = out.setdefault(nm, {'group': t.get('group'), 'taxid': None, 'lineage': None})
            rec['taxid'] = rec['taxid'] or t.get('taxid')
            rec['lineage'] = rec['lineage'] or t.get('lineage')
        for st in run.get('stations') or []:
            for r in st.get('rows') or []:
                if r.get('species') and r['species'] not in out:
                    out[r['species']] = {'group': r.get('group'), 'taxid': None, 'lineage': None}
    return out


def match_all(store, taxa):
    rows = {}
    for nm, rec in sorted(taxa.items()):
        rows[nm] = store.match(nm, rec.get('group'), rec.get('taxid'), rec.get('lineage'))
    groups = sorted({rec.get('group') or '' for rec in taxa.values()})
    grows = {g: store.match_group(g) for g in groups if g}
    return rows, grows


def write_matches_csv(store, taxa, rows, grows, path=None):
    path = Path(path or store.dir / 'matches.csv')
    with path.open('w', newline='', encoding='utf-8') as fh:
        w = csv.writer(fh)
        w.writerow(['organism', 'group', 'taxid', 'how', 'matched', 'uuid', 'phylopic_title',
                    'attribution', 'svg_downloaded'])
        for nm, m in rows.items():
            rec = taxa.get(nm, {})
            e = store.entry(m['uuid']) if m else {}
            w.writerow([nm, rec.get('group') or '', rec.get('taxid') or '', m['how'] if m else 'none',
                        m['key'] if m else '', m['uuid'] if m else '', e.get('title', ''),
                        (store.manifest['images'].get(m['uuid'], {}) if m else {}).get('attribution')
                        or e.get('attribution', ''), 'yes' if m and store.has_svg(m['uuid']) else 'no'])
        for g, m in grows.items():
            w.writerow([f'group:{g}', g, '', m['how'] if m else 'none', m['key'] if m else '',
                        m['uuid'] if m else '', store.entry(m['uuid']).get('title', '') if m else '',
                        '', 'yes' if m and store.has_svg(m['uuid']) else 'no'])
    return path


# ------------------------------------------------------------------ SVG -> embeddable symbol
_VB = re.compile(r'viewBox\s*=\s*"([^"]+)"', re.I)


def _dim(svg_open, attr):
    m = re.search(attr + r'\s*=\s*"([\d.]+)', svg_open)
    return float(m.group(1)) if m else None


def svg_to_symbol(svg_text):
    """-> (viewBox, inner markup) with black fills mapped to currentColor."""
    s = re.sub(r'<\?xml[^>]*>|<!DOCTYPE[^>]*>|<!--.*?-->', '', svg_text, flags=re.S)
    m = re.search(r'<svg\b[^>]*>', s, re.I)
    if not m:
        return None
    open_tag = m.group(0)
    end = s.rfind('</svg>')
    inner = s[m.end():end if end > 0 else len(s)]
    inner = re.sub(r'<(metadata|title|desc)\b.*?</\1>', '', inner, flags=re.S | re.I)
    inner = re.sub(r'\s+', ' ', inner).strip()
    inner = re.sub(r'(fill|stroke)="(#000000|#000|black)"', r'\1="currentColor"', inner, flags=re.I)
    inner = re.sub(r'(fill|stroke):\s*(#000000|#000|black)', r'\1:currentColor', inner, flags=re.I)
    vb = _VB.search(open_tag)
    if vb:
        viewbox = vb.group(1).strip()
    else:
        w, h = _dim(open_tag, 'width'), _dim(open_tag, 'height')
        if not (w and h):
            return None
        viewbox = f'0 0 {w:g} {h:g}'
    # mytax/potrace SVGs paint with the default (black) fill on the root <svg>
    fill = re.search(r'\bfill="([^"]+)"', open_tag)
    if not fill or fill.group(1).lower() in ('#000', '#000000', 'black'):
        inner = f'<g fill="currentColor">{inner}</g>'
    return viewbox, inner


def icon_payload(payload, folder=HERE, log=print):
    """Build the `icons` object the report template reads. Only images whose SVG
    is present in svg/ are embedded; nothing is fetched."""
    store = Store(folder)
    taxa = taxa_of_payload(payload)
    rows, grows = match_all(store, taxa)
    img, keys, names = {}, {}, {}

    def ref(m):
        if not m or not m.get('uuid') or not store.has_svg(m['uuid']):
            return None
        u = m['uuid']
        if u not in keys:
            sym = svg_to_symbol((store.svg_dir / f'{u}.svg').read_text(encoding='utf-8', errors='replace'))
            if not sym:
                return None
            e, man = store.entry(u), store.manifest['images'].get(u, {})
            k = f'i{len(keys)}'
            keys[u] = k
            img[k] = {'u': u, 'vb': sym[0], 'b': sym[1],
                      't': man.get('title') or e.get('title') or '',
                      'a': man.get('attribution') or e.get('attribution') or '',
                      'l': man.get('license') or ''}
        return keys[u]

    level = {'override': 'species', 'species': 'species', 'taxid': 'species',
             'genus': 'genus', 'lineage': 'lineage', 'group': 'group'}
    for nm, m in rows.items():
        k = ref(m)
        if not k:
            continue
        rec = [k, level[m['how']], m['key']]
        if m['how'] == 'group':
            rec.append(taxa.get(nm, {}).get('group') or '')
        for key in name_candidates(nm):
            names.setdefault(key, rec)
        g = genus_of(nm)
        if g and g not in names:
            gm = store.match(g)            # genus-only lookups for names the report adds later
            gk = ref(gm) if gm and gm['how'] in ('species', 'genus', 'override') else None
            if gk:
                names[g] = [gk, 'genus', gm['key']]
    groups = {}
    for g, m in grows.items():
        k = ref(m)
        if k:
            groups[norm(g)] = [k, 'group', m['key']]
    n_hit = sum(1 for m in rows.values() if m and m['uuid'] in keys)
    by = {}
    for m in rows.values():
        if m and m['uuid'] in keys:
            by[level[m['how']]] = by.get(level[m['how']], 0) + 1
    missing = sorted({m['uuid'] for m in list(rows.values()) + list(grows.values())
                      if m and m.get('uuid') and not store.has_svg(m['uuid'])})
    log(f'icons: {n_hit}/{len(rows)} organisms have a silhouette '
        f'({", ".join(f"{v} {k}" for k, v in sorted(by.items()))}); {len(img)} SVGs embedded'
        + (f'; {len(missing)} matched image(s) not downloaded yet — run `phylopic_icons.py fetch`'
           if missing else ''))
    return {'v': 1, 'source': 'PhyloPic (https://www.phylopic.org)',
            'img': img, 'names': names, 'groups': groups}


# ------------------------------------------------------------------ commands
def cmd_fetch(a):
    store = Store(a.dir)
    store.svg_dir.mkdir(parents=True, exist_ok=True)
    if a.bundle:
        bundle = json.loads(Path(a.bundle).expanduser().read_text(encoding='utf-8'))
        n = 0
        for u, svg in bundle.items():
            if re.fullmatch(r'[0-9a-f-]{36}', u) and '<svg' in svg[:4000]:
                (store.svg_dir / f'{u}.svg').write_text(svg, encoding='utf-8')
                n += 1
        print(f'unpacked {n} SVG(s) from {a.bundle} into {store.svg_dir}')
    taxa = {}
    for rp in a.report or []:
        taxa.update(taxa_of_payload(read_report_payload(rp)[0]))
    for pj in a.payload or []:
        taxa.update(taxa_of_payload(json.loads(Path(pj).read_text(encoding='utf-8'))))
    for nm in a.names or []:
        taxa.setdefault(nm, {'group': None, 'taxid': None, 'lineage': None})
    if not taxa:
        if not a.bundle:
            print('nothing to do: pass --report, --payload, --names or --bundle')
        return 0
    online = not a.offline
    build = current_build() if online else None
    if online and build is None:
        print('! cannot reach api.phylopic.org — matching offline, downloads will be tried anyway')
    # taxids for organisms that don't match by name
    if online and build is not None and not a.no_taxid:
        todo = [(nm, str(r['taxid'])) for nm, r in taxa.items() if r.get('taxid')
                and str(r['taxid']) not in store.manifest['taxids']
                and not (store.match(nm) or {}).get('how') in ('species', 'override')]
        if todo:
            print(f'resolving {len(todo)} NCBI taxid(s) via PhyloPic…')
            with ThreadPoolExecutor(6) as ex:
                for (nm, tid), u in zip(todo, ex.map(lambda x: resolve_taxid(x[1], build), todo)):
                    store.manifest['taxids'][tid] = u
    rows, grows = match_all(store, taxa)
    want = sorted({m['uuid'] for m in list(rows.values()) + list(grows.values()) if m and m.get('uuid')})
    need = [u for u in want if not store.has_svg(u)]
    print(f'{len(taxa)} organisms -> {len(want)} distinct silhouettes; {len(need)} to download')
    ok = 0
    if need:
        with ThreadPoolExecutor(6) as ex:
            for u, good in zip(need, ex.map(lambda u: download_svg(store, u), need)):
                ok += bool(good)
                if not good:
                    print(f'  ! could not download {u}')
        print(f'downloaded {ok}/{len(need)} into {store.svg_dir}')
    for u in want:
        rec = store.manifest['images'].setdefault(u, {})
        e = store.entry(u)
        rec.setdefault('title', e.get('title'))
        rec.setdefault('attribution', e.get('attribution'))
        rec.setdefault('svg', f'{IMG}/images/{u}/vector.svg')
        if online and build is not None and not a.no_license and 'license' not in rec:
            info = image_info(u, build)
            rec['license'] = info.get('license')
            rec['attribution'] = info.get('attribution') or rec.get('attribution')
            rec['title'] = info.get('title') or rec.get('title')
            time.sleep(0.05)
    store.save_manifest()
    p = write_matches_csv(store, taxa, rows, grows)
    print(f'match table: {p}')
    if a.inject:
        for rp in a.report or []:
            cmd_inject(argparse.Namespace(report=rp, output=None, template=None, remove=False, dir=a.dir))
    return 0


def cmd_inject(a):
    payload, html, i0, i1 = read_report_payload(a.report)
    if a.remove:
        payload.pop('icons', None)
    else:
        payload['icons'] = icon_payload(payload, a.dir)
    js = (json.dumps(payload, ensure_ascii=True, allow_nan=False)
          .replace('</', '<\\/').replace('<!--', '<\\!--'))
    out = html[:i0 + len(DATA_START)] + js + html[i1:]
    if a.template:
        tpl = Path(a.template).read_text(encoding='utf-8')
        t0, t1 = tpl.find(DATA_START), tpl.find(DATA_END)
        out = tpl[:t0 + len(DATA_START)] + js + tpl[t1:]
    dest = Path(a.output or a.report)
    tmp = dest.with_name(dest.name + '.tmp')
    tmp.write_text(out, encoding='utf-8')
    tmp.replace(dest)
    print(f'wrote {dest} ({len(out) / 1e6:.1f} MB)')
    return 0


def cmd_match(a):
    store = Store(a.dir)
    for nm in a.names:
        m = store.match(nm, a.group, a.taxid)
        e = store.entry(m['uuid']) if m else {}
        print(f'{nm!r:40} -> ' + (f"{m['how']:8} {m['key']!r:28} {e.get('title', '')}  {m['uuid']}"
                                   f"{'' if store.has_svg(m['uuid']) else '  (not downloaded)'}"
                                   if m else 'no match'))
    return 0


def cmd_refresh_index(a):
    """Re-page api.phylopic.org/images into phylopic_index.json (same shape as mytax)."""
    store = Store(a.dir)
    meta = _api_json(f'{API}/images')
    if not meta:
        raise SystemExit('cannot reach api.phylopic.org')
    build, pages = meta['build'], int(meta['totalPages'])
    orgs = {}

    def page(i):
        j = _api_json(f'{API}/images?build={build}&embed_items=true&page={i}') or {}
        return (j.get('_embedded') or {}).get('items') or []

    with ThreadPoolExecutor(6) as ex:
        for items in ex.map(page, range(pages)):
            for it in items:
                links = it.get('_links') or {}
                title = (links.get('self') or {}).get('title')
                uuid = it.get('uuid')
                if not title or not uuid:
                    continue
                orgs.setdefault(title, []).append({
                    'UUID': uuid, 'svg': (links.get('vectorFile') or {}).get('href'),
                    'sourceFile': (links.get('sourceFile') or {}).get('href'),
                    'attribution': it.get('attribution'),
                    'specificNode': {'title': title},
                    'generalNode': {'title': (links.get('generalNode') or {}).get('title')}})
    out = {'_metadata': {'build': build, 'total_pages': pages, 'source': f'{API}/images',
                         'last_updated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                         'organism_key_count': len(orgs)}, 'organisms': orgs}
    store.index_path.write_text(json.dumps(out), encoding='utf-8')
    print(f'wrote {store.index_path}: {len(orgs)} names, build {build}')
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--dir', default=str(HERE), help='icons folder (default: this script\'s folder)')
    sub = p.add_subparsers(dest='cmd', required=True)
    f = sub.add_parser('fetch', help='match organisms and download any missing SVGs')
    f.add_argument('--report', action='append', help='report HTML (repeatable)')
    f.add_argument('--payload', action='append', help='payload JSON from build_mbon_html.py --json-out')
    f.add_argument('--names', nargs='*', help='extra organism names')
    f.add_argument('--bundle', help='unpack a {uuid: svg} JSON bundle into svg/ first')
    f.add_argument('--offline', action='store_true', help='no API calls (still tries downloads)')
    f.add_argument('--no-taxid', action='store_true', help='skip NCBI taxid resolution')
    f.add_argument('--no-license', action='store_true', help='skip per-image licence lookups')
    f.add_argument('--inject', action='store_true', help='then embed the icons into each --report (in place)')
    f.set_defaults(fn=cmd_fetch)
    i = sub.add_parser('inject', help='embed icons into an existing report (no rebuild)')
    i.add_argument('--report', required=True)
    i.add_argument('-o', '--output', help='write here instead of overwriting --report')
    i.add_argument('--template', help='also swap in this template (keeps the report\'s data)')
    i.add_argument('--remove', action='store_true', help='strip icons from the report')
    i.set_defaults(fn=cmd_inject)
    m = sub.add_parser('match', help='show what a name would match (offline)')
    m.add_argument('names', nargs='+')
    m.add_argument('--group')
    m.add_argument('--taxid')
    m.set_defaults(fn=cmd_match)
    r = sub.add_parser('refresh-index', help='rebuild phylopic_index.json from the PhyloPic API')
    r.set_defaults(fn=cmd_refresh_index)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == '__main__':
    sys.exit(main())
