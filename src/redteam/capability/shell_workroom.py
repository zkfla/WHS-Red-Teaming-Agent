"""Stateful Docker workroom manager. Owned by Capability Team.

TASK-003: one long-lived container per workroom key, entered with
``docker exec``. The trusted key is derived inside the adapter's
``bind_context`` from the execution identity
(``ExecutionContext.run_id`` + ``instance_id`` via ``workroom_key_for``);
no caller builds the key and a model-supplied key is compare-only, never
trusted. This manager only sees opaque keys and never imports
orchestration code.

Local execution defaults to ``--network none``. The configured admission
extension may bind an explicitly approved gate namespace; adoption verifies
that namespace and all confinement settings before execution.

Labels are disjoint from the execution MCP server (``io.whs-redteam.
execution.*``) so its orphan cleanup never matches a shell workroom.

Timeout reporting is exact, not heuristic: every execution runs under a
provisioned root supervisor that is the single deadline and termination
authority. It enables subreaper BEFORE the model child starts (fail
closed with no record when subreaper is unavailable), drops the child to
the policy uid/gid with zero capabilities plus no-new-privileges via an
explicit verified step, owns the interpreter child in its own session,
enforces the deadline itself (TERM, grace, KILL, reap, child sweep), and
writes a protected result record LAST, after reaping the tree, into a
root-only rundir the script can neither read nor forge. Only two 124
shapes are clean: a genuine completed 124 (trusted completed record with
code 124) is not a timeout, and a trusted timeout 124 (supervisor own
deadline record) is. Every other 124, every outer host timeout without a
verified protected terminal record, every absent/unprotected/unreaped
record, and every code the supervisor never emits is loud uncertainty
(``shell_execution_interrupted``, ``uncertainty=True``), never a clean
result. Every timed-out result carries ``exit_code=None``.

Output is bounded while it is collected, not after the fact: each stream
is drained through its own ``max_output_bytes`` budget, so a chatty script
cannot force the host to buffer an unbounded pipe. ``truncated`` means a
budget was reached. The gateway applies its own per-server serialized
output cut afterwards (default 1MiB, omitted with an error); the shell
budget is the inner bound and normally wins.

Cancelling the host-side wait kills the host ``docker exec`` child and
reaps it; the warm container survives. Container-side teardown goes to
the root supervisor (see ``kill_tagged_exec``): TERM the supervisor
owning the tag (found by its own cmdline plus its protected pgid file,
never by the script scrubbable env), then allow its bounded grace/reap.
Escalation signals the child group/tag carriers, never kills the trusted
subreaper that must collect env-scrubbed/setsid descendants; the supervisor
tracks the child it spawned (subreaper re-parenting plus sweep), so env
scrub and setsid separation in normal programs do not escape teardown.
No container is ever killed, so sibling jobs keep their state;
``--init`` reaps the zombies. Verified teardown reclaims the
``max_jobs`` slot immediately; teardown-unverified cancels hold
``cleanup-unknown`` (slot held) until a deadline-confirmed
supervisor lookup settles them, and supervisor loss after execution may
have started holds ``uncertain`` (slot held, common ``execution_uncertain``
marker) until a supervisor-verified outcome or an authoritative container
teardown. TTL and deadlines alone never free a possibly-live budget.

Mechanism separation: provider-owned docker execs (setup, supervisor
launch, inventory, signaling) resolve helpers via trusted absolute image
paths and a system-only mechanism PATH plus isolated Python, never the
workroom-first model PATH, so an instance-installed ``python3``/``sh``/``tee`` cannot
intercept the provider own mechanism. The confined container privilege
set is exactly ``SETUID``/``SETGID`` (explicit child drop) plus ``KILL``
(teardown signaling of demoted trees) on top of ``--cap-drop ALL``
with ``no-new-privileges`` on every model child; model children run
demoted with zero effective capabilities. Verified live: child id equals
the policy uid/gid, ``CapEff: 0``, ``NoNewPrivs: 1``.

Adoption is verified, never blind: a same-name container is reused only
after ownership plus policy verification (managed/context labels for the
key, live image id, ``--network none``, unprivileged, exactly the
confined cap set with ``ALL`` dropped, ``no-new-privileges``, user,
workdir, and resource bounds); anything else raises
``shell_workroom_adoption_rejected`` before the foreign container
is started, set up, or removed. ``max_workrooms`` is enforced against the
label-filtered ``docker ps -a`` count, not process memory, and creation
serializes the count-plus-create section through a same-host OS file
lock so two managers or processes cannot both slip past the cap. An
unreadable daemon count refuses creation instead of falling back to
memory (fail closed). Different Docker daemons or hosts are out of local
scope. Cross-restart job recovery belongs to TASK-007: jobs live in
adapter process memory, so after a restart every old ``job_id`` is
permanently unknown.

Residual limits (honest, verified where stated): the UID separation is
PROVEN, not excluded - the model child runs demoted to the policy
uid/gid with zero effective capabilities plus no-new-privileges, and
Docker tests prove it cannot kill the supervisor, cannot write the
root-only rundir, and cannot forge a completion record (a workdir-forged
file is unprotected and fails record_trusted; the supervisor writes LAST
to the root-only dir). Proven by test_child_runs_demoted_and_tamper_fails
(slim) and test_kali_child_runs_demoted_and_tamper_fails (Kali).
Remaining honest limits: D-state (unkillable sleep) survivors report loud
uncertainty, never a clean result; the file-lock scope is same-host
only; a stopped container pins its active slots until it is gone or
verified; unknown-arch images are flagged unprotected rather than
trusted; kernel/container-escape level attacks are outside this
mechanism.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import secrets
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from redteam.capability.shell_manifest import SHELL_SERVER_ID
from redteam.capability.shell_policy import WORKROOM_KEY_PATTERN, ShellWorkroomPolicy

SHELL_MANAGED_LABEL = "io.whs-redteam.shell-workroom.managed=true"
SHELL_CONTEXT_LABEL_KEY = "io.whs-redteam.shell-workroom.context"
CONTAINER_NAME_PREFIX = "whs-shell-"
SHELL_LANGUAGES = ("bash", "python3")

_WORKROOM_KEY = re.compile(WORKROOM_KEY_PATTERN)
_CONTAINER_PATH = "/usr/local/bin:/usr/bin:/bin"
# Mechanism PATH: system directories only, never the workroom install
# surface. Every provider-owned docker exec carries it via -e so setup,
# inventory, pip, and signaling binaries cannot be intercepted by an
# instance-installed shadow (python3/sh/tee); model scripts keep the
# workroom-first PATH built in create_argv. The two PATHs stay distinct.
_MECHANISM_PATH = "/usr/local/bin:/usr/bin:/bin"
_MODEL_PATH_ENV = "WHS_MODEL_PATH"
_MECH_PATH_ENV = "WHS_MECH_PATH"
# Tags label one execution inside its container (exec environment, never
# argv) so kill_tagged_exec can find it later. Job ids already fit.
_JOB_TAG = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_JOB_TAG_ENV = "WHS_SHELL_JOB"
_KILL_SCAN_ENV = "WHS_SHELL_KILL"
_KILL_MODE_ENV = "WHS_SHELL_MODE"
_KILL_TIMEOUT_SECONDS = 15.0
_RUN_ID_ENV = "WHS_RUN_ID"
_WORKDIR_ENV = "WHS_WORKDIR"
_LANG_ENV = "WHS_LANG"
_TIMEOUT_ENV = "WHS_TIMEOUT_S"
_GRACE_ENV = "WHS_GRACE_S"
_RUN_UID_ENV = "WHS_RUN_UID"
_RUN_GID_ENV = "WHS_RUN_GID"
_RUNDIR_ENV = "WHS_RUNDIR"
_SPOOL_CAP_ENV = "WHS_SPOOL_CAP"
_INVOCATION_ID_ENV = "WHS_INVOCATION_ID"
_INPUT_DIGEST_ENV = "WHS_INPUT_DIGEST"
_EXTRA_ENV_ENV = "WHS_EXTRA_ENV"
_EXTRA_ENV_JSON_LIMIT = 4096
# Root-only supervisor state directory (0700 root): result and pgid files
# live here, never in the model-writable workdir, so a script can neither
# read nor forge them. Created at setup; adoption repairs it the same way.
_SUPERVISOR_RUNDIR = "/run/whs-shell"
# Extra host-side margin over the supervisor's own deadline plus grace, so
# the supervisor always reports first and the host only stops waiting after
# it. Killing the host child never kills the container side; that stays
# terminate-supervisor work.
_HOST_OUTER_MARGIN_SECONDS = 12.0
# Cross-process creation mutex (same host, same temp dir, same daemon):
# serializes the label-count plus create critical section across
# managers and processes. The OS releases it on crash. Different Docker
# daemons or hosts are out of local scope.
_CREATE_LOCK_FILENAME = "whs-shell-workroom-create.lock"
_ADOPTION_SENTINEL = "WHS_ADOPTION_RECORD"
# Single-inspect adoption record: one `docker inspect` renders Config,
# HostConfig, the LIVE attached networks (.NetworkSettings.Networks),
# and the content image ID as JSON documents plus a sentinel line (the
# sentinel lets the unit fake answer without a daemon). HostConfig
# .NetworkMode alone is not proof: detaching `none` and attaching a
# bridge leaves NetworkMode stale at `none` while the container carries
# a live route, so _verify_adoption requires the live attachments to be
# exactly the admission boundary's expected set (`none` by default).
# Adoption reuses a same-name container only when every
# field checked in _verify_adoption matches the current policy; anything
# else, or an unreadable answer, is rejected before any start/setup
# mutation, so a foreign or reconfigured container is never adopted,
# started, rewritten, or removed. No new label was needed: the existing
# managed/context labels plus these live fields already bind owner, key,
# and policy.
_ADOPTION_FORMAT = (
    "{{json .Config}}\n{{json .HostConfig}}\n"
    "{{json .NetworkSettings.Networks}}\n{{json .Image}}\n" + _ADOPTION_SENTINEL
)
_CREATE_LOCK_TIMEOUT_SECONDS = 60.0
_SETUP_TIMEOUT_SECONDS = 30.0
_STREAM_CHUNK_BYTES = 8192
# Container-side per-execution supervisor. Provisioned into the workroom by
# the manager (no image dependency beyond python3, which the policy fixes
# as an interpreter); the filename carries a version so a newer manager
# never runs a stale copy. The supervisor owns its interpreter child
# (subreaper plus new session plus group kill plus child sweep) and is the
# ONLY completion authority: the result file below is the exact signal the
# host reads, so a fast parent exit never masks a live background child,
# and no argv ever carries a NUL byte.
_SUPERVISOR_VERSION = 2
_SUPERVISOR_FILENAME = ".whs-supervisor-v2.py"
_SUPERVISOR_SOURCE = '''"""Per-execution supervisor: exact completion signal plus measured teardown.
Owns one interpreter child in its own session, enforces the deadline, and
reaps the whole tree before reporting. The result file is the completion
signal: written exactly once, on every terminal path; its outcome and code
fields disambiguate genuine 124/137 exits from kills. A fast parent exit
never masks a live background child: the settle phase reaps the whole
tree before the completion record is written.
Environment in (all via docker exec -e, never argv):
  WHS_WORKDIR   absolute workdir for model files (not supervisor state)
  WHS_RUN_ID    unique id for this execution (result and pgid file names)
  WHS_RUN_UID   policy uid the model child is dropped to (explicit)
  WHS_RUN_GID   policy gid the model child is dropped to (explicit)
  WHS_MODEL_PATH bare-name PATH for the model child only
  WHS_MECH_PATH  system-only PATH used to resolve helper binaries
  WHS_LANG      bash or python3
  WHS_TIMEOUT_S deadline seconds from spawn
  WHS_GRACE_S   escalation grace seconds
Files out (under the root-only rundir, never the workdir):
  .whs-pgid-<id>    child pgid recorded at spawn (external kill reference)
  .whs-result-<id>  JSON outcome completed, timeout, or cancelled, plus
  code, plus protected (drop verified) and unreaped survivors.
