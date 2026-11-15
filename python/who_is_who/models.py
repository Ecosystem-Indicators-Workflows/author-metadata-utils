"""Data models and normalization utilities for who-is-who metadata management."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any
import unicodedata

ORCID_PREFIX = "https://orcid.org/"
ORCID_BARE_RE = re.compile(r"^(\d{4}-\d{4}-\d{4}-\d{3}[\dX])$", re.IGNORECASE)
ORCID_ANY_RE = re.compile(
    r"^(?:https?://orcid\.org/)?(\d{4}-\d{4}-\d{4}-\d{3}[\dX])/?$",
    re.IGNORECASE,
)

# Controlled Zenodo / DataCite contributor types accepted by /api/deposit/depositions
ZENODO_CONTRIBUTOR_TYPES: tuple[str, ...] = (
    "ContactPerson",
    "DataCollector",
    "DataCurator",
    "DataManager",
    "Distributor",
    "Editor",
    "HostingInstitution",
    "Producer",
    "ProjectLeader",
    "ProjectManager",
    "ProjectMember",
    "RegistrationAgency",
    "RegistrationAuthority",
    "RelatedPerson",
    "Researcher",
    "ResearchGroup",
    "RightsHolder",
    "Sponsor",
    "Supervisor",
    "WorkPackageLeader",
    "Other",
)

ZENODO_TYPE_LOOKUP: dict[str, str] = {t.lower(): t for t in ZENODO_CONTRIBUTOR_TYPES}

# Frictionless Data Package v1 standard contributor roles
DATAPACKAGE_ROLES: tuple[str, ...] = (
    "author",
    "publisher",
    "maintainer",
    "wrangler",
    "contributor",
)

SUPPORTED_FORMATS: tuple[str, ...] = (
    "cff",
    "codemeta",
    "zenodo",
    "datapackage",
)


def fold_text(value: str | None) -> str:
    """Lowercase, strip accents (NFKD), and normalize whitespace."""
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", stripped).strip().lower()


def normalize_whitespace(value: str | None) -> str:
    """Collapse internal runs of whitespace and strip leading/trailing spaces."""
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def orcid_to_bare(value: str | None) -> str | None:
    """Extract canonical bare 19-char ORCID (e.g. '0000-0002-9554-3395') or None."""
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned or "TODO" in cleaned.upper():
        return None
    match = ORCID_ANY_RE.match(cleaned)
    if not match:
        return None
    bare = match.group(1).upper()
    return bare


def orcid_to_uri(value: str | None) -> str | None:
    """Convert bare or URI ORCID into canonical 'https://orcid.org/...' URI."""
    bare = orcid_to_bare(value)
    if not bare:
        return None
    return f"{ORCID_PREFIX}{bare}"


def is_placeholder_orcid(value: str | None) -> bool:
    """Return True if an ORCID field contains a non-empty invalid/placeholder string."""
    if not value or not value.strip():
        return False
    return orcid_to_bare(value) is None


def split_full_name(name: str) -> tuple[str, str]:
    """Split a full name ('Family, Given' or 'Given Family') into (given, family)."""
    cleaned = normalize_whitespace(name)
    if not cleaned:
        return "", ""
    if "," in cleaned:
        family, given = cleaned.split(",", 1)
        return normalize_whitespace(given), normalize_whitespace(family)
    parts = cleaned.split(" ")
    if len(parts) == 1:
        return "", parts[0]
    # Handle common multi-word surnames only when no comma is used; default last token
    # unless known compound particle exists.
    particles = {"de", "del", "della", "van", "von", "mac", "mc", "st", "st."}
    if len(parts) >= 3 and parts[-2].lower() in particles:
        return " ".join(parts[:-2]), " ".join(parts[-2:])
    return " ".join(parts[:-1]), parts[-1]


def extract_initials(given_name: str | None) -> list[str]:
    """Extract ordered lowercase first letters from a given-name string.

    Examples:
        'J. R.' -> ['j', 'r']
        'José R.' -> ['j', 'r']
        'J.A.' -> ['j', 'a']
        'David A.' -> ['d', 'a']
    """
    folded = fold_text(given_name)
    if not folded:
        return []
    tokens = re.split(r"[\s.\-]+", folded)
    return [tok[0] for tok in tokens if tok]


def given_names_compatible(given_a: str | None, given_b: str | None) -> bool:
    """Check whether two given names are compatible (allowing initials/middle names)."""
    fa = fold_text(given_a)
    fb = fold_text(given_b)
    if not fa or not fb:
        return True
    if fa == fb:
        return True
    init_a = extract_initials(given_a)
    init_b = extract_initials(given_b)
    if not init_a or not init_b:
        return True
    # First initial must match, and shorter initial list must be a prefix of longer
    min_len = min(len(init_a), len(init_b))
    if init_a[:min_len] != init_b[:min_len]:
        return False
    # If both first tokens are full words (length > 1 and not abbreviated), ensure they match
    tok_a = [t for t in re.split(r"[\s.]+", fa) if t]
    tok_b = [t for t in re.split(r"[\s.]+", fb) if t]
    if tok_a and tok_b and len(tok_a[0]) > 1 and len(tok_b[0]) > 1:
        # Allow minor spelling variants like Malcom / Malcolm via prefix or edit check
        if tok_a[0] != tok_b[0] and not (
            tok_a[0].startswith(tok_b[0]) or tok_b[0].startswith(tok_a[0])
        ):
            return False
    return True


def suggest_person_code(
    given_name: str, family_name: str, existing_codes: set[str]
) -> str:
    """Suggest a concise unique lowercase slug (e.g. 'jbalmer') for a Person."""
    fam_slug = re.sub(r"[^a-z0-9]", "", fold_text(family_name))
    inits = "".join(extract_initials(given_name))
    first_init = inits[:1]
    candidates = []
    if first_init and fam_slug:
        candidates.append(f"{first_init}{fam_slug}")
    if inits and fam_slug:
        candidates.append(f"{inits}{fam_slug}")
    if fam_slug:
        candidates.append(fam_slug)
    base = candidates[0] if candidates else "person"
    for cand in candidates:
        if cand not in existing_codes:
            return cand
    idx = 2
    while f"{base}{idx}" in existing_codes:
        idx += 1
    return f"{base}{idx}"


def suggest_org_code(org_name: str, existing_codes: set[str]) -> str:
    """Suggest a concise unique lowercase slug for an Organization."""
    stopwords = {"of", "the", "for", "and", "in", "at", "on", "to", "de", "la", "del"}
    words = [
        w
        for w in re.split(r"[^a-z0-9]+", fold_text(org_name))
        if w and w not in stopwords
    ]
    if not words:
        base = "org"
    elif len(words) == 1:
        base = words[0][:10]
    else:
        base = "".join(w[0] for w in words[:6])
    if base not in existing_codes:
        return base
    idx = 2
    while f"{base}{idx}" in existing_codes:
        idx += 1
    return f"{base}{idx}"


def normalize_zenodo_type(role: str | None, default: str = "ProjectMember") -> str:
    """Validate and normalize a Zenodo contributor type."""
    if not role:
        return default
    cleaned = role.strip()
    if cleaned.lower() in ZENODO_TYPE_LOOKUP:
        return ZENODO_TYPE_LOOKUP[cleaned.lower()]
    # Map common Frictionless / generic role names gracefully if passed
    aliases = {
        "contributor": default,
        "author": "Researcher",
        "maintainer": "DataManager",
        "wrangler": "DataCurator",
        "publisher": "Distributor",
    }
    if cleaned.lower() in aliases:
        return aliases[cleaned.lower()]
    valid = ", ".join(ZENODO_CONTRIBUTOR_TYPES)
    raise ValueError(
        f"Invalid Zenodo contributor type '{role}'. Expected one of: {valid}"
    )


@dataclass
class Organization:
    """An organization entry in who-is-who.json."""

    code: str
    name: str
    id_uri: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Organization:
        known = {"code", "name", "@id"}
        extra = {k: v for k, v in data.items() if k not in known}
        return cls(
            code=str(data["code"]).strip(),
            name=normalize_whitespace(str(data["name"])),
            id_uri=normalize_whitespace(data.get("@id")) or None,
            extra=extra,
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "name": self.name}
        if self.id_uri:
            out["@id"] = self.id_uri
        out.update(self.extra)
        return out


@dataclass
class Person:
    """A person entry in who-is-who.json."""

    family_name: str
    given_name: str
    orcid: str | None = None  # Stored as canonical URI 'https://orcid.org/...'
    affiliations: list[str] = field(default_factory=list)
    code: str | None = None
    email: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def orcid_bare(self) -> str | None:
        return orcid_to_bare(self.orcid)

    @property
    def orcid_uri(self) -> str | None:
        return orcid_to_uri(self.orcid)

    @property
    def display_name(self) -> str:
        if self.given_name:
            return f"{self.given_name} {self.family_name}"
        return self.family_name

    @property
    def zenodo_name(self) -> str:
        if self.given_name:
            return f"{self.family_name}, {self.given_name}"
        return self.family_name

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Person:
        known = {"code", "@id", "affiliations", "familyName", "givenName", "email"}
        extra = {k: v for k, v in data.items() if k not in known}
        raw_orcid = data.get("@id")
        orcid_uri = orcid_to_uri(raw_orcid) if raw_orcid else None
        affs = [str(a).strip() for a in data.get("affiliations", []) if str(a).strip()]
        code_val = normalize_whitespace(data.get("code")) or None
        email_val = normalize_whitespace(data.get("email")) or None
        return cls(
            family_name=normalize_whitespace(str(data.get("familyName", ""))),
            given_name=normalize_whitespace(str(data.get("givenName", ""))),
            orcid=orcid_uri,
            affiliations=affs,
            code=code_val,
            email=email_val,
            extra=extra,
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.code:
            out["code"] = self.code
        if self.orcid_uri:
            out["@id"] = self.orcid_uri
        out["affiliations"] = list(self.affiliations)
        out["familyName"] = self.family_name
        out["givenName"] = self.given_name
        if self.email:
            out["email"] = self.email
        out.update(self.extra)
        return out


@dataclass
class SelectSpec:
    """Selection of a person from the registry plus an optional role/type override."""

    selector: str
    role: str | None = None

    @classmethod
    def parse(cls, token: str) -> SelectSpec:
        """Parse 'selector' or 'selector:Role' (ignoring 'https://orcid.org/...' colons)."""
        cleaned = token.strip()
        if not cleaned:
            raise ValueError("Empty person selector")
        # Do not split on 'https://...' or 'http://...' unless another ':Role' follows
        if cleaned.lower().startswith(("http://", "https://")):
            scheme_end = cleaned.find("://") + 3
            rest = cleaned[scheme_end:]
            if ":" in rest:
                idx = scheme_end + rest.rfind(":")
                return cls(
                    selector=cleaned[:idx].strip(),
                    role=cleaned[idx + 1 :].strip() or None,
                )
            return cls(selector=cleaned)
        if ":" in cleaned:
            sel, role = cleaned.rsplit(":", 1)
            return cls(selector=sel.strip(), role=role.strip() or None)
        return cls(selector=cleaned)


@dataclass
class ExtractedPerson:
    """Normalized person record extracted from a metadata file."""

    source_file: Path
    source_format: str  # 'cff', 'codemeta', 'zenodo', 'datapackage'
    block_name: str  # e.g. 'authors', 'author', 'contributor', 'creators', 'contributors'
    given_name: str
    family_name: str
    orcid_raw: str | None = None
    email: str | None = None
    affiliations: list[str] = field(default_factory=list)
    role: str | None = None
    schema_notes: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def orcid_bare(self) -> str | None:
        return orcid_to_bare(self.orcid_raw)

    @property
    def orcid_uri(self) -> str | None:
        return orcid_to_uri(self.orcid_raw)

    @property
    def display_name(self) -> str:
        if self.given_name and self.family_name:
            return f"{self.given_name} {self.family_name}"
        return self.family_name or self.given_name or "<unnamed>"


@dataclass
class Inconsistency:
    """A discrepancy between an extracted person record and who-is-who.json."""

    category: str
    severity: str  # 'error', 'warning', 'info'
    field: str
    message: str
    file_value: Any = None
    registry_value: Any = None


@dataclass
class PersonVerification:
    """Verification outcome for a single extracted person."""

    extracted: ExtractedPerson
    matched_person: Person | None = None
    match_method: str | None = None  # 'orcid+name', 'orcid', 'name', 'fuzzy_name', None
    inconsistencies: list[Inconsistency] = field(default_factory=list)

    @property
    def is_matched(self) -> bool:
        return self.matched_person is not None

    @property
    def has_errors(self) -> bool:
        return any(inc.severity == "error" for inc in self.inconsistencies)

    @property
    def is_clean(self) -> bool:
        return self.is_matched and not self.inconsistencies


@dataclass
class FileVerification:
    """Verification outcome for all extracted persons in a file."""

    file_path: Path
    format_type: str
    entries: list[PersonVerification] = field(default_factory=list)

    @property
    def unmatched(self) -> list[PersonVerification]:
        return [e for e in self.entries if not e.is_matched]

    @property
    def inconsistent(self) -> list[PersonVerification]:
        return [e for e in self.entries if e.is_matched and e.inconsistencies]

    @property
    def clean(self) -> list[PersonVerification]:
        return [e for e in self.entries if e.is_clean]
