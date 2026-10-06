import sys
import unittest

sys.path.insert(0, "src")

from redteaming.capability.process import ProcessRunner, render_for_shell


class ProcessRunnerTests(unittest.TestCase):
    def test_shell_rendering_is_available_for_bash_and_powershell(self):
        argv = ["python-port-recon", "80,443", "scan.example.test"]
        self.assertIn("'scan.example.test'", render_for_shell(argv, "powershell"))
        self.assertIn("scan.example.test", render_for_shell(argv, "bash"))

    def test_runner_uses_argv_without_shell(self):
        command = [sys.executable, "-c", "print('ok')"]
        result = ProcessRunner().run(command)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