Trust model: the supervisor runs as container root while the model
child is demoted to the policy uid/gid with zero capabilities plus
no-new-privileges before exec. The script therefore can neither kill
the supervisor, read its environment, nor write its state files; the
supervisor writes the result LAST (after reaping the tree), so a
forged or stale file can never pass as the outcome. Any drop failure
fails closed with no record, which the host reads as uncertainty.
Exit codes: child code passthrough on completion, 124 on timeout, 142 on
cancel. Stdin is the script (bounded host-side); the child receives it on
a pipe followed by EOF, so scripts cannot rely on post-source stdin.
Stdout and stderr relay through threads and stream while the child runs.
"""
import ctypes
import ctypes.util
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
_CANCEL_REQUESTED = False
def _request_cancel(signum, frame):
    global _CANCEL_REQUESTED
    _CANCEL_REQUESTED = True
def _enable_subreaper():
    try:
        name = ctypes.util.find_library("c")
        libc = ctypes.CDLL(name or "libc.so.6", use_errno=True)
        # 36 is PR_SET_CHILD_SUBREAPER on Linux.
        return libc.prctl(36, 1, 0, 0, 0) == 0
    except Exception:
        return False
def _status_code(status):
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 127
def _live_children():
    me = str(os.getpid())
    found = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return found
    for entry in entries:
        if not entry.isdigit() or entry == me:
            continue
        try:
            with open("/proc/" + entry + "/stat") as handle:
                tail = handle.read().rsplit(")", 1)[-1].split()
            if len(tail) >= 2 and tail[1] == me:
                found.append(int(entry))
        except (OSError, ValueError, IndexError):
            continue
    return found
_PR_SET_NO_NEW_PRIVS = 38


def _demote_to_policy(uid, gid):
    """Drop the calling process to uid/gid with no-new-privileges.

    Raises on any failure: the caller must never exec the model script
    without a verified drop.
    """
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    name = ctypes.util.find_library("c")
    libc = ctypes.CDLL(name or "libc.so.6", use_errno=True)
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError("no-new-privileges rejected")
    if os.getuid() != uid or os.getgid() != gid:
        raise OSError("drop verification failed")


def _write_result(path, outcome, code, reaped, unreaped, subreaper, protected, spool, spool_digest):
    # Returns True only when the record reached the file with a successful
    # fsync. Callers downgrade to partial on False: an unsynced record may
    # be torn after a crash, so claiming complete would be dishonest.
    try:
        handle = open(path, "w")
    except OSError:
        return False
    durable = True
    try:
        json.dump(
            {
                "outcome": outcome,
                "code": code,
                "reaped": reaped,
                "unreaped": unreaped,
                "subreaper": subreaper,
                "protected": protected,
                "spool": spool,
                "spool_digest": spool_digest,
            },
            handle,
        )
        try:
            handle.flush()
            os.fsync(handle.fileno())
        except OSError:
            durable = False
    except (OSError, TypeError, ValueError):
        durable = False
    finally:
        try:
            handle.close()
        except OSError:
            durable = False
    return durable
def _spool_relay(source, pipe_target, spool_path, cap_bytes):
    # Drain one child stream to the spool file first, pipe mirror second.
    # Returns (truncated, digest_hex_or_None, size, complete). The spool
    # file is authoritative; a closed host pipe only stops the mirror,
    # never the spool. Past the cap the spool stops growing but the source
    # keeps being drained so the child never blocks. complete False means
    # bytes may be missing; callers report partial, never success.
    truncated = False
    size = 0
    digest = hashlib.sha256()
    complete = True
    handle = None
    try:
        handle = open(spool_path, "wb")
    except OSError:
        handle = None
        complete = False
    try:
        while True:
            try:
                chunk = source.read(8192)
            except Exception:
                complete = False
                break
            if not chunk:
                break
            if handle is not None:
                if not truncated:
                    remaining = cap_bytes - size
                    if len(chunk) > remaining:
                        truncated = True
                    piece = chunk[:remaining]
                    if piece:
                        try:
                            handle.write(piece)
                        except OSError:
                            complete = False
                            try:
                                handle.close()
                            except OSError:
                                pass
                            handle = None
                        else:
                            size += len(piece)
                            digest.update(piece)
            if pipe_target is not None:
                try:
                    pipe_target.write(chunk)
                    pipe_target.flush()
                except Exception:
                    pipe_target = None
    finally:
        if handle is not None:
            try:
                handle.flush()
                os.fsync(handle.fileno())
            except OSError:
                complete = False
            finally:
                try:
                    handle.close()
                except OSError:
                    pass
    return (truncated, digest.hexdigest() if complete else None, size, complete)
def _terminate_tree(pgid, grace_seconds):
    reaped = 0
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    term_start = time.monotonic()
    killed = False
    while True:
        try:
            finished_pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if finished_pid > 0:
            reaped += 1
        now = time.monotonic()
        if not killed and now - term_start >= grace_seconds:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            killed = True
        if killed and now - term_start >= grace_seconds + 1.0:
            break
        if finished_pid == 0:
            time.sleep(0.02)
    for child_pid in _live_children():
        try:
            os.kill(child_pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            continue
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        try:
            finished_pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if finished_pid > 0:
            reaped += 1
        else:
            time.sleep(0.02)
    return reaped
def _seal_spool(relays, relay_outcomes, meta_path, invocation_ref,
                input_ref, spool_cap, start_epoch):
    # Join relay threads, write the sealed spool meta, report spool state.
    # Returns (spool, spool_digest). The meta file is written BEFORE the
    # terminal result record; every terminal path below writes the result
    # LAST, so the result stays the completion authority. spool is
    # complete/partial/missing; missing raw must surface as
    # partial/uncertain downstream, never success.
    for thread in relays:
        thread.join(timeout=2.0)
    alive = any(thread.is_alive() for thread in relays)
    out_truncated, out_digest, out_size, out_ok = relay_outcomes.get(
        "out", (False, None, 0, False)
    )
    err_truncated, err_digest, err_size, err_ok = relay_outcomes.get(
        "err", (False, None, 0, False)
    )
    meta = {
        "sealed": not alive,
        "invocation_id": invocation_ref,
        "input_digest": input_ref,
        "cap_bytes": spool_cap,
        "started_epoch": start_epoch,
        "finished_epoch": time.time(),
        "out": {"truncated": bool(out_truncated), "sha256": out_digest,
                "size": out_size, "complete": bool(out_ok) and not alive},
        "err": {"truncated": bool(err_truncated), "sha256": err_digest,
                "size": err_size, "complete": bool(err_ok) and not alive},
    }
    try:
        handle = open(meta_path, "w")
    except OSError:
        return ("missing", None)
    try:
        json.dump(meta, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    except (OSError, TypeError, ValueError):
        try:
            handle.close()
        except OSError:
            pass
        return ("missing", None)
    try:
        handle.close()
    except OSError:
        return ("missing", None)
    try:
        digest = hashlib.sha256(
            json.dumps(meta, sort_keys=True).encode("utf-8")
        ).hexdigest()
    except (TypeError, ValueError):
        return ("missing", None)
    if meta["out"]["complete"] and meta["err"]["complete"]:
        return ("complete", digest)
    return ("partial", digest)
_SCRUB_MODEL_ENV = frozenset({
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_TRUSTED_HOST",
    "PIP_CONFIG_FILE", "PIP_FIND_LINKS", "PIP_NO_INDEX",
    "UV_INDEX_URL", "UV_EXTRA_INDEX_URL", "UV_NO_INDEX",
    "UV_CONFIG_FILE", "UV_FIND_LINKS",
    "npm_config_registry", "npm_config_proxy",
    "npm_config_http_proxy", "npm_config_https_proxy",
    "NPM_CONFIG_REGISTRY",
})
def _provider_env():
    # Provider-seeded installer env (WHS_EXTRA_ENV JSON). Absent means
    # no seeding. Anything malformed fails closed: None tells main to
    # exit without a record (uncertainty), never to run with half an env.
    raw = os.environ.get("WHS_EXTRA_ENV", "")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(parsed, dict) or not parsed:
        return None
    cleaned = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or not isinstance(value, str):
            return None
        if (not key or not (key[0].isalpha() or key[0] == "_")
                or not all(c.isalnum() and ord(c) < 128 or c == "_" for c in key)):
            return None
        if len(value.encode("utf-8")) > 4096:
            return None
        cleaned[key] = value
    return cleaned
def main():
    workdir = os.environ.get("WHS_WORKDIR", "/workroom")
    run_id = os.environ.get("WHS_RUN_ID", "")
    lang = os.environ.get("WHS_LANG", "bash")
    mech_path = os.environ.get("WHS_MECH_PATH", "/usr/local/bin:/usr/bin:/bin")
    model_path = os.environ.get("WHS_MODEL_PATH", "")
    rundir = os.environ.get("WHS_RUNDIR", "/run/whs-shell")
    try:
        spool_cap = int(os.environ.get("WHS_SPOOL_CAP", "65536"))
    except ValueError:
        spool_cap = 65536
    if spool_cap <= 0:
        spool_cap = 65536
    invocation_ref = os.environ.get("WHS_INVOCATION_ID", "")
    input_ref = os.environ.get("WHS_INPUT_DIGEST", "")
    start_epoch = time.time()
    try:
        run_uid = int(os.environ.get("WHS_RUN_UID", "0"))
        run_gid = int(os.environ.get("WHS_RUN_GID", "0"))
    except ValueError:
        return 127
    try:
        deadline = float(os.environ.get("WHS_TIMEOUT_S", "60"))
    except ValueError:
        deadline = 60.0
    try:
        grace = float(os.environ.get("WHS_GRACE_S", "2"))
    except ValueError:
        grace = 2.0
    if not run_id or lang not in ("bash", "python3"):
        return 127
    if run_uid <= 0 or run_gid <= 0:
        return 127
    try:
        os.makedirs(rundir, mode=0o700, exist_ok=True)
        os.chmod(rundir, 0o700)
    except OSError:
        return 127
    result_path = os.path.join(rundir, ".whs-result-" + run_id)
    pgid_path = os.path.join(rundir, ".whs-pgid-" + run_id)
    spool_out_path = os.path.join(rundir, ".whs-spool-" + run_id + ".out")
    spool_err_path = os.path.join(rundir, ".whs-spool-" + run_id + ".err")
    spool_meta_path = os.path.join(rundir, ".whs-spool-" + run_id + ".meta")
    subreaper = _enable_subreaper()
    if not subreaper:
        # Without subreaper authority a double-fork descendant escapes to
        # PID 1 and an empty live-children scan would look completed.
        # Fail closed with no record: the host reads that as uncertainty.
        return 127
    try:
        script = sys.stdin.buffer.read()
    except Exception:
        script = b""
    python_bin = shutil.which("python3", path=mech_path)
    bash_bin = shutil.which("bash", path=mech_path)
    picked = python_bin if lang == "python3" else bash_bin
    if picked is None:
        return 127
    argv = [picked, "-"] if lang == "python3" else [picked, "-s"]
    child_env = dict(os.environ)
    if model_path:
        child_env["PATH"] = model_path
    provider_env = _provider_env()
    if provider_env is None:
        return 127
    for scrubbed in _SCRUB_MODEL_ENV:
        child_env.pop(scrubbed, None)
    child_env.update(provider_env)
    drop_r, drop_w = os.pipe()
    def _drop_privileges():
        try:
            _demote_to_policy(run_uid, run_gid)
            try:
                os.write(drop_w, b"1")
            except OSError:
                pass
        finally:
            try:
                os.close(drop_w)
            except OSError:
                pass
    try:
        child = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=child_env,
            preexec_fn=_drop_privileges,
            pass_fds=(drop_w,),
        )
    except Exception:
        try:
            os.close(drop_r)
        except OSError:
            pass
        try:
            os.close(drop_w)
        except OSError:
            pass
        return 127
    try:
        protected = os.read(drop_r, 1) == b"1"
    except OSError:
        protected = False
    try:
        os.close(drop_r)
    except OSError:
        pass
    if not protected:
        try:
            child.kill()
        except OSError:
            pass
        try:
            child.wait(timeout=5.0)
        except Exception:
            pass
        return 127
    pgid = child.pid
    try:
        with open(pgid_path, "w") as handle:
            handle.write(str(pgid))
    except OSError:
        pass
    def feed_stdin():
        try:
            child.stdin.write(script)
        except Exception:
            pass
        try:
            child.stdin.close()
        except Exception:
            pass
    threading.Thread(target=feed_stdin, daemon=True).start()
    relays = []
    relay_outcomes = {}
    def _run_relay(name, source, pipe_target, spool_path):
        relay_outcomes[name] = _spool_relay(source, pipe_target, spool_path, spool_cap)
    for name, source, pipe_target, spool_path in (
        ("out", child.stdout, sys.stdout.buffer, spool_out_path),
        ("err", child.stderr, sys.stderr.buffer, spool_err_path),
    ):
        thread = threading.Thread(
            target=_run_relay, args=(name, source, pipe_target, spool_path), daemon=True
        )
        thread.start()
        relays.append(thread)
    signal.signal(signal.SIGTERM, _request_cancel)
    signal.signal(signal.SIGINT, _request_cancel)
    start = time.monotonic()
    main_status = None
    reaped = 0
    outcome = None
    while True:
        try:
            finished_pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            finished_pid, status = 0, 0
        if finished_pid == child.pid:
            main_status = status
        elif finished_pid > 0:
            reaped += 1
        now = time.monotonic()
        if _CANCEL_REQUESTED and outcome is None:
            outcome = "cancelled"
            break
        if outcome is None and now - start >= deadline:
            outcome = "timeout"
            break
        if main_status is not None:
            break
        time.sleep(0.02)
    if outcome is None:
        code = _status_code(main_status)
        settle_until = time.monotonic() + 1.0
        while time.monotonic() < settle_until:
            try:
                finished_pid, _status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if finished_pid == child.pid:
                code = _status_code(_status)
            elif finished_pid > 0:
                reaped += 1
            else:
                break
        if _live_children():
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            for survivor in _live_children():
                try:
                    os.kill(survivor, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    continue
            settle_until = time.monotonic() + 1.0
            while time.monotonic() < settle_until:
                try:
                    finished_pid, _status = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    break
                if finished_pid > 0:
                    reaped += 1
                else:
                    time.sleep(0.02)
        unreaped = _live_children()
        spool, spool_digest = _seal_spool(
            relays, relay_outcomes, spool_meta_path, invocation_ref,
            input_ref, spool_cap, start_epoch
        )
        if not _write_result(result_path, "completed", code, reaped, unreaped,
                             subreaper, protected, spool, spool_digest):
            _write_result(result_path, "completed", code, reaped, unreaped,
                          subreaper, protected, "partial", None)
        return code
    extra = _terminate_tree(pgid, grace)
    unreaped = _live_children()
    terminal_code = 142 if outcome == "cancelled" else 124
    spool, spool_digest = _seal_spool(
        relays, relay_outcomes, spool_meta_path, invocation_ref,
        input_ref, spool_cap, start_epoch
    )
    if not _write_result(
        result_path, outcome, terminal_code, reaped + extra, unreaped,
        subreaper, protected, spool, spool_digest
    ):
        _write_result(
            result_path, outcome, terminal_code, reaped + extra, unreaped,
            subreaper, protected, "partial", None
        )
    return terminal_code
if __name__ == "__main__":
    sys.exit(main())
'''
# Fixed container-side control script (bash). No NUL byte appears anywhere
# in this text: environ matching uses grep -qzF whose patterns carry no
# NUL, the tag travels in WHS_SHELL_KILL (exec -e, never interpolated into
# argv), and the supervisor is identified by its cmdline, never by a NUL
# scan. Modes: term (TERM the supervisor owning this tag), kill (KILL the
# child process group/tag carriers but preserve the subreaper), anything else
# only observes (check: exit 0 when nothing remains, 1 otherwise).
# Signalling is fixed TERM-then-KILL; the supervisor owns grace and reap.
# A script that unsets or scrubs its tag cannot hide the supervisor: it is
# found by its own cmdline plus the pgid file it wrote at spawn. A retained
# PGID without a live supervisor is historical, not authority over a later
# process that reused the same numeric group ID.
_KILL_TAG_SCRIPT = '''
tag="$WHS_SHELL_KILL"
mode="$WHS_SHELL_MODE"
sup=""
tagged=""
for d in /proc/[0-9]*/environ; do
  pid=${d#/proc/}
  pid=${pid%/environ}
  case $pid in ''|*[!0-9]*) continue;; esac
  if [ "$pid" = "$$" ]; then continue; fi
  if /bin/grep -qzF "WHS_SHELL_JOB=$tag" "$d" 2>/dev/null || \
     /bin/grep -qzF "WHS_RUN_ID=$tag" "$d" 2>/dev/null; then
    tagged="$tagged $pid"
    if /bin/grep -qF ".whs-supervisor-v" "/proc/$pid/cmdline" 2>/dev/null; then
      sup="$sup $pid"
    fi
  fi
done
pgid=""
pf="$WHS_RUNDIR/.whs-pgid-$tag"
if [ -r "$pf" ]; then
  pgid=$(/bin/cat "$pf" 2>/dev/null)
  case $pgid in ''|*[!0-9]*) pgid="";; esac
fi
if [ -z "$sup" ]; then pgid=""; fi
if [ "$mode" = term ]; then
  for p in $sup; do kill -TERM "$p" 2>/dev/null; done
  exit 0
fi
if [ "$mode" = kill ]; then
  if [ -n "$pgid" ]; then kill -KILL -"$pgid" 2>/dev/null; fi
  for p in $tagged; do
    case " $sup " in *" $p "*) continue;; esac
    kill -KILL "$p" 2>/dev/null
  done
  exit 0
fi
dirty=0
if [ -n "$sup$tagged" ]; then dirty=1; fi
if [ -n "$pgid" ] && kill -0 -"$pgid" 2>/dev/null; then dirty=1; fi
exit $dirty
'''



ProcessFactory = Callable[..., Awaitable[Any]]


class ShellWorkroomError(ValueError):
    """Stable, non-secret workroom failure (code-compatible message).

    ``uncertainty`` marks failures raised after execution may already
    have started (supervisor loss): the adapter forwards it into the
    common uncertain-execution marker. Before-start rejections stay
    False. ``str(exc)`` remains the stable error code, and ``reason_code``
    carries the same stable code as a structural attribute so consumers
    never parse exception text.
    """

    def __init__(self, message: str, *, uncertainty: bool = False) -> None:
        super().__init__(message)
        self.reason_code = message
        self.uncertainty = uncertainty


@dataclass(frozen=True)
class ExecResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool
    duration_ms: int
    workroom_key: str
    container_name: str

    def to_output_dict(self, script_sha256: str) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "truncated": self.truncated,
            "duration_ms": self.duration_ms,
            "script_sha256": script_sha256,
            "workroom_key": self.workroom_key,
        }


@dataclass(frozen=True)
class CancelReport:
    """Measured container-side teardown for one tagged execution.

    ``verified`` True needs a trusted terminal supervisor record plus a
    clean final scan, or an authoritative gone/stopped owned container.
    A tag/group scan alone cannot observe escaped, env-scrubbed children.
    The original protected record is retained for recovery. ``verified`` False never
    masquerades as success: the caller keeps the execution budget until
    a deadline-confirmed lookup settles it.
    """

    verified: bool
    outcome: str | None


def check_workroom_key(value: str) -> str:
    if not isinstance(value, str) or not _WORKROOM_KEY.fullmatch(value):
        raise ShellWorkroomError("shell_invalid_workroom_key")
    return value


class ExecutionAdmission:
    """Side-effect-free execution gate consulted by the manager (TASK-005).

    The gated-window extension implements this: while a transmit window is
    held for a key, same-instance arbitrary execution is busy-denied and
    inventory drops to the static scan. The default implementation is
    unrestricted, so the existing closed local-script path works unchanged.
    Implementations must not take manager locks or start work; both methods
    are read-only snapshots. TASK-007 owns job persistence/recovery and
    must keep every execution path funneled through ``exec_script`` so this
    single hook keeps covering them.
    """

    def check_exec_allowed(self, key: str, *, invocation_id: str | None = None) -> None:
        """Raise ``ShellWorkroomError("window_busy")" when key must wait."""

    def inventory_mode(self, key: str) -> str:
        """Normally full; static skips interpreter-running inventory."""
        return "full"

    def expected_networks(self, key: str) -> frozenset[str]:
        """Live attachments owned by the configured execution boundary."""
        return frozenset({"none"})

    def expected_network_ids(self, key: str) -> dict[str, str]:
        """Optional immutable IDs for operator-selected active attachments."""
        return {}

    def program_execution_routes(self):
        from redteam.core.ports.program_discovery import ProgramExecutionRoute

        return [ProgramExecutionRoute(
            purpose=purpose, status="unavailable", reason_code="gated_network_unconfigured",
        ) for purpose in ("target", "install")]


