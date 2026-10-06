import sys
import unittest

sys.path.insert(0, "src")

from redteaming.detection import AccessControlDetector
from redteaming.detector_registry import DetectorRegistry
from redteaming.runtime import LocalRuntime
from redteaming.models import Scope


class DetectorRegistryTests(unittest.TestCase):
    def test_task_selects_registered_detectors_dynamically(self):
        detector = AccessControlDetector()
        registry = DetectorRegistry((detector,))
        self.assertEqual(registry.resolve(("access-control",)), [detector])

    def test_unknown_detector_is_rejected(self):
        registry = DetectorRegistry((AccessControlDetector(),))
        with self.assertRaises(KeyError):
            registry.resolve(("not-configured",))

    def test_runtime_registration_refreshes_hypothesis_agents(self):
        class CustomDetector:
            name = "custom-passive"
            category = "custom"

            def detect(self, observations):
                return []

        runtime = LocalRuntime()
        runtime.register_detector(CustomDetector())
        self.assertIn("custom-passive", {agent.name for agent in runtime.hypothesis_orchestrator.agents})

    def test_runtime_rejects_unregistered_detector_subset(self):
        runtime = LocalRuntime()
        with self.assertRaises(KeyError):
            runtime.run_http_assessment(
                Scope(("approved.test",)), [], detector_names=("not-configured",)
            )


if __name__ == "__main__":
    unittest.main()
