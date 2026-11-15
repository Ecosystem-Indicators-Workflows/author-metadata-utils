"""Format author/contributor blocks and surgically update target metadata files."""

from __future__ import annotations

import difflib
import json
from pathlib import Path
import re
from typing import Any, Sequence

from .extractors import detect_format, find_cff_authors_span
from .models import (
    DATAPACKAGE_ROLES,
    Person,
    SelectSpec,
    normalize_zenodo_type,
)
from .registry import Registry

YAML_SPECIAL_RE = re.compile(
    r"(?:^[-?:,\[\]{}#&*!|>%'\"@`\s])|(?:\s$)|(?::\s)|(?:\s#)"
)


def _format_yaml_scalar(value: str, force_single_quote: bool = False) -> str:
    """Format a single-line YAML scalar cleanly (quoting only when required)."""
    if force_single_quote or YAML_SPECIAL_RE.search(value) or value == "":
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    return value


def resolve_selection(
    specs: Sequence[str | SelectSpec | Person],
    registry: Registry,
) -> list[tuple[Person, str | None]]:
    """Resolve a sequence of person selectors/specs into (Person, role_override) pairs."""
    resolved: list[tuple[Person, str | None]] = []
    for item in specs:
        if isinstance(item, Person):
            resolved.append((item, None))
        elif isinstance(item, SelectSpec):
            person = registry.resolve_person(item.selector)
            resolved.append((person, item.role))
        elif isinstance(item, str):
            parsed = SelectSpec.parse(item)
            person = registry.resolve_person(parsed.selector)
            resolved.append((person, parsed.role))
        else:
            raise TypeError(f"Unsupported selector type: {type(item)}")
    return resolved


# ---------------------------------------------------------------------------
# Block Builders (Python data structures & YAML/JSON strings)
# ---------------------------------------------------------------------------


def build_cff_authors_list(
    authors: Sequence[Person], registry: Registry
) -> list[dict[str, str]]:
    """Build CFF 1.2.0 `authors` list of dicts from Person entries."""
    out: list[dict[str, str]] = []
    for person in authors:
        entry: dict[str, str] = {}
        if person.given_name:
            entry["given-names"] = person.given_name
        entry["family-names"] = person.family_name
        if person.email:
            entry["email"] = person.email
        aff_names = registry.get_person_affiliation_names(person)
        if aff_names:
            entry["affiliation"] = "; ".join(aff_names)
        if person.orcid_uri:
            entry["orcid"] = person.orcid_uri
        out.append(entry)
    return out


def render_cff_authors_yaml(
    authors: Sequence[Person], registry: Registry
) -> str:
    """Render top-level `authors:` YAML block matching cffinit conventions."""
    items = build_cff_authors_list(authors, registry)
    if not items:
        return "authors: []"
    lines: list[str] = ["authors:"]
    for item in items:
        first = True
        for key, val in item.items():
            force_quote = key == "orcid"
            formatted_val = _format_yaml_scalar(val, force_single_quote=force_quote)
            prefix = "  - " if first else "    "
            lines.append(f"{prefix}{key}: {formatted_val}")
            first = False
    return "\n".join(lines)


def build_codemeta_people_list(
    people: Sequence[Person], registry: Registry
) -> list[dict[str, Any]]:
    """Build CodeMeta v3.0 `author` or `contributor` list of Person objects."""
    out: list[dict[str, Any]] = []
    for person in people:
        entry: dict[str, Any] = {"@type": "Person"}
        if person.given_name:
            entry["givenName"] = person.given_name
        entry["familyName"] = person.family_name
        if person.orcid_uri:
            entry["@id"] = person.orcid_uri
        if person.email:
            entry["email"] = person.email
        orgs = registry.get_person_organizations(person)
        if orgs:
            aff_list: list[dict[str, str]] = []
            for org in orgs:
                org_dict: dict[str, str] = {
                    "@type": "Organization",
                    "name": org.name,
                }
                if org.id_uri:
                    org_dict["@id"] = org.id_uri
                aff_list.append(org_dict)
            entry["affiliation"] = aff_list
        out.append(entry)
    return out