PROGRAM_MANIFEST_FILENAME = "program-manifest.json"


@dataclass(frozen=True)
class ProgramRecord:
    """One invocable program visible to the model.

    ``name`` is the command as invoked (dig, not dnsutils); ``executable``
    is its absolute path. ``owner_scope`` is image (baked into the
    workroom image, enumerated from the generated manifest) or instance
    (installed by one instance into its own workroom, invisible
    elsewhere). ``provenance`` is the apt origin (kali-rolling/main) or
    the instance install path. Cards show commands, never
    package-to-command guesses.
    """

    name: str
    executable: str
    version: str | None
    owner_scope: str
    provenance: str | None


def default_program_manifest_path() -> str:
    """Repo-resolved default for the generated program manifest."""
    root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    return os.path.join(
        root, "deploy", "docker", "shell-workroom", PROGRAM_MANIFEST_FILENAME
    )


def container_name_for(key: str) -> str:
    """Deterministic container name for a validated workroom key."""
    check_workroom_key(key)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return CONTAINER_NAME_PREFIX + digest


def workroom_key_for(run_id: str, instance_id: str) -> str:
    """Build the conventional per-instance key; the manager treats it as opaque."""
    key = str(run_id) + "." + str(instance_id)
    return check_workroom_key(key)


def create_argv(policy: ShellWorkroomPolicy, container_name: str, key: str) -> tuple[str, ...]:
    """Pure ``docker create`` argv. ``--network none`` is unconditional.

    Confined launcher privilege: ``--cap-drop ALL`` is kept, with only
    ``SETUID``/``SETGID`` (privilege drop to the policy uid/gid) and
    ``KILL`` (teardown signaling of demoted trees) added back. A root
    supervisor without ``KILL`` provably cannot signal a demoted child
    (``EPERM``), so the set is exactly what teardown needs and nothing
    more. Model children run demoted with zero effective capabilities
    plus no-new-privileges, and adoption requires exactly this set.

    ``--init`` gives the container a tiny init so zombies left by timed-out
    scripts are reaped. ``HOME``/``PYTHONUSERBASE`` point into the writable
    workdir so per-instance installs land on the writable+exec surface
    (the ``/tmp`` tmpfs stays ``noexec``). ``PATH`` leads with the
    workdir ``.local/bin`` so instance-installed programs resolve by bare
    name; virtualenv bins are deliberately not on ``PATH`` and need
    explicit activation.
    """
    check_workroom_key(key)
    workdir = policy.workdir
    return (
        "create",
        "--name", container_name,
        "--label", SHELL_MANAGED_LABEL,
        "--label", SHELL_CONTEXT_LABEL_KEY + "=" + key,
        "--network", "none",
        "--init",
        "--cap-drop", "ALL",
        "--cap-add", "SETUID",
        "--cap-add", "SETGID",
        "--cap-add", "KILL",
        "--security-opt", "no-new-privileges",
        "--user", policy.container_user,
        "--pids-limit", str(policy.pids_limit),
        "--memory", policy.memory.lower(),
        "--cpus", str(policy.cpus),
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=" + str(policy.tmpfs_bytes),
        "--workdir", workdir,
        "--env", "PATH=" + workdir.rstrip("/") + "/.local/bin:" + _CONTAINER_PATH,
        "--env", "LANG=C.UTF-8",
        "--env", "HOME=" + workdir,
        "--env", "PYTHONUSERBASE=" + workdir + "/.local",
        policy.image,
        "sleep", "infinity",
    )


def exec_argv(
    container_name: str,
    language: str,
    timeout_seconds: float,
    grace_seconds: float,
    job_tag: str | None = None,
    *,
    run_id: str,
    workdir: str,
    run_uid: int,
    run_gid: int,
    invocation_id: str | None = None,
    input_digest: str | None = None,
    spool_cap_bytes: int | None = None,
    extra_env_json: str | None = None,
) -> tuple[str, ...]:
    """Pure ``docker exec`` argv. The script arrives on stdin, never in argv.

    The supervisor launches as container root so its state (result and
    pgid files under a root-only directory) is never same-uid mutable;
    it drops the model child to ``run_uid``/``run_gid`` with zero
    capabilities plus no-new-privileges before exec, and fails closed
    (no record, hence uncertainty, never a clean result) when the drop
    is unavailable. Mechanism binaries resolve under a system-only PATH
    carried on this argv; the model PATH for the child travels
    separately, so instance installs never shadow the mechanism.
    ``job_tag`` (when given) is carried as a fixed environment variable
    so a later termination round can address this execution; ``run_id``
    (always present, equal to the job tag for jobs) names the supervisor
    state files. Script text never enters argv.
    """
    if language == "bash":
        whs_lang = "bash"
    elif language == "python3":
        whs_lang = "python3"
    else:
        raise ShellWorkroomError("shell_unsupported_language")
    if job_tag is not None and not _JOB_TAG.fullmatch(job_tag):
        raise ShellWorkroomError("shell_unknown_job")
    if not isinstance(run_id, str) or not _JOB_TAG.fullmatch(run_id):
        raise ShellWorkroomError("shell_invalid_arguments")
    if not isinstance(workdir, str) or not workdir.startswith("/"):
        raise ShellWorkroomError("shell_invalid_arguments")
    if not math.isfinite(timeout_seconds) or not math.isfinite(grace_seconds):
        raise ShellWorkroomError("shell_invalid_arguments")
    if timeout_seconds <= 0 or grace_seconds <= 0:
        raise ShellWorkroomError("shell_timeout_out_of_range")
    if not isinstance(run_uid, int) or isinstance(run_uid, bool):
        raise ShellWorkroomError("shell_invalid_arguments")
    if not isinstance(run_gid, int) or isinstance(run_gid, bool):
        raise ShellWorkroomError("shell_invalid_arguments")
    if run_uid <= 0 or run_gid <= 0:
        raise ShellWorkroomError("shell_invalid_arguments")
    if invocation_id is not None and (
        not isinstance(invocation_id, str) or not _JOB_TAG.fullmatch(invocation_id)
    ):
        raise ShellWorkroomError("shell_invalid_arguments")
    if input_digest is not None and (
        not isinstance(input_digest, str)
        or len(input_digest) != 64
        or any(c not in "0123456789abcdef" for c in input_digest)
    ):
        raise ShellWorkroomError("shell_invalid_arguments")
    if spool_cap_bytes is not None and (
        isinstance(spool_cap_bytes, bool)
        or not isinstance(spool_cap_bytes, int)
        or spool_cap_bytes <= 0
    ):
        raise ShellWorkroomError("shell_invalid_arguments")
    if extra_env_json is not None and (
        not isinstance(extra_env_json, str)
        or not extra_env_json
        or len(extra_env_json.encode("utf-8")) > _EXTRA_ENV_JSON_LIMIT
    ):
        raise ShellWorkroomError("shell_invalid_arguments")
    model_path = workdir.rstrip("/") + "/.local/bin:" + _CONTAINER_PATH
    prefix: tuple[str, ...] = ("exec", "--user", "0")
    if job_tag is not None:
        prefix += ("-e", _JOB_TAG_ENV + "=" + job_tag)
    if invocation_id is not None:
        prefix += ("-e", _INVOCATION_ID_ENV + "=" + invocation_id)
    if input_digest is not None:
        prefix += ("-e", _INPUT_DIGEST_ENV + "=" + input_digest)
    if spool_cap_bytes is not None:
        prefix += ("-e", _SPOOL_CAP_ENV + "=" + str(spool_cap_bytes))
    if extra_env_json is not None:
        prefix += ("-e", _EXTRA_ENV_ENV + "=" + extra_env_json)
    supervisor = workdir.rstrip("/") + "/" + _SUPERVISOR_FILENAME
    return (
        *prefix,
        "-e", "PATH=" + _MECHANISM_PATH,
        "-e", _RUN_ID_ENV + "=" + run_id,
        "-e", _WORKDIR_ENV + "=" + workdir,
        "-e", _RUN_UID_ENV + "=" + str(run_uid),
        "-e", _RUN_GID_ENV + "=" + str(run_gid),
        "-e", _RUNDIR_ENV + "=" + _SUPERVISOR_RUNDIR,
        "-e", _MODEL_PATH_ENV + "=" + model_path,
        "-e", _MECH_PATH_ENV + "=" + _MECHANISM_PATH,
        "-e", _TIMEOUT_ENV + "=" + repr(float(timeout_seconds)),
        "-e", _GRACE_ENV + "=" + repr(float(grace_seconds)),
        "-e", _LANG_ENV + "=" + whs_lang,
        "-i", container_name,
        "python3", "-I", "-u", supervisor,
    )


