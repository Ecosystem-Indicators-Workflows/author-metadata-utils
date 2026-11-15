"""Extract person records and discover metadata files across repository formats."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any

from .models import (
    ExtractedPerson,
    normalize_whitespace,
    split_full_name,
)

IGNORED_DIRS: set[str] = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "renv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".Rproj.user",
    "cache",
}

IGNORED_JSON_FILENAMES: set[str] = {
    "who-is-who.json",
    "eiw-xrefs.json",
    "package.json",
    "package-lock.json",
    "tsconfig.json",
    "settings.json",
    "extensions.json",
}


def _split_semicolon_affiliations(value: Any) -> list[str]:
    """Split ';'-delimited affiliation string(s) into a clean list."""
    if not value:
        return []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_split_semicolon_affiliations(item))
        return out
    text = normalize_whitespace(str(value))
    if not text or text.upper() == "TODO":
        return []
    return [part.strip() for part in text.split(";") if part.strip()]


def detect_format(path: Path, parsed_json: Any = None) -> str | None:
    """Auto-detect metadata file format ('cff', 'codemeta', 'zenodo', 'datapackage')."""
    name_lower = path.name.lower()
    if name_lower == "citation.cff" or path.suffix.lower() == ".cff":
        return "cff"
    if name_lower in IGNORED_JSON_FILENAMES:
        return None
    if "inveniordm-draft" in name_lower:
        return None
    if name_lower == "codemeta.json":
        return "codemeta"
    if name_lower == "datapackage.json":
        return "datapackage"
    if name_lower in {".zenodo.json", "zenodo.json"} or "zenodo" in name_lower:
        if path.suffix.lower() == ".json":
            return "zenodo"

    if path.suffix.lower() != ".json":
        return None

    # Inspect JSON content for generic filenames (e.g. metadata.json, authorlist.json)
    data = parsed_json
    if data is None and path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    if isinstance(data, dict):
        ctx = str(data.get("@context", "")).lower()
        if "codemeta" in ctx or data.get("@type") == "SoftwareSourceCode":
            return "codemeta"
        profile = str(data.get("profile", "")).lower()
        if "data-package" in profile or (
            "resources" in data and "contributors" in data
        ):
            return "datapackage"
        if "creators" in data or (
            isinstance(data.get("metadata"), dict)
            and ("creators" in data["metadata"] or "contributors" in data["metadata"])
        ):
            return "zenodo"
    elif isinstance(data, list) and data and isinstance(data[0], dict):
        # Standalone Zenodo creator/contributor list (e.g. authorlist.json)
        sample = data[0]
        if "name" in sample and (
            "affiliation" in sample or "orcid" in sample or "type" in sample
        ):
            return "zenodo_list"

    return None


def discover_metadata_files(
    target_path: Path | str,
    formats: set[str] | None = None,
) -> list[tuple[Path, str]]:
    """Find metadata files in a directory tree or inspect a single file."""
    root = Path(target_path).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"Target path does not exist: {root}")

    if root.is_file():
        fmt = detect_format(root)
        if fmt is None:
            raise ValueError(
                f"Could not detect metadata format for file: {root}. "
                "Specify --format (cff, codemeta, zenodo, datapackage)."
            )
        if formats and fmt not in formats:
            return []
        return [(root, fmt)]

    discovered: list[tuple[Path, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in IGNORED_DIRS and not d.endswith("_cache")
        )
        for fname in sorted(filenames):
            fpath = Path(dirpath) / fname
            lname = fname.lower()
            # Only inspect standard metadata filenames during directory scans
            is_candidate = (
                lname == "citation.cff"
                or lname == "codemeta.json"
                or lname == "datapackage.json"
                or lname in {".zenodo.json", "zenodo.json"}
                or ("zenodo" in lname and lname.endswith(".json"))
            )
            if not is_candidate:
                continue
            fmt = detect_format(fpath)
            if fmt is None:
                continue
            if formats and fmt not in formats:
                continue
            discovered.append((fpath, fmt))

    return discovered


# ---------------------------------------------------------------------------
# CFF (YAML) Parser — uses PyYAML if available, with built-in CFF fallback
# ---------------------------------------------------------------------------


def _unquote_yaml_scalar(raw: str) -> str:
    """Parse a single-line YAML scalar (quoted or unquoted), stripping comments."""
    val = raw.strip()
    if not val:
        return ""
    if len(val) >= 2 and val[0] == "'" and val[-1] == "'":
        return val[1:-1].replace("''", "'")
    if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
        return val[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    # Strip trailing inline comment (` # ...`) when unquoted
    comment_match = re.search(r"\s+#", val)
    if comment_match:
        val = val[: comment_match.start()].rstrip()
    return val


def find_cff_authors_span(lines: list[str]) -> tuple[int, int] | None:
    """Locate the line span [start, end) of top-level `authors:` in a CFF file."""
    start_idx: int | None = None
    for idx, line in enumerate(lines):
        if re.match(r"^authors\s*:(?:\s*#.*)?\s*$", line):
            start_idx = idx
            break
        # Handle inline empty list `authors: []`
        if re.match(r"^authors\s*:\s*\[\s*\](?:\s*#.*)?\s*$", line):
            return (idx, idx + 1)
    if start_idx is None:
        return None

    end_idx = len(lines)
    for idx in range(start_idx + 1, len(lines)):
        line = lines[idx]
        stripped = line.strip()
        if not stripped:
            continue
        # A top-level comment or top-level key at column 0 ends `authors:`
        if not line[0].isspace():
            end_idx = idx
            break
    # Keep trailing blank lines outside the replaced span unless inside block
    while end_idx > start_idx + 1 and not lines[end_idx - 1].strip():
        end_idx -= 1
    return (start_idx, end_idx)


def _parse_cff_authors_stdlib(text: str) -> list[dict[str, Any]]:
    """Zero-dependency parser for top-level `authors:` sequence in CITATION.cff."""
    lines = text.splitlines()
    span = find_cff_authors_span(lines)
    if span is None:
        return []
    start_idx, end_idx = span
    header_line = lines[start_idx]
    if re.match(r"^authors\s*:\s*\[\s*\]", header_line):
        return []

    items: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    idx = start_idx + 1

    while idx < end_idx:
        line = lines[idx]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            idx += 1
            continue

        item_match = re.match(r"^(\s*)-\s*(.*)$", line)
        if item_match:
            if current is not None:
                items.append(current)
            current = {}
            rest = item_match.group(2).strip()
            if rest and not rest.startswith("#") and ":" in rest:
                key, raw_val = rest.split(":", 1)
                idx, val = _read_yaml_value(
                    lines, idx, end_idx, len(item_match.group(1)) + 2, raw_val
                )
                current[key.strip()] = val
                continue
            idx += 1
            continue

        kv_match = re.match(r"^(\s+)([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
        if kv_match and current is not None:
            indent_len = len(kv_match.group(1))
            key = kv_match.group(2).strip()
            raw_val = kv_match.group(3)
            idx, val = _read_yaml_value(lines, idx, end_idx, indent_len, raw_val)
            current[key] = val
            continue

        idx += 1

    if current is not None:
        items.append(current)
    return items


def _read_yaml_value(
    lines: list[str],
    idx: int,
    end_idx: int,
    key_indent: int,
    raw_val: str,
) -> tuple[int, str]:
    """Read an inline or block/folded (`>-`, `>`, `|`, `|-`) YAML scalar."""
    trimmed = raw_val.strip()
    if trimmed in {">", ">-", "|", "|-"}:
        folded = trimmed.startswith(">")
        collected: list[str] = []
        next_idx = idx + 1
        while next_idx < end_idx:
            sub = lines[next_idx]
            if not sub.strip():
                collected.append("")
                next_idx += 1
                continue
            sub_indent = len(sub) - len(sub.lstrip(" "))
            if sub_indent <= key_indent:
                break
            collected.append(sub.strip())
            next_idx += 1
        joiner = " " if folded else "\n"
        return next_idx, joiner.join(collected).strip()
    return idx + 1, _unquote_yaml_scalar(raw_val)


def parse_cff_authors(text: str) -> list[dict[str, Any]]:
    """Parse `authors:` list from CITATION.cff using PyYAML if installed, else stdlib."""
    try:
        import yaml  # type: ignore[import-not-found]

        loaded = yaml.safe_load(text)
        if isinstance(loaded, dict):
            authors = loaded.get("authors") or []
            if isinstance(authors, list):
                return [a for a in authors if isinstance(a, dict)]
        return []
    except ImportError:
        return _parse_cff_authors_stdlib(text)


# ---------------------------------------------------------------------------
# Format-Specific Extractors
# ---------------------------------------------------------------------------


def extract_from_cff(path: Path, text: str | None = None) -> list[ExtractedPerson]:
    """Extract authors from a CITATION.cff file."""
    content = text if text is not None else path.read_text(encoding="utf-8")
    raw_authors = parse_cff_authors(content)
    results: list[ExtractedPerson] = []
    for item in raw_authors:
        given = normalize_whitespace(str(item.get("given-names", "")))
        family = normalize_whitespace(str(item.get("family-names", "")))
        if not family and "name" in item:
            # Entity or full-name fallback
            given, family = split_full_name(str(item.get("name", "")))
        email = normalize_whitespace(str(item.get("email", ""))) or None
        if email and email.upper() == "TODO":
            email = None
        orcid_raw = normalize_whitespace(str(item.get("orcid", ""))) or None
        affs = _split_semicolon_affiliations(item.get("affiliation"))
        results.append(
            ExtractedPerson(
                source_file=path,
                source_format="cff",
                block_name="authors",
                given_name=given,
                family_name=family,
                orcid_raw=orcid_raw,
                email=email,
                affiliations=affs,
                role="author",
                raw=dict(item),
            )
        )
    return results


def _extract_codemeta_affiliations(aff_field: Any) -> list[str]:
    if not aff_field:
        return []
    items = aff_field if isinstance(aff_field, list) else [aff_field]
    names: list[str] = []
    for entry in items:
        if isinstance(entry, dict):
            nm = normalize_whitespace(str(entry.get("name", "")))
            if nm and nm.upper() != "TODO":
                names.append(nm)
        elif isinstance(entry, str):
            names.extend(_split_semicolon_affiliations(entry))
    return names


def extract_from_codemeta(
    path: Path, data: dict[str, Any] | None = None
) -> list[ExtractedPerson]:
    """Extract `author` and `contributor` records from codemeta.json."""
    doc = data if data is not None else json.loads(path.read_text(encoding="utf-8"))
    results: list[ExtractedPerson] = []
    for block_name, default_role in (("author", "author"), ("contributor", "contributor")):
        raw_block = doc.get(block_name)
        if not raw_block:
            continue
        entries = raw_block if isinstance(raw_block, list) else [raw_block]
        for item in entries:
            if not isinstance(item, dict):
                continue
            given = normalize_whitespace(str(item.get("givenName", "")))
            family = normalize_whitespace(str(item.get("familyName", "")))
            if given.upper() == "TODO" and family.upper() == "TODO":
                continue
            if not family and "name" in item:
                given, family = split_full_name(str(item.get("name", "")))
            email = normalize_whitespace(str(item.get("email", ""))) or None
            if email and email.upper() == "TODO":
                email = None
            orcid_raw = normalize_whitespace(str(item.get("@id", ""))) or None
            affs = _extract_codemeta_affiliations(item.get("affiliation"))
            results.append(
                ExtractedPerson(
                    source_file=path,
                    source_format="codemeta",
                    block_name=block_name,
                    given_name=given,
                    family_name=family,
                    orcid_raw=orcid_raw,
                    email=email,
                    affiliations=affs,
                    role=default_role,
                    raw=dict(item),
                )
            )
    return results


def _extract_zenodo_entry(
    item: dict[str, Any], path: Path, fmt: str, block_name: str
) -> ExtractedPerson:
    raw_name = str(item.get("name", ""))
    given, family = split_full_name(raw_name)
    orcid_raw = normalize_whitespace(str(item.get("orcid", ""))) or None
    affs = _split_semicolon_affiliations(item.get("affiliation"))
    role = normalize_whitespace(str(item.get("type", ""))) or (
        "creator" if block_name == "creators" else "contributor"
    )
    return ExtractedPerson(
        source_file=path,
        source_format=fmt,
        block_name=block_name,
        given_name=given,
        family_name=family,
        orcid_raw=orcid_raw,
        email=None,
        affiliations=affs,
        role=role,
        raw=dict(item),
    )


def extract_from_zenodo(path: Path, data: Any = None) -> list[ExtractedPerson]:
    """Extract `creators` and `contributors` from .zenodo.json / zenodo.json."""
    doc = data if data is not None else json.loads(path.read_text(encoding="utf-8"))
    results: list[ExtractedPerson] = []

    if isinstance(doc, list):
        for item in doc:
            if isinstance(item, dict):
                bname = "contributors" if "type" in item else "creators"
                results.append(_extract_zenodo_entry(item, path, "zenodo", bname))
        return results

    if not isinstance(doc, dict):
        return results

    target = (
        doc["metadata"]
        if isinstance(doc.get("metadata"), dict)
        and "creators" not in doc
        and "contributors" not in doc
        else doc
    )
    for block_name in ("creators", "contributors"):
        entries = target.get(block_name) or []
        if not isinstance(entries, list):
            continue
        for item in entries:
            if isinstance(item, dict):
                results.append(_extract_zenodo_entry(item, path, "zenodo", block_name))
    return results


def extract_from_datapackage(
    path: Path, data: dict[str, Any] | None = None
) -> list[ExtractedPerson]:
    """Extract `contributors` from datapackage.json (Frictionless & legacy schemas)."""
    doc = data if data is not None else json.loads(path.read_text(encoding="utf-8"))
    entries = doc.get("contributors") or []
    if not isinstance(entries, list):
        return []

    results: list[ExtractedPerson] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        notes: list[str] = []
        if "title" in item:
            given, family = split_full_name(str(item.get("title", "")))
        elif "givenName" in item or "familyName" in item:
            given = normalize_whitespace(str(item.get("givenName", "")))
            family = normalize_whitespace(str(item.get("familyName", "")))
        elif "name" in item:
            given, family = split_full_name(str(item.get("name", "")))
            notes.append(
                "Uses non-standard key 'name' instead of Frictionless 'title'."
            )
        else:
            given, family = "", ""

        orcid_raw = normalize_whitespace(str(item.get("path", ""))) or None
        if not orcid_raw and "orcid" in item:
            orcid_raw = normalize_whitespace(str(item.get("orcid", ""))) or None
            notes.append(
                "Uses non-standard key 'orcid' instead of Frictionless 'path'."
            )

        aff_val = item.get("organization")
        if aff_val is None and "affiliation" in item:
            aff_val = item.get("affiliation")
            notes.append(
                "Uses non-standard key 'affiliation' instead of Frictionless 'organization'."
            )
        affs = _split_semicolon_affiliations(aff_val)

        email = normalize_whitespace(str(item.get("email", ""))) or None
        role_val = item.get("role")
        if not role_val and isinstance(item.get("roles"), list) and item["roles"]:
            role_val = item["roles"][0]
        role = normalize_whitespace(str(role_val or "contributor"))

        results.append(
            ExtractedPerson(
                source_file=path,
                source_format="datapackage",
                block_name="contributors",
                given_name=given,
                family_name=family,
                orcid_raw=orcid_raw,
                email=email,
                affiliations=affs,
                role=role,
                schema_notes=notes,
                raw=dict(item),
            )
        )
    return results


def extract_from_file(
    path: Path | str, format_type: str | None = None
) -> tuple[str, list[ExtractedPerson]]:
    """Detect format (unless specified) and extract all person entries from `path`."""
    fpath = Path(path).expanduser().resolve()
    if not fpath.is_file():
        raise FileNotFoundError(f"Metadata file not found: {fpath}")

    fmt = format_type.lower() if format_type else detect_format(fpath)
    if fmt is None:
        raise ValueError(
            f"Unable to auto-detect metadata format for {fpath}. "
            "Pass format_type='cff'|'codemeta'|'zenodo'|'datapackage'."
        )

    if fmt == "cff":
        return "cff", extract_from_cff(fpath)
    if fmt == "codemeta":
        return "codemeta", extract_from_codemeta(fpath)
    if fmt in {"zenodo", "zenodo_list"}:
        return fmt, extract_from_zenodo(fpath)
    if fmt == "datapackage":
        return "datapackage", extract_from_datapackage(fpath)

    raise ValueError(f"Unsupported metadata format: {fmt}")
