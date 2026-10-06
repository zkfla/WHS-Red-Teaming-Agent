import sys
import unittest

sys.path.insert(0, "src")


class CompatibilityTests(unittest.TestCase):
    def test_historical_package_name_imports(self):
        import redteam
        import redteaming

        self.assertIsNotNone(redteam)
        self.assertIsNotNone(redteaming)


if __name__ == "__main__":
    unittest.main()
