"""Comptes, sessions, jetons, partages et espace personnel (SQLite)."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import store  # noqa: E402
import ui  # noqa: E402


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = store.DB_PATH
        store.DB_PATH = Path(self.tmp.name) / "test.db"
        store.init()

    def tearDown(self):
        store.DB_PATH = self.old
        self.tmp.cleanup()

    def test_password_hash(self):
        h = store.hash_password("motdepasse123")
        self.assertTrue(h.startswith("scrypt$"))
        self.assertTrue(store.verify_password("motdepasse123", h))
        self.assertFalse(store.verify_password("mauvais", h))
        self.assertFalse(store.verify_password("motdepasse123", "n'importe quoi"))
        with self.assertRaises(ValueError):
            store.hash_password("court")

    def test_compte_et_session(self):
        user = store.create_user("A@Example.com", "motdepasse123", "Alice")
        self.assertEqual(user["email"], "a@example.com")
        with self.assertRaises(ValueError):
            store.create_user("a@example.com", "motdepasse123")
        self.assertIsNone(store.authenticate("a@example.com", "faux"))
        self.assertEqual(store.authenticate("a@example.com", "motdepasse123")["id"], user["id"])

        token = store.create_session(user["id"])
        self.assertEqual(store.get_session_user(token)["id"], user["id"])
        store.delete_session(token)
        self.assertIsNone(store.get_session_user(token))

    def test_session_expiree(self):
        user = store.create_user("b@example.com", "motdepasse123")
        token = store.create_session(user["id"])
        with store.connect() as conn:
            conn.execute("UPDATE sessions SET expires_at=? WHERE user_id=?", (time.time() - 1, user["id"]))
        self.assertIsNone(store.get_session_user(token))

    def test_jeton_api(self):
        user = store.create_user("c@example.com", "motdepasse123")
        row, token = store.create_api_token(user["id"], "CI")
        self.assertTrue(token.startswith("tbr_"))
        self.assertEqual(store.user_by_api_token(token)["id"], user["id"])
        self.assertIsNone(store.user_by_api_token("tbr_faux"))
        self.assertEqual(len(store.list_api_tokens(user["id"])), 1)
        store.delete_api_token(user["id"], row["id"])
        self.assertIsNone(store.user_by_api_token(token))

    def test_classeurs_et_quota(self):
        user = store.create_user("d@example.com", "motdepasse123")
        wb = store.create_workbook(user["id"], "w_1", "Test", "/tmp/w_1",
                                   ["a.twb", "a.hyper"], 120)
        self.assertEqual(store.user_used(user["id"]), 120)
        self.assertEqual(store.list_workbooks(user["id"])[0]["share_count"], 0)
        store.update_workbook(user["id"], "w_1", name="Renommé", size=80,
                              expires_at=time.time() + 3600)
        got = store.get_workbook(user["id"], "w_1")
        self.assertEqual(got["name"], "Renommé")
        self.assertEqual(got["size_bytes"], 80)
        self.assertEqual(store.user_used(user["id"]), 80)
        self.assertIsNone(store.get_workbook("autre", "w_1"))
        self.assertIsNotNone(store.delete_workbook(user["id"], "w_1"))
        self.assertIsNone(store.get_workbook(user["id"], "w_1"))
        self.assertEqual(store.user_used(user["id"]), 0)

    def test_partages(self):
        user = store.create_user("e@example.com", "motdepasse123")
        store.create_workbook(user["id"], "w_2", "Test", "/tmp/w_2", [], 0)
        share = store.create_share("w_2", label="Client", password="secret123",
                                   allow_embed=False,
                                   expires_at=time.time() + 3600)
        self.assertTrue(share["pw_hash"])
        self.assertEqual(share["allow_embed"], 0)
        self.assertTrue(store.verify_password("secret123", share["pw_hash"]))
        self.assertEqual(store.get_share_by_token(share["token"])["id"], share["id"])
        joined = store.share_with_workbook(share["token"])
        self.assertEqual(joined["workbook_name"], "Test")
        store.touch_share(share["id"])
        self.assertEqual(store.list_shares("w_2")[0]["views"], 1)
        self.assertTrue(store.delete_share("w_2", share["id"]))
        self.assertIsNone(store.get_share_by_token(share["token"]))

    def test_admin_et_conditions_de_partage(self):
        first = store.create_user("admin@example.com", "motdepasse123")
        second = store.create_user("user@example.com", "motdepasse123")
        self.assertEqual(first["is_admin"], 1)
        self.assertEqual(second["is_admin"], 0)
        self.assertEqual(len(store.list_users()), 2)
        self.assertEqual(store.admin_stats()["users"], 2)

        store.create_workbook(first["id"], "w_3", "Test", "/tmp/w_3", [], 0)
        share = store.create_share("w_3", label="Mon menu", require_key=True,
                                   iframe_only=True, ip_whitelist="127.0.0.1",
                                   password="secret123")
        self.assertTrue(share["token"].endswith("-mon-menu"))
        self.assertTrue(share["url_key"])
        self.assertEqual(share["iframe_only"], 1)
        self.assertEqual(share["allow_embed"], 1)
        self.assertEqual(share["ip_whitelist"], "127.0.0.1")

        updated = store.update_share(share["id"], label="Autre",
                                     require_key=False, iframe_only=False,
                                     ip_whitelist="")
        self.assertEqual(updated["label"], "Autre")
        self.assertIsNone(updated["url_key"])
        self.assertEqual(updated["iframe_only"], 0)
        self.assertEqual(updated["ip_whitelist"], "")

    def test_lecteur_public(self):
        sh = "session-hash"
        store.public_add("p_1", sh, "Test", "/tmp/p_1", 42)
        self.assertEqual(store.public_get("p_1")["size_bytes"], 42)
        self.assertEqual(len(store.public_list(sh)), 1)
        store.public_touch("p_1")
        rows = store.public_end_session(sh)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(store.public_get("p_1"))
        self.assertEqual(store.public_list(sh), [])

    def test_dates(self):
        ts = ui.parse_date("2030-01-02")
        self.assertEqual(ui.fmt_date(ts), "2030-01-02")
        self.assertIsNone(ui.parse_date("pas une date"))


if __name__ == "__main__":
    unittest.main()
