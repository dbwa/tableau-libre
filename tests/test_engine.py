"""Auto-vérification du moteur : LOD, calculs de table, groupes, bins, sets, filtres."""
import os
import tempfile
import unittest

from tableauhyperapi import (Connection, CreateMode, Date, HyperProcess, Inserter,
                             SqlType, TableDefinition, TableName, Telemetry)

from reader import (_apply_custom_tooltip, Sheet, ShelfItem, open_workbook,
                    treemap_spec, vega_spec, TWBXError)

TWB = """<?xml version='1.0' encoding='utf-8'?>
<workbook>
 <datasources>
  <datasource name='Parameters'>
   <column name='[p]' caption='P' datatype='date' value='#2024-01-01#'/>
  </datasource>
  <datasource caption='Test' name='ds'>
   <connection class='hyper' dbname='test.hyper'/>
   <column name='[cat]' caption='Catégorie' datatype='string' role='dimension'/>
   <column name='[sub]' caption='Sous' datatype='string' role='dimension'/>
   <column name='[x]' caption='X' datatype='real' role='measure' default-format='n0.0'/>
   <column name='[d]' caption='Date' datatype='date' role='dimension'/>
   <column name='[LOD]' caption='Total catégorie' datatype='real' role='measure'>
     <calculation class='tableau' formula='{FIXED [cat] : SUM([x])}'/>
   </column>
   <column name='[LOD_INC]' caption='Inclure sous' datatype='real' role='measure'>
     <calculation class='tableau' formula='{INCLUDE [sub] : SUM([x])}'/>
   </column>
   <column name='[LOD_EXC]' caption='Exclure sous' datatype='real' role='measure'>
     <calculation class='tableau' formula='{EXCLUDE [sub] : SUM([x])}'/>
   </column>
   <column name='[RUN]' caption='Cumul' datatype='real' role='measure'>
     <calculation class='tableau' formula='RUNNING_SUM(SUM([x]))'/>
   </column>
   <column name='[RANK]' caption='Rang' datatype='real' role='measure'>
     <calculation class='tableau' formula='RANK(SUM([x]))'/>
   </column>
   <column name='[IN_SET]' caption='Dans A' datatype='boolean' role='dimension'>
     <calculation class='tableau' formula='[cat] IN [SetA]'/>
   </column>
   <column name='[BIN]' caption='X bin' datatype='real' role='dimension'>
     <bin level='[x]' size='10' peg='0'/>
   </column>
   <column name='[GRP]' caption='Groupe' datatype='string' role='dimension'>
     <group>
       <groupfilter function='member' level='[cat]' member='&quot;A&quot;' caption='Alpha'/>
     </group>
   </column>
   <group name='[SetA]' caption='A seulement'>
     <groupfilter function='union'>
       <groupfilter function='member' level='[cat]' member='&quot;A&quot;'/>
     </groupfilter>
   </group>
  </datasource>
 </datasources>
 <worksheets>
  <worksheet name='BarCount'>
   <table>
    <view><datasources><datasource name='ds'/></datasources></view>
    <panes><pane><mark class='Automatic'/></pane></panes>
    <rows>[ds].[none:cat:nk]</rows>
   </table>
  </worksheet>
  <worksheet name='TextCount'>
   <table>
    <view><datasources><datasource name='ds'/></datasources></view>
    <panes><pane><mark class='Automatic'/><encodings><text column='[ds].[sum:x:qk]'/></encodings></pane></panes>
    <rows>[ds].[none:cat:nk]</rows>
   </table>
  </worksheet>
  <worksheet name='Treemap'>
   <table>
    <view><datasources><datasource name='ds'/></datasources></view>
    <panes><pane><mark class='Square'/><encodings><text column='[ds].[none:cat:nk]'/><size column='[ds].[sum:x:qk]'/></encodings></pane></panes>
   </table>
  </worksheet>
  <worksheet name='TwoMeasures'>
   <table>
    <view><datasources><datasource name='ds'/></datasources>
     <filter class='categorical' column='[ds].[:Measure Names]'>
      <groupfilter function='union'>
       <groupfilter function='member' level='[:Measure Names]' member='&quot;[ds].[sum:x:qk]&quot;'/>
      </groupfilter>
     </filter>
    </view>
    <panes><pane><mark class='Automatic'/></pane></panes>
    <rows>[ds].[sum:x:qk],[ds].[avg:x:qk]</rows>
   </table>
  </worksheet>
 </worksheets>
</workbook>
"""


