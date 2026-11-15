"""Verify extracted metadata persons against who-is-who.json and reconcile differences."""

from __future__ import annotations

from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

from .extractors import discover_metadata_files, extract_from_file
from .models import (
    ExtractedPerson,
    FileVerification,
    Inconsistency,
    Person,
    PersonVerification,
    fold_text,
    given_names_compatible,
    is_placeholder_orcid,
    normalize_whitespace,
    orcid_to_uri,
    suggest_org_code,
    suggest_person_code,
)
from .registry import Registry

# Formats whose person blocks natively support an `email` field
FORMATS_WITH_EMAIL: set[str] = {"cff", "codemeta", "datapackage"}


def _find_best_name_match(
    extracted: ExtractedPerson, registry: Registry
) -> tuple[Person | None, str | None]:
    """Match an extracted person by exact or fuzzy name against registry."""
    # 0. Exact full display name match (handles multi-word surnames in Frictionless `title`)
    full_fold = fold_text(extracted.display_name)
    if full_fold:
        full_matches = [
            p for p in registry.persons if fold_text(p.display_name) == full_fold
        ]
        if len(full_matches) == 1:
            return full_matches[0], "name"

    # 1. Exact folded surname + compatible given name / initials
    candidates = registry.find_by_name(extracted.given_name, extracted.family_name)
    if len(candidates) == 1:
        return candidates[0], "name"
    if len(candidates) > 1:
        # Prefer exact folded given_name if multiple share a surname
        g_fold = fold_text(extracted.given_name)
        exact_g = [p for p in candidates if fold_text(p.given_name) == g_fold]
        if len(exact_g) == 1:
            return exact_g[0], "name"
        return candidates[0], "name"

    # 2. Check if multi-word surname in registry matches tail of display_name
    for person in registry.persons:
        p_fam = fold_text(person.family_name)
        if " " in p_fam and full_fold.endswith(" " + p_fam):
            rem_given = full_fold[: -(len(p_fam) + 1)].strip()
            if given_names_compatible(person.given_name, rem_given):
                return person, "name"

    # 3. High-confidence fuzzy surname match + compatible given name
    fam_fold = fold_text(extracted.family_name)
    if not fam_fold:
        return None, None
    best_person: Person | None = None
    best_score = 0.0
    for person in registry.persons:
        ratio = SequenceMatcher(None, fam_fold, fold_text(person.family_name)).ratio()
        if ratio >= 0.86 and given_names_compatible(
            person.given_name, extracted.given_name
        ):
            if ratio > best_score:
                best_score = ratio
                best_person = person
    if best_person is not None:
        return best_person, "fuzzy_name"
    return None, None


