"""Régression des frontières non fiables : SQL, chemins, purge, entrées API."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import core  # noqa: E402
import hosted  # noqa: E402
import reader  # noqa: E402
from calcs import CalcError, translate  # noqa: E402


class ParametresEtSQL(unittest.TestCase):
    def test_parametre_date_echappe(self):
        lit = reader._param_literal("2025-01-01' OR '1'='1", "date")
        self.assertIn("''", lit)
        self.assertNotIn("2025-01-01' OR", lit)

    def test_unite_date_inconnue(self):
        def resolve(name):
            return f'"{name}"', "any"
        with self.assertRaises(CalcError):
            translate('DATEPART("day FROM x --", [d])', resolve, {})

    def test_direction_filtre(self):
        sql = reader.filter_sql(
            {"field": "cat", "top": {"n": 1, "by": "sum:x",
                                     "direction": "DESC, (SELECT 1)"}},
            {"cat": 't."cat"'}, {}, None, None, "t", {})
        self.assertIn("DESC", sql)
        self.assertNotIn("SELECT 1", sql)


class Chemins(unittest.TestCase):
    def test_extrait_hors_dossier_refuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"
            work.mkdir()
            (root / "secret.hyper").write_bytes(b"x")
            (work / "good.hyper").write_bytes(b"x")
            wb = reader.Workbook.__new__(reader.Workbook)
            wb.twb_path = str(work / "a.twb")
            wb.workdir = str(work)
            self.assertIsNone(wb._resolve("/etc/passwd", {}))
            self.assertIsNone(wb._resolve("../secret.hyper", {}))
            self.assertEqual(Path(wb._resolve("good.hyper", {})).name, "good.hyper")

    def test_twb_imbrique_ou_majuscule(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sub").mkdir()
            (root / "sub" / "a.twb").write_text("<workbook/>", encoding="utf-8")
            (root / "B.TWB").write_text("<workbook/>", encoding="utf-8")
            found = core._find_twb(root)
            self.assertEqual(found.suffix.lower(), ".twb")


class EntreesAPI(unittest.TestCase):
    def test_types_refuses(self):
        with self.assertRaises(reader.TWBXError):
            reader._validate_sheet_inputs([], None, None)
        with self.assertRaises(reader.TWBXError):
            reader._validate_sheet_inputs(None, "x", None)
        with self.assertRaises(reader.TWBXError):
            reader._validate_sheet_inputs(None, {"x": {"expression": "1"}}, None)
        with self.assertRaises(reader.TWBXError):
            reader._validate_sheet_inputs(None, {"x": {"top": {"direction": "DESC, x"}}}, None)
        with self.assertRaises(reader.TWBXError):
            reader._validate_sheet_inputs(None, None, [1])

    def test_redirection(self):
        self.assertEqual(hosted._safe_next("/account"), "/account")
        self.assertEqual(hosted._safe_next("/\\evil.com"), "/account")
        self.assertEqual(hosted._safe_next("//evil.com"), "/account")
        self.assertEqual(hosted._safe_next("https://evil.com"), "/account")


class Purge(unittest.TestCase):
    def test_vieux_dossier_ne_casse_pas_la_purge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "old"
            old.mkdir()
            (old / "x").write_text("x", encoding="utf-8")
            old_time = time.time() - 8 * 86400
            os.utime(old, (old_time, old_time))
            old_data, old_pending = core.DATA, core.PENDING
            core.DATA, core.PENDING = root, root / "_pending"
            core.PENDING.mkdir()
            try:
                core.cleanup_public()
            finally:
                core.DATA, core.PENDING = old_data, old_pending
            self.assertFalse(old.exists())


if __name__ == "__main__":
    unittest.main()
