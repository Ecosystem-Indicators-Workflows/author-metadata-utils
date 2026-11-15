"""Centralised who-is-who.json registry loading, lookup, and modification."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

from .models import (
    Organization,
    Person,
    fold_text,
    given_names_compatible,
    normalize_whitespace,
    orcid_to_bare,
    orcid_to_uri,
    split_full_name,
    suggest_org_code,
    suggest_person_code,
)

# Default path: <PID-implementation-scripts>/who-is-who.json
DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parents[2] / "who-is-who.json"


@dataclass
class Registry:
    """In-memory representation of who-is-who.json."""

    organizations: list[Organization] = field(default_factory=list)
    persons: list[Person] = field(default_factory=list)
    path: Path = field(default_factory=lambda: DEFAULT_REGISTRY_PATH)
    extra_top_level: dict[str, Any] = field(default_factory=dict)

    @property
    def org_by_code(self) -> dict[str, Organization]:
        return {org.code.lower(): org for org in self.organizations}

    @property
    def existing_person_codes(self) -> set[str]:
        return {p.code.lower() for p in self.persons if p.code}

    @property
    def existing_org_codes(self) -> set[str]:
        return {o.code.lower() for o in self.organizations if o.code}

    def find_organization(self, query: str) -> Organization | None:
        """Find an organization by code, canonical name, or @id URI."""
        q_clean = normalize_whitespace(query)
        if not q_clean:
            return None
        q_fold = fold_text(q_clean)
        # 1. Exact code match
        for org in self.organizations:
            if org.code.lower() == q_clean.lower():
                return org
        # 2. Exact @id URI match
        for org in self.organizations:
            if org.id_uri and org.id_uri.rstrip("/").lower() == q_clean.rstrip("/").lower():
                return org
        # 3. Folded name match
        for org in self.organizations:
            if fold_text(org.name) == q_fold:
                return org
        return None

    def get_person_organizations(self, person: Person) -> list[Organization]:
        """Resolve a person's affiliation codes to Organization objects in order."""
        lookup = self.org_by_code
        resolved: list[Organization] = []
        for aff_code in person.affiliations:
            org = lookup.get(aff_code.lower())
            if org is not None:
                resolved.append(org)
            else:
                # Fallback if someone put a raw org string or unindexed code
                resolved.append(Organization(code=aff_code, name=aff_code))
        return resolved

    def get_person_affiliation_names(self, person: Person) -> list[str]:
        """Return ordered canonical organization names for a person."""
        return [org.name for org in self.get_person_organizations(person)]

    def find_by_orcid(self, orcid: str | None) -> Person | None:
        """Find a Person in the registry by bare or URI ORCID."""
        bare = orcid_to_bare(orcid)
        if not bare:
            return None
        for person in self.persons:
            if person.orcid_bare == bare:
                return person
        return None

    def find_by_name(
        self, given_name: str | None, family_name: str | None
    ) -> list[Person]:
        """Find Person entries matching family_name and compatible given_name."""
        fam_fold = fold_text(family_name)
        if not fam_fold:
            return []
        candidates = [p for p in self.persons if fold_text(p.family_name) == fam_fold]
        if not candidates or not given_name:
            return candidates
        compatible = [
            p for p in candidates if given_names_compatible(p.given_name, given_name)
        ]
        return compatible

    def resolve_person(self, selector: str) -> Person:
        """Resolve a CLI/API person selector to a unique Person in the registry.

        Supported selector formats:
          - Short code (e.g. 'jrfp', 'dkeith')
          - 1-based index from `list` output (e.g. '1', '3')
          - Bare or full ORCID ('0000-0002-9554-3395' or 'https://orcid.org/...')
          - Surname ('Ferrer-Paris', 'Keith') if unique
          - Full name ('Ferrer-Paris, J. R.' or 'J. R. Ferrer-Paris')
        """
        q = normalize_whitespace(selector)
        if not q:
            raise ValueError("Empty person selector.")

        # 1. Exact short code match
        code_matches = [
            p for p in self.persons if p.code and p.code.lower() == q.lower()
        ]
        if len(code_matches) == 1:
            return code_matches[0]

        # 2. ORCID match
        by_orcid = self.find_by_orcid(q)
        if by_orcid is not None:
            return by_orcid

        # 3. 1-based numeric index
        if q.isdigit():
            idx = int(q)
            if 1 <= idx <= len(self.persons):
                return self.persons[idx - 1]
            raise ValueError(
                f"Person index {idx} out of range (1..{len(self.persons)})."
            )

        # 4. Exact surname match
        q_fold = fold_text(q)
        surname_matches = [
            p for p in self.persons if fold_text(p.family_name) == q_fold
        ]
        if len(surname_matches) == 1:
            return surname_matches[0]
        if len(surname_matches) > 1:
            opts = ", ".join(
                f"{p.code or p.display_name} ({p.display_name})"
                for p in surname_matches
            )
            raise ValueError(
                f"Ambiguous surname '{selector}'; matches multiple people: {opts}"
            )

        # 5. Full name ('Family, Given' or 'Given Family')
        for p in self.persons:
            if fold_text(p.display_name) == q_fold or fold_text(p.zenodo_name) == q_fold:
                return p

        given, family = split_full_name(q)
        if family:
            name_matches = self.find_by_name(given, family)
            if len(name_matches) == 1:
                return name_matches[0]
            if len(name_matches) > 1:
                opts = ", ".join(p.display_name for p in name_matches)
                raise ValueError(
                    f"Ambiguous name '{selector}'; matches multiple people: {opts}"
                )

        raise KeyError(
            f"No person matching '{selector}' found in {self.path.name}."
        )

    def add_organization(
        self,
        name: str,
        code: str | None = None,
        id_uri: str | None = None,
    ) -> Organization:
        """Add a new Organization (or return existing match if already registered)."""
        existing = self.find_organization(code or name)
        if existing is not None:
            if id_uri and not existing.id_uri:
                existing.id_uri = normalize_whitespace(id_uri)
            return existing
        org_code = (
            normalize_whitespace(code).lower()
            if code and normalize_whitespace(code)
            else suggest_org_code(name, self.existing_org_codes)
        )
        if org_code in self.existing_org_codes:
            raise ValueError(f"Organization code '{org_code}' already exists.")
        org = Organization(
            code=org_code,
            name=normalize_whitespace(name),
            id_uri=normalize_whitespace(id_uri) or None,
        )
        self.organizations.append(org)
        return org

    def add_person(
        self,
        given_name: str,
        family_name: str,
        orcid: str | None = None,
        affiliations: list[str] | None = None,
        email: str | None = None,
        code: str | None = None,
    ) -> Person:
        """Add a new Person to the registry and return it."""
        orcid_uri = orcid_to_uri(orcid) if orcid else None
        if orcid_uri:
            existing = self.find_by_orcid(orcid_uri)
            if existing is not None:
                raise ValueError(
                    f"ORCID {orcid_uri} already belongs to {existing.display_name}."
                )
        person_code = (
            normalize_whitespace(code).lower()
            if code and normalize_whitespace(code)
            else suggest_person_code(
                given_name, family_name, self.existing_person_codes
            )
        )
        if person_code in self.existing_person_codes:
            raise ValueError(f"Person code '{person_code}' already exists.")

        person = Person(
            family_name=normalize_whitespace(family_name),
            given_name=normalize_whitespace(given_name),
            orcid=orcid_uri,
            affiliations=[a.strip() for a in (affiliations or []) if a.strip()],
            code=person_code,
            email=normalize_whitespace(email) or None,
        )
        self.persons.append(person)
        return person

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "Organization": [o.to_dict() for o in self.organizations],
            "Person": [p.to_dict() for p in self.persons],
        }
        out.update(self.extra_top_level)
        return out

    def save(self, path: Path | str | None = None) -> Path:
        """Write registry JSON with 4-space indentation."""
        target = Path(path) if path is not None else self.path
        payload = json.dumps(self.to_dict(), indent=4, ensure_ascii=False) + "\n"
        target.write_text(payload, encoding="utf-8")
        self.path = target
        return target


def load_registry(path: Path | str | None = None) -> Registry:
    """Load who-is-who.json from disk into a Registry object."""
    target = Path(path).expanduser().resolve() if path else DEFAULT_REGISTRY_PATH
    if not target.is_file():
        raise FileNotFoundError(f"Registry file not found: {target}")
    raw = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Expected JSON object in {target}")
    orgs = [Organization.from_dict(item) for item in raw.get("Organization", [])]
    persons = [Person.from_dict(item) for item in raw.get("Person", [])]
    extra = {k: v for k, v in raw.items() if k not in {"Organization", "Person"}}
    return Registry(
        organizations=orgs,
        persons=persons,
        path=target,
        extra_top_level=extra,
    )


def save_registry(registry: Registry, path: Path | str | None = None) -> Path:
    """Save a Registry instance to disk."""
    return registry.save(path)