def build_zenodo_creators_list(
    creators: Sequence[Person], registry: Registry
) -> list[dict[str, str]]:
    """Build Zenodo `creators` list ('name', 'affiliation', bare 'orcid')."""
    out: list[dict[str, str]] = []
    for person in creators:
        entry: dict[str, str] = {"name": person.zenodo_name}
        aff_names = registry.get_person_affiliation_names(person)
        if aff_names:
            entry["affiliation"] = "; ".join(aff_names)
        if person.orcid_bare:
            entry["orcid"] = person.orcid_bare
        out.append(entry)
    return out


def build_zenodo_contributors_list(
    contributors: Sequence[tuple[Person, str | None]],
    registry: Registry,
    default_type: str = "ProjectMember",
) -> list[dict[str, str]]:
    """Build Zenodo `contributors` list ('name', 'affiliation', 'type', bare 'orcid')."""
    out: list[dict[str, str]] = []
    for person, role_override in contributors:
        z_type = normalize_zenodo_type(role_override, default=default_type)
        entry: dict[str, str] = {"name": person.zenodo_name}
        aff_names = registry.get_person_affiliation_names(person)
        if aff_names:
            entry["affiliation"] = "; ".join(aff_names)
        entry["type"] = z_type
        if person.orcid_bare:
            entry["orcid"] = person.orcid_bare
        out.append(entry)
    return out


def build_datapackage_contributors_list(
    authors: Sequence[tuple[Person, str | None]],
    contributors: Sequence[tuple[Person, str | None]],
    registry: Registry,
) -> list[dict[str, str]]:
    """Build Frictionless Data Package v1 `contributors` array."""
    out: list[dict[str, str]] = []

    def _make_item(person: Person, role: str) -> dict[str, str]:
        role_norm = role.lower().strip()
        if role_norm not in DATAPACKAGE_ROLES:
            role_norm = "contributor"
        entry: dict[str, str] = {"title": person.display_name}
        if person.orcid_uri:
            entry["path"] = person.orcid_uri
        if person.email:
            entry["email"] = person.email
        aff_names = registry.get_person_affiliation_names(person)
        if aff_names:
            entry["organization"] = "; ".join(aff_names)
        entry["role"] = role_norm
        return entry

    for person, role_override in authors:
        out.append(_make_item(person, role_override or "author"))
    for person, role_override in contributors:
        out.append(_make_item(person, role_override or "contributor"))
    return out


def format_block(
    format_type: str,
    registry: Registry,
    authors: Sequence[str | SelectSpec | Person] | None = None,
    contributors: Sequence[str | SelectSpec | Person] | None = None,
    default_contributor_type: str = "ProjectMember",
    indent: int = 2,
) -> str:
    """Format selected authors/contributors into a standalone YAML or JSON block."""
    fmt = format_type.lower().strip()
    resolved_authors = resolve_selection(authors or [], registry)
    resolved_contribs = resolve_selection(contributors or [], registry)
    author_persons = [p for p, _ in resolved_authors]

    if fmt == "cff":
        return render_cff_authors_yaml(author_persons, registry) + "\n"

    if fmt == "codemeta":
        payload: dict[str, Any] = {}
        if authors is not None:
            payload["author"] = build_codemeta_people_list(author_persons, registry)
        if contributors is not None:
            contrib_persons = [p for p, _ in resolved_contribs]
            payload["contributor"] = build_codemeta_people_list(
                contrib_persons, registry
            )
        return json.dumps(payload, indent=indent, ensure_ascii=False) + "\n"

    if fmt == "zenodo":
        payload = {}
        if authors is not None:
            payload["creators"] = build_zenodo_creators_list(author_persons, registry)
        if contributors is not None:
            payload["contributors"] = build_zenodo_contributors_list(
                resolved_contribs,
                registry,
                default_type=default_contributor_type,
            )
        return json.dumps(payload, indent=indent, ensure_ascii=False) + "\n"

    if fmt == "datapackage":
        payload = {
            "contributors": build_datapackage_contributors_list(
                resolved_authors, resolved_contribs, registry
            )
        }
        return json.dumps(payload, indent=indent, ensure_ascii=False) + "\n"

    raise ValueError(
        f"Unsupported format '{format_type}'. Expected one of: cff, codemeta, zenodo, datapackage."
    )


