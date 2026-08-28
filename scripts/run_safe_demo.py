"""Run the safe negative-control demo through approval and final reporting."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "safe-demo.yaml"
SECRET = "0123456789abcdef0123456789abcdef"


def cli(*arguments: str, env: dict[str, str]) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, "-m", "whs_redteam.cli", *arguments],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    return json.loads(result.stdout)


def wait_for_demo() -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with urlopen("http://127.0.0.1:8766/", timeout=0.5) as response:
                if response.status == 200:
                    return
        except (OSError, URLError):
            time.sleep(0.1)
    raise TimeoutError("safe demo did not start")


def main() -> None:
    environment = os.environ.copy()
    environment["WHS_SAFE_DEMO_APPROVAL_SECRET"] = SECRET
    demo = subprocess.Popen(
        [sys.executable, str(ROOT / "examples" / "safe_demo.py")],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    token_file: Path | None = None
    try:
        wait_for_demo()
        run = cli("run", str(CONFIG), env=environment)
        run_id = str(run["run_id"])
        actions = cli("pending-approvals", str(CONFIG), run_id, env=environment)["actions"]
        if not isinstance(actions, list) or not actions:
            raise RuntimeError("safe demo produced no approval-gated actions")
        handle = tempfile.NamedTemporaryFile(prefix="whs-safe-", suffix=".token", delete=False)
        token_file = Path(handle.name)
        handle.close()
        cli(
            "issue-approval",
            str(CONFIG),
            "--output",
            str(token_file),
            "--capability",
            "sqli_safe",
            "--capability",
            "xss_safe",
            "--max-uses",
            str(len(actions)),
            env=environment,
        )
        for action in actions:
            cli(
                "approve",
                str(CONFIG),
                run_id,
                str(action["queue_id"]),
                "--token-file",
                str(token_file),
                env=environment,
            )
        completed = cli("resume", str(CONFIG), run_id, env=environment)
        report = json.loads(Path(str(completed["findings_path"])).read_text(encoding="utf-8"))
        decision = report["exploitability_decision"]
        confirmed_injection = [
            item
            for item in report["findings"]
            if item["status"] == "CONFIRMED"
            and item["category"] in {"sqli", "xss_reflection"}
        ]
        if decision["bounded_exploit_demonstrated"] or confirmed_injection:
            raise RuntimeError(
                f"safe demo unexpectedly demonstrated an injection primitive: {decision}"
            )
        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "status": completed["status"],
                    "verdict": decision["verdict"],
                    "confirmed_injection_findings": len(confirmed_injection),
                },
                indent=2,
            )
        )
    finally:
        if token_file is not None:
            token_file.unlink(missing_ok=True)
        if demo.poll() is None:
            demo.terminate()
            demo.wait(timeout=3)


if __name__ == "__main__":
    main()
