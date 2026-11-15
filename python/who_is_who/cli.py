"""Command-line and interactive interface for who-is-who metadata management."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from .extractors import discover_metadata_files, extract_from_file
from .formatters import format_block, update_file
from .models import SUPPORTED_FORMATS, SelectSpec
from .registry import DEFAULT_REGISTRY_PATH, Registry, load_registry
from .verifier import (
    format_verification_report,
    reconcile_interactive,
    verify_extracted_person,
    verify_file,
    verify_repo,
)


def _parse_selector_tokens(
    raw_tokens: Sequence[str] | None, registry: Registry
) -> list[SelectSpec] | None:
    """Parse CLI `--authors` / `--contributors` tokens (comma- or space-separated)."""
    if raw_tokens is None:
        return None
    specs: list[SelectSpec] = []
    for token in raw_tokens:
        cleaned = token.strip()
        if not cleaned:
            continue
        # If token contains ',', first check whether the whole string resolves to a single
        # person (e.g. "Ferrer-Paris, J. R."); otherwise split on commas.
        if "," in cleaned:
            whole = SelectSpec.parse(cleaned)
            try:
                registry.resolve_person(whole.selector)
                specs.append(whole)
                continue
            except (KeyError, ValueError):
                pass
            for part in cleaned.split(","):
                if part.strip():
                    specs.append(SelectSpec.parse(part.strip()))
        else:
            specs.append(SelectSpec.parse(cleaned))
    return specs


def _resolve_from_source_file(
    from_file: Path, registry: Registry
) -> tuple[list[SelectSpec], list[SelectSpec]]:
    """Extract ordered authors & contributors from an existing file and map to registry."""
    _, extracted = extract_from_file(from_file)
    authors: list[SelectSpec] = []
    contributors: list[SelectSpec] = []
    for ext in extracted:
        pv = verify_extracted_person(ext, registry)
        if pv.matched_person is None:
            raise KeyError(
                f"Person '{ext.display_name}' in {from_file} is not in {registry.path.name}. "
                f"Run `check --file {from_file} --interactive` first to add them."
            )
        selector = (
            pv.matched_person.code
            or pv.matched_person.orcid_uri
            or pv.matched_person.display_name
        )
        is_author = ext.block_name in {"authors", "author", "creators"} or (
            ext.block_name == "contributors"
            and ext.source_format == "datapackage"
            and (ext.role or "").lower() == "author"
        )
        if is_author:
            authors.append(SelectSpec(selector=selector))
        else:
            role = (
                ext.role
                if ext.role not in {"contributor", "creator", "author", None}
                else None
            )
            contributors.append(SelectSpec(selector=selector, role=role))
    return authors, contributors


def _print_roster(registry: Registry) -> None:
    """Print numbered Person and Organization tables from who-is-who.json."""
    print(f"\nRegistry: {registry.path}")
    print("\nPersons:")
    print(
        f"  {'#':>2}  {'Code':<12} {'Name':<26} {'ORCID':<21} {'Affiliations'}"
    )
    print("  " + "-" * 84)
    for idx, p in enumerate(registry.persons, start=1):
        code = p.code or "-"
        orcid = p.orcid_bare or "-"
        affs = ", ".join(p.affiliations) if p.affiliations else "-"
        email = f" <{p.email}>" if p.email else ""
        print(
            f"  {idx:>2}  {code:<12} {p.display_name:<26} {orcid:<21} {affs}{email}"
        )

    print("\nOrganizations:")
    print(f"  {'Code':<12} {'Name':<48} {'@id'}")
    print("  " + "-" * 84)
    for o in registry.organizations:
        print(f"  {o.code:<12} {o.name:<48} {o.id_uri or '-'}")


def _collect_target_files(
    repo: str | None,
    files: Sequence[str] | None,
    formats: set[str] | None,
    interactive: bool,
) -> tuple[list[tuple[Path, str]], Path | None]:
    """Resolve target files from `--repo`, `--file`, or interactive prompt."""
    base_dir: Path | None = None
    chosen_repo = repo
    chosen_files = list(files or [])

    if not chosen_repo and not chosen_files and interactive:
        raw_target = input(
            "Target repository directory or metadata file path [.]: "
        ).strip()
        target = Path(raw_target or ".").expanduser().resolve()
        if target.is_dir():
            chosen_repo = str(target)
        else:
            chosen_files = [str(target)]

    discovered: list[tuple[Path, str]] = []
    if chosen_repo:
        base_dir = Path(chosen_repo).expanduser().resolve()
        discovered.extend(discover_metadata_files(base_dir, formats=formats))

    for f in chosen_files:
        fp = Path(f).expanduser().resolve()
        if fp.is_dir():
            discovered.extend(discover_metadata_files(fp, formats=formats))
        else:
            fmt_override = next(iter(formats)) if formats and len(formats) == 1 else None
            if fmt_override:
                discovered.append((fp, fmt_override))
            else:
                discovered.extend(discover_metadata_files(fp, formats=formats))

    # Deduplicate preserving order
    seen: set[Path] = set()
    unique: list[tuple[Path, str]] = []
    for fp, fmt in discovered:
        if fp not in seen:
            seen.add(fp)
            unique.append((fp, fmt))

    if interactive and len(unique) > 1:
        print("\nDiscovered metadata files:")
        for idx, (fp, fmt) in enumerate(unique, start=1):
            rel = fp
            if base_dir is not None:
                try:
                    rel = fp.relative_to(base_dir)
                except ValueError:
                    pass
            print(f"  [{idx}] {rel} ({fmt})")
        sel = input(
            "Select file numbers to process (comma-separated, or Enter for all): "
        ).strip()
        if sel and sel.lower() != "all":
            indices = [int(x.strip()) for x in sel.split(",") if x.strip().isdigit()]
            unique = [
                unique[i - 1] for i in indices if 1 <= i <= len(unique)
            ]

    return unique, base_dir


def _prompt_selection_interactive(
    registry: Registry,
) -> tuple[list[SelectSpec] | None, list[SelectSpec] | None, bool]:
    """Interactively ask the user to select ordered authors and contributors."""
    _print_roster(registry)
    print(
        "\nEnter person numbers, codes, or surnames in desired order "
        "(e.g. `1, 2, 3` or `jrfp, mtozer, dkeith`)."
    )
    raw_aut = input(
        "Authors / creators (or '-' to leave unchanged): "
    ).strip()
    if raw_aut == "-" or raw_aut == "":
        authors = None
    else:
        authors = _parse_selector_tokens([raw_aut], registry)

    print(
        "\nFor contributors, you may optionally append `:Role` "
        "(e.g. `5:ProjectMember, 6:DataCurator`; default role is ProjectMember)."
    )
    raw_con = input(
        "Contributors ('-' to leave unchanged, 'clear' to empty): "
    ).strip()
    clear_contributors = False
    if raw_con.lower() == "clear":
        contributors = []
        clear_contributors = True
    elif raw_con in {"-", ""}:
        contributors = None
    else:
        contributors = _parse_selector_tokens([raw_con], registry)

    return authors, contributors, clear_contributors


# ---------------------------------------------------------------------------
# Subcommand Handlers
# ---------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    registry = load_registry(args.registry)
    if args.json:
        print(json.dumps(registry.to_dict(), indent=2, ensure_ascii=False))
    else:
        _print_roster(registry)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    registry = load_registry(args.registry)
    formats = {f.lower() for f in args.format} if args.format else None
    interactive = bool(args.interactive or args.add_missing)

    if not args.repo and not args.file and not interactive:
        if sys.stdin.isatty():
            interactive = True
        else:
            print(
                "Error: specify --repo <path> or --file <path>.",
                file=sys.stderr,
            )
            return 2

    targets, base_dir = _collect_target_files(
        args.repo, args.file, formats, interactive=interactive
    )
    results = [verify_file(fp, registry, format_type=fmt) for fp, fmt in targets]

    if args.json:
        payload = [
            {
                "file": str(r.file_path),
                "format": r.format_type,
                "entries": [
                    {
                        "display_name": e.extracted.display_name,
                        "block": e.extracted.block_name,
                        "matched_code": (
                            e.matched_person.code if e.matched_person else None
                        ),
                        "matched_name": (
                            e.matched_person.display_name
                            if e.matched_person
                            else None
                        ),
                        "match_method": e.match_method,
                        "inconsistencies": [
                            {
                                "category": inc.category,
                                "severity": inc.severity,
                                "field": inc.field,
                                "message": inc.message,
                                "file_value": inc.file_value,
                                "registry_value": inc.registry_value,
                            }
                            for inc in e.inconsistencies
                        ],
                    }
                    for e in r.entries
                ],
            }
            for r in results
        ]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(format_verification_report(results, base_dir=base_dir))

    if interactive and results:
        reconcile_interactive(results, registry, save=True)

    has_errors = any(
        pv.has_errors for fver in results for pv in fver.entries
    )
    return 1 if has_errors else 0


def cmd_format(args: argparse.Namespace) -> int:
    registry = load_registry(args.registry)
    interactive = bool(args.interactive)

    authors: list[SelectSpec] | None = None
    contributors: list[SelectSpec] | None = None

    if args.from_file:
        authors, contributors = _resolve_from_source_file(
            Path(args.from_file).expanduser().resolve(), registry
        )
    if args.authors is not None:
        authors = _parse_selector_tokens(args.authors, registry)
    if args.contributors is not None:
        contributors = _parse_selector_tokens(args.contributors, registry)

    if authors is None and contributors is None:
        if interactive or sys.stdin.isatty():
            authors, contributors, _ = _prompt_selection_interactive(registry)
        else:
            print(
                "Error: specify --authors, --contributors, or --from-file.",
                file=sys.stderr,
            )
            return 2

    output = format_block(
        format_type=args.format,
        registry=registry,
        authors=authors,
        contributors=contributors,
        default_contributor_type=args.contributor_type,
    )
    sys.stdout.write(output)
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    registry = load_registry(args.registry)
    formats = {f.lower() for f in args.format} if args.format else None
    interactive = bool(args.interactive)

    if (
        not interactive
        and args.authors is None
        and args.contributors is None
        and not args.from_file
        and not args.clear_contributors
        and sys.stdin.isatty()
    ):
        interactive = True

    targets, base_dir = _collect_target_files(
        args.repo, args.file, formats, interactive=interactive
    )
    if not targets:
        print("No matching metadata files found.", file=sys.stderr)
        return 1

    authors: list[SelectSpec] | None = None
    contributors: list[SelectSpec] | None = None
    clear_contributors = bool(args.clear_contributors)

    if args.from_file:
        src_aut, src_con = _resolve_from_source_file(
            Path(args.from_file).expanduser().resolve(), registry
        )
        authors = src_aut
        if src_con:
            contributors = src_con

    if args.authors is not None:
        authors = _parse_selector_tokens(args.authors, registry)
    if args.contributors is not None:
        contributors = _parse_selector_tokens(args.contributors, registry)

    if (
        authors is None
        and contributors is None
        and not clear_contributors
        and interactive
    ):
        authors, contributors, clear_contributors = _prompt_selection_interactive(
            registry
        )

    if authors is None and contributors is None and not clear_contributors:
        print(
            "Nothing selected to update (pass --authors, --contributors, or --clear-contributors).",
            file=sys.stderr,
        )
        return 0

    # First compute dry-run diffs across all target files
    pending: list[tuple[Path, str, str]] = []
    for fp, fmt in targets:
        updated_text, changed, diff_str = update_file(
            fp,
            registry=registry,
            authors=authors,
            contributors=contributors,
            clear_contributors=clear_contributors,
            default_contributor_type=args.contributor_type,
            format_type=fmt,
            dry_run=True,
        )
        rel = fp
        if base_dir is not None:
            try:
                rel = fp.relative_to(base_dir)
            except ValueError:
                pass
        if changed:
            pending.append((fp, updated_text, diff_str))
            if args.dry_run or not args.in_place:
                print(f"\n--- Preview changes for {rel} ({fmt}) ---")
                print(diff_str.rstrip())
        else:
            print(f"[Unchanged] {rel} ({fmt})")

    if not pending:
        print("\nAll target files are already up to date.")
        return 0

    if args.dry_run:
        print(
            f"\nDry run complete: {len(pending)} file(s) would be updated (pass --in-place to write)."
        )
        return 0

    should_write = bool(args.in_place)
    if not should_write:
        if interactive or sys.stdin.isatty():
            ans = (
                input(f"\nWrite updates to {len(pending)} file(s)? [y/N]: ")
                .strip()
                .lower()
            )
            should_write = ans == "y"
        else:
            print(
                "\nPass --in-place (-i) to apply changes to target files.",
                file=sys.stderr,
            )
            return 0

    if should_write:
        for fp, updated_text, _ in pending:
            fp.write_text(updated_text, encoding="utf-8")
            print(f"✔ Updated {fp}")

    return 0


def cmd_add_person(args: argparse.Namespace) -> int:
    registry = load_registry(args.registry)
    given = args.given
    family = args.family
    orcid = args.orcid
    email = args.email
    code = args.code
    affs_raw = args.affiliations

    if not family and sys.stdin.isatty():
        given = given or input("Given name: ").strip()
        family = input("Family name: ").strip()
        orcid = orcid or (input("ORCID (optional): ").strip() or None)
        email = email or (input("Email (optional): ").strip() or None)
        code = code or (input("Short code (Enter for auto): ").strip() or None)
        if affs_raw is None:
            existing_orgs = ", ".join(
                f"{o.code}={o.name}" for o in registry.organizations
            )
            print(f"Available orgs: {existing_orgs}")
            raw_a = input("Affiliation code(s), comma-separated: ").strip()
            affs_raw = [x.strip() for x in raw_a.split(",") if x.strip()]

    if not family:
        print("Error: --family is required.", file=sys.stderr)
        return 2

    aff_codes: list[str] = []
    for item in affs_raw or []:
        for part in item.split(","):
            tok = part.strip()
            if not tok:
                continue
            org = registry.find_organization(tok)
            if org is not None:
                aff_codes.append(org.code)
            else:
                new_org = registry.add_organization(name=tok)
                aff_codes.append(new_org.code)
                print(f"Added organization [{new_org.code}] {new_org.name}")

    person = registry.add_person(
        given_name=given or "",
        family_name=family,
        orcid=orcid,
        affiliations=aff_codes,
        email=email,
        code=code,
    )
    saved = registry.save()
    print(f"✔ Added [{person.code}] {person.display_name} to {saved}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manage-authors.py",
        description=(
            "Verify, reconcile, format, and update author/contributor blocks "
            "across CITATION.cff, codemeta.json, .zenodo.json, and datapackage.json "
            "using a centralised who-is-who.json registry."
        ),
    )
    parser.add_argument(
        "--registry",
        default=str(DEFAULT_REGISTRY_PATH),
        help=f"Path to who-is-who.json (default: {DEFAULT_REGISTRY_PATH})",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # list
    p_list = sub.add_parser(
        "list", help="List all Person and Organization entries in who-is-who.json"
    )
    p_list.add_argument("--json", action="store_true", help="Output raw JSON")
    p_list.set_defaults(func=cmd_list)

    # check
    p_check = sub.add_parser(
        "check",
        aliases=["verify"],
        help="Extract persons from repo/files, verify against who-is-who.json, and flag inconsistencies",
    )
    p_check.add_argument(
        "--repo",
        help="Path to target repository directory to scan for metadata files",
    )
    p_check.add_argument(
        "--file",
        action="append",
        help="Path to a specific metadata file (repeatable)",
    )
    p_check.add_argument(
        "--format",
        action="append",
        choices=SUPPORTED_FORMATS,
        help="Restrict check to specific format(s)",
    )
    p_check.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="Interactively add missing persons/orgs and enrich missing registry fields",
    )
    p_check.add_argument(
        "--add-missing",
        action="store_true",
        help="Alias for --interactive to prompt adding missing entries to who-is-who.json",
    )
    p_check.add_argument(
        "--json", action="store_true", help="Output verification report as JSON"
    )
    p_check.set_defaults(func=cmd_check)

    # format
    p_fmt = sub.add_parser(
        "format",
        help="Print a formatted YAML/JSON author/contributor block to stdout",
    )
    p_fmt.add_argument(
        "--format",
        required=True,
        choices=SUPPORTED_FORMATS,
        help="Target block format (cff, codemeta, zenodo, datapackage)",
    )
    p_fmt.add_argument(
        "--authors",
        nargs="+",
        help="Ordered author/creator selectors (codes, surnames, ORCIDs, or indices; comma- or space-separated)",
    )
    p_fmt.add_argument(
        "--contributors",
        nargs="+",
        help="Ordered contributor selectors (optionally with :Role suffix, e.g. egould:ProjectMember)",
    )
    p_fmt.add_argument(
        "--from-file",
        help="Read author/contributor selection order from an existing metadata file",
    )
    p_fmt.add_argument(
        "--contributor-type",
        default="ProjectMember",
        help="Default Zenodo contributor type when not specified per contributor (default: ProjectMember)",
    )
    p_fmt.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="Select authors and contributors interactively",
    )
    p_fmt.set_defaults(func=cmd_format)

    # update
    p_upd = sub.add_parser(
        "update",
        aliases=["sync"],
        help="Insert or replace author/contributor blocks in target repository files",
    )
    p_upd.add_argument(
        "--repo",
        help="Path to target repository directory to scan for metadata files",
    )
    p_upd.add_argument(
        "--file",
        action="append",
        help="Path to a specific metadata file to update (repeatable)",
    )
    p_upd.add_argument(
        "--format",
        action="append",
        choices=SUPPORTED_FORMATS,
        help="Filter discovered repo files to specific format(s)",
    )
    p_upd.add_argument(
        "--authors",
        nargs="+",
        help="Ordered author/creator selectors (codes, surnames, ORCIDs, or indices)",
    )
    p_upd.add_argument(
        "--contributors",
        nargs="+",
        help="Ordered contributor selectors (optionally with :Role suffix)",
    )
    p_upd.add_argument(
        "--clear-contributors",
        action="store_true",
        help="Empty the contributors block in updated files",
    )
    p_upd.add_argument(
        "--from-file",
        help="Copy ordered author/contributor selection from an existing metadata file (e.g. CITATION.cff)",
    )
    p_upd.add_argument(
        "--contributor-type",
        default="ProjectMember",
        help="Default Zenodo contributor type (default: ProjectMember)",
    )
    p_upd.add_argument(
        "--in-place",
        "-y",
        action="store_true",
        help="Write changes directly to target files without interactive confirmation",
    )
    p_upd.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview unified diffs without modifying any files",
    )
    p_upd.add_argument(
        "--interactive",
        action="store_true",
        help="Guided interactive selection of files, authors, and contributors",
    )
    p_upd.set_defaults(func=cmd_update)

    # add-person
    p_add = sub.add_parser(
        "add-person",
        help="Add a new Person entry directly to who-is-who.json",
    )
    p_add.add_argument("--given", help="Given name(s), e.g. 'Jayne'")
    p_add.add_argument("--family", help="Family name, e.g. 'Balmer'")
    p_add.add_argument("--orcid", help="ORCID ID or URL")
    p_add.add_argument("--email", help="Email address")
    p_add.add_argument("--code", help="Short code slug (auto-generated if omitted)")
    p_add.add_argument(
        "--affiliations",
        nargs="+",
        help="Organization codes or names (comma- or space-separated)",
    )
    p_add.set_defaults(func=cmd_add_person)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, KeyError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        return 130