# ---------------------------------------------------------------------------
# Surgical In-Place File Updaters
# ---------------------------------------------------------------------------


def _detect_json_indent(raw_text: str, default: int = 2) -> int:
    """Detect existing indentation width (e.g. 2 or 4 spaces) in a JSON file."""
    for line in raw_text.splitlines()[1:30]:
        match = re.match(r"^( +)\"", line)
        if match:
            spaces = len(match.group(1))
            if spaces in {2, 4}:
                return spaces
    return default


def _insert_key_after(
    mapping: dict[str, Any], key: str, value: Any, after_keys: Sequence[str]
) -> dict[str, Any]:
    """Insert or update `key` in `mapping`, positioning it right after `after_keys` if new."""
    if key in mapping:
        mapping[key] = value
        return mapping
    for anchor in after_keys:
        if anchor in mapping:
            rebuilt: dict[str, Any] = {}
            for k, v in mapping.items():
                rebuilt[k] = v
                if k == anchor:
                    rebuilt[key] = value
            return rebuilt
    mapping[key] = value
    return mapping


def update_cff_content(
    original_text: str,
    authors: Sequence[Person],
    registry: Registry,
) -> str:
    """Surgically replace (or insert) top-level `authors:` in CITATION.cff text."""
    line_sep = "\r\n" if "\r\n" in original_text else "\n"
    trailing_newline = original_text.endswith(("\n", "\r\n"))
    lines = original_text.splitlines()
    new_block_lines = render_cff_authors_yaml(authors, registry).splitlines()

    span = find_cff_authors_span(lines)
    if span is not None:
        start_idx, end_idx = span
        updated_lines = lines[:start_idx] + new_block_lines + lines[end_idx:]
    else:
        # Insert after `type:`, `message:`, `title:`, or `cff-version:`
        insert_at = len(lines)
        for anchor in ("type", "message", "title", "cff-version"):
            for idx, line in enumerate(lines):
                if re.match(rf"^{anchor}\s*:", line):
                    # Advance past any continuation lines of a folded block
                    j = idx + 1
                    while j < len(lines) and (
                        not lines[j].strip() or lines[j][0].isspace()
                    ):
                        j += 1
                    insert_at = j
                    break
            if insert_at != len(lines):
                break
        updated_lines = lines[:insert_at] + new_block_lines + lines[insert_at:]

    result = line_sep.join(updated_lines)
    if trailing_newline or original_text == "":
        result += line_sep
    return result


