"""Unit tests for who_is_who extraction, verification, reconciliation, and formatting."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from who_is_who import (  # noqa: E402
    DEFAULT_REGISTRY_PATH,
    discover_metadata_files,
    extract_from_file,
    format_block,
    load_registry,
    reconcile_interactive,
    update_file,
    verify_file,
)
from who_is_who.cli import main as cli_main  # noqa: E402


SAMPLE_CFF = """# This CITATION.cff file was generated with cffinit.
# Visit https://bit.ly/cffinit to generate yours today!

cff-version: 1.2.0
title: >-
  Cross-referencing Vegetation Associations to the IUCN Global Ecosystem Typology
message: >-
  If you use these scripts and dataset, please cite it using
  the metadata from this file.
type: dataset
authors:
  - given-names: José R.
    family-names: Ferrer-Paris
    email: j.ferrer@unsw.edu.au
    affiliation: UNSW Sydney
    orcid: 'https://orcid.org/0000-0002-9554-3395'
  - given-names: Mark G.
    family-names: Tozer
    affiliation: Centre for Ecosystem Science, UNSW Sydney
    orcid: 'https://orcid.org/0000-0002-7373-0077'
  - given-names: David A.
    family-names: Keith
    affiliation: UNSW Sydney
    orcid: 'https://orcid.org/0000-0002-7373-0077'
repository-code: >-
  https://github.com/Ecosystem-Indicators-Workflows/EXAMPLE-XREF
