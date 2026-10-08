"""Generated Python executes only in an operator-pinned, networkless Docker.

The command MCP intentionally forbids Python; do not widen its executable
allowlist for this different contract. Only disposable fixture files mount.
"""

import asyncio
import json
import os
import tempfile
import time
import uuid
from pathlib import Path

from redteam.core.ports.fixture_runner import (
    FixturePolicy,
    FixtureProgram,
    FixtureResult,
)

_HARNESS = '''import importlib.util, json
spec = importlib.util.spec_from_file_location("candidate", "/fixture/candidate.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
inputs = json.load(open("/fixture/inputs.json"))
observed = [module.evaluate(value) for value in inputs]
print(json.dumps({"observed": observed}, allow_nan=False))
'''


class _OutputLimit(Exception):
    pass


class DockerFixtureRunner:
    def __init__(self, policy: FixturePolicy, *, docker_binary: str = "docker"):
        if not docker_binary or "\x00" in docker_binary:
            raise ValueError("fixture_docker_binary_invalid")
        self._policy = policy
        self._binary = docker_binary

    @property
    def policy(self) -> FixturePolicy:
        return self._policy

    def arguments(self, directory: str, name: str) -> tuple[str, ...]:
        return (
            self._binary, "run", "--rm", "--pull=never", "--name", name,
            "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--user", "65534:65534",
            "--pids-limit", "32", "--memory", "128m", "--cpus", "0.5",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=8388608",
            "--mount", f"type=bind,src={directory},dst=/fixture,readonly",
            "--workdir", "/fixture", "--entrypoint", "/usr/local/bin/python",
            self.policy.image, "-I", "-B", "/fixture/harness.py",
        )

    async def _remove(self, name: str) -> None:
        # Exact UUID owned by this invocation; never prune other containers.
        deadline = time.monotonic() + 5

        async def command(*args) -> int:
            process = await asyncio.create_subprocess_exec(
                self._binary, *args,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                return await asyncio.wait_for(
                    process.wait(), max(0.01, deadline - time.monotonic()),
                )
            finally:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()

        try:
            await command("rm", "-f", name)
            # --rm can race with explicit removal: wait for absence, not rm exit wording.
            while await command("inspect", name) == 0:
                if time.monotonic() >= deadline:
                    raise TimeoutError
                await asyncio.sleep(0.05)
        except TimeoutError:
            raise RuntimeError("fixture_cleanup_unconfirmed") from None
        except OSError:
            pass

    async def run(self, program: FixtureProgram) -> FixtureResult:
        # Serialize before writing; unbounded nested fixture input is rejected.
        cases = json.dumps([c.model_dump() for c in program.cases], allow_nan=False)
        if len(cases.encode()) > 32768:
            raise ValueError("fixture_inputs_oversized")
        total = len(program.cases)
        # Expected answers and the verdict stay on the trusted host. Sandbox stdout
        # is untrusted candidate output, never a self-reported passed/total count.
        inputs = json.dumps([c.input for c in program.cases], allow_nan=False)
        name = "redteam-fixture-" + uuid.uuid4().hex
        process = None
        readers: list[asyncio.Task] = []
        with tempfile.TemporaryDirectory(prefix="redteam-fixture-") as directory:
            # The unprivileged container needs read access to these files only.
            os.chmod(directory, 0o755)
            for filename, data in (("candidate.py", program.source), ("inputs.json", inputs),
                                   ("harness.py", _HARNESS)):
                path = Path(directory) / filename
                with path.open("x", encoding="utf-8") as output:
                    output.write(data)
                path.chmod(0o444)
            used = 0

            async def read(stream) -> bytes:
                nonlocal used
                content = bytearray()
                while chunk := await stream.read(1024):
                    used += len(chunk)
                    if used > self.policy.max_output_bytes:
                        raise _OutputLimit
                    content.extend(chunk)
                return bytes(content)

            try:
                process = await asyncio.create_subprocess_exec(
                    *self.arguments(directory, name), stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env={key: os.environ[key] for key in ("PATH", "HOME", "DOCKER_HOST",
                         "DOCKER_CONTEXT", "DOCKER_CONFIG") if key in os.environ},
                )
                readers = [asyncio.create_task(read(process.stdout)),
                           asyncio.create_task(read(process.stderr))]
                stdout, _, code = await asyncio.wait_for(
                    asyncio.gather(*readers, process.wait()), self.policy.timeout_seconds,
                )
                if code in (125, 126, 127):
                    return FixtureResult(status="unavailable", total=total)
                if code != 0:
                    return FixtureResult(status="failed", total=total)
                observed = json.loads(stdout)
                if not isinstance(observed, dict) or set(observed) != {"observed"}:
                    return FixtureResult(status="failed", total=total)
                outputs = observed["observed"]
                if not isinstance(outputs, list) or len(outputs) != total:
                    return FixtureResult(status="failed", total=total)
                # Canonical JSON comparison distinguishes true from 1, rejects
                # non-finite values, and is independent of dictionary key order.
                def canonical(value):
                    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                                      allow_nan=False)

                passed = sum(canonical(value) == canonical(case.expected)
                             for value, case in zip(outputs, program.cases, strict=True))
                return FixtureResult(status="passed" if passed == total else "failed",
                                     passed=passed, total=total)
            except TimeoutError:
                return FixtureResult(status="timed_out", total=total)
            except _OutputLimit:
                return FixtureResult(status="output_limit", total=total)
            except OSError:
                return FixtureResult(status="unavailable", total=total)
            except (ValueError, TypeError):
                return FixtureResult(status="failed", total=total)
            finally:
                for reader in readers:
                    if not reader.done():
                        reader.cancel()
                if readers:
                    await asyncio.gather(*readers, return_exceptions=True)
                if process is not None:
                    if process.returncode is None:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                    await process.wait()
                await asyncio.shield(self._remove(name))