def build(directory: str) -> None:
    hyper = os.path.join(directory, "test.hyper")
    with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hp:
        with Connection(endpoint=hp.endpoint, database=hyper,
                        create_mode=CreateMode.CREATE_AND_REPLACE) as c:
            table = TableDefinition(TableName("public", "Extract"), [
                TableDefinition.Column("cat", SqlType.text()),
                TableDefinition.Column("sub", SqlType.text()),
                TableDefinition.Column("x", SqlType.double()),
                TableDefinition.Column("d", SqlType.date()),
            ])
            c.catalog.create_table(table)
            with Inserter(c, table) as ins:
                ins.add_rows([
                    ["A", "a1", 10.0, Date(2024, 1, 1)],
                    ["A", "a2", 20.0, Date(2024, 1, 2)],
                    ["B", "b1", 5.0, Date(2024, 1, 1)],
                    ["B", "b2", 15.0, Date(2024, 1, 2)],
                ])
                ins.execute()
    with open(os.path.join(directory, "test.twb"), "w", encoding="utf-8") as fh:
        fh.write(TWB)


class Engine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        build(cls.tmp.name)
        cls.wb = open_workbook(os.path.join(cls.tmp.name, "test.twb"), cls.tmp.name)
        cls.src = cls.wb.sources["ds"]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def q(self, *fields, filters=None):
        items = [ShelfItem("ds", "none", f, "nk") if f in ("cat", "sub", "BIN", "GRP", "IN_SET")
                 else ShelfItem("ds", "usr" if f in ("LOD", "LOD_INC", "LOD_EXC", "RUN", "RANK")
                                else "sum", f, "qk")
                 for f in fields]
        names, rows = self.wb.query(self.src, items, filters or [])
        return [dict(zip(names, r)) for r in rows]

    def test_lod_fixed(self):
        rows = self.q("cat", "LOD")
        got = {r["cat"]: r["usr:LOD"] for r in rows}
        self.assertEqual(got, {"A": 30.0, "B": 20.0})

    def test_lod_include_exclude(self):
        rows = self.q("cat", "sub", "LOD_INC", "LOD_EXC")
        inc = {r["sub"]: r["usr:LOD_INC"] for r in rows}
        exc = {r["sub"]: r["usr:LOD_EXC"] for r in rows}
        self.assertEqual(inc, {"a1": 10.0, "a2": 20.0, "b1": 5.0, "b2": 15.0})
        self.assertEqual(exc, {"a1": 30.0, "a2": 30.0, "b1": 20.0, "b2": 20.0})

    def test_table_calc_running_and_rank(self):
        rows = self.q("cat", "RUN", "RANK")
        got = {r["cat"]: (r["usr:RUN"], r["usr:RANK"]) for r in rows}
        self.assertEqual(got, {"A": (30.0, 1), "B": (50.0, 2)})

    def test_bin_and_group(self):
        rows = self.q("BIN", "x")
        bins = {r["BIN"]: r["sum:x"] for r in rows}
        self.assertEqual(bins, {0.0: 5.0, 10.0: 25.0, 20.0: 20.0})
        rows = self.q("GRP", "x")
        groups = {r["GRP"]: r["sum:x"] for r in rows}
        self.assertEqual(groups, {"Alpha": 30.0, "Autre": 20.0})

    def test_set_membership(self):
        rows = self.q("IN_SET", "x")
        got = {bool(r["IN_SET"]): r["sum:x"] for r in rows}
        self.assertEqual(got, {True: 30.0, False: 20.0})

    def test_range_filter(self):
        rows = self.q("sub", "x", filters=[{"field": "x", "class": "quantitative",
                                            "range": {"min": "10", "max": "20"}}])
        self.assertEqual(sorted(r["sub"] for r in rows), ["a1", "a2", "b2"])

    def test_top_filter(self):
        rows = self.q("cat", "x", filters=[{"field": "cat", "class": "categorical",
                                            "members": [], "exclude": [], "nonnull": False,
                                            "top": {"n": 1, "by": "sum:x",
                                                    "direction": "DESC"}}])
        self.assertEqual([r["cat"] for r in rows], ["A"])


    def test_agregations_cnt_ctd(self):
        items = [ShelfItem("ds", "none", "cat", "nk"),
                 ShelfItem("ds", "cnt", "cat", "qk"),
                 ShelfItem("ds", "ctd", "sub", "qk")]
        names, rows = self.wb.query(self.src, items, [])
        got = {r[0]: (r[1], r[2]) for r in rows}
        self.assertEqual(got, {"A": (2, 2), "B": (2, 2)})

    def test_quartiles(self):
        items = [ShelfItem("ds", "none", "cat", "nk"),
                 ShelfItem("ds", "quart1", "x", "qk"),
                 ShelfItem("ds", "quart3", "x", "qk")]
        names, rows = self.wb.query(self.src, items, [])
        got = {r[0]: (r[1], r[2]) for r in rows}
        self.assertAlmostEqual(got["A"][0], 12.5)
        self.assertAlmostEqual(got["A"][1], 17.5)
        self.assertAlmostEqual(got["B"][0], 7.5)
        self.assertAlmostEqual(got["B"][1], 12.5)

    def test_parts_de_date(self):
        items = [ShelfItem("ds", "yr", "d", "ok"),
                 ShelfItem("ds", "mn", "d", "ok"),
                 ShelfItem("ds", "tdy", "d", "qk"),
                 ShelfItem("ds", "sum", "x", "qk")]
        names, rows = self.wb.query(self.src, items, [])
        self.assertIn("d (year)", names)
        self.assertIn("d (month)", names)
        self.assertIn("d (day trunc)", names)
        rec = dict(zip(names, rows[0]))
        self.assertEqual(rec["d (year)"], 2024)
        self.assertEqual(rec["d (month)"], 1)

    def test_filtre_mdy(self):
        rows = self.q("sub", "x", filters=[{
            "field": "d (date)", "class": "categorical",
            "members": ["20240101"], "exclude": [], "nonnull": False}])
        self.assertEqual(sorted(r["sub"] for r in rows), ["a1", "b1"])

    def test_filtre_expression(self):
        rows = self.q("sub", "x", filters=[{
            "field": "d", "class": "categorical", "members": [],
            "exclude": [], "nonnull": False,
            "expression": "[d] > [Parameters].[p]"}])
        self.assertEqual(sorted(r["sub"] for r in rows), ["a2", "b2"])

    def test_compte_implicite_query(self):
        items = [ShelfItem("ds", "none", "cat", "nk"),
                 ShelfItem("ds", "cnt", "__records", "qk")]
        names, rows = self.wb.query(self.src, items, [])
        self.assertIn("cnt:__records", names)
        self.assertEqual(sorted(r[1] for r in rows), [2, 2])

    def test_compte_implicite_render(self):
        bar = self.wb.render_sheet_by_name("BarCount")
        self.assertEqual(bar["kind"], "chart")
        self.assertIn("cnt:__records", bar["columns"])

    def test_table_texte(self):
        text = self.wb.render_sheet_by_name("TextCount")
        self.assertEqual(text["kind"], "table")
        self.assertIn("sum:x", text["columns"])

    def test_filtre_measure_names(self):
        two = self.wb.render_sheet_by_name("TwoMeasures")
        self.assertIn("sum:x", two["columns"])
        self.assertNotIn("avg:x", two["columns"])

    def test_info_bulle_personnalisee(self):
        s = Sheet(name="m", tooltip="Jour: <[ds].[none:cat:nk]>")
        spec = vega_spec(
            s, [ShelfItem("ds", "none", "cat", "nk")],
            [ShelfItem("ds", "sum", "x", "qk")], ["cat", "sum:x"],
            [{"cat": "A", "sum:x": 1}])
        spec = _apply_custom_tooltip(spec, s, ["cat", "sum:x"])
        self.assertEqual(spec["encoding"]["tooltip"]["field"], "__tooltip")
        self.assertIn('datum["cat"]', spec["transform"][0]["calculate"])

    def test_treemap_spec(self):
        s = Sheet(name="t")
        spec = treemap_spec(s, ShelfItem("ds", "none", "cat", "nk"), "sum:x", None,
                            ["cat", "sum:x"],
                            [{"cat": "A", "sum:x": 30}, {"cat": "B", "sum:x": 20}],
                            {}, {}, {})
        self.assertEqual(len(spec["data"]["values"]), 2)
        self.assertEqual(spec["data"]["values"][0]["__x"], 0)

    def test_treemap_render(self):
        tm = self.wb.render_sheet_by_name("Treemap")
        self.assertEqual(tm["kind"], "chart")
        self.assertIn("layer", tm["spec"])
        self.assertEqual(len(tm["spec"]["data"]["values"]), 2)

    def test_multi_mesures_spec(self):
        s = Sheet(name="m")
        spec = vega_spec(
            s, [ShelfItem("ds", "none", "cat", "nk")],
            [ShelfItem("ds", "sum", "x", "qk"), ShelfItem("ds", "avg", "x", "qk")],
            ["cat", "sum:x", "avg:x"],
            [{"cat": "A", "sum:x": 1, "avg:x": 2}])
        self.assertEqual(spec["encoding"]["color"]["field"], "__measure")
        self.assertEqual(spec["mark"]["type"], "bar")

    def test_parametres_restaures(self):
        old = dict(self.wb.params)
        self.wb.render_sheet_by_name("BarCount", params={"p": "2025-01-02"})
        self.assertEqual(self.wb.params, old)

    def test_entrees_invalides(self):
        with self.assertRaises(TWBXError):
            self.wb.render_sheet_by_name("BarCount", params=[])
        with self.assertRaises(TWBXError):
            self.wb.render_sheet_by_name("BarCount", filters="x")


if __name__ == "__main__":
    unittest.main()