license: CC-BY-4.0
version: 0.1.0
date-released: '2026-11-15'
"""


SAMPLE_CODEMETA = {
    "@context": "https://w3id.org/codemeta/3.0",
    "@type": "SoftwareSourceCode",
    "name": "EXAMPLE-XREF",
    "author": [
        {
            "@type": "Person",
            "familyName": "Ferrer-Paris",
            "givenName": "J. R.",
            "@id": "https://orcid.org/0000-0002-9554-3395",
            "affiliation": [
                {
                    "@type": "Organization",
                    "name": "Centre for Ecosystem Science, UNSW Sydney",
                },
                {
                    "@type": "Organization",
                    "name": "IUCN Commission on Ecosystem Management",
                },
            ],
        }
    ],
    "contributor": [
        {
            "@type": "Person",
            "familyName": "Gould",
            "givenName": "Elliot",
            "@id": "https://orcid.org/0000-0002-6585-538X",
            # Single dict affiliation (real-world edge case)
            "affiliation": {
                "@type": "Organization",
                "name": "Centre for Ecosystem Science, UNSW Sydney",
            },
        }
    ],
}


SAMPLE_ZENODO_WRAPPED = {
    "metadata": {
        "title": "Wrapped Zenodo Template",
        "upload_type": "dataset",
        "creators": [
            {
                "name": "Keith, David",
                "affiliation": "Centre for Ecosystem Science, UNSW Sydney; IUCN Commission on Ecosystem Management",
            },
            {
                "name": "Balmer, Jayne",
                "affiliation": "Department of Natural Resources and Environment Tasmania",
            },
        ],
        "contributors": [],
    }
}


SAMPLE_DATAPACKAGE_LEGACY = {
    "profile": "data-package",
    "name": "example-pkg",
    "contributors": [
        {
            "name": "Ferrer-Paris, J. R.",
            "affiliation": "Centre for Ecosystem Science, UNSW Sydney; IUCN Commission on Ecosystem Management",
            "orcid": "0000-0002-9554-3395",
            "role": "author",
        },
        {
            "name": "Gould, Elliot",
            "affiliation": "Centre for Ecosystem Science, UNSW Sydney",
            "orcid": "0000-0002-6585-538X",
            "role": "contributor",
        },
    ],
    "resources": [],
}


class WhoIsWhoTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        # Copy central registry into temp folder for isolated mutation tests
        self.reg_path = self.tmp / "who-is-who.json"
        self.reg_path.write_text(
            DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        self.registry = load_registry(self.reg_path)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_registry_selectors(self) -> None:
        p1 = self.registry.resolve_person("jrfp")
        p2 = self.registry.resolve_person("0000-0002-9554-3395")
        p3 = self.registry.resolve_person("https://orcid.org/0000-0002-9554-3395")
        p4 = self.registry.resolve_person("Ferrer-Paris")
        p5 = self.registry.resolve_person("Ferrer-Paris, J. R.")
        self.assertIs(p1, p2)
        self.assertIs(p1, p3)
        self.assertIs(p1, p4)
        self.assertIs(p1, p5)

    def test_cff_extraction_and_orcid_conflict_detection(self) -> None:
        cff_path = self.tmp / "CITATION.cff"
        cff_path.write_text(SAMPLE_CFF, encoding="utf-8")

        fver = verify_file(cff_path, self.registry)
        self.assertEqual(fver.format_type, "cff")
        self.assertEqual(len(fver.entries), 3)

        # Entry 1: José R. Ferrer-Paris -> matched to jrfp, flags givenName, email missing in registry, affiliation mismatch
        e0 = fver.entries[0]
        self.assertTrue(e0.is_matched)
        self.assertEqual(e0.matched_person.code, "jrfp")
        cats0 = {inc.category for inc in e0.inconsistencies}
        self.assertIn("NAME_MISMATCH", cats0)
        self.assertIn("MISSING_IN_REGISTRY", cats0)
        self.assertIn("AFFILIATION_MISMATCH", cats0)

        # Entry 2: Mark G. Tozer -> clean exact match!
        e1 = fver.entries[1]
        self.assertTrue(e1.is_clean)
        self.assertEqual(e1.matched_person.code, "mtozer")

        # Entry 3: David A. Keith carrying Mark Tozer's ORCID -> CRITICAL ORCID_CONFLICT!
        e2 = fver.entries[2]
        self.assertTrue(e2.is_matched)
        self.assertEqual(e2.matched_person.code, "dkeith")
        self.assertTrue(e2.has_errors)
        conflict_msgs = [
            inc.message for inc in e2.inconsistencies if inc.category == "ORCID_CONFLICT"
        ]
        self.assertEqual(len(conflict_msgs), 1)
        self.assertIn("Mark G. Tozer", conflict_msgs[0])

    def test_codemeta_single_dict_affiliation_clean(self) -> None:
        cm_path = self.tmp / "codemeta.json"
        cm_path.write_text(json.dumps(SAMPLE_CODEMETA, indent=4), encoding="utf-8")

        fver = verify_file(cm_path, self.registry)
        self.assertEqual(len(fver.entries), 2)
        self.assertTrue(fver.entries[0].is_clean)
        self.assertTrue(fver.entries[1].is_clean)

    def test_zenodo_wrapped_and_interactive_reconcile(self) -> None:
        zen_path = self.tmp / "metadata.json"
        zen_path.write_text(
            json.dumps(SAMPLE_ZENODO_WRAPPED, indent=2), encoding="utf-8"
        )
        cff_path = self.tmp / "CITATION.cff"
        cff_path.write_text(SAMPLE_CFF, encoding="utf-8")

        z_ver = verify_file(zen_path, self.registry)
        c_ver = verify_file(cff_path, self.registry)

        # Keith missing ORCID in file; Jayne Balmer unmatched
        self.assertEqual(len(z_ver.unmatched), 1)
        self.assertEqual(z_ver.unmatched[0].extracted.display_name, "Jayne Balmer")
        self.assertIn(
            "MISSING_IN_FILE",
            {inc.category for inc in z_ver.entries[0].inconsistencies},
        )

        # Simulate interactive answers:
        # 1) Add Jayne Balmer? 'y', accept default Given, Family, blank ORCID, blank email,
        #    default code ('jbalmer'), new org code ('dnret'), accept org name, blank ROR
        # 2) Add email='j.ferrer@unsw.edu.au' to 'J. R. Ferrer-Paris'? 'y'
        answers = iter(["y", "", "", "", "", "", "dnret", "", "", "y"])
        logs: list[str] = []
        changed = reconcile_interactive(
            [z_ver, c_ver],
            self.registry,
            save=True,
            input_fn=lambda _p: next(answers),
            output_fn=logs.append,
        )
        self.assertTrue(changed)

        reloaded = load_registry(self.reg_path)
        balmer = reloaded.resolve_person("jbalmer")
        self.assertEqual(balmer.display_name, "Jayne Balmer")
        self.assertEqual(balmer.affiliations, ["dnret"])
        self.assertEqual(
            reloaded.find_organization("dnret").name,
            "Department of Natural Resources and Environment Tasmania",
        )
        self.assertEqual(
            reloaded.resolve_person("jrfp").email, "j.ferrer@unsw.edu.au"
        )

    def test_surgical_cff_update_preserves_comments_and_folded_scalars(self) -> None:
        cff_path = self.tmp / "CITATION.cff"
        cff_path.write_text(SAMPLE_CFF, encoding="utf-8")

        updated_text, changed, diff_str = update_file(
            cff_path,
            self.registry,
            authors=["jrfp", "mtozer", "dkeith"],
            dry_run=False,
        )
        self.assertTrue(changed)
        self.assertIn("# This CITATION.cff file was generated with cffinit.", updated_text)
        self.assertIn("title: >-", updated_text)
        self.assertIn("repository-code: >-", updated_text)
        self.assertIn("date-released: '2026-11-15'", updated_text)
        # David Keith now has his true ORCID (0000-0002-7627-4150)!
        self.assertIn("orcid: 'https://orcid.org/0000-0002-7627-4150'", updated_text)
        self.assertTrue(diff_str)

        # Re-verify updated CFF -> 100% clean!
        fver = verify_file(cff_path, self.registry)
        self.assertEqual(len(fver.clean), 3)
        self.assertEqual(len(fver.inconsistent), 0)
        self.assertEqual(len(fver.unmatched), 0)

    def test_update_codemeta_zenodo_and_datapackage(self) -> None:
        repo = self.tmp / "target-repo"
        (repo / "scripts").mkdir(parents=True)
        (repo / "results" / "out1").mkdir(parents=True)

        (repo / "CITATION.cff").write_text(SAMPLE_CFF, encoding="utf-8")
        (repo / "scripts" / "codemeta.json").write_text(
            json.dumps(SAMPLE_CODEMETA, indent=4), encoding="utf-8"
        )
        (repo / "results" / "out1" / ".zenodo.json").write_text(
            json.dumps(SAMPLE_ZENODO_WRAPPED, indent=2), encoding="utf-8"
        )
        (repo / "results" / "out1" / "datapackage.json").write_text(
            json.dumps(SAMPLE_DATAPACKAGE_LEGACY, indent=2), encoding="utf-8"
        )

        discovered = discover_metadata_files(repo)
        self.assertEqual(len(discovered), 4)

        rc = cli_main(
            [
                "--registry",
                str(self.reg_path),
                "update",
                "--repo",
                str(repo),
                "--authors",
                "jrfp,mtozer,dkeith",
                "--contributors",
                "egould:ProjectMember,asingh:DataCurator",
                "--in-place",
            ]
        )
        self.assertEqual(rc, 0)

        # Check CodeMeta includes ROR @id on IUCN-CEM & QCIF/CES affiliations
        cm_doc = json.loads((repo / "scripts" / "codemeta.json").read_text("utf-8"))
        self.assertEqual(len(cm_doc["author"]), 3)
        self.assertEqual(len(cm_doc["contributor"]), 2)
        self.assertEqual(
            cm_doc["author"][0]["affiliation"][1]["@id"],
            "https://ror.org/00ybvyr04",
        )

        # Check wrapped Zenodo preserved `metadata` wrapper and contributor types
        zen_doc = json.loads(
            (repo / "results" / "out1" / ".zenodo.json").read_text("utf-8")
        )
        self.assertIn("metadata", zen_doc)
        self.assertEqual(len(zen_doc["metadata"]["creators"]), 3)
        self.assertEqual(
            zen_doc["metadata"]["creators"][0]["orcid"], "0000-0002-9554-3395"
        )
        self.assertEqual(
            zen_doc["metadata"]["contributors"][1]["type"], "DataCurator"
        )

        # Check Data Package uses Frictionless v1 schema (`title`, `path`, `organization`, `role`)
        dp_doc = json.loads(
            (repo / "results" / "out1" / "datapackage.json").read_text("utf-8")
        )
        self.assertEqual(len(dp_doc["contributors"]), 5)
        self.assertEqual(dp_doc["contributors"][0]["title"], "J. R. Ferrer-Paris")
        self.assertEqual(
            dp_doc["contributors"][0]["path"],
            "https://orcid.org/0000-0002-9554-3395",
        )
        self.assertEqual(dp_doc["contributors"][0]["role"], "author")
        self.assertEqual(dp_doc["contributors"][3]["role"], "contributor")

        # Verify entire repo now passes check cleanly!
        for fp, fmt in discover_metadata_files(repo):
            v = verify_file(fp, self.registry, format_type=fmt)
            self.assertEqual(len(v.unmatched), 0, msg=f"Unmatched in {fp}")
            self.assertEqual(len(v.inconsistent), 0, msg=f"Inconsistent in {fp}: {v.inconsistent}")

    def test_partial_update_preserves_existing_contributors(self) -> None:
        dp_path = self.tmp / "datapackage.json"
        # Initialize with Frictionless schema containing 1 author + 1 contributor
        update_file(
            dp_path if dp_path.exists() else self._write_json(dp_path, SAMPLE_DATAPACKAGE_LEGACY),
            self.registry,
            authors=["jrfp"],
            contributors=["egould"],
        )
        # Now update ONLY authors -> egould contributor must remain intact!
        update_file(dp_path, self.registry, authors=["jrfp", "dkeith"])
        _, extracted = extract_from_file(dp_path)
        self.assertEqual(len(extracted), 3)
        roles = [(e.family_name, e.role) for e in extracted]
        self.assertEqual(
            roles,
            [("Ferrer-Paris", "author"), ("Keith", "author"), ("Gould", "contributor")],
        )

    def test_format_block_all_four_formats(self) -> None:
        for fmt in ("cff", "codemeta", "zenodo", "datapackage"):
            rendered = format_block(
                fmt,
                self.registry,
                authors=["jrfp", "mtozer"],
                contributors=["egould:ProjectMember"],
            )
            self.assertTrue(rendered.strip(), msg=f"Empty output for {fmt}")
            if fmt != "cff":
                parsed = json.loads(rendered)
                self.assertIsInstance(parsed, dict)

    @staticmethod
    def _write_json(path: Path, obj: dict) -> Path:
        path.write_text(json.dumps(obj, indent=2), encoding="utf-8")
        return path


if __name__ == "__main__":
    unittest.main()