_SETUP_SCRIPT = """set -e
/bin/mkdir -p "$WHS_WORKDIR" "$WHS_WORKDIR/.local/bin"
# Best-effort hardening only: under cap-drop ALL even root cannot chmod a
# path owned by another uid, so a chmod failure here must not fail setup
# by itself. The verify step below enforces the real requirement.
/bin/chmod 1777 "$WHS_WORKDIR" "$WHS_WORKDIR/.local" "$WHS_WORKDIR/.local/bin" || true
"""

# Run AS THE POLICY USER: proves HOME/.local/bin are writable by the uid
# that will actually install and execute there. Any failure raises.
_VERIFY_SCRIPT = """set -e
/bin/mkdir -p "$WHS_WORKDIR/.local/bin"
/bin/touch "$WHS_WORKDIR/.local/.whs-writetest"
/bin/rm -f "$WHS_WORKDIR/.local/.whs-writetest"
"""


def _create_lock_path() -> str:
    """Same-host path for the creation mutex (shared temp dir)."""
    return os.path.join(tempfile.gettempdir(), _CREATE_LOCK_FILENAME)


class _CreateLock:
    """Held OS file lock for the count-plus-create critical section."""

    def __init__(self, handle: Any) -> None:
        self._handle = handle

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            # mypy prunes sys.platform branches per target platform, so
            # each side only type-checks where it can run.
            if sys.platform == "win32":
                import msvcrt

                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            try:
                handle.close()
            except OSError:
                pass


def _acquire_create_lock(path: str, timeout_seconds: float) -> _CreateLock:
    """Blocking acquire for thread use only (never the event loop).

    Non-blocking attempts with a bounded wait; the OS releases the lock
    when the holder process dies. Raises ``ShellWorkroomError`` on
    timeout instead of letting creation proceed uncoordinated.
    """
    handle = open(path, "a+b")
    deadline = time.monotonic() + timeout_seconds
    try:
        if sys.platform == "win32":
            import msvcrt

            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    return _CreateLock(handle)
                except OSError:
                    if time.monotonic() >= deadline:
                        raise ShellWorkroomError("shell_create_lock_timeout") from None
                    time.sleep(0.05)
        import fcntl

        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return _CreateLock(handle)
            except OSError:
                if time.monotonic() >= deadline:
                    raise ShellWorkroomError("shell_create_lock_timeout") from None
                time.sleep(0.05)
    except Exception:
        try:
            handle.close()
        except OSError:
            pass
        raise


class PreparedEndpointLike(Protocol):
    """Structural view of the Docker endpoint restored by shell preparation."""

    @property
    def binary(self) -> str: ...

    @property
    def context(self) -> str: ...

    @property
    def host(self) -> str: ...


@dataclass(frozen=True)
class EndpointSnapshot:
    """Immutable Docker endpoint frozen when preparation is bound to a manager."""

    binary: str
    context: str
    host: str


