import tempfile
import unittest
from unittest.mock import Mock
from leanwatts_can.production_flow import ProductionFlow
from leanwatts_can.session_store import SessionStore
from leanwatts_can.tests.test_production_sessions import production_recipe


class SerialBackendTests(unittest.TestCase):
    def test_invalid_serial_rejected_before_factory_for_all_workflows(self):
        for cal, tests in ((True, False), (True, True), (False, True)):
            for serial in ("OTHER", "WO-S00430-1-001\n"):
                factory = Mock(side_effect=AssertionError("Hardware must not open"))
                flow = ProductionFlow(production_recipe(), serial, "OP", 1, factory, factory,
                                      run_calibration=cal, run_tests=tests)
                with self.assertRaises(ValueError):
                    flow.start()
                factory.assert_not_called()

    def test_custom_override_record_is_explicit_and_cannot_be_changed_after_start(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SessionStore(folder)
            store.start(dict(id="OP", name="Operator", enabled=True))
            with self.assertRaises(ValueError):
                store.begin_unit("OTHER", production_recipe(), 1)
            self.assertEqual(store.active()["units"], [])
            attempt = store.begin_unit("OTHER", production_recipe(), 1, serial_override=True)
            with self.assertRaises(ValueError):
                store.finish_unit(attempt, dict(outcome="FAIL", serial_override=False))
            result = store.finish_unit(attempt, dict(outcome="FAIL", serial_override=True))
            self.assertTrue(result["serial_override"])


if __name__ == "__main__":
    unittest.main()
