import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, "src")

from redteaming.capability.process import ProcessResult
from redteaming.graphs import AssetGraph, EvidenceStore, ExplorationGraph
from redteaming.models import Scope
from redteaming.recon.runner import PortReconRunner


class ReconTests(unittest.TestCase):
    def test_runner_rejects_out_of_scope_target(self):
        runner = PortReconRunner(AssetGraph(), ExplorationGraph(), EvidenceStore())
        with self.assertRaises(ValueError):
            runner.run("other.example.test", Scope(("scan.example.test",)), ports=range(1, 2))

    def test_runner_persists_host_and_evidence(self):
        runner = PortReconRunner(AssetGraph(), ExplorationGraph(), EvidenceStore())
        result = runner.run("localhost", Scope(("localhost",)), ports=range(1, 2))
        self.assertEqual(result.assets[0].kind, "host")
        self.assertEqual(len(runner.evidence.items), 1)
        self.assertTrue(runner.exploration.anchors)

    def test_scope_exclusion_blocks_external_target(self):
        runner = PortReconRunner(AssetGraph(), ExplorationGraph(), EvidenceStore())
        scope = Scope(("staging.example.com",), excluded_hosts=("staging.example.com",))
        with self.assertRaises(ValueError):
            runner.run("staging.example.com", scope, ports=range(1, 2))

    def test_scope_matches_scheme_and_default_port_per_host(self):
        scope = Scope(("http://http-only.test", "https://secure-only.test:443"))
        self.assertTrue(scope.allows("http://http-only.test/"))
        self.assertFalse(scope.allows("https://http-only.test/"))
        self.assertTrue(scope.allows("https://secure-only.test/"))
        self.assertFalse(scope.allows("http://secure-only.test:443/"))

    def test_host_port_scope_limits_scan_to_declared_port(self):
        class FakeRunner(PortReconRunner):
            def __init__(self):
                super().__init__(AssetGraph(), ExplorationGraph(), EvidenceStore())
                self.seen = None

            def run(self, target, scope, ports=()):
                self.seen = (target, scope.allowed_hosts)
                return super().run(target, scope, ports=(1,))

        scope = Scope(("http://127.0.0.1:8000",))
        self.assertTrue(scope.allows("http://127.0.0.1:8000/"))
        self.assertFalse(scope.allows("https://127.0.0.1:8000/"))
        self.assertFalse(scope.allows("http://127.0.0.1:8001/"))

    def test_runner_resumes_from_completed_port_checkpoint(self):
        runner = PortReconRunner(AssetGraph(), ExplorationGraph(), EvidenceStore())
        scope = Scope(("localhost",))
        runner.run("localhost", scope, ports=range(1, 2))
        resumed = runner.run("localhost", scope, ports=range(1, 2))
        self.assertTrue(resumed.evidence.metadata.get("resumed"))

    def test_checkpoint_survives_runner_recreation_without_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            assets = AssetGraph()
            exploration = ExplorationGraph()
            evidence = EvidenceStore()
            first = PortReconRunner(assets, exploration, evidence, checkpoint_path=checkpoint)
            first.run("localhost", Scope(("localhost",)), ports=range(1, 2))
            second = PortReconRunner(AssetGraph(), ExplorationGraph(), EvidenceStore(),
                                     checkpoint_path=checkpoint)
            resumed = second.run("localhost", Scope(("localhost",)), ports=range(1, 2))
            self.assertTrue(resumed.evidence.metadata.get("resumed"))
            self.assertNotIn("Authorization", checkpoint.read_text(encoding="utf-8"))

    def test_exact_port_checkpoint_isolated_from_host_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            runner = PortReconRunner(AssetGraph(), ExplorationGraph(), EvidenceStore(),
                                     checkpoint_path=checkpoint)
            runner.run("localhost", Scope(("localhost",)), ports=range(1, 2))
            runner.run("localhost:2", Scope(("localhost:2",)), ports=range(1, 2))
            payload = checkpoint.read_text(encoding="utf-8")
            self.assertIn('"localhost"', payload)
            self.assertIn('"localhost:2"', payload)


if __name__ == "__main__":
    unittest.main()