def verify_extracted_person(
    extracted: ExtractedPerson,
    registry: Registry,
) -> PersonVerification:
    """Verify a single ExtractedPerson against the Registry and list inconsistencies."""
    inconsistencies: list[Inconsistency] = []

    for note in extracted.schema_notes:
        inconsistencies.append(
            Inconsistency(
                category="SCHEMA_WARNING",
                severity="info",
                field="schema",
                message=note,
            )
        )

    if is_placeholder_orcid(extracted.orcid_raw):
        inconsistencies.append(
            Inconsistency(
                category="INVALID_ORCID",
                severity="warning",
                field="orcid",
                message=f"Invalid or placeholder ORCID value '{extracted.orcid_raw}'.",
                file_value=extracted.orcid_raw,
            )
        )

    orcid_person = registry.find_by_orcid(extracted.orcid_bare)
    name_person, name_method = _find_best_name_match(extracted, registry)

    matched: Person | None = None
    match_method: str | None = None

    if orcid_person is not None and name_person is not None:
        if orcid_person is name_person:
            matched = orcid_person
            match_method = "orcid+name"
        else:
            # Copy-pasted ORCID belonging to a different registered person!
            matched = name_person
            match_method = name_method
            inconsistencies.append(
                Inconsistency(
                    category="ORCID_CONFLICT",
                    severity="error",
                    field="orcid",
                    message=(
                        f"ORCID {extracted.orcid_uri} in file belongs to "
                        f"'{orcid_person.display_name}' in who-is-who.json; "
                        f"'{name_person.display_name}' has ORCID "
                        f"{name_person.orcid_uri or '<none>'}."
                    ),
                    file_value=extracted.orcid_uri,
                    registry_value=name_person.orcid_uri,
                )
            )
    elif orcid_person is not None and name_person is None:
        fam_sim = SequenceMatcher(
            None,
            fold_text(extracted.family_name),
            fold_text(orcid_person.family_name),
        ).ratio()
        if fam_sim < 0.60 and not given_names_compatible(
            orcid_person.given_name, extracted.given_name
        ):
            # Unlisted person carrying another registered person's ORCID
            inconsistencies.append(
                Inconsistency(
                    category="ORCID_CONFLICT",
                    severity="error",
                    field="orcid",
                    message=(
                        f"ORCID {extracted.orcid_uri} belongs to "
                        f"'{orcid_person.display_name}' in who-is-who.json, "
                        f"which does not match '{extracted.display_name}'."
                    ),
                    file_value=extracted.orcid_uri,
                    registry_value=orcid_person.display_name,
                )
            )
            return PersonVerification(
                extracted=extracted,
                matched_person=None,
                match_method=None,
                inconsistencies=inconsistencies,
            )
        matched = orcid_person
        match_method = "orcid"
    elif name_person is not None:
        matched = name_person
        match_method = name_method
        if extracted.orcid_bare and matched.orcid_bare:
            if extracted.orcid_bare != matched.orcid_bare:
                inconsistencies.append(
                    Inconsistency(
                        category="ORCID_CONFLICT",
                        severity="error",
                        field="orcid",
                        message=(
                            f"File ORCID {extracted.orcid_uri} differs from "
                            f"who-is-who.json ORCID {matched.orcid_uri}."
                        ),
                        file_value=extracted.orcid_uri,
                        registry_value=matched.orcid_uri,
                    )
                )
        elif extracted.orcid_bare and not matched.orcid_bare:
            inconsistencies.append(
                Inconsistency(
                    category="MISSING_IN_REGISTRY",
                    severity="info",
                    field="@id",
                    message=(
                        f"File has ORCID {extracted.orcid_uri}, which is missing in "
                        "who-is-who.json."
                    ),
                    file_value=extracted.orcid_uri,
                    registry_value=None,
                )
            )
        elif not extracted.orcid_bare and matched.orcid_bare:
            inconsistencies.append(
                Inconsistency(
                    category="MISSING_IN_FILE",
                    severity="warning",
                    field="orcid",
                    message=(
                        f"Missing ORCID in file (who-is-who.json has {matched.orcid_uri})."
                    ),
                    file_value=None,
                    registry_value=matched.orcid_uri,
                )
            )

    if matched is None:
        return PersonVerification(
            extracted=extracted,
            matched_person=None,
            match_method=None,
            inconsistencies=inconsistencies,
        )

    # Compare names
    if extracted.source_format == "datapackage" and "title" in extracted.raw:
        if normalize_whitespace(str(extracted.raw.get("title", ""))) != matched.display_name:
            inconsistencies.append(
                Inconsistency(
                    category="NAME_MISMATCH",
                    severity="info",
                    field="name",
                    message=(
                        f"Name '{extracted.raw.get('title')}' differs from "
                        f"who-is-who.json '{matched.display_name}'."
                    ),
                    file_value=extracted.raw.get("title"),
                    registry_value=matched.display_name,
                )
            )
    else:
        if extracted.family_name != matched.family_name:
            inconsistencies.append(
                Inconsistency(
                    category="NAME_MISMATCH",
                    severity="warning",
                    field="familyName",
                    message=(
                        f"Family name '{extracted.family_name}' differs from "
                        f"who-is-who.json '{matched.family_name}'."
                    ),
                    file_value=extracted.family_name,
                    registry_value=matched.family_name,
                )
            )
        if extracted.given_name != matched.given_name:
            inconsistencies.append(
                Inconsistency(
                    category="NAME_MISMATCH",
                    severity="info",
                    field="givenName",
                    message=(
                        f"Given name '{extracted.given_name}' differs from "
                        f"who-is-who.json '{matched.given_name}'."
                    ),
                    file_value=extracted.given_name,
                    registry_value=matched.given_name,
                )
            )

    # Compare email
    if extracted.email and matched.email:
        if extracted.email.lower() != matched.email.lower():
            inconsistencies.append(
                Inconsistency(
                    category="EMAIL_MISMATCH",
                    severity="warning",
                    field="email",
                    message=(
                        f"Email '{extracted.email}' differs from "
                        f"who-is-who.json '{matched.email}'."
                    ),
                    file_value=extracted.email,
                    registry_value=matched.email,
                )
            )
    elif extracted.email and not matched.email:
        inconsistencies.append(
            Inconsistency(
                category="MISSING_IN_REGISTRY",
                severity="info",
                field="email",
                message=(
                    f"File has email '{extracted.email}', which is not yet recorded "
                    "in who-is-who.json."
                ),
                file_value=extracted.email,
                registry_value=None,
            )
        )
    elif (
        not extracted.email
        and matched.email
        and extracted.source_format in FORMATS_WITH_EMAIL
    ):
        inconsistencies.append(
            Inconsistency(
                category="MISSING_IN_FILE",
                severity="info",
                field="email",
                message=(
                    f"File omits email (who-is-who.json has '{matched.email}')."
                ),
                file_value=None,
                registry_value=matched.email,
            )
        )

    # Compare affiliations
    canonical_affs = registry.get_person_affiliation_names(matched)
    file_affs = extracted.affiliations
    norm_canon = [fold_text(a) for a in canonical_affs]
    norm_file = [fold_text(a) for a in file_affs]
    if norm_file != norm_canon:
        if set(norm_file) == set(norm_canon):
            inconsistencies.append(
                Inconsistency(
                    category="AFFILIATION_ORDER",
                    severity="info",
                    field="affiliations",
                    message=(
                        f"Affiliation order {file_affs} differs from "
                        f"who-is-who.json {canonical_affs}."
                    ),
                    file_value=file_affs,
                    registry_value=canonical_affs,
                )
            )
        else:
            inconsistencies.append(
                Inconsistency(
                    category="AFFILIATION_MISMATCH",
                    severity="warning",
                    field="affiliations",
                    message=(
                        f"Affiliation(s) {file_affs or ['<none>']} differ from "
                        f"who-is-who.json {canonical_affs or ['<none>']}."
                    ),
                    file_value=file_affs,
                    registry_value=canonical_affs,
                )
            )

    return PersonVerification(
        extracted=extracted,
        matched_person=matched,
        match_method=match_method,
        inconsistencies=inconsistencies,
    )


