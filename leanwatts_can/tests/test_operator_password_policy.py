"""Production-lead account boundaries and credential changes; no instruments."""
import json
from pathlib import Path
import tempfile
import unittest

from leanwatts_can.session_store import OperatorStore


class OperatorPasswordPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = OperatorStore(self.temp.name)
        self.store.upsert_operator("ADMIN", "Administrator fixture", "admin-old-fixture", role="admin")
        self.store.upsert_operator("LEAD", "Lead fixture", "lead-old-fixture", role="lead")
        self.store.upsert_operator("OP", "Operator fixture", "operator-old-fixture")

    def tearDown(self):
        self.temp.cleanup()

    def raw(self, operator_id):
        data = json.loads(self.store.path.read_text(encoding="utf-8"))
        return next(item for item in data["operators"] if item["id"] == operator_id)

    def test_lead_can_reset_only_operator_password_and_preserves_account_fields(self):
        before = self.raw("OP")
        result = self.store.reset_operator_password("LEAD", "lead-old-fixture", "OP", "operator-new-fixture")
        after = self.raw("OP")
        self.assertEqual(result, {key: before[key] for key in ("id", "name", "role", "enabled")})
        self.assertNotEqual(before["salt"], after["salt"])
        self.assertNotEqual(before["password_hash"], after["password_hash"])
        self.assertEqual(after["iterations"], self.store.ITERATIONS)
        self.assertEqual(self.store.authenticate("OP", "operator-new-fixture")["role"], "operator")
        with self.assertRaises(ValueError):
            self.store.authenticate("OP", "operator-old-fixture")

    def test_lead_and_admin_cannot_reset_lead_or_admin_through_operator_reset(self):
        original = self.store.path.read_bytes()
        for actor, password in (("LEAD", "lead-old-fixture"), ("ADMIN", "admin-old-fixture")):
            for target in ("LEAD", "ADMIN"):
                with self.subTest(actor=actor, target=target), self.assertRaisesRegex(ValueError, "only available for operator"):
                    self.store.reset_operator_password(actor, password, target, "replacement-fixture")
        self.assertEqual(self.store.path.read_bytes(), original)

    def test_actor_password_and_role_are_verified_before_reset(self):
        original = self.store.path.read_bytes()
        for actor, password in (("LEAD", "wrong-password"), ("OP", "operator-old-fixture")):
            with self.subTest(actor=actor), self.assertRaises(ValueError):
                self.store.reset_operator_password(actor, password, "OP", "replacement-fixture")
        self.assertEqual(self.store.path.read_bytes(), original)

    def test_own_lead_password_requires_current_password(self):
        original = self.store.path.read_bytes()
        with self.assertRaises(ValueError):
            self.store.change_own_password("LEAD", "wrong-current", "lead-new-fixture")
        self.assertEqual(self.store.path.read_bytes(), original)
        result = self.store.change_own_password("LEAD", "lead-old-fixture", "lead-new-fixture")
        self.assertEqual(result["role"], "lead")
        self.assertEqual(self.store.authenticate("LEAD", "lead-new-fixture")["id"], "LEAD")
        with self.assertRaises(ValueError):
            self.store.authenticate("LEAD", "lead-old-fixture")

    def test_upsert_cannot_reset_existing_lead_password_and_blank_keeps_hash(self):
        before = self.raw("LEAD")
        for role in ("lead", "operator", "admin"):
            with self.subTest(role=role), self.assertRaisesRegex(ValueError, "current password"):
                self.store.upsert_operator("LEAD", "New name", "lead-reset-fixture", role=role)
        self.store.upsert_operator("LEAD", "Updated lead name", "", role="lead")
        after = self.raw("LEAD")
        self.assertEqual(after["password_hash"], before["password_hash"])
        self.assertEqual(after["salt"], before["salt"])
        self.assertEqual(after["name"], "Updated lead name")

    def test_no_password_is_stored_or_returned_in_plaintext(self):
        result = self.store.reset_operator_password("ADMIN", "admin-old-fixture", "OP", "operator-new-fixture")
        self.store.change_own_password("LEAD", "lead-old-fixture", "lead-new-fixture")
        serialized = self.store.path.read_text(encoding="utf-8")
        for password in ("admin-old-fixture", "lead-old-fixture", "lead-new-fixture", "operator-new-fixture"):
            self.assertNotIn(password, serialized)
        self.assertEqual(set(result), {"id", "name", "role", "enabled"})
        for public in self.store.list_operators():
            self.assertEqual(set(public), {"id", "name", "role", "enabled"})

    def test_lead_password_protection_survives_role_changes_and_legacy_records(self):
        # Simulate a lead record written before the policy flag was introduced.
        original = json.loads(self.store.path.read_text(encoding="utf-8"))
        for record in original["operators"]:
            record.pop("self_managed_password", None)
        self.store.path.write_text(json.dumps(original), encoding="utf-8")
        self.store.upsert_operator("LEAD", "Former lead", "", role="operator")
        self.assertTrue(self.raw("LEAD")["self_managed_password"])
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "current password"):
            self.store.reset_operator_password("ADMIN", "admin-old-fixture", "LEAD", "bypass-fixture")
        with self.assertRaisesRegex(ValueError, "current password"):
            self.store.upsert_operator("LEAD", "Former lead", "bypass-fixture", role="operator")
        self.assertEqual(self.store.path.read_bytes(), before)
        self.store.change_own_password("LEAD", "lead-old-fixture", "own-change-fixture")
        self.assertEqual(self.store.authenticate("LEAD", "own-change-fixture")["role"], "operator")
        self.assertTrue(self.raw("LEAD")["self_managed_password"])

    def test_disabled_lead_cannot_reset_and_disabled_operator_stays_disabled(self):
        self.store.upsert_operator("OP", "Operator fixture", "", enabled=False)
        self.store.reset_operator_password("LEAD", "lead-old-fixture", "OP", "new-disabled-fixture")
        self.assertFalse(self.raw("OP")["enabled"])
        self.store.upsert_operator("LEAD", "Lead fixture", "", enabled=False, role="lead")
        with self.assertRaises(ValueError):
            self.store.reset_operator_password("LEAD", "lead-old-fixture", "OP", "another-fixture")

    def test_first_account_and_last_enabled_admin_rules_are_retained(self):
        with tempfile.TemporaryDirectory() as folder:
            empty = OperatorStore(folder)
            with self.assertRaisesRegex(ValueError, "first account"):
                empty.upsert_operator("LEAD", "Lead", "lead-fixture", role="lead")
        for role in ("lead", "operator"):
            with self.subTest(role=role), self.assertRaisesRegex(ValueError, "enabled administrator"):
                self.store.upsert_operator("ADMIN", "Administrator fixture", "", role=role)
        with self.assertRaises(ValueError):
            self.store.delete_operator("ADMIN")
        self.assertEqual(self.store.authenticate("ADMIN", "admin-old-fixture")["role"], "admin")


if __name__ == "__main__":
    unittest.main()
