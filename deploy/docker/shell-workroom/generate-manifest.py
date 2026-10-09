"""Generate the publishable shell-workroom program manifest.

Single-source direction: packages.txt is the ONLY package list. This
generator reads it, resolves each requested name against the LIVE built
image (installed dpkg Status plus Provides, so virtual or transitional
requests such as dnsutils resolve to whatever apt actually installed,
for example bind9-dnsutils, with no hardcoded rename table), enumerates
real executables with the image-user access check, and writes
program-manifest.json with full provenance. Nothing here duplicates the
package list: the Dockerfile installs FROM the same packages.txt, and
TASK-004 program cards consume the generated JSON.

Usage:
    python deploy/docker/shell-workroom/generate-manifest.py --image shell-workroom:t003-draft
"""

from __future__ import annotations

import argparse
import datetime
import gzip
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE0 = Path(__file__).resolve()
_SRC = HERE0.parents[3] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
from redteam.capability.shell_workroom import _CONTAINER_PATH as _OWNER_PATH  # noqa: E402

HERE = Path(__file__).resolve().parent
PACKAGES_FILE = HERE / "packages.txt"
DOCKERFILE = HERE / "Dockerfile"
DEFAULT_MANIFEST = HERE / "program-manifest.json"
BASE_IMAGE_REF = "kalilinux/kali-rolling:latest"
RELEASE = "kali-rolling"
COMPONENTS = ("main", "contrib", "non-free", "non-free-firmware")
# Owner PATH definition: the manager builds PATH as workdir .local/bin plus
# this system suffix, so bare-name resolution matches exactly this set.
DEFAULT_PATH_DIRS = tuple(_OWNER_PATH.split(":"))

USABLE_NOTES = {
    "nmap": "loopback -sT verified; -sS needs CAP_NET_RAW (dropped)",
    "tcpdump": "live capture needs CAP_NET_RAW (dropped); -r read verified",
    "python3": "PEP 668: pip install --user rejected; use instance venvs",
    "pip": "usable inside instance venvs; system installs blocked (PEP 668)",
    "pip3": "usable inside instance venvs; system installs blocked (PEP 668)",
}

PROBE_SCRIPT = """
import json, os, subprocess, sys
from pathlib import Path
rows = []
installed = {}
providers = {}
SEP = chr(31)
metadata = subprocess.check_output(
    ['dpkg-query', '-W',
     '-f=${Package}' + SEP + '${Status}' + SEP + '${Version}' + SEP
     + '${source:Package}' + SEP + '${Provides}' + SEP + chr(10)],
    text=True)
for line in metadata.split(SEP + chr(10)):
    fields = line.split(SEP)
    if len(fields) < 5 or fields[1] != 'install ok installed':
        continue
    installed[fields[0]] = fields
    for provided in fields[4].split(','):
        name = provided.strip().split()[0] if provided.strip() else ''
        if name:
            providers.setdefault(name, []).append(fields[0])
for requested in json.loads(sys.argv[1]):
    choices = [requested] if requested in installed else providers.get(requested, [])
    if len(choices) != 1:
        raise RuntimeError('ambiguous or absent installed provider: ' + requested)
    package = choices[0]
    info = installed[package]
    paths = subprocess.check_output(['dpkg', '-L', package], text=True).splitlines()
    executable = sorted({
        path for path in paths if path.startswith('/')
        and Path(path).is_file() and os.access(path, os.X_OK)})
    rows.append({'requested_package': requested, 'package': info[0],
                 'version': info[2], 'source_package': info[3],
                 'executables': [{'command': Path(p).name, 'path': p} for p in executable]})
print(json.dumps(rows))
"""

def read_packages():
    """The single-source requested set, minus comments and blanks."""
    names = []
    for line in PACKAGES_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip().replace(chr(13), "")
        if stripped and not stripped.startswith("#"):
            names.append(stripped)
    return names


def docker_inspect_id(ref):
    """Local immutable image ID for a reference; raises when absent."""
    out = subprocess.check_output(
        ["docker", "inspect", "--format", "{{.Id}}", ref], text=True)
    return out.strip()


def run_probe(image, packages):
    """One ephemeral offline container; script arrives via argv, no shell."""
    cmd = ["docker", "run", "--rm", "--network", "none", "--cap-drop", "ALL",
           "--security-opt", "no-new-privileges", "--user", "65532:65532",
           image, "python3", "-c", PROBE_SCRIPT, json.dumps(packages)]
    out = subprocess.check_output(cmd, text=True, timeout=180)
    rows = json.loads(out)
    if not isinstance(rows, list) or len(rows) != len(packages):
        raise RuntimeError("probe returned an unexpected row set")
    return rows

