"""Tests de bout en bout sur les classeurs réels (extraits Hyper requis)."""
import os
import unittest
from collections import Counter

from tableauhyperapi import Date

from reader import _jsonable, open_workbook

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "import_donnees_exemples")
CPH = os.path.join(DATA, "CPHv3_online3.twb")
FE = os.path.join(DATA, "demande_antoine_fe_laposte.twb")


@unittest.skipUnless(os.path.exists(CPH), "classeur CPHv3 absent")
class CPHv3(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wb = open_workbook(CPH, DATA)
        cls.r = cls.wb.render()
        cls.by_name = {s["name"]: s for s in cls.r["sheets"]}

    def test_aucune_feuille_perdue(self):
        self.assertEqual(len(self.r["sheets"]), 30)

    def test_types_de_rendu(self):
        self.assertEqual(Counter(s["kind"] for s in self.r["sheets"]),
                         {"crosstab": 7, "table": 6, "chart": 6, "notice": 5,
                          "map": 4, "kpi": 2})

    def test_cartes(self):
        cartes = [s for s in self.r["sheets"] if s["kind"] == "map"]
        self.assertEqual(len(cartes), 4)
        for s in cartes:
            self.assertTrue(s["features"])
            self.assertEqual(s["features"][0]["type"], "Feature")
            self.assertIn("geometry", s["features"][0])
        hor = next(s for s in cartes if s["name"] == "carto_hor")
        self.assertEqual(hor["valueLabel"], "Moyenne hor")
        self.assertIsInstance(hor["min"], (int, float))

    def test_actions_parsees(self):
        acts = self.r["actions"]
        self.assertEqual(len(acts), 14)
        for a in acts:
            self.assertEqual(a["type"], "filter")
            self.assertEqual(a["activation"], "on-select")
            self.assertTrue(a["source_sheet"])
            self.assertTrue(a["target"])

    def test_libelles(self):
        s = self.by_name["Extraction données"]
        self.assertEqual(s["labels"]["sum:MOYENNE_HORAIRE"], "Moyenne hor")

    def test_crosstabs_coherents(self):
        for s in self.r["sheets"]:
            if s["kind"] != "crosstab":
                continue
            self.assertIn(s["measure"], s["columns"])
            for rec in s["records"][:20]:
                self.assertIn(s["measure"], rec)

    def test_filtre_multichamp(self):
        base = self.wb.render_sheet_by_name("taux_boucles_temporel")["total"]
        un = self.wb.render_sheet_by_name("taux_boucles_temporel",
                                          filters={"IND_MES": ["DE45897P"]})["total"]
        # champ absent de la feuille cible : Tableau l'ignore
        ignore = self.wb.render_sheet_by_name("taux_boucles_temporel",
                                              filters={"HEURE": ["19"]})["total"]
        deux = self.wb.render_sheet_by_name(
            "taux_boucles_temporel", filters={"IND_MES": ["DE45897P"], "HEURE": ["19"]})["total"]
        self.assertGreater(base, 0)
        self.assertTrue(0 < un < base)
        self.assertEqual(ignore, base)
        self.assertEqual(deux, un)

    def test_filtre_dimension_tronquee(self):
        s = self.wb.render_sheet_by_name("analyse long terme")
        valeur = s["records"][0]["JOUR (month)"]
        r = self.wb.render_sheet_by_name("analyse long terme", filters={"JOUR (month)": [valeur]})
        self.assertTrue(0 < r["total"] < s["total"])

    def test_agregations_et_parts(self):
        s = self.by_name["taux_emission_all_boucles"]
        self.assertEqual(s["kind"], "chart")
        self.assertIn("cnt:MOYENNE_HORAIRE", s["columns"])
        long = self.by_name["analyse long terme"]
        self.assertIn("JOUR (year)", long["columns"])
        self.assertIn("JOUR (month)", long["columns"])
        rec = long["records"][0]
        self.assertTrue(1900 < rec["JOUR (year)"] < 2100)
        self.assertTrue(1 <= rec["JOUR (month)"] <= 12)

    def test_compte_implicite_et_couleur(self):
        bar = self.by_name["zones_etudes_taux"]
        self.assertEqual(bar["kind"], "chart")
        self.assertIn("cnt:__records", bar["columns"])
        ct = self.by_name["zones_etudes_val_theoriques"]
        self.assertEqual(ct["kind"], "crosstab")
        self.assertEqual(ct["color"]["type"], "measure")
        self.assertIn(ct["measure"], ct["columns"])

    def test_story_filtres(self):
        story = next(d for d in self.r["dashboards"] if d["name"] == "CPHv3")
        first = story["story"][0]
        self.assertTrue(first["filters"])
        fields = [f.get("field") for fs in first["filters"].values() for f in fs]
        self.assertIn("JOUR (date)", fields)

    def test_device_layouts_et_size(self):
        d = next(d for d in self.r["dashboards"] if d["name"] == "analyse boucles")
        self.assertIn("Phone", d["device_layouts"])
        self.assertTrue(d["device_layouts"]["Phone"]["zones"])
        v = next(d for d in self.r["dashboards"] if d["name"] == "valid_calage")
        self.assertIn("size", [z["kind"] for z in v["zones"]])

    def test_filtre_expression_parametre(self):
        base = self.wb.render_sheet_by_name("taux_boucles_temporel")["total"]
        narrow = self.wb.render_sheet_by_name(
            "taux_boucles_temporel", params={"Paramètre 1": "2025-03-01"})["total"]
        self.assertGreater(base, 0)
        self.assertLess(narrow, base)

    def test_info_bulles_personnalisees(self):
        s = self.by_name["taux_boucles_temporel"]
        self.assertIn("Jour", s["tooltip"])
        spec = s["spec"]
        layers = spec.get("layer") or [spec]
        self.assertTrue(any((l.get("encoding") or {}).get("tooltip", {}).get("field") == "__tooltip"
                            for l in layers))
        self.assertIn("__tooltip", spec["transform"][0]["as"])

    def test_dashboards(self):
        noms = {d["name"] for d in self.r["dashboards"]}
        self.assertIn("analyse boucles", noms)
        boucles = next(d for d in self.r["dashboards"] if d["name"] == "analyse boucles")
        kinds = Counter(z["kind"] for z in boucles["zones"])
        self.assertGreaterEqual(kinds["sheet"], 3)
        self.assertGreaterEqual(kinds["filter"], 1)
        self.assertEqual(next(d for d in self.r["dashboards"] if d["name"] == "CPHv3")["background"], "#e6ecf0")

    def test_sizing_dashboard(self):
        d = next(d for d in self.r["dashboards"] if d["name"] == "analyse boucles diff")
        self.assertEqual(d["sizing"], "automatic")

    def test_viewpoints(self):
        d = next(d for d in self.r["dashboards"] if d["name"] == "analyse boucles diff")
        self.assertEqual(d["viewpoints"].get("Evolution horaire"), "entire-view")
        self.assertEqual(d["viewpoints"].get("evol diff"), "standard")
        t = next(d for d in self.r["dashboards"] if d["name"] == "taux_emission_boucles")
        self.assertEqual(t["viewpoints"].get("taux_boucles_temporel"), "entire-view")
        self.assertEqual(t["viewpoints"].get("taux_emission_all_boucles"), "standard")

    def test_storyboard(self):
        story = next(d for d in self.r["dashboards"] if d["name"] == "CPHv3")
        self.assertGreaterEqual(len(story["story"]), 5)
        self.assertTrue(story["story"][0]["sheet"])

    def test_info_bulle_carte(self):
        s = self.by_name["position boucles"]
        self.assertIn("Id boucle", s["tooltip"])
        self.assertIn("code_d", s["details"])
        diff = self.by_name["carto diff _boucles"]
        self.assertIn("sum:moyenne_boucle", diff["details"])

    def test_styles_et_legende(self):
        carte = next(s for s in self.r["sheets"] if s["name"] == "carto_hor")
        self.assertTrue(carte["palette"])
        self.assertIn("min", carte)
        self.assertTrue(carte["features"])

    def test_rejeu_sans_effet_de_bord(self):
        sheet = next(x for x in self.wb.sheets if x.name == "taux_boucles_temporel")
        avant = len(sheet.filters)
        self.wb.render_sheet_by_name("taux_boucles_temporel", filters={"IND_MES": ["DE45897P"]})
        self.assertEqual(len(sheet.filters), avant)


@unittest.skipUnless(os.path.exists(FE), "classeur FE absent")
class FELaposte(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wb = open_workbook(FE, DATA)
        cls.r = cls.wb.render()

    def test_deux_crosstabs(self):
        self.assertEqual([s["kind"] for s in self.r["sheets"]], ["crosstab", "crosstab"])
        self.assertEqual(self.r["warnings"], [])

    def test_lignes_rendues(self):
        for s in self.r["sheets"]:
            self.assertEqual(s["total"], 6)


class Serialisation(unittest.TestCase):
    def test_types_hyper(self):
        # sans conversion, l'API renvoyait 500 sur toute feuille datée
        self.assertEqual(_jsonable(Date(2024, 11, 18)), "2024-11-18")
        self.assertEqual(_jsonable(b"\x01\x02"), "0102")
        self.assertEqual(_jsonable(3), 3)


if __name__ == "__main__":
    unittest.main()