def verify_file(
    path: Path | str,
    registry: Registry,
    format_type: str | None = None,
) -> FileVerification:
    """Extract and verify all persons in a single metadata file."""
    fmt, extracted_list = extract_from_file(path, format_type=format_type)
    entries = [verify_extracted_person(ext, registry) for ext in extracted_list]

    # Intra-file duplicate ORCID check across distinct names
    orcid_owners: dict[str, list[PersonVerification]] = defaultdict(list)
    for pv in entries:
        bare = pv.extracted.orcid_bare
        if bare:
            orcid_owners[bare].append(pv)

    for bare, group in orcid_owners.items():
        distinct_names = {fold_text(pv.extracted.display_name) for pv in group}
        if len(distinct_names) > 1:
            names_str = ", ".join(sorted({pv.extracted.display_name for pv in group}))
            for pv in group:
                # Skip flagging the rightful owner when registered in who-is-who.json
                if (
                    pv.matched_person is not None
                    and pv.matched_person.orcid_bare == bare
                ):
                    continue
                already_flagged = any(
                    inc.category == "ORCID_CONFLICT" for inc in pv.inconsistencies
                )
                if not already_flagged:
                    pv.inconsistencies.append(
                        Inconsistency(
                            category="ORCID_CONFLICT",
                            severity="error",
                            field="orcid",
                            message=(
                                f"Duplicate ORCID {bare} shared by multiple people "
                                f"in the same file: {names_str}."
                            ),
                            file_value=bare,
                        )
                    )

    return FileVerification(
        file_path=Path(path).expanduser().resolve(),
        format_type=fmt,
        entries=entries,
    )


def verify_repo(
    repo_path: Path | str,
    registry: Registry,
    formats: set[str] | None = None,
) -> list[FileVerification]:
    """Discover and verify all supported metadata files inside a repository."""
    files = discover_metadata_files(repo_path, formats=formats)
    return [verify_file(fpath, registry, format_type=fmt) for fpath, fmt in files]


