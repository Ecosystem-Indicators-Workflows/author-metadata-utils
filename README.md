---
author: JR Ferrer-Paris
date: 28 Oct 2026
---

# Centralised Author & Contributor Metadata (`who-is-who.json`)

[who-is-who.json](who-is-who.json) stores canonical `Person` and `Organization` records used across repositories to keep author/contributor names, affiliations, and persistent identifiers (ORCID, ROR) consistent across four metadata formats:

- **`CITATION.cff`** (`authors` YAML block)
- **`codemeta.json`** (`author` and `contributor` blocks)
- **`.zenodo.json` / `zenodo.json`** (`creators` and `contributors` blocks, both top-level and `{"metadata": ...}` templates)
- **`datapackage.json`** (`contributors` block using Frictionless Data Package v1 fields: `title`, `path`, `email`, `organization`, `role`)

### Command-Line Usage (`python/manage-authors.py`)

Runs with Python 3 standard library out of the box (uses `PyYAML` automatically if installed):

```bash
# 1. List registered people and organizations
python3 python/manage-authors.py list

# 2. Check all metadata files in a target repository against who-is-who.json
python3 python/manage-authors.py check --repo /path/to/target-repo

# 3. Check and interactively add missing people/organizations to who-is-who.json
python3 python/manage-authors.py check --repo /path/to/target-repo --interactive

# 4. Print a standalone formatted block to stdout (cff | codemeta | zenodo | datapackage)
python3 python/manage-authors.py format \
  --format cff \
  --authors jrfp,mtozer,dkeith

# 5. Preview (dry-run) or update files in-place across a repository via CLI arguments
python3 python/manage-authors.py update \
  --repo /path/to/target-repo \
  --authors jrfp,mtozer,dkeith \
  --contributors egould:ProjectMember,asingh:ProjectMember \
  --in-place

# 6. Guided interactive selection & diff preview
python3 python/manage-authors.py update --repo /path/to/target-repo --interactive
```

### Python API Usage (`who_is_who`)

```python
import sys
sys.path.insert(0, "/Users/z3529065/proyectos/EIW/PID-implementation-scripts/python")

from who_is_who import (
    load_registry,
    verify_repo,
    format_verification_report,
    format_block,
    update_file,
)

reg = load_registry()

# Verify all metadata files in another repository
results = verify_repo("/path/to/target-repo", reg)
print(format_verification_report(results))

# Format a block as a string
zenodo_block = format_block(
    "zenodo",
    reg,
    authors=["jrfp", "mtozer", "dkeith"],
    contributors=["egould:ProjectMember", "asingh:DataCurator"],
)

# Update a specific file in-place
update_file(
    "/path/to/target-repo/CITATION.cff",
    reg,
    authors=["jrfp", "mtozer", "dkeith"],
)
```

### Running Unit Tests

```bash
python3 -m unittest discover -s python/tests -v
```
