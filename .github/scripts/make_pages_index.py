"""Write index.html for the GitHub Pages site, linking every .html file in it.

Usage: python .github/scripts/make_pages_index.py _site
If the folder already has an index.html (e.g. committed under pages/), it is kept.
"""
import html
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

site = Path(sys.argv[1] if len(sys.argv) > 1 else "_site")
index = site / "index.html"
if index.exists():
    print(f"{index} exists; leaving it alone")
    sys.exit(0)

LABELS = {"example_report.html": "Example report (synthetic ExampleCoast data)"}
pages = sorted(p for p in site.rglob("*.html") if p.name != "index.html")
items = []
for p in pages:
    rel = p.relative_to(site).as_posix()
    label = LABELS.get(rel, p.stem.replace("_", " ").replace("-", " "))
    size = p.stat().st_size / 1e6
    items.append(f'<li><a href="{html.escape(rel)}">{html.escape(label)}</a>'
                 f' <span>{html.escape(rel)} · {size:.1f} MB</span></li>')

try:
    sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
except Exception:
    sha = os.getenv("GITHUB_SHA", "")[:7]
built = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
repo = os.getenv("GITHUB_REPOSITORY", "jhuapl-bio/eDNA_processing")

index.write_text(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>eDNA Reports</title>
<style>
:root {{ --bg:#f7f9fb; --fg:#1b2733; --mute:#5f6f7f; --link:#0b6aa2; --card:#fff; --line:#dbe3ea; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#0f1720; --fg:#e6edf3; --mute:#94a3b3; --link:#6cc4ff; --card:#16212c; --line:#26343f; }}
}}
body {{ margin:0; background:var(--bg); color:var(--fg);
       font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:760px; margin:0 auto; padding:40px 16px; }}
h1 {{ margin:0 0 4px; font-size:1.6rem; }}
p {{ color:var(--mute); margin:0 0 24px; }}
ul {{ list-style:none; padding:0; margin:0; }}
li {{ background:var(--card); border:1px solid var(--line); border-radius:8px;
     padding:14px 16px; margin-bottom:10px; }}
a {{ color:var(--link); font-weight:600; text-decoration:none; }}
a:hover {{ text-decoration:underline; }}
li span {{ display:block; color:var(--mute); font-size:.85rem; }}
footer {{ margin-top:28px; color:var(--mute); font-size:.85rem; }}
</style></head>
<body><main>
<h1>Marine eDNA reports</h1>
<p>Interactive map-based reports of eDNA taxonomic hits, built by
<a href="https://github.com/{html.escape(repo)}">{html.escape(repo)}</a>.</p>
<ul>
{chr(10).join(items) or '<li>No reports built.</li>'}
</ul>
<footer>Built {built} from commit {html.escape(sha)}.</footer>
</main></body></html>
""", encoding="utf-8")
print(f"wrote {index} with {len(pages)} link(s)")