def format_verification_report(
    results: list[FileVerification], base_dir: Path | None = None
) -> str:
    """Render a human-readable verification report across checked files."""
    if not results:
        return "No matching metadata files found to verify."

    lines: list[str] = []
    total_entries = 0
    total_clean = 0
    total_inconsistent = 0
    total_unmatched = 0

    for fver in results:
        rel = fver.file_path
        if base_dir is not None:
            try:
                rel = fver.file_path.relative_to(base_dir.resolve())
            except ValueError:
                pass
        lines.append(f"\n=== {rel} [{fver.format_type}] ===")
        if not fver.entries:
            lines.append("  (no author/contributor entries found)")
            continue

        for pv in fver.entries:
            total_entries += 1
            ext = pv.extracted
            block_tag = f"{ext.block_name}" + (
                f":{ext.role}"
                if ext.role and ext.role not in {"author", "creator", "contributor"}
                else ""
            )
            if not pv.is_matched:
                total_unmatched += 1
                orcid_note = f" ({ext.orcid_uri})" if ext.orcid_uri else ""
                aff_note = (
                    f" — {'; '.join(ext.affiliations)}" if ext.affiliations else ""
                )
                lines.append(
                    f"  [NOT IN WHO-IS-WHO] {ext.display_name}{orcid_note}{aff_note} [{block_tag}]"
                )
                for inc in pv.inconsistencies:
                    lines.append(
                        f"      ! [{inc.severity.upper()}:{inc.category}] {inc.message}"
                    )
            elif pv.is_clean:
                total_clean += 1
                code_tag = (
                    f"@{pv.matched_person.code}"
                    if pv.matched_person and pv.matched_person.code
                    else ""
                )
                lines.append(
                    f"  [OK] {ext.display_name} -> {pv.matched_person.display_name} {code_tag} [{block_tag}]".rstrip()
                )
            else:
                total_inconsistent += 1
                code_tag = (
                    f"@{pv.matched_person.code}"
                    if pv.matched_person and pv.matched_person.code
                    else ""
                )
                lines.append(
                    f"  [DIFF] {ext.display_name} -> {pv.matched_person.display_name} {code_tag} [{block_tag}]".rstrip()
                )
                for inc in pv.inconsistencies:
                    marker = "!" if inc.severity == "error" else "~"
                    lines.append(
                        f"      {marker} [{inc.severity.upper()}:{inc.category}] {inc.message}"
                    )

    lines.append(
        "\nSummary: "
        f"{len(results)} file(s) checked | "
        f"{total_entries} entry/entries | "
        f"{total_clean} clean | "
        f"{total_inconsistent} with differences | "
        f"{total_unmatched} not in who-is-who.json"
    )
    return "\n".join(lines)


def _prompt_with_default(
    prompt_text: str,
    default: str,
    input_fn: Callable[[str], str],
) -> str:
    suffix = f" [{default}]" if default else ""
    resp = input_fn(f"{prompt_text}{suffix}: ").strip()
    return resp if resp else default


