#!/usr/bin/env python3
"""Refresh examples/icons/ — the slim PhyloPic icon set for the example report.

The full icons folder (../icons, with a 10 MB name index) lives outside this
repo. GitHub Pages builds only see committed files, so this copies just what the
synthetic example organisms need: the matching script, the index entries they
match, their downloaded SVGs, credits/licences, and overrides.

    python examples/make_example_icons.py              # from ../icons
    python examples/make_example_icons.py --src /path/to/icons

Re-run it after changing make_example_data.py, overrides.tsv or the icons.
Silhouettes matched but not yet downloaded are fetched by the Pages build
(`phylopic_icons.py fetch`, best effort), or run `fetch` in ../icons first.
"""
import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEST = REPO / 'examples' / 'icons'


def load_module(folder):
    spec = importlib.util.spec_from_file_location('phylopic_icons_src', Path(folder) / 'phylopic_icons.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def example_payload():
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / 'payload.json'
        subprocess.run([sys.executable, str(REPO / 'build_mbon_html.py'), '--example', '--no-icons',
                        '-o', str(Path(td) / 'r.html'), '--json-out', str(out)],
                       check=True, cwd=REPO, env={**os.environ, 'EDNA_LOG_FILE': '-'},
                       stdout=subprocess.DEVNULL)
        return json.loads(out.read_text(encoding='utf-8'))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--src', default=str(REPO.parent / 'icons'), help='full icons folder (default ../icons)')
    a = ap.parse_args()
    src = Path(a.src).expanduser().resolve()
    mod = load_module(src)
    payload = example_payload()

    # Record every image uuid any lookup returns while matching the example.
    store = mod.Store(src)
    used = set()
    orig = store._lookup

    def rec_lookup(key, allow_congener=False):
        u = orig(key, allow_congener)
        if u:
            used.add(u)
        return u
    store._lookup = rec_lookup
    taxa = mod.taxa_of_payload(payload)
    rows, grows = mod.match_all(store, taxa)
    for nm in taxa:
        g = mod.genus_of(nm)
        if g:
            store.match(g)
    used |= {m['uuid'] for m in list(rows.values()) + list(grows.values()) if m and m.get('uuid')}
    used |= {u for u in store.overrides.values() if u}
    used |= {store.manifest['taxids'][str(t['taxid'])] for t in taxa.values()
             if t.get('taxid') and store.manifest['taxids'].get(str(t['taxid']))}

    # Slim index: every title whose entry points at a used image (keeps lookup order).
    raw = json.loads((src / 'phylopic_index.json').read_text(encoding='utf-8'))
    orgs = {}
    for title, val in (raw.get('organisms') or {}).items():
        es = [e for e in (val if isinstance(val, list) else [val]) if e and e.get('UUID') in used]
        if es:
            orgs[title] = es if isinstance(val, list) else es[0]
    slim = {'_metadata': dict(raw.get('_metadata') or {}, note='subset for the example report; '
                              'regenerate with examples/make_example_icons.py'), 'organisms': orgs}

    (DEST / 'svg').mkdir(parents=True, exist_ok=True)
    shutil.copy2(src / 'phylopic_icons.py', DEST / 'phylopic_icons.py')
    if (src / 'overrides.tsv').exists():
        shutil.copy2(src / 'overrides.tsv', DEST / 'overrides.tsv')
    (DEST / 'phylopic_index.json').write_text(json.dumps(slim, indent=0, sort_keys=True), encoding='utf-8')
    taxids = {str(t['taxid']) for t in taxa.values() if t.get('taxid')}
    man = {'images': {u: v for u, v in store.manifest['images'].items() if u in used},
           'taxids': {k: v for k, v in store.manifest['taxids'].items() if k in taxids}}
    (DEST / 'manifest.json').write_text(json.dumps(man, indent=1, sort_keys=True), encoding='utf-8')
    for f in (DEST / 'svg').glob('*.svg'):          # drop silhouettes no longer used
        if f.stem not in used:
            try:
                f.unlink()
            except OSError:
                pass
    n_svg = 0
    for u in sorted(used):
        f = src / 'svg' / f'{u}.svg'
        if f.exists():
            shutil.copy2(f, DEST / 'svg' / f.name)
            n_svg += 1

    # Check: the slim folder gives the same icons as the full one.
    quiet = lambda *_: None
    full = mod.icon_payload(payload, src, log=quiet)
    small = load_module(DEST).icon_payload(payload, DEST, log=print)
    if full != small:
        sys.exit('! examples/icons gives different icons than ' + str(src))
    matched = {m['uuid'] for m in list(rows.values()) + list(grows.values()) if m and m.get('uuid')}
    missing = sorted(u for u in matched if not (DEST / 'svg' / f'{u}.svg').exists())
    print(f'examples/icons: {len(orgs)} index titles, {n_svg} SVGs, {len(missing)} not downloaded'
          + (' (run `python3 phylopic_icons.py fetch --report <report>` in ' + str(src) + ')' if missing else ''))


if __name__ == '__main__':
    main()