class ShellWorkroomManager:
    """Owns warm containers and bounded script execution inside them."""

    def __init__(
        self,
        policy: ShellWorkroomPolicy,
        *,
        process_factory: ProcessFactory = asyncio.create_subprocess_exec,
        program_manifest_path: str | None = None,
    ) -> None:
        policy.validate_image()
        self._policy = policy
        self._process_factory = process_factory
        self._program_manifest_path = program_manifest_path
        self._known: dict[str, str] = {}
        self._ensure_lock = asyncio.Lock()
        self._admission: ExecutionAdmission | None = None
        self._bound_endpoint: EndpointSnapshot | None = None

    def set_execution_admission(self, admission: ExecutionAdmission | None) -> None:
        """Attach the TASK-005 admission hook (default unrestricted)."""
        self._admission = admission

    def _admission_for(self) -> ExecutionAdmission:
        if self._admission is not None:
            return self._admission
        return ExecutionAdmission()

    @property
    def policy(self) -> ShellWorkroomPolicy:
        return self._policy

    @property
    def source_id(self) -> str:
        # Provider-owned source label for TASK-004 program cards.
        # Card serverIds come from here, never from a central constant.
        return SHELL_SERVER_ID

    def describe_execution_environment(self):
        """Read owner metadata without Docker calls, grants or reachability claims."""
        from redteam.capability.shell_manifest import (
            JOB_START_OPERATION,
            RUN_OPERATION,
            build_shell_descriptors,
        )
        from redteam.core.ports.program_discovery import (
            ProgramExecutionEnvironment,
            ProgramExecutionRoute,
        )

        local = [str(tool.tool_id) for tool in build_shell_descriptors(self.policy)
                 if tool.operations[0] in (RUN_OPERATION, JOB_START_OPERATION)]
        return ProgramExecutionEnvironment(
            source_id=self.source_id,
            routes=[ProgramExecutionRoute(
                purpose="local", status="local_only", reason_code="network_none",
                tool_ids=local, limits={
                    "max_timeout_seconds": self.policy.max_timeout_seconds,
                    "max_script_bytes": self.policy.max_script_bytes,
                },
            ), *self._admission_for().program_execution_routes()],
            unsupported_modes=["raw_socket", "network_admin", "external_ipv6"],
        )

    def known_workrooms(self) -> tuple[str, ...]:
        return tuple(self._known)

    def bind_prepared_endpoint(self, endpoint: PreparedEndpointLike) -> None:
        """Freeze the prepared Docker endpoint; identical rebinding is idempotent.

        The policy must already name the same binary and context, so the
        execution path and the prepared endpoint cannot diverge. A different
        endpoint after binding is refused rather than replaced.
        """
        snapshot = EndpointSnapshot(
            binary=endpoint.binary, context=endpoint.context, host=endpoint.host
        )
        if (
            snapshot.binary != self._policy.docker_binary
            or self._policy.docker_context is None
            or snapshot.context != self._policy.docker_context
        ):
            raise ShellWorkroomError("prepared_endpoint_policy_mismatch")
        if self._bound_endpoint is not None:
            if self._bound_endpoint != snapshot:
                raise ShellWorkroomError("prepared_endpoint_rebind_refused")
            return
        self._bound_endpoint = snapshot

    @property
    def bound_endpoint(self) -> EndpointSnapshot | None:
        return self._bound_endpoint

    async def revalidate_bound_endpoint(self, endpoint: PreparedEndpointLike | None) -> str:
        """Return endpoint_verified or a stable refusal reason; never raises.

        Host drift is read through the preparation resolver, the same protocol
        that produced the binding, so no second host-resolution path exists.
        """
        bound = self._bound_endpoint
        if endpoint is None or bound is None:
            return "endpoint_unbound"
        if (endpoint.binary, endpoint.context, endpoint.host) != (
            bound.binary, bound.context, bound.host,
        ):
            return "endpoint_mismatch"
        # Imported here: shell_preparation imports the gated window module,
        # which imports this module, so a top-level import would cycle.
        from redteam.capability.shell_preparation import resolve_docker_endpoint

        try:
            actual = await resolve_docker_endpoint(self._policy)
        except Exception:
            return "endpoint_unverified"
        if (actual.binary, actual.context, actual.host) != (
            bound.binary, bound.context, bound.host,
        ):
            return "endpoint_drift"
        return "endpoint_verified"

    async def observe_room_for_recovery(self, key: str) -> str:
        """Read-only room observation: absent, baseline, or unverified.

        Never creates, adopts, starts, or signals a container. Only a complete,
        successful, exact name query with no match proves absence. A failed,
        truncated, or unparseable answer is unverified, never absence.
        """
        check_workroom_key(key)
        name = container_name_for(key)
        code, out, _err, timed_out, cut = await self._run_docker(
            ("ps", "-a", "--no-trunc", "--filter", "name=^" + name + "$",
             "--format", "{{.Names}}"),
            None, 15.0, 1 << 16,
        )
        if code != 0 or timed_out or cut:
            return "unverified"
        names = [
            line.strip()
            for line in out.decode("utf-8", errors="replace").splitlines()
            if line.strip()
        ]
        if not names:
            return "absent"
        if names != [name]:
            return "unverified"
        code, out, _err, timed_out, cut = await self._run_docker(
            ("inspect", "--format",
             "{{json .Config.Labels}}|{{json .NetworkSettings.Networks}}|{{.State.Running}}",
             name),
            None, 15.0, 1 << 16,
        )
        if code != 0 or timed_out or cut:
            return "unverified"
        fields = out.decode("utf-8", errors="replace").strip().split("|")
        if len(fields) != 3:
            return "unverified"
        try:
            labels = json.loads(fields[0])
            networks = json.loads(fields[1])
        except ValueError:
            return "unverified"
        managed_key = SHELL_MANAGED_LABEL.split("=", 1)[0]
        if (
            not isinstance(labels, dict)
            or labels.get(managed_key) != "true"
            or labels.get(SHELL_CONTEXT_LABEL_KEY) != key
        ):
            return "unverified"
        if not isinstance(networks, dict) or set(networks) != {"none"}:
            return "unverified"
        if fields[2] == "false":
            return "baseline"
        if fields[2] != "true":
            return "unverified"
        code, out, _err, timed_out, cut = await self._run_docker(
            ("exec", name, "sh", "-c",
             "for d in /proc/[0-9]*/cmdline; do tr '\\0' ' ' < \"$d\"; echo; done"),
            None, 15.0, 1 << 20,
        )
        if code != 0 or timed_out or cut:
            return "unverified"
        for line in out.decode("utf-8", errors="replace").splitlines():
            process = line.strip()
            if not process or process == "sleep infinity" or "sh -c for d in /proc" in process:
                continue
            return "unverified"
        return "baseline"
    async def _run_docker(
        self,
        argv: tuple[str, ...],
        stdin_bytes: bytes | None,
        timeout_seconds: float,
        output_limit: int | None = None,
    ) -> tuple[int | None, bytes, bytes, bool, bool]:
        """Run one docker CLI call with streaming output budgets.

        Both pipes drain concurrently through per-stream byte budgets
        (``output_limit`` per stream; ``None`` disables the cap for tiny
        control-plane outputs). Bytes past the budget are read and discarded,
        so the child never blocks on a full pipe while host memory stays
        bounded. ``docker exec`` without a TTY keeps stdout/stderr on
        separate pipes (the same assumption the execution MCP server relies
        on), which is why host-side budgeting preserves stream separation
        where a container-side ``head -c`` pipeline would need an extra shell
        layer and would mask exit codes through SIGPIPE. Returns
        ``(returncode, stdout, stderr, outer_timeout, truncated)``;
        ``truncated`` strictly means a budget was reached.

        The host child is always reaped: timeout, cancellation, and error
        paths kill it when still alive and wait for it under a shield, so no
        ``docker`` process survives this call. Killing the host child does
        not kill the container side; that is ``kill_tagged_exec`` work.
        """
        process = await self._process_factory(
            *self._policy.docker_command(*argv),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if stdin_bytes:
            try:
                process.stdin.write(stdin_bytes)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        try:
            process.stdin.close()
        except (BrokenPipeError, OSError, AttributeError):
            pass
        stdout_buf = bytearray()
        stderr_buf = bytearray()
        outer_timeout = False
        out_task = asyncio.create_task(
            self._read_bounded(process.stdout, stdout_buf, output_limit)
        )
        err_task = asyncio.create_task(
            self._read_bounded(process.stderr, stderr_buf, output_limit)
        )
        wait_task = asyncio.create_task(process.wait())
        try:
            await asyncio.wait(
                {out_task, err_task, wait_task}, timeout=timeout_seconds
            )
            if not wait_task.done():
                # Host-side deadline expired. The in-container ``timeout``
                # wrapper still bounds the container side; stop waiting here.
                outer_timeout = True
            try:
                out_cut, err_cut = await asyncio.wait_for(
                    asyncio.gather(out_task, err_task), 5.0
                )
            except asyncio.TimeoutError:
                for task in (out_task, err_task):
                    if not task.done():
                        task.cancel()
                out_cut, err_cut = False, False
            truncated = self._task_flag(out_task, out_cut) or self._task_flag(
                err_task, err_cut
            )
            return (
                process.returncode,
                bytes(stdout_buf),
                bytes(stderr_buf),
                outer_timeout,
                truncated,
            )
        except asyncio.CancelledError:
            for task in (out_task, err_task, wait_task):
                if not task.done():
                    task.cancel()
            raise
        except Exception:
            for task in (out_task, err_task, wait_task):
                if not task.done():
                    task.cancel()
            raise
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except (ProcessLookupError, OSError, AttributeError):
                    pass
                try:
                    await asyncio.shield(process.wait())
                except (OSError, asyncio.CancelledError):
                    pass

    async def _read_bounded(
        self, stream: Any, output: bytearray, limit: int | None
    ) -> bool:
        """Drain one pipe, keeping at most ``limit`` bytes. True on cap hit.

        Excess is read and discarded so the child never blocks on a full
        pipe; host memory stays bounded by ``limit`` per stream.
        """
        truncated = False
        try:
            while True:
                chunk = await stream.read(_STREAM_CHUNK_BYTES)
                if not chunk:
                    return truncated
                if limit is None:
                    output.extend(chunk)
                    continue
                accepted = min(len(chunk), max(0, limit - len(output)))
                if accepted:
                    output.extend(chunk[:accepted])
                if accepted < len(chunk):
                    truncated = True
        except OSError:
            return truncated

    @staticmethod
    def _task_flag(task: asyncio.Task[Any], fallback: bool) -> bool:
        """Best-effort reader flag when the drain was abandoned."""
        if task.done() and not task.cancelled():
            return bool(task.result())
        return fallback

    async def _inspect_state(self, container_name: str) -> tuple[bool, bool]:
        """Return ``(exists, running)`` for a container name. Never raises.

        An unreadable answer counts as absent; the create path surfaces
        the real error afterwards.
        """
        try:
            code, stdout, _stderr, _outer, _trunc = await self._run_docker(
                ("inspect", "-f", "{{.State.Running}}", container_name),
                None,
                15.0,
                None,
            )
        except Exception:
            return False, False
        if code != 0:
            return False, False
        return True, stdout.decode("utf-8", errors="replace").strip() == "true"

    async def _is_running(self, container_name: str) -> bool:
        _exists, running = await self._inspect_state(container_name)
        return running

    async def _managed_container_count(self) -> int:
        """Count shell-workroom containers via an exact label filter.

        This is the cross-restart source of truth for ``max_workrooms``;
        the exact ``key=value`` filter can never match execution-server
        containers. Raises ``ShellWorkroomError`` when the daemon answer
        cannot be read (the caller falls back to process memory).
        """
        code, stdout, _stderr, _outer, _trunc = await self._run_docker(
            (
                "ps",
                "-a",
                "--filter",
                "label=" + SHELL_MANAGED_LABEL,
                "--format",
                "{{.Names}}",
            ),
            None,
            15.0,
            None,
        )
        if code != 0:
            raise ShellWorkroomError("shell_workroom_count_failed")
        lines = stdout.decode("utf-8", errors="replace").splitlines()
        return sum(1 for line in lines if line.strip())

    async def _list_managed(self) -> list[tuple[str, str]]:
        """Return ``(container name, workroom key)`` for managed containers.

        Uses the singular ``.Label`` lookup: in ``docker ps`` output
        ``.Labels`` is a rendered string, not a map, so ``index`` on it
        fails the whole listing.
        """
        code, stdout, _stderr, _outer, _trunc = await self._run_docker(
            (
                "ps",
                "-a",
                "--filter",
                "label=" + SHELL_MANAGED_LABEL,
                "--format",
                "{{.Names}}|{{.Label \"" + SHELL_CONTEXT_LABEL_KEY + "\"}}",
            ),
            None,
            15.0,
            None,
        )
        if code != 0:
            raise ShellWorkroomError("shell_workroom_count_failed")
        entries = []
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            name, sep, context = line.partition("|")
            if name.strip() and sep and context.strip():
                entries.append((name.strip(), context.strip()))
        return entries

    async def _setup_container(self, container_name: str) -> None:
        """Prepare the workdir tree, verify it for the policy user, provision.

        Root setup makes the workdir tree and attempts the opening chmod;
        the chmod is best-effort because under ``--cap-drop ALL`` even
        root cannot chmod another uid's path, so baked-in image modes
        are authoritative there. The verify step runs AS the policy user
        and proves HOME/``.local/bin`` are writable by the uid that will
        actually install and execute there; it fails closed. The
        supervisor file is written root-owned (execute, never modify),
        its state directory is created root-only, and adopt repairs both
        the same way.
        Any failure raises: scripts never run without a writable surface
        and an exact completion signal underneath.
        """
        workdir = self._policy.workdir
        setup = await self._run_docker(
            (
                "exec", "--user", "0",
                "-e", _WORKDIR_ENV + "=" + workdir,
                "-e", "PATH=" + _MECHANISM_PATH,
                "-i", container_name,
                "/bin/sh", "-c", _SETUP_SCRIPT,
            ),
            None,
            _SETUP_TIMEOUT_SECONDS,
            None,
        )
        if setup[0] != 0:
            raise ShellWorkroomError("shell_workroom_create_failed")
        verify = await self._run_docker(
            (
                "exec", "--user", self._policy.container_user,
                "-e", _WORKDIR_ENV + "=" + workdir,
                "-e", "PATH=" + _MECHANISM_PATH,
                "-i", container_name,
                "/bin/sh", "-c", _VERIFY_SCRIPT,
            ),
            None,
            _SETUP_TIMEOUT_SECONDS,
            None,
        )
        if verify[0] != 0:
            raise ShellWorkroomError("shell_workroom_create_failed")
        supervisor_path = workdir.rstrip("/") + "/" + _SUPERVISOR_FILENAME
        rundir = await self._run_docker(
            (
                "exec", "--user", "0",
                "-e", "PATH=" + _MECHANISM_PATH,
                "-i", container_name,
                "/bin/sh", "-c",
                "mkdir -p " + _SUPERVISOR_RUNDIR
                + " && chmod 700 " + _SUPERVISOR_RUNDIR,
            ),
            None,
            _SETUP_TIMEOUT_SECONDS,
            None,
        )
        if rundir[0] != 0:
            raise ShellWorkroomError("shell_workroom_create_failed")
        tee = await self._run_docker(
            (
                "exec", "--user", "0",
                "-e", "PATH=" + _MECHANISM_PATH,
                "-i", container_name,
                "/usr/bin/tee", supervisor_path,
            ),
            _SUPERVISOR_SOURCE.encode("utf-8"),
            _SETUP_TIMEOUT_SECONDS,
            None,
        )
        if tee[0] != 0:
            raise ShellWorkroomError("shell_workroom_create_failed")

    def _kill_scan_argv(
        self, container_name: str, tag: str, mode: str
    ) -> tuple[str, ...]:
        """Pure argv for one supervisor signal/verify round.

        Runs as container root (the supervisor is root-owned) with a
        system-only PATH, so instance shadows cannot intercept the
        signaling binaries. The tag and the rundir travel via ``-e``
        (never interpolated); ``mode`` is one of term, kill, or check
        (anything else observes).
        """
        return (
            "exec",
            "--user", "0",
            "-e",
            _KILL_SCAN_ENV + "=" + tag,
            "-e",
            _KILL_MODE_ENV + "=" + mode,
            "-e",
            "PATH=" + _MECHANISM_PATH,
            "-e",
            _RUNDIR_ENV + "=" + _SUPERVISOR_RUNDIR,
            "-i",
            container_name,
            "/bin/bash",
            "-c",
            _KILL_TAG_SCRIPT,
        )

    async def ensure_workroom(self, key: str) -> str:
        """Return the running container for key, creating it when absent.

        A same-name container left behind by a dead process is adopted only
        after ownership/policy verification: running ones are reused and
        stopped ones are started when their labels, image, network,
        privilege, caps, user, and workdir all match this manager. Anything
        else raises ``shell_workroom_adoption_rejected`` before the foreign
        container is started, set up, or removed. A name that exists but
        cannot be started raises ``shell_workroom_exists_stopped`` instead
        of the generic creation failure so operators can tell a wedged
        leftover apart from a broken create. ``max_workrooms`` is enforced
        against the label-filtered container count so a restarted process
        cannot overshoot a cap it can no longer see in memory.
        """
        check_workroom_key(key)
        async with self._ensure_lock:
            known = self._known.get(key)
            if known is not None:
                exists, running = await self._inspect_state(known)
                if running:
                    try:
                        await self._verify_adoption(known, key)
                    except ShellWorkroomError:
                        del self._known[key]
                        raise
                    return known
                if exists:
                    try:
                        await self._verify_adoption(known, key)
                    except ShellWorkroomError:
                        del self._known[key]
                        raise
                    started = await self._run_docker((
                        "start",
                        known,
                    ), None, 30.0, None)
                    if started[0] == 0 and await self._is_running(known):
                        await self._setup_container(known)
                        return known
                del self._known[key]
            name = container_name_for(key)
            adopted = await self._adopt_existing(key, name)
            if adopted is not None:
                return adopted
            lock = await asyncio.to_thread(
                _acquire_create_lock, _create_lock_path(), _CREATE_LOCK_TIMEOUT_SECONDS
            )
            try:
                await self._enforce_workroom_cap()
                created = await self._run_docker(
                    create_argv(self._policy, name, key), None, 120.0, None
                )
                if created[0] != 0:
                    # Lost a creation race: re-inspect once before failing.
                    adopted = await self._adopt_existing(key, name)
                    if adopted is not None:
                        return adopted
                    raise ShellWorkroomError("shell_workroom_create_failed")
                started = await self._run_docker(("start", name), None, 30.0, None)
                if started[0] != 0 or not await self._is_running(name):
                    raise ShellWorkroomError("shell_workroom_start_failed")
                await self._setup_container(name)
                self._known[key] = name
                return name
            finally:
                lock.release()

    async def _verify_adoption(self, name: str, key: str) -> None:
        """Reject a same-name container that is not ours or not our policy.

        Raises ``shell_workroom_adoption_rejected`` (before-start, never
        uncertain) when the container lacks our managed/context labels for
        this key, or when its actual image, LIVE network attachments,
        privilege, caps, security options, user, workdir, or resource
        bounds (memory, cpus, pids, tmpfs size) disagree with the current
        policy. Live attachments must exactly match the admission-owned
        set: `none` by default, or the configured boundary's active window.
        Additional attachments, even with a stale `none` NetworkMode,
        are rejected. Unreadable or
        unparsable answers fail closed the same way. Nothing is started,
        stopped, removed, or rewritten here; callers validate before any
        mutation so a foreign container is never touched.
        """
        try:
            code, stdout, _stderr, _outer, _trunc = await self._run_docker(
                ("inspect", "-f", _ADOPTION_FORMAT, name),
                None,
                _KILL_TIMEOUT_SECONDS,
                None,
            )
        except Exception:
            raise ShellWorkroomError("shell_workroom_adoption_rejected") from None
        if code != 0:
            raise ShellWorkroomError("shell_workroom_adoption_rejected")
        try:
            lines = stdout.decode("utf-8", errors="replace").splitlines()
            config = json.loads(lines[0])
            host = json.loads(lines[1])
            networks = json.loads(lines[2])
            image_id = json.loads(lines[3])
            sentinel = lines[4] if len(lines) > 4 else ""
        except (ValueError, IndexError, AttributeError):
            raise ShellWorkroomError("shell_workroom_adoption_rejected") from None
        if sentinel.strip() != _ADOPTION_SENTINEL:
            raise ShellWorkroomError("shell_workroom_adoption_rejected")
        try:
            live_names = set(networks.keys()) if isinstance(networks, dict) else None
        except AttributeError:
            live_names = None
        expected_networks = self._admission_for().expected_networks(key)
        detached_closed = live_names == set() and expected_networks == frozenset({"none"})
        if live_names is None or (live_names != expected_networks and not detached_closed):
            raise ShellWorkroomError("shell_workroom_adoption_rejected")
        for network_name, network_id in self._admission_for().expected_network_ids(key).items():
            if networks.get(network_name, {}).get("NetworkID") != network_id:
                raise ShellWorkroomError("shell_workroom_adoption_rejected")
        try:
            labels = config.get("Labels") or {}
            managed_key = SHELL_MANAGED_LABEL.split("=", 1)[0]
            host_config = host if isinstance(host, dict) else {}
            mem = self._policy.memory
            mem_bytes = int(mem[:-1]) * (1024 * 1024 if mem[-1] in "mM" else 1024 * 1024 * 1024)
            tmp_entry = (host_config.get("Tmpfs") or {}).get("/tmp", "")
            tmp_size = None
            for part in tmp_entry.split(","):
                if part.startswith("size="):
                    tmp_size = int(part[len("size="):])
            sec_opt = host_config.get("SecurityOpt") or []
            no_priv = any(
                s == "no-new-privileges" or s.startswith("no-new-privileges:")
                or s.startswith("no-new-privileges=") for s in sec_opt)
            nanos = host_config.get("NanoCpus") or 0
            # Docker normalizes added caps to CAP_* form; compare normalized.
            normed_caps = set()
            for cap in host_config.get("CapAdd") or []:
                cap_name = str(cap).upper()
                if cap_name.startswith("CAP_"):
                    cap_name = cap_name[4:]
                normed_caps.add(cap_name)
            ok = (
                labels.get(managed_key) == "true"
                and labels.get(SHELL_CONTEXT_LABEL_KEY) == key
                and host_config.get("NetworkMode") == "none"
                and host_config.get("Privileged") is False
                and normed_caps == {"SETUID", "SETGID", "KILL"}
                and "ALL" in (host_config.get("CapDrop") or [])
                and no_priv
                and config.get("User") == self._policy.container_user
                and config.get("WorkingDir") == self._policy.workdir
                and host_config.get("Memory") == mem_bytes
                and abs(nanos / 1000000000 - self._policy.cpus) < 0.000001
                and host_config.get("PidsLimit") == self._policy.pids_limit
                and tmp_size == self._policy.tmpfs_bytes
            )
        except (AttributeError, ValueError, TypeError, KeyError):
            raise ShellWorkroomError("shell_workroom_adoption_rejected") from None
        if not ok:
            raise ShellWorkroomError("shell_workroom_adoption_rejected")
        resolved = await self._resolve_image_id(self._policy.image)
        if resolved is None or image_id != resolved:
            raise ShellWorkroomError("shell_workroom_adoption_rejected")

    async def _adopt_existing(self, key: str, name: str) -> str | None:
        """Adopt a same-name container, or return None when there is none.

        A leftover that fails ownership/policy verification raises
        ``shell_workroom_adoption_rejected`` before it is started or set
        up, so foreign containers are never touched. Raises
        ``shell_workroom_exists_stopped`` when the leftover exists but
        cannot be started.
        """
        exists, running = await self._inspect_state(name)
        if not exists:
            return None
        await self._verify_adoption(name, key)
        if not running:
            started = await self._run_docker(("start", name), None, 30.0, None)
            if started[0] != 0 or not await self._is_running(name):
                raise ShellWorkroomError("shell_workroom_exists_stopped")
        await self._setup_container(name)
        self._known[key] = name
        return name

    async def _enforce_workroom_cap(self) -> None:
        """Raise ``shell_too_many_workrooms`` at the label-counted cap.

        Fail closed: an unreadable daemon count raises instead of falling
        back to process memory, so a restarted process can never silently
        overshoot ``max_workrooms``. Called under the creation lock.
        """
        try:
            count = await self._managed_container_count()
        except ShellWorkroomError:
            raise
        except Exception as exc:
            raise ShellWorkroomError("shell_workroom_count_failed") from exc
        if count >= self._policy.max_workrooms:
            raise ShellWorkroomError("shell_too_many_workrooms")

    def _truncate(self, data: bytes) -> tuple[str, bool]:
        limit = self._policy.max_output_bytes
        text = data.decode("utf-8", errors="replace")
        encoded = text.encode("utf-8")
        if len(encoded) <= limit:
            return text, False
        cut = encoded[:limit].decode("utf-8", errors="ignore")
        return cut, True

    async def exec_script(
        self,
        key: str,
        language: str,
        script: str,
        timeout_seconds: float | None = None,
        exec_tag: str | None = None,
        *,
        invocation_id: str | None = None,
        input_digest: str | None = None,
        retain_record: bool = True,
        extra_env: dict[str, str] | None = None,
    ) -> ExecResult:
        """Run one script under the provisioned supervisor and return output.

        ``exec_tag`` optionally labels this execution (see ``exec_argv``)
        so ``kill_tagged_exec`` can terminate it on cancellation; the
        supervisor run id is the tag for jobs and a fresh id otherwise, so
        even untagged sync executions settle through the supervisor (F4).
        Streams are capped at ``max_output_bytes`` each during collection;
        ``truncated`` reports a reached cap. A host-side cancel settles the
        container side through the supervisor under a shield before the
        cancel propagates, so a cancelled sync run leaves nothing behind.
        ``retain_record`` keeps the supervisor terminal record in place
        instead of removing it after a trusted-clean outcome, so a later
        read-only recovery (jobs restored from the protected manifest)
        can still collect the original. Sync callers leave it False.
        It defaults True: the seal is the recovery authority and the
        spool it binds is retained anyway, so a later query never
        degrades to RUNNING for a sealed execution. Only the cancel
        teardown consumes (its verified report plus the durable
        cancel mark carry the outcome instead).
        """
        check_workroom_key(key)
        try:
            self._admission_for().check_exec_allowed(key, invocation_id=invocation_id)
        except ShellWorkroomError:
            raise
        except ValueError as exc:
            raise ShellWorkroomError(str(exc) or "window_busy") from exc
        if language not in SHELL_LANGUAGES:
            raise ShellWorkroomError("shell_unsupported_language")
        if not isinstance(script, str) or not script:
            raise ShellWorkroomError("shell_empty_script")
        script_bytes = script.encode("utf-8")
        if len(script_bytes) > self._policy.max_script_bytes:
            raise ShellWorkroomError("shell_script_too_large")
        timeout = self._policy.default_timeout_seconds if timeout_seconds is None else float(
            timeout_seconds
        )
        if (
            not math.isfinite(timeout)
            or timeout <= 0
            or timeout > self._policy.max_timeout_seconds
        ):
            raise ShellWorkroomError("shell_timeout_out_of_range")
        if exec_tag is not None and not _JOB_TAG.fullmatch(exec_tag):
            raise ShellWorkroomError("shell_unknown_job")
        if invocation_id is not None and (
            not isinstance(invocation_id, str) or not _JOB_TAG.fullmatch(invocation_id)
        ):
            raise ShellWorkroomError("shell_invalid_arguments")
        if input_digest is not None and (
            not isinstance(input_digest, str)
            or len(input_digest) != 64
            or any(c not in "0123456789abcdef" for c in input_digest)
        ):
            raise ShellWorkroomError("shell_invalid_arguments")
        if retain_record is not True and retain_record is not False:
            raise ShellWorkroomError("shell_invalid_arguments")
        extra_env_json: str | None = None
        if extra_env is not None:
            if not isinstance(extra_env, dict) or not extra_env:
                raise ShellWorkroomError("shell_invalid_arguments")
            for env_key, env_value in extra_env.items():
                if not isinstance(env_key, str) or not isinstance(env_value, str):
                    raise ShellWorkroomError("shell_invalid_arguments")
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_key):
                    raise ShellWorkroomError("shell_invalid_arguments")
                if len(env_value.encode("utf-8")) > 4096:
                    raise ShellWorkroomError("shell_invalid_arguments")
            try:
                extra_env_json = json.dumps(extra_env, sort_keys=True)
            except (TypeError, ValueError) as exc:
                raise ShellWorkroomError("shell_invalid_arguments") from exc
            if len(extra_env_json.encode("utf-8")) > _EXTRA_ENV_JSON_LIMIT:
                raise ShellWorkroomError("shell_invalid_arguments")
        if exec_tag is not None:
            run_id = exec_tag
        elif invocation_id is not None:
            run_id = invocation_id
        else:
            run_id = secrets.token_hex(8)
        container = await self.ensure_workroom(key)
        uid_text, _, gid_text = self._policy.container_user.partition(":")
        argv = exec_argv(
            container,
            language,
            timeout,
            self._policy.exec_grace_seconds,
            exec_tag,
            run_id=run_id,
            workdir=self._policy.workdir,
            run_uid=int(uid_text),
            run_gid=int(gid_text),
            invocation_id=invocation_id,
            input_digest=input_digest,
            spool_cap_bytes=self._policy.max_output_bytes,
            extra_env_json=extra_env_json,
        )
        started = time.monotonic()
        try:
            code, raw_out, raw_err, outer_timeout, collected_cut = await self._run_docker(
                argv,
                script_bytes,
                timeout + self._policy.exec_grace_seconds + _HOST_OUTER_MARGIN_SECONDS,
                self._policy.max_output_bytes,
            )
        except asyncio.CancelledError:
            try:
                await asyncio.shield(self.kill_tagged_exec(key, run_id))
            except Exception:
                pass
            raise
        elapsed = time.monotonic() - started
        duration_ms = max(0, int(elapsed * 1000))
        if outer_timeout:
            # The supervisor deadline plus grace passed long ago; stop
            # waiting here and terminate whatever remains, best-effort —
            # but never trust the cleanup return. Only a verified
            # protected terminal record authorizes an outcome (a
            # disappeared supervisor with no record is uncertainty, and
            # the job path holds its slot on the raised marker).
            try:
                await self.kill_tagged_exec(key, run_id)
            except Exception:
                pass
            record = await self._read_result_record(container, run_id)
            if (
                record is not None
                and record.get("outcome") in ("completed", "timeout", "cancelled")
                and self.record_trusted(record)
            ):
                timed_out = record.get("outcome") == "timeout"
                record_code = record.get("code")
                code = record_code if isinstance(record_code, int) else code
                if not retain_record:
                    await self._remove_run_files(container, run_id)
            else:
                raise ShellWorkroomError(
                    "shell_execution_interrupted", uncertainty=True
                )
        else:
            timed_out = await self._classify_exit(
                container, run_id, code, retain_record=retain_record
            )
        stdout, out_cut = self._truncate(raw_out)
        stderr, err_cut = self._truncate(raw_err)
        return ExecResult(
            exit_code=None if timed_out else code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            truncated=collected_cut or out_cut or err_cut,
            duration_ms=duration_ms,
            workroom_key=key,
            container_name=container,
        )

    @staticmethod
    def record_trusted(record: dict[str, Any]) -> bool:
        """A completion record counts only when the protected supervisor
        wrote it last with subreaper authority and nothing left unreaped.

        The supervisor runs as container root (unkillable and unreadable
        by the demoted script), holds subreaper authority (required before
        the child starts, so no double-fork escapee can hide at PID 1),
        and writes after reaping the tree, so a trusted record cannot be
        a forgery. An absent record, an unprotected run, a missing
        subreaper flag, or anything but an explicit empty unreaped list
        is uncertainty, never a clean result. Shared by the sync
        classifier and the async settle path so the rule lives in
        exactly one place.
        """
        return (
            record.get("protected") is True
            and record.get("subreaper") is True
            and isinstance(record.get("unreaped"), list)
            and not record.get("unreaped")
        )

    async def _classify_exit(
        self, container: str, run_id: str, code: int | None, retain_record: bool = False
    ) -> bool:
        """True when the exit proves a supervisor-enforced timeout.

        Only two 124 shapes are clean: a genuine completed 124 (the
        script itself exited 124 under a trusted record) and a trusted
        timeout 124 (the supervisor's own deadline record). Every other
        124 — absent, unprotected, unreaped, or wrong outcome — is
        uncertainty, never a clean timeout. A trusted completed record
        always wins, so a genuine 124/137 exit (even past a settled
        background child) is never misreported. Codes the supervisor
        never emits (142, or 137 without a trusted record), an absent
        record, an unprotected run, or unreaped survivors mean external
        interference or tampering and fail loudly as uncertainty instead
        of masquerading as a result. Run files are removed ONLY on
        trusted-clean terminal outcomes; unsafe records (absent,
        unprotected, unreaped, subreaper-failure, or wrong outcome) are
        RETAINED for TASK-007 uncertain-original audit and raw-evidence
        review, preserving the original unreaped/subreaper-failure facts.
        """
        record = await self._read_result_record(container, run_id)
        if code == 124:
            if (
                record is not None
                and record.get("outcome") == "completed"
                and record.get("code") == 124
                and self.record_trusted(record)
            ):
                if not retain_record:
                    await self._remove_run_files(container, run_id)
                return False
            if (
                record is not None
                and record.get("outcome") == "timeout"
                and self.record_trusted(record)
            ):
                if not retain_record:
                    await self._remove_run_files(container, run_id)
                return True
            raise ShellWorkroomError(
                "shell_execution_interrupted", uncertainty=True
            )
        if code == 137:
            if (
                record is not None
                and record.get("outcome") == "completed"
                and record.get("code") == 137
                and self.record_trusted(record)
            ):
                if not retain_record:
                    await self._remove_run_files(container, run_id)
                return False
            raise ShellWorkroomError(
                "shell_execution_interrupted", uncertainty=True
            )
        if code == 142:
            raise ShellWorkroomError(
                "shell_execution_interrupted", uncertainty=True
            )
        if (
            record is not None
            and record.get("outcome") == "completed"
            and record.get("code") == code
            and self.record_trusted(record)
        ):
            if not retain_record:
                await self._remove_run_files(container, run_id)
            return False
        raise ShellWorkroomError("shell_execution_interrupted", uncertainty=True)

    async def _check_tag_clean(self, container_name: str, tag: str) -> bool:
        """True when the verify round observes nothing left for tag."""
        try:
            check = await self._run_docker(
                self._kill_scan_argv(container_name, tag, "check"),
                None,
                _KILL_TIMEOUT_SECONDS,
                None,
            )
            return check[0] == 0 and not check[3] and not check[4]
        except Exception:
            return False

    def _result_paths(self, tag: str) -> tuple[str, str]:
        # Root-only supervisor state: never inside the model workdir, so a
        # script can neither read nor forge the completion record.
        return (
            _SUPERVISOR_RUNDIR + "/.whs-result-" + tag,
            _SUPERVISOR_RUNDIR + "/.whs-pgid-" + tag,
        )

    async def _resolve_image_id(self, image: str) -> str | None:
        """Local immutable image ID for a configured reference, if present."""
        try:
            code, stdout, _stderr, outer, trunc = await self._run_docker(
                ("inspect", "--format", "{{.Id}}", image),
                None,
                _KILL_TIMEOUT_SECONDS,
                65536,
            )
            if code != 0 or outer or trunc:
                return None
            image_id = stdout.decode("utf-8", errors="replace").strip()
            return image_id or None
        except Exception:
            return None

    async def _read_result_record(
        self, container_name: str, tag: str
    ) -> dict[str, Any] | None:
        """Best-effort read of the supervisor result file. None when absent.

        Reads as container root: the state directory is root-only, and the
        binary resolves under the system-only PATH.
        """
        try:
            result_path, _pgid_path = self._result_paths(tag)
            code, stdout, _stderr, outer, trunc = await self._run_docker(
                (
                    "exec", "--user", "0",
                    "-e", "PATH=" + _MECHANISM_PATH,
                    "-i", container_name, "/bin/cat", result_path,
                ),
                None,
                _KILL_TIMEOUT_SECONDS,
                65536,
            )
            if code != 0 or outer or trunc:
                return None
            record = json.loads(stdout.decode("utf-8"))
            return record if isinstance(record, dict) else None
        except Exception:
            return None

    async def supervisor_record(self, key: str, selector: str) -> dict[str, Any] | None:
        """Full supervisor result record, or None when unreadable. Never raises."""
        try:
            check_workroom_key(key)
            if not isinstance(selector, str) or not _JOB_TAG.fullmatch(selector):
                return None
            name = self._known.get(key) or container_name_for(key)
            return await self._read_result_record(name, selector)
        except Exception:
            return None

    def _spool_paths(self, tag: str) -> tuple[str, str, str]:
        # Companion to _result_paths: spool streams plus the sealed meta.
        # Spool files are provider-owned recovery state, never
        # model-visible, and are intentionally NOT removed with run files
        # so a later query/fetch can still collect the original after a
        # host crash. Container removal bounds their lifetime.
        rundir = _SUPERVISOR_RUNDIR
        return (
            rundir + "/.whs-spool-" + tag + ".out",
            rundir + "/.whs-spool-" + tag + ".err",
            rundir + "/.whs-spool-" + tag + ".meta",
        )

    async def _read_spool_bytes(
        self, container_name: str, tag: str, *, stream: str
    ) -> bytes | None:
        # Best-effort read of one sealed spool stream. None when absent.
        # Reads as container root under the system-only PATH, like the
        # result record. The file itself is already policy-capped by the
        # supervisor; the read carries a bounded limit regardless.
        # A host-side outer timeout or collection cut means the bytes may
        # be partial: reject them rather than letting a hash match a
        # prefix against prefix-altered meta.
        try:
            out_path, err_path, _meta_path = self._spool_paths(tag)
            spool_path = out_path if stream == "out" else err_path
            code, stdout, _stderr, outer, trunc = await self._run_docker(
                (
                    "exec", "--user", "0",
                    "-e", "PATH=" + _MECHANISM_PATH,
                    "-i", container_name, "/bin/cat", spool_path,
                ),
                None,
                _KILL_TIMEOUT_SECONDS,
                2 * self._policy.max_output_bytes + 65536,
            )
            if code != 0 or outer or trunc:
                return None
            return stdout
        except Exception:
            return None

    async def read_spool_stream(
        self, key: str, supervisor_id: str, *, stream: str = "out"
    ) -> bytes | None:
        # One spool stream for a supervisor execution, or None when
        # unreadable. Never raises. The selector must be the
        # invocation-derived supervisor id, never a model-supplied label.
        try:
            check_workroom_key(key)
            if not isinstance(supervisor_id, str) or not _JOB_TAG.fullmatch(supervisor_id):
                return None
            if stream not in ("out", "err"):
                return None
            name = self._known.get(key) or container_name_for(key)
            return await self._read_spool_bytes(name, supervisor_id, stream=stream)
        except Exception:
            return None

    async def spool_record(
        self, key: str, supervisor_id: str
    ) -> dict[str, Any] | None:
        # Sealed spool meta for a supervisor execution, or None when
        # unreadable. Never raises. Callers treat a missing or unsealed
        # meta as partial/uncertain, never as success.
        try:
            check_workroom_key(key)
            if not isinstance(supervisor_id, str) or not _JOB_TAG.fullmatch(supervisor_id):
                return None
            name = self._known.get(key) or container_name_for(key)
            _out_path, _err_path, meta_path = self._spool_paths(supervisor_id)
            code, stdout, _stderr, outer, trunc = await self._run_docker(
                (
                    "exec", "--user", "0",
                    "-e", "PATH=" + _MECHANISM_PATH,
                    "-i", name, "/bin/cat", meta_path,
                ),
                None,
                _KILL_TIMEOUT_SECONDS,
                65536,
            )
            if code != 0 or outer or trunc:
                return None
            record = json.loads(stdout.decode("utf-8"))
            return record if isinstance(record, dict) else None
        except Exception:
            return None

    async def _read_outcome(self, container_name: str, tag: str) -> str | None:
        """Supervisor outcome string when its result file is readable."""
        record = await self._read_result_record(container_name, tag)
        if record is None:
            return None
        outcome = record.get("outcome")
        return outcome if isinstance(outcome, str) else None
    def _job_manifest_path(self, job_id: str) -> str:
        return _SUPERVISOR_RUNDIR + "/.whs-job-" + job_id + ".json"

    async def write_job_manifest(self, key: str, job_id: str, manifest: dict[str, Any]) -> None:
        # Protected submit receipt: root-only rundir, written BEFORE launch.
        # Raises ShellWorkroomError when unwritable so the caller fails
        # closed without spawning. Never raises anything else.
        check_workroom_key(key)
        if not isinstance(job_id, str) or not _JOB_TAG.fullmatch(job_id):
            raise ShellWorkroomError("shell_unknown_job")
        try:
            payload = json.dumps(manifest, sort_keys=True).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ShellWorkroomError("shell_invalid_arguments") from exc
        # Bound admits the pre-spawn retained-window cargo alongside
        # the submit receipt (same bound as the annotation merge).
        if len(payload) > 8192:
            raise ShellWorkroomError("shell_invalid_arguments")
        name = self._known.get(key) or container_name_for(key)
        code, _out, _err, outer, trunc = await self._run_docker(
            (
                "exec", "--user", "0",
                "-e", "PATH=" + _MECHANISM_PATH,
                "-i", name, "/usr/bin/tee", self._job_manifest_path(job_id),
            ),
            payload,
            _KILL_TIMEOUT_SECONDS,
            None,
        )
        if code != 0 or outer or trunc:
            raise ShellWorkroomError("shell_job_manifest_unwritable")

    async def annotate_job_manifest(self, key: str, job_id: str, patch: dict[str, Any]) -> bool:
        # Bounded merge of provider-approved cargo into the protected
        # submit manifest. Only the retained_window handle plus an
        # annotation epoch are accepted; anything else is refused.
        # Returns False (never raises) when unreadable, invalid, or
        # unwritable, so a missed annotation never breaks an ACK.
        try:
            check_workroom_key(key)
            if not isinstance(job_id, str) or not _JOB_TAG.fullmatch(job_id):
                return False
            if not isinstance(patch, dict) or set(patch) != {"retained_window"}:
                return False
            retained = patch["retained_window"]
            if not isinstance(retained, dict):
                return False
            current = await self.read_job_manifest(key, job_id)
            if not isinstance(current, dict):
                return False
            try:
                cargo = json.dumps(retained, sort_keys=True).encode("utf-8")
            except (TypeError, ValueError):
                return False
            if len(cargo) > 8192:
                return False
            merged = dict(current)
            merged["retained_window"] = json.loads(cargo.decode("utf-8"))
            merged["window_annotated"] = True
            try:
                payload = json.dumps(merged, sort_keys=True).encode("utf-8")
            except (TypeError, ValueError):
                return False
            if len(payload) > 8192:
                return False
            name = self._known.get(key) or container_name_for(key)
            code, _out, _err, outer, trunc = await self._run_docker(
                (
                    "exec", "--user", "0",
                    "-e", "PATH=" + _MECHANISM_PATH,
                    "-i", name, "/usr/bin/tee", self._job_manifest_path(job_id),
                ),
                payload,
                _KILL_TIMEOUT_SECONDS,
                None,
            )
            return code == 0 and not outer and not trunc
        except Exception:
            return False

    async def read_job_manifest(self, key: str, job_id: str) -> dict[str, Any] | None:
        # Protected submit receipt, or None when unreadable. Never raises.
        try:
            check_workroom_key(key)
            if not isinstance(job_id, str) or not _JOB_TAG.fullmatch(job_id):
                return None
            name = self._known.get(key) or container_name_for(key)
            code, stdout, _stderr, outer, trunc = await self._run_docker(
                (
                    "exec", "--user", "0",
                    "-e", "PATH=" + _MECHANISM_PATH,
                    "-i", name, "/bin/cat", self._job_manifest_path(job_id),
                ),
                None,
                _KILL_TIMEOUT_SECONDS,
                65536,
            )
            if code != 0 or outer or trunc:
                return None
            record = json.loads(stdout.decode("utf-8"))
            return record if isinstance(record, dict) else None
        except Exception:
            return None

    def _job_cancel_path(self, supervisor_id: str) -> str:
        return _SUPERVISOR_RUNDIR + "/.whs-cancel-" + supervisor_id + ".json"

    async def write_cancel_mark(self, key: str, supervisor_id: str,
                                 mark: dict[str, Any]) -> None:
        # Protected host-observed cancel seal, written BEFORE the cancel
        # ACK is returned. Raises ShellWorkroomError when unwritable so
        # the caller fails closed instead of acknowledging an unsealed
        # cancel. Never raises anything else. Bounded to 2048 bytes.
        check_workroom_key(key)
        if not isinstance(supervisor_id, str) or not _JOB_TAG.fullmatch(supervisor_id):
            raise ShellWorkroomError("shell_unknown_job")
        try:
            payload = json.dumps(mark, sort_keys=True).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ShellWorkroomError("shell_invalid_arguments") from exc
        if len(payload) > 2048:
            raise ShellWorkroomError("shell_invalid_arguments")
        name = self._known.get(key) or container_name_for(key)
        code, _out, _err, outer, trunc = await self._run_docker(
            (
                "exec", "--user", "0",
                "-e", "PATH=" + _MECHANISM_PATH,
                "-i", name, "/usr/bin/tee", self._job_cancel_path(supervisor_id),
            ),
            payload,
            _KILL_TIMEOUT_SECONDS,
            None,
        )
        if code != 0 or outer or trunc:
            raise ShellWorkroomError("shell_cancel_mark_unwritable")

    async def read_cancel_mark(self, key: str, supervisor_id: str) -> dict[str, Any] | None:
        # Protected cancel seal, or None when absent/unreadable. Never raises.
        try:
            check_workroom_key(key)
            if not isinstance(supervisor_id, str) or not _JOB_TAG.fullmatch(supervisor_id):
                return None
            name = self._known.get(key) or container_name_for(key)
            code, stdout, _stderr, outer, trunc = await self._run_docker(
                (
                    "exec", "--user", "0",
                    "-e", "PATH=" + _MECHANISM_PATH,
                    "-i", name, "/bin/cat", self._job_cancel_path(supervisor_id),
                ),
                None,
                _KILL_TIMEOUT_SECONDS,
                8192,
            )
            if code != 0 or outer or trunc:
                return None
            record = json.loads(stdout.decode("utf-8"))
            return record if isinstance(record, dict) else None
        except Exception:
            return None

    async def supervisor_trace_present(self, key: str, supervisor_id: str) -> bool:
        # Any supervisor trace for one execution: the spawn pgid file,
        # the terminal record, or the sealed spool meta. Single-shot,
        # read-only, never raises. Submit waits on this (bounded) before
        # acknowledging; recovery uses it to prove registration from a
        # manifest without resubmitting.
        try:
            check_workroom_key(key)
            if not isinstance(supervisor_id, str) or not _JOB_TAG.fullmatch(supervisor_id):
                return False
            name = self._known.get(key) or container_name_for(key)
            _result_path, pgid_path = self._result_paths(supervisor_id)
            code, _out, _err, outer, trunc = await self._run_docker(
                (
                    "exec", "--user", "0",
                    "-e", "PATH=" + _MECHANISM_PATH,
                    "-i", name, "/bin/cat", pgid_path,
                ),
                None,
                _KILL_TIMEOUT_SECONDS,
                1024,
            )
            if code == 0 and not outer and not trunc:
                return True
            if await self._read_result_record(name, supervisor_id) is not None:
                return True
            _out_path, _err_path, meta_path = self._spool_paths(supervisor_id)
            code, _mout, _merr, outer2, trunc2 = await self._run_docker(
                (
                    "exec", "--user", "0",
                    "-e", "PATH=" + _MECHANISM_PATH,
                    "-i", name, "/bin/cat", meta_path,
                ),
                None,
                _KILL_TIMEOUT_SECONDS,
                1024,
            )
            return code == 0 and not outer2 and not trunc2
        except Exception:
            return False

    async def verify_workroom_ownership(self, key: str) -> bool:
        # Read-only ownership check for recovery readers. True only on
        # a verified label/image/network/user/bounds match. Never raises
        # and never ensures, creates or starts anything, so a foreign
        # same-name container cannot supply a fake protected result. A
        # gone workroom reads as unverified, and callers hold on that.
        try:
            check_workroom_key(key)
            name = self._known.get(key) or container_name_for(key)
            await self._verify_adoption(name, key)
            return True
        except Exception:
            return False

    async def _remove_run_files(self, container_name: str, tag: str) -> None:
        """Best-effort removal of per-execution state files. Never raises."""
        try:
            result_path, pgid_path = self._result_paths(tag)
            await self._run_docker(
                (
                    "exec", "--user", "0",
                    "-e", "PATH=" + _MECHANISM_PATH,
                    "-i", container_name, "/bin/rm",
                    "-f", result_path, pgid_path,
                ),
                None,
                _KILL_TIMEOUT_SECONDS,
                None,
            )
        except Exception:
            pass

    async def kill_tagged_exec(self, key: str, tag: str) -> CancelReport:
        """Signal the supervisor owning this tag, then verify. Never raises.

        TERM goes only to the supervisor (found by its tag env plus its own
        cmdline, immune to script env scrubbing); the supervisor owns grace,
        KILL escalation, and reap. The host never kills this trusted
        subreaper before it can collect a scrubbed/setsid descendant.
        A bounded wait uses the configured grace and existing host margin;
        no proof means cleanup-unknown, not a clean-scan success. No warm
        container/sibling is killed. Protected terminal records are kept.
        """
        try:
            check_workroom_key(key)
            if not isinstance(tag, str) or not _JOB_TAG.fullmatch(tag):
                return CancelReport(verified=False, outcome=None)
            name = self._known.get(key) or container_name_for(key)
            state = await self.workroom_container_state(key)
            if state == "gone":
                return CancelReport(verified=True, outcome=None)
            if state == "unknown" or not await self.verify_workroom_ownership(key):
                return CancelReport(verified=False, outcome=None)
            if state == "stopped":
                return CancelReport(verified=True, outcome=None)

            async def proven() -> CancelReport | None:
                record = await self._read_result_record(name, tag)
                if (
                    record is not None and self.record_trusted(record)
                    and record.get("outcome") in {"completed", "timeout", "cancelled"}
                    and await self._check_tag_clean(name, tag)
                ):
                    return CancelReport(verified=True, outcome=record["outcome"])
                return None

            await self._run_docker(
                self._kill_scan_argv(name, tag, "term"),
                None,
                _KILL_TIMEOUT_SECONDS,
                None,
            )
            deadline = (
                time.monotonic() + self._policy.exec_grace_seconds
                + _HOST_OUTER_MARGIN_SECONDS
            )
            while time.monotonic() < deadline:
                report = await proven()
                if report is not None:
                    return report
                await asyncio.sleep(0.1)
            await self._run_docker(
                self._kill_scan_argv(name, tag, "kill"),
                None,
                _KILL_TIMEOUT_SECONDS,
                None,
            )
            report = await proven()
            if report is not None:
                return report
            return CancelReport(
                verified=False, outcome=await self._read_outcome(name, tag)
            )
        except Exception:
            return CancelReport(verified=False, outcome=None)

    async def supervisor_outcome(self, key: str, selector: str) -> tuple[str | None, bool]:
        """Best-effort ``(supervisor outcome, anything-alive)``. Never raises.

        Errors resolve to ``(None, True)``: assume alive and keep holding
        the execution budget. A missing container resolves to
        ``(None, False)``: nothing can be alive in it.
        """
        try:
            check_workroom_key(key)
            if not isinstance(selector, str) or not _JOB_TAG.fullmatch(selector):
                return None, True
            name = self._known.get(key) or container_name_for(key)
            if not await self._is_running(name):
                return None, False
            outcome = await self._read_outcome(name, selector)
            alive = not await self._check_tag_clean(name, selector)
            return outcome, alive
        except Exception:
            return None, True

    async def workroom_container_state(self, key: str) -> str:
        """Running, stopped, gone, or unknown for the key. Never raises.

        Gone is authoritative (no such container, so nothing can run).
        Stopped is a present but fully halted container (no live
        processes, though its files persist). Unknown is unreadable and
        callers must hold, never settle, on it.
        """
        try:
            check_workroom_key(key)
            name = self._known.get(key) or container_name_for(key)
            code, stdout, err, outer, trunc = await self._run_docker(
                ("inspect", "-f", "{{.State.Running}}", name),
                None,
                _KILL_TIMEOUT_SECONDS,
                65536,
            )
        except Exception:
            return "unknown"
        if outer or trunc:
            return "unknown"
        if code == 0:
            text = stdout.decode("utf-8", errors="replace").strip()
            return {"true": "running", "false": "stopped"}.get(text, "unknown")
        message = err.decode("utf-8", errors="replace").lower()
        if "no such container" in message or "no such object" in message:
            return "gone"
        return "unknown"

    async def remove_workroom(self, key: str) -> bool:
        """Best-effort ``docker rm -f``. True when nothing remains for key."""
        check_workroom_key(key)
        name = self._known.pop(key, None) or container_name_for(key)
        code, _out, err, _outer, _trunc = await self._run_docker((
            "rm",
            "-f",
            name,
        ), None, 30.0, None)
        if code == 0:
            return True
        message = err.decode("utf-8", errors="replace").lower()
        return "no such container" in message

    async def remove_workrooms_by_prefix(self, prefix: str) -> list[str]:
        """Remove workrooms whose key starts with prefix (run teardown).

        Label-listed containers are included so teardown works after a
        restart. Fail closed: an unreadable daemon listing raises instead
        of silently under-removing.
        """
        keys = {key for key in self._known if key.startswith(prefix)}
        try:
            listed = await self._list_managed()
        except ShellWorkroomError:
            raise
        except Exception as exc:
            raise ShellWorkroomError("shell_workroom_count_failed") from exc
        for _name, context in listed:
            if context.startswith(prefix):
                keys.add(context)
        removed = []
        for key in sorted(keys):
            if await self.remove_workroom(key):
                removed.append(key)
        return removed

    async def probe(self) -> bool:
        """True when the docker daemon answers. Never raises."""
        try:
            code, _out, _err, _outer, _trunc = await self._run_docker(
                ("version", "--format", "{{.Server.Version}}"),
                None,
                10.0,
                None,
            )
            return code == 0
        except Exception:
            return False

    async def list_image_programs(
        self, manifest_path: str | None = None
    ) -> list[ProgramRecord]:
        """Enumerate image-baked commands from the generated manifest.

        The manifest enumerates per-package executables (via dpkg -L at
        generation time), so cards show invocable commands with absolute
        paths, never package-to-command guesses. Provenance is enforced:
        the manifest ``image_digest`` must equal the CONFIGURED image
        resolved to its local immutable ID, otherwise the inventory is
        refused instead of publishing another image's commands. An
        alternate image needs its own manifest generated from itself and
        passed explicitly as ``manifest_path``. Raises
        ``shell_manifest_unreadable`` on a missing or corrupt file and
        ``shell_image_manifest_mismatch`` on a provenance mismatch.
        """
        path = manifest_path or self._program_manifest_path or default_program_manifest_path()
        try:
            with open(path, encoding="utf-8") as handle:
                manifest = json.load(handle)
            programs = manifest["programs"]
            manifest_digest = manifest["image_digest"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ShellWorkroomError("shell_manifest_unreadable") from exc
        resolved = await self._resolve_image_id(self._policy.image)
        if resolved is None:
            raise ShellWorkroomError("shell_image_unavailable")
        if resolved != manifest_digest:
            raise ShellWorkroomError("shell_image_manifest_mismatch")
        records: list[ProgramRecord] = []
        try:
            for program in programs:
                version = program.get("version")
                origin = program.get("origin")
                for entry in program.get("executables", []):
                    records.append(
                        ProgramRecord(
                            name=entry["command"],
                            executable=entry["path"],
                            version=version,
                            owner_scope="image",
                            provenance=origin,
                        )
                    )
        except (AttributeError, TypeError, KeyError) as exc:
            raise ShellWorkroomError("shell_manifest_unreadable") from exc
        return records

    async def _pip_version_map(self, argv: tuple[str, ...]) -> dict[str, str]:
        """Exact-name pip version table; unavailable pip means empty."""
        try:
            pip_call = self._run_docker(argv, None, _KILL_TIMEOUT_SECONDS * 2, None)
            pip_code, pip_out, _pip_err, _pip_outer, _pip_trunc = await pip_call
        except Exception:
            return {}
        if pip_code != 0:
            return {}
        try:
            entries = json.loads(pip_out.decode("utf-8"))
        except (ValueError, AttributeError, TypeError):
            return {}
        versions: dict[str, str] = {}
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                entry_name = entry.get("name")
                entry_version = entry.get("version")
                if isinstance(entry_name, str) and isinstance(entry_version, str):
                    versions.setdefault(entry_name, entry_version)
        return versions

    async def list_instance_programs(
        self,
        *,
        run_id: str,
        instance_id: str,
        workroom_key: str | None = None,
    ) -> list[ProgramRecord]:
        """Enumerate executables one instance installed into its workroom.

        Trusted identity in, never model args out: the key is derived
        inside from run_id plus instance_id (instance identity is
        required); a caller-supplied workroom_key is compare-only and a
        mismatch raises. Beyond that the key is shape-validated only:
        callers must pass keys derived from ExecutionContext, never model
        arguments. A stopped workroom raises instead of reporting empty.

        The probe runs inside the workroom as the policy user and prints
        only regular executable files, so directories, text files, and
        non-executable entries never become advertised commands. The scan
        covers the workdir local bin plus every workdir venv bin dir,
        matching the documented PATH convention: the local bin leads PATH
        so those commands resolve by bare name, while venv commands run
        by the absolute executable path each record carries. A failed
        probe raises shell_inventory_failed instead of silently
        reporting an empty list. Pip versions join by exact distribution
        name only, per install root, and stay None when unavailable.

        While a transmit window is held for this key the admission reports
        ``static`` mode and the pip version joins are skipped.
        """
        if not run_id or not instance_id:
            raise ShellWorkroomError("shell_invalid_workroom_key")
        trusted = workroom_key_for(str(run_id), str(instance_id))
        if workroom_key is not None and workroom_key != trusted:
            raise ShellWorkroomError("shell_invalid_workroom_key")
        name = self._known.get(trusted) or container_name_for(trusted)
        if not await self._is_running(name):
            raise ShellWorkroomError("shell_workroom_not_running")
        workdir = self._policy.workdir.rstrip("/")
        local_bin = workdir + "/.local/bin"
        q = chr(34)
        script = chr(10).join(
            [
                "# WHS_INVENTORY_SCAN",
                "for d in " + q + local_bin + q + " " + q + workdir + q + "/*/bin; do",
                "  [ -d " + q + "$d" + q + " ] || continue",
                "  for f in " + q + "$d" + q + "/*; do",
                "    [ -e " + q + "$f" + q + " ] || continue",
                "    [ -f " + q + "$f" + q + " ] || continue",
                "    [ -x " + q + "$f" + q + " ] || continue",
                "    echo " + q + "$f" + q,
                "  done",
                "done",
            ]
        )
        code, stdout, _stderr, _outer, _trunc = await self._run_docker(
            (
                "exec", "-e", "PATH=" + _MECHANISM_PATH,
                "-i", name, "/bin/sh", "-c", script,
            ),
            None,
            _KILL_TIMEOUT_SECONDS,
            self._policy.max_output_bytes,
        )
        if code != 0:
            raise ShellWorkroomError("shell_inventory_failed")
        prefix = workdir + "/"
        paths: list[str] = []
        seen: set[str] = set()
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            path = line.strip()
            if not path or not path.startswith(prefix):
                continue
            if path.endswith("/"):
                continue
            head, _, tail = path.rpartition("/")
            if not tail or not head:
                continue
            if path in seen:
                continue
            seen.add(path)
            paths.append(path)
        paths.sort()
        static_only = self._admission_for().inventory_mode(trusted) == "static"
        user_versions: dict[str, str] = {}
        venv_versions: dict[str, dict[str, str]] = {}
        if not static_only:
            user_versions = await self._pip_version_map(
                (
                    'exec', '-e', 'PATH=' + _MECHANISM_PATH,
                    '-i', name, 'python3', '-s', '-m', 'pip', 'list',
                    '--user', '--format=json',
                ),
            )
            venv_versions = {}
            bindirs: list[str] = sorted({path.rpartition('/')[0] for path in paths})
            for bindir in bindirs:
                if bindir == local_bin:
                    continue
                venv_versions[bindir] = await self._pip_version_map(
                    (
                        'exec', '-e', 'PATH=' + _MECHANISM_PATH,
                        '-i', name, bindir + '/python', '-s', '-m', 'pip', 'list',
                        '--format=json',
                    )
                )
        records: list[ProgramRecord] = []
        for path in paths:
            bindir, _, command = path.rpartition("/")
            table = user_versions if bindir == local_bin else venv_versions.get(bindir, {})
            records.append(
                ProgramRecord(
                    name=command,
                    executable=path,
                    version=table.get(command),
                    owner_scope="instance",
                    provenance="workroom:" + bindir,
                )
            )
        return records
