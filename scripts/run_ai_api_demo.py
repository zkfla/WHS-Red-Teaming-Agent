"""Exercise the hybrid planner through a real local OpenAI-compatible HTTP server."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "examples" / "vulnerable_demo.py"
SECRET = "0123456789abcdef0123456789abcdef"


class PlannerApiHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("content-length", "0"))
        request = json.loads(self.rfile.read(length))
        content = request["messages"][1]["content"]
        context = json.loads(content)
        candidates = context.get("candidates", [])
        selected = []
        if candidates:
            selected.append(
                {
                    "candidate_id": candidates[0]["candidate_id"],
                    "action": "run_sqli_safe",
                    "reason": "Local compatibility API selected the first bounded candidate.",
                    "confidence": 0.9,
                }
            )
        payload = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "schema_version": "planner-decision-v1",
                                "selected_actions": selected,
                                "finish": not bool(selected),
                            }
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 42, "completion_tokens": 18},
        }
        encoded = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return


def _wait(url: str) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except (OSError, URLError):
            time.sleep(0.1)
    raise TimeoutError(f"server did not start: {url}")


def _cli(*args: str, env: dict[str, str]) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, "-m", "whs_redteam.cli", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    return json.loads(result.stdout)


def main() -> None:
    environment = os.environ.copy()
    environment["WHS_APPROVAL_SECRET"] = SECRET
    target = subprocess.Popen(
        [sys.executable, str(TARGET)],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    api = ThreadingHTTPServer(("127.0.0.1", 0), PlannerApiHandler)
    api_thread = threading.Thread(target=api.serve_forever, daemon=True)
    api_thread.start()
    api_url = f"http://127.0.0.1:{api.server_port}/v1"
    token_file: Path | None = None
    try:
        _wait("http://127.0.0.1:8765/")
        first = _cli(
            "sqli-scan",
            "http://127.0.0.1:8765/sql?id=1",
            "--allow-private-dns",
            "--planner",
            "hybrid",
            "--ai-base-url",
            api_url,
            "--ai-model",
            "compatibility-model",
            "--output-dir",
            "outputs/ai-api-demo",
            env=environment,
        )
        run_id = str(first["run_id"])
        config_path = str(first["config_path"])
        if first["status"] != "WAITING_APPROVAL":
            raise RuntimeError(f"hybrid demo did not stop for approval: {first}")
        handle = tempfile.NamedTemporaryFile(prefix="whs-ai-", suffix=".token", delete=False)
        token_file = Path(handle.name)
        handle.close()
        _cli(
            "issue-approval",
            config_path,
            "--output",
            str(token_file),
            "--capability",
            "sqli_safe",
            env=environment,
        )
        # The generated profile is value-free and stores the API endpoint/model.
        completed = _cli(
            "sqli-scan",
            "http://127.0.0.1:8765/sql?id=1",
            "--allow-private-dns",
            "--planner",
            "hybrid",
            "--ai-base-url",
            api_url,
            "--ai-model",
            "compatibility-model",
            "--output-dir",
            "outputs/ai-api-demo",
            "--run-id",
            run_id,
            "--token-file",
            str(token_file),
            env=environment,
        )
        report = json.loads(Path(str(completed["findings_path"])).read_text(encoding="utf-8"))
        planning = report["planning"]
        if planning["provider"] != "openai_compatible" or planning["api_calls"] < 1:
            raise RuntimeError(f"AI planner provenance missing: {planning}")
        if not any(item.get("category") == "sqli" for item in report["findings"]):
            raise RuntimeError("hybrid API demo did not confirm SQLi")
        print(json.dumps({"status": completed["status"], "planner": planning}, indent=2))
    finally:
        if token_file is not None:
            token_file.unlink(missing_ok=True)
        api.shutdown()
        api.server_close()
        if target.poll() is None:
            target.terminate()
            target.wait(timeout=3)


if __name__ == "__main__":
    main()
