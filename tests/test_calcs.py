"""Tests du traducteur de calculs Tableau -> SQL."""
import unittest

from calcs import (CalcError, has_aggregate, referenced_fields, translate,
                   window_sql)

TYPES = {"a": "num", "s": "str", "d": "any"}


def resolve(name: str):
    return f'"{name}"', TYPES.get(name, "any")


def tr(formula: str) -> str:
    return translate(formula, resolve, {})


class Chaines(unittest.TestCase):
    def test_guillemet_double(self):
        self.assertEqual(tr('"a""b"'), '\'a"b\'')

    def test_litteral_intact(self):
        # régression : le code remplaçait "yy" par "y" et cassait "yyyyMMdd"
        self.assertEqual(tr('"yyyyMMdd"'), "'yyyyMMdd'")

    def test_concat_plus(self):
        self.assertEqual(tr('[s] + "x"'), '"s" || \'x\'')

    def test_addition_numerique(self):
        self.assertEqual(tr("[a] + [a]"), '"a" + "a"')


class Controle(unittest.TestCase):
    def test_if(self):
        self.assertEqual(tr('IF [a] > 1 THEN "oui" ELSE "non" END'),
                         "CASE WHEN \"a\" > 1 THEN 'oui' ELSE 'non' END")

    def test_case(self):
        self.assertTrue(tr('CASE [a] WHEN 1 THEN "un" ELSE "autre" END')
                        .startswith('CASE "a" WHEN 1 THEN \'un\''))

    def test_elseif(self):
        sql = tr('IF [a] = 1 THEN "un" ELSEIF [a] = 2 THEN "deux" ELSE "autre" END')
        self.assertEqual(sql.count("WHEN"), 2)


class Agregats(unittest.TestCase):
    def test_countd(self):
        self.assertEqual(tr("COUNTD([a])"), 'COUNT(DISTINCT "a")')

    def test_division_entiere(self):
        self.assertEqual(tr("[a] / 2"), 'CAST("a" AS DOUBLE PRECISION) / 2')

    def test_isnull(self):
        self.assertEqual(tr("ISNULL([a])"), '("a" IS NULL)')

    def test_ifnull(self):
        self.assertEqual(tr("IFNULL([a], 0)"), 'COALESCE("a", 0)')


class Fonctions(unittest.TestCase):
    def test_dateparse(self):
        self.assertEqual(
            tr('DATEPARSE("yyyyMMdd HH:mm", [s])'),
            "(CASE WHEN \"s\" LIKE '% 24:%' THEN to_timestamp(REPLACE(\"s\", ' 24:', ' 00:'), "
            "'yyyyMMdd HH24:MI') + INTERVAL '1' DAY ELSE to_timestamp(\"s\", "
            "'yyyyMMdd HH24:MI') END)")

    def test_datediff(self):
        self.assertEqual(tr('DATEDIFF("day", [d], [d])'), "datediff('day', \"d\", \"d\")")

    def test_inconnue_leve(self):
        with self.assertRaises(CalcError):
            tr("UNKNOWN_FN([a])")


class Introspection(unittest.TestCase):
    def test_has_aggregate(self):
        self.assertTrue(has_aggregate("SUM([a])"))
        self.assertFalse(has_aggregate("[a] + 1"))

    def test_referenced_fields(self):
        self.assertEqual(referenced_fields("[a] + [s]"), ["a", "s"])


class CalculsDeTable(unittest.TestCase):
    def test_lookup_decallage_espace(self):
        # translate() écrit « - 1 » ; LOOKUP doit quand même l'accepter
        self.assertEqual(window_sql("LOOKUP", ['"a"', "- 1"], "1"),
                         'LEAD("a", 1) OVER (ORDER BY 1)')

    def test_lookup_decallage_invalide(self):
        with self.assertRaises(CalcError):
            window_sql("LOOKUP", ['"a"', "x"], "1")


if __name__ == "__main__":
    unittest.main()