def reconcile_interactive(
    results: list[FileVerification],
    registry: Registry,
    save: bool = True,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> bool:
    """Interactively add missing persons/orgs and enrich missing registry fields.

    Returns True if `registry` was modified (and saved if `save=True`).
    """
    modified = False

    # 1. Deduplicate unmatched persons across checked files
    seen_unmatched: set[tuple[str, str, str]] = set()
    unique_unmatched: list[PersonVerification] = []
    for fver in results:
        for pv in fver.unmatched:
            ext = pv.extracted
            # Do not auto-copy a conflicting ORCID belonging to someone else
            has_orcid_conflict = any(
                inc.category == "ORCID_CONFLICT" for inc in pv.inconsistencies
            )
            safe_orcid = "" if has_orcid_conflict else (ext.orcid_bare or "")
            key = (fold_text(ext.family_name), fold_text(ext.given_name), safe_orcid)
            if key not in seen_unmatched:
                seen_unmatched.add(key)
                unique_unmatched.append(pv)

    if unique_unmatched:
        output_fn(
            f"\nFound {len(unique_unmatched)} person(s) not yet in {registry.path.name}:"
        )
        for pv in unique_unmatched:
            ext = pv.extracted
            has_orcid_conflict = any(
                inc.category == "ORCID_CONFLICT" for inc in pv.inconsistencies
            )
            output_fn(
                f"\n  • {ext.display_name}"
                + (f" | ORCID: {ext.orcid_uri}" if ext.orcid_uri and not has_orcid_conflict else "")
                + (f" | Affiliation(s): {'; '.join(ext.affiliations)}" if ext.affiliations else "")
                + f" (from {ext.source_file.name})"
            )
            ans = input_fn("    Add this person to who-is-who.json? [y/N/q]: ").strip().lower()
            if ans == "q":
                break
            if ans != "y":
                continue

            given = _prompt_with_default("    Given name", ext.given_name, input_fn)
            family = _prompt_with_default("    Family name", ext.family_name, input_fn)
            default_orcid = "" if has_orcid_conflict else (ext.orcid_uri or "")
            orcid_in = _prompt_with_default(
                "    ORCID (or blank if none)", default_orcid, input_fn
            )
            email_in = _prompt_with_default(
                "    Email (or blank if none)", ext.email or "", input_fn
            )
            suggested_code = suggest_person_code(
                given, family, registry.existing_person_codes
            )
            code_in = _prompt_with_default(
                "    Short code", suggested_code, input_fn
            )

            # Resolve or add each affiliation
            aff_codes: list[str] = []
            for raw_aff in ext.affiliations:
                existing_org = registry.find_organization(raw_aff)
                if existing_org is not None:
                    aff_codes.append(existing_org.code)
                    output_fn(
                        f"    Matched affiliation '{raw_aff}' -> [{existing_org.code}] {existing_org.name}"
                    )
                else:
                    output_fn(
                        f"    Organization '{raw_aff}' is not in who-is-who.json."
                    )
                    existing_summary = ", ".join(
                        f"{o.code}={o.name}" for o in registry.organizations
                    )
                    output_fn(f"    Existing orgs: {existing_summary}")
                    suggested_org = suggest_org_code(
                        raw_aff, registry.existing_org_codes
                    )
                    org_choice = _prompt_with_default(
                        "    Enter existing org code, or new code to add it (or '-' to skip)",
                        suggested_org,
                        input_fn,
                    )
                    if org_choice == "-":
                        continue
                    found_by_code = registry.find_organization(org_choice)
                    if found_by_code is not None:
                        aff_codes.append(found_by_code.code)
                    else:
                        org_name = _prompt_with_default(
                            f"    Canonical name for [{org_choice}]",
                            raw_aff,
                            input_fn,
                        )
                        ror_in = _prompt_with_default(
                            f"    ROR @id URL for [{org_choice}] (or blank)",
                            "",
                            input_fn,
                        )
                        new_org = registry.add_organization(
                            name=org_name,
                            code=org_choice,
                            id_uri=ror_in or None,
                        )
                        aff_codes.append(new_org.code)
                        modified = True

            if not ext.affiliations:
                extra_aff = input_fn(
                    "    Affiliation org code(s), comma-separated (or blank): "
                ).strip()
                if extra_aff:
                    aff_codes = [
                        c.strip() for c in extra_aff.split(",") if c.strip()
                    ]

            added = registry.add_person(
                given_name=given,
                family_name=family,
                orcid=orcid_in or None,
                affiliations=aff_codes,
                email=email_in or None,
                code=code_in,
            )
            modified = True
            output_fn(f"    + Added [{added.code}] {added.display_name}")

    # 2. Offer to enrich matched persons with fields present in file but missing in registry
    seen_enrich: set[tuple[int, str, str]] = set()
    for fver in results:
        for pv in fver.inconsistent:
            if pv.matched_person is None:
                continue
            for inc in pv.inconsistencies:
                if inc.category != "MISSING_IN_REGISTRY" or not inc.file_value:
                    continue
                key = (id(pv.matched_person), inc.field, str(inc.file_value))
                if key in seen_enrich:
                    continue
                seen_enrich.add(key)
                ans = (
                    input_fn(
                        f"\n  Add {inc.field}='{inc.file_value}' to "
                        f"'{pv.matched_person.display_name}' in who-is-who.json? [y/N]: "
                    )
                    .strip()
                    .lower()
                )
                if ans == "y":
                    if inc.field == "@id":
                        pv.matched_person.orcid = orcid_to_uri(str(inc.file_value))
                        modified = True
                    elif inc.field == "email":
                        pv.matched_person.email = str(inc.file_value).strip()
                        modified = True

    if modified and save:
        saved_path = registry.save()
        output_fn(f"\nSaved updated registry to {saved_path}")

    return modified
