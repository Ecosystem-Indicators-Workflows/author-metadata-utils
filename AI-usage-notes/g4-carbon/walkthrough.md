# Walkthrough: Centralised Author & Contributor Management (`who-is-who`)

## Changes Made

1. **Centralised Registry ([who-is-who.json](file:///Users/z3529065/proyectos/EIW/PID-implementation-scripts/who-is-who.json))**
   - Added concise short `code` slugs (`jrfp`, `mtozer`, `dkeith`, `eaddicott`, `egould`, `asingh`) to existing `Person` records while supporting optional `email` and missing `@id` (for authors without an ORCID).

2. **Reusable Python Package (`python/who_is_who/`)**
   - [models.py](file:///Users/z3529065/proyectos/EIW/PID-implementation-scripts/python/who_is_who/models.py): Data structures (`Person`, `Organization`, `ExtractedPerson`, `Inconsistency`, `PersonVerification`, `FileVerification`, `SelectSpec`), ORCID bare/URI normalization, Unicode NFKD accent folding, initial compatibility checks, and Zenodo controlled contributor type validation.
   - [registry.py](file:///Users/z3529065/proyectos/EIW/PID-implementation-scripts/python/who_is_who/registry.py): Loading/saving `who-is-who.json`, person lookup by `code`, ORCID, 1-based index, surname, or full name, and helpers to add new `Person` and `Organization` entries.
   - [extractors.py](file:///Users/z3529065/proyectos/EIW/PID-implementation-scripts/python/who_is_who/extractors.py): Recursive repository file discovery (`discover_metadata_files`) and format-specific extractors for:
     - `CITATION.cff` (`authors` YAML block; uses `PyYAML` if installed with a zero-dependency built-in CFF parser fallback)
     - `codemeta.json` (`author` and `contributor` blocks, supporting single-object and array `affiliation`)
     - `.zenodo.json` / `zenodo.json` (`creators` and `contributors`, both root-level and `{"metadata": ...}` templates, plus standalone creator/contributor list JSON files)
     - `datapackage.json` (`contributors`, parsing both Frictionless `title`/`path`/`organization`/`role` and legacy `name`/`orcid`/`affiliation`/`role`)
   - [verifier.py](file:///Users/z3529065/proyectos/EIW/PID-implementation-scripts/python/who_is_who/verifier.py): Cross-checks extracted records against `who-is-who.json`, flags `ORCID_CONFLICT` (including copy-pasted ORCIDs belonging to another person), `MISSING_IN_FILE`, `MISSING_IN_REGISTRY`, `NAME_MISMATCH`, `EMAIL_MISMATCH`, `AFFILIATION_MISMATCH`, and `SCHEMA_WARNING`, and provides `reconcile_interactive` to add missing people/organizations or enrich missing registry fields.
   - [formatters.py](file:///Users/z3529065/proyectos/EIW/PID-implementation-scripts/python/who_is_who/formatters.py): Generates canonical blocks for all four target formats (`format_block`) and updates target files in-place (`update_file`) while preserving surrounding fields, JSON indentation/key ordering, and `CITATION.cff` comments and `>-` folded scalars.
   - [cli.py](file:///Users/z3529065/proyectos/EIW/PID-implementation-scripts/python/who_is_who/cli.py) & [manage-authors.py](file:///Users/z3529065/proyectos/EIW/PID-implementation-scripts/python/manage-authors.py): Executable CLI supporting `list`, `check`, `format`, `update`, and `add-person` via either command-line flags or `--interactive` mode.

3. **Documentation & Tests**
   - [test_who_is_who.py](file:///Users/z3529065/proyectos/EIW/PID-implementation-scripts/python/tests/test_who_is_who.py): 8 unit tests covering all 4 formats, ORCID conflict detection, interactive reconciliation, partial block updates, and comment-preserving `CITATION.cff` edits.
   - [README.md](file:///Users/z3529065/proyectos/EIW/PID-implementation-scripts/README.md) & [.gitignore](file:///Users/z3529065/proyectos/EIW/PID-implementation-scripts/.gitignore): Added CLI and Python API usage examples and ignored Python bytecode caches.

---

## Validation Results

- **Unit Tests**: Ran `python3 -m unittest discover -s python/tests -v` — all **8 tests passed** (`0.030s`).
- **Repository Check Verification**: Ran `python3 python/manage-authors.py check --repo .` and `--file metadata-templates/authorlist.json`:
  - Accurately matched registered authors (`@jrfp`, `@mtozer`, `@dkeith`, `@egould`, `@asingh`).
  - Flagged missing ORCIDs in `zenodo-metadata-24207312.json` and `zenodo-metadata-24034201.json`, name initial differences (`David A.` vs `David`, `Jose R.` vs `J. R.`), and affiliation differences (`QCIF Ltd.` vs `QCIF`).
  - Identified unlisted co-authors (`Jayne Balmer`, `Anne Kitchener`, `Adelina Latinovic`, `Emily Nicholson`, `Richard T. Kingsford`) as `[NOT IN WHO-IS-WHO]`.
- **Format Verification**: Verified `python3 python/manage-authors.py format --format <cff|codemeta|zenodo|datapackage>` produces valid YAML and JSON blocks with proper ROR `@id` inclusion in CodeMeta, bare ORCIDs + contributor `type` in Zenodo, and Frictionless Data Package v1 keys in `datapackage.json`.
