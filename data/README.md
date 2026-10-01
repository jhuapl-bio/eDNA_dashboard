# data/

Everything in here is git-ignored **except** this file and `example/`.

```
data/
├── example/            synthetic demo data (committed; regenerate with make_example_data.py)
│   ├── dashboard_example.xlsx
│   └── projects/
│       ├── report_manifest.xlsx
│       └── ExampleCoast/{faire,kraken2,wilder,meta}/
└── projects/           your real data — same layout as example/projects/
    ├── report_manifest.xlsx
    └── <project_dir>/{faire,kraken2,wilder,meta}/
```

Formats and manifest columns: [../docs/DATA_FORMATS.md](../docs/DATA_FORMATS.md).
The fastest way to start is to copy the example layout:

```bash
cp -r data/example/projects data/projects     # then replace the files with your own
python build_mbon_html.py --validate
```