def apt_source_from_image(image):
    """Actual configured APT source read from the built image, never a copy."""
    for path in ("/etc/apt/sources.list.d/kali.sources", "/etc/apt/sources.list"):
        try:
            out = subprocess.check_output(
                ["docker", "run", "--rm", "--network", "none", "--cap-drop", "ALL",
                 "--user", "65532:65532", image, "cat", path],
                text=True, timeout=60, stderr=subprocess.DEVNULL)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            continue
        for line in out.splitlines():
            stripped = line.strip()
            if stripped.startswith("URIs:"):
                return stripped.split(":", 1)[1].strip()
            if stripped.startswith("deb ") and not stripped.startswith("deb-src"):
                parts = stripped.split()
                if len(parts) >= 2:
                    return parts[1]
    raise RuntimeError("no Kali APT source found in built image")


def component_index(mirror):
    """Best-effort (package, version) to component map from mirror indices.

    The index is fetched over unsigned HTTP and never signature-checked:
    a hit means component match only, while dpkg versions stay authoritative.
    """
    found = {}
    note = "unfetched"
    try:
        for component in COMPONENTS:
            url = (mirror.rstrip("/") + "/dists/" + RELEASE + "/"
                   + component + "/binary-amd64/Packages.gz")
            with urllib.request.urlopen(url, timeout=30) as response:
                raw = gzip.decompress(response.read()).decode("utf-8", errors="replace")
            package = ""
            version = ""
            for line in raw.splitlines():
                if line.startswith("Package: "):
                    package = line[len("Package: "):].strip()
                elif line.startswith("Version: "):
                    version = line[len("Version: "):].strip()
                elif line == "":
                    if package and version:
                        found.setdefault((package, version), component)
                    package = ""
                    version = ""
        note = "mirror-index-matched-unverified"
    except Exception as exc:
        note = "index-fetch-failed: " + type(exc).__name__
    return found, note

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="shell-workroom:t003-draft")
    parser.add_argument("--base-digest", default=None,
                        help="pinned base-image digest from the image build record")
    parser.add_argument("--output", default=str(DEFAULT_MANIFEST))
    args = parser.parse_args()

    packages = read_packages()
    print("requested packages: " + str(len(packages)), flush=True)
    image_digest = docker_inspect_id(args.image)
    if not args.image.startswith("sha256:"):
        print("warning: mutable image ref; manifest pins resolved " + image_digest,
              flush=True)
    base_pinned = args.base_digest is not None
    base_note = ("build-record pin" if base_pinned
                 else "unpinned: pass --base-digest from the image build record")
    mirror = apt_source_from_image(image_digest)
    transport = "https" if mirror.startswith("https://") else "http"

    rows = run_probe(image_digest, packages)
    index, index_note = component_index(mirror)
    generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    unresolved = []
    programs = []
    for row in rows:
        component = index.get((row["package"], row["version"]))
        origin = (RELEASE + "/" + component) if component else None
        if component is None:
            unresolved.append(row["package"] + "==" + row["version"])
        executables = []
        for entry in row["executables"]:
            on_path = any(
                entry["path"].startswith(d + "/") for d in DEFAULT_PATH_DIRS)
            executables.append({
                "command": entry["command"],
                "path": entry["path"],
                "on_default_path": on_path,
                "usable_note": USABLE_NOTES.get(entry["command"]),
            })
        programs.append({
            "requested_package": row["requested_package"],
            "package": row["package"],
            "version": row["version"],
            "source_package": row["source_package"],
            "origin": origin,
            "executables": executables,
        })
    manifest = {
        "generator": "deploy/docker/shell-workroom/generate-manifest.py",
        "generated_at": generated_at,
        "image": args.image,
        "image_digest": image_digest,
        "base_image": BASE_IMAGE_REF,
        "base_digest": args.base_digest,
        "base_pinned": base_pinned,
        "base_note": base_note,
        "apt_source": mirror,
        "apt_transport": transport,
        "apt_release": RELEASE,
        "origin_index": index_note,
        "origin_unresolved": sorted(unresolved),
        "programs": programs,
    }
    Path(args.output).write_text(json.dumps(manifest, indent=2) + chr(10), encoding="utf-8")
    total = sum(len(p["executables"]) for p in programs)
    print("wrote " + args.output + ": " + str(len(programs)) + " packages, "
          + str(total) + " executables; unresolved: " + str(len(unresolved)),
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