def update_file(
    path: Path | str,
    registry: Registry,
    authors: Sequence[str | SelectSpec | Person] | None = None,
    contributors: Sequence[str | SelectSpec | Person] | None = None,
    clear_contributors: bool = False,
    default_contributor_type: str = "ProjectMember",
    format_type: str | None = None,
    dry_run: bool = False,
) -> tuple[str, bool, str]:
    """Update author/contributor blocks in a target file while preserving all other fields.

    Returns:
        (updated_text, changed, unified_diff_str)
    """
    fpath = Path(path).expanduser().resolve()
    if not fpath.is_file():
        raise FileNotFoundError(f"Target file not found: {fpath}")

    original_text = fpath.read_text(encoding="utf-8")
    fmt = format_type.lower() if format_type else detect_format(fpath)
    if fmt is None:
        raise ValueError(
            f"Could not detect metadata format for {fpath}. Specify format_type."
        )

    resolved_authors = (
        resolve_selection(authors, registry) if authors is not None else None
    )
    resolved_contribs = (
        []
        if clear_contributors
        else (
            resolve_selection(contributors, registry)
            if contributors is not None
            else None
        )
    )

    if fmt == "cff":
        if resolved_authors is None:
            # CITATION.cff only has `authors:` — leave unchanged if only contributors passed
            updated_text = original_text
        else:
            author_persons = [p for p, _ in resolved_authors]
            updated_text = update_cff_content(
                original_text, author_persons, registry
            )
    elif fmt == "codemeta":
        indent = _detect_json_indent(original_text, default=4)
        doc = json.loads(original_text)
        if resolved_authors is not None:
            author_persons = [p for p, _ in resolved_authors]
            doc = _insert_key_after(
                doc,
                "author",
                build_codemeta_people_list(author_persons, registry),
                after_keys=("keywords", "description", "name"),
            )
        if resolved_contribs is not None:
            contrib_persons = [p for p, _ in resolved_contribs]
            doc = _insert_key_after(
                doc,
                "contributor",
                build_codemeta_people_list(contrib_persons, registry),
                after_keys=("author", "keywords"),
            )
        updated_text = json.dumps(doc, indent=indent, ensure_ascii=False) + "\n"
    elif fmt == "zenodo":
        indent = _detect_json_indent(original_text, default=2)
        doc = json.loads(original_text)
        has_wrapper = (
            isinstance(doc, dict)
            and isinstance(doc.get("metadata"), dict)
            and "creators" not in doc
            and "contributors" not in doc
        )
        target_dict = doc["metadata"] if has_wrapper else doc
        if resolved_authors is not None:
            author_persons = [p for p, _ in resolved_authors]
            target_dict = _insert_key_after(
                target_dict,
                "creators",
                build_zenodo_creators_list(author_persons, registry),
                after_keys=("embargo_date", "access_right", "description", "title"),
            )
        if resolved_contribs is not None:
            contrib_list = build_zenodo_contributors_list(
                resolved_contribs,
                registry,
                default_type=default_contributor_type,
            )
            if contrib_list or "contributors" in target_dict:
                target_dict = _insert_key_after(
                    target_dict,
                    "contributors",
                    contrib_list,
                    after_keys=("creators", "description", "title"),
                )
        if has_wrapper:
            doc["metadata"] = target_dict
        else:
            doc = target_dict
        updated_text = json.dumps(doc, indent=indent, ensure_ascii=False) + "\n"
    elif fmt == "datapackage":
        indent = _detect_json_indent(original_text, default=2)
        doc = json.loads(original_text)
        existing_contribs = doc.get("contributors") or []
        if not isinstance(existing_contribs, list):
            existing_contribs = []

        if resolved_authors is not None and resolved_contribs is not None:
            new_contribs = build_datapackage_contributors_list(
                resolved_authors, resolved_contribs, registry
            )
        elif resolved_authors is not None and resolved_contribs is None:
            # Replace author entries while keeping existing non-author contributors
            preserved_non_authors = [
                item
                for item in existing_contribs
                if isinstance(item, dict)
                and str(item.get("role", "contributor")).lower() != "author"
            ]
            new_contribs = (
                build_datapackage_contributors_list(resolved_authors, [], registry)
                + preserved_non_authors
            )
        elif resolved_authors is None and resolved_contribs is not None:
            # Preserve existing author entries while replacing non-author contributors
            preserved_authors = [
                item
                for item in existing_contribs
                if isinstance(item, dict)
                and str(item.get("role", "")).lower() == "author"
            ]
            new_contribs = preserved_authors + build_datapackage_contributors_list(
                [], resolved_contribs, registry
            )
        else:
            new_contribs = existing_contribs

        doc = _insert_key_after(
            doc,
            "contributors",
            new_contribs,
            after_keys=("spatial", "licenses", "homepage", "description", "title"),
        )
        updated_text = json.dumps(doc, indent=indent, ensure_ascii=False) + "\n"
    else:
        raise ValueError(f"In-place update not supported for format '{fmt}'.")

    changed = updated_text != original_text
    diff_lines = difflib.unified_diff(
        original_text.splitlines(keepends=True),
        updated_text.splitlines(keepends=True),
        fromfile=f"a/{fpath.name}",
        tofile=f"b/{fpath.name}",
    )
    diff_str = "".join(diff_lines)

    if changed and not dry_run:
        fpath.write_text(updated_text, encoding="utf-8")

    return updated_text, changed, diff_str
