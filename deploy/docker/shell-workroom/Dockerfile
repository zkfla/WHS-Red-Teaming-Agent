# TASK-003 shell workroom image (minimal deployment base).
#
# Deployment pins the digest at publish time, for example:
#   docker build -t shell-workroom:20261008 deploy/docker/shell-workroom
#   docker push <registry>/shell-workroom:20261008
# then record the resulting repo@sha256:<digest> in the shell policy file.
# The adapter refuses unpinned images unless allow_unpinned_image is set
# explicitly (local tests only, never production).
#
# Kali is the required base (ExecPlan FR-003). kalilinux/kali-rolling
# ships no analysis tools by default (ER-005), so TASK-003 installs its
# FR-003 baseline explicitly here. This set is the TASK-003 starting
# point; TASK-004 owns the final "MCP replaced-by-shell/Kali vs retained"
# decision and may adjust it during reconciliation.
FROM kalilinux/kali-rolling

ARG DEBIAN_FRONTEND=noninteractive

# Deterministic official mirror over plain HTTP. Root cause (verified
# 2026-10-09 against kalilinux/kali-rolling:latest): the base ships NO CA
# bundle (ca-certificates not installed), so every HTTPS mirror fails
# TLS verify (exit 100) regardless of mirror. The fix keeps the shipped
# signed deb822 source and points it at one concrete official HTTP mirror
# (mirror2.krfoss.org, listed on the official Kali mirror statistics with
# httpUp). Package integrity comes from the Signed-By keyring signatures,
# never from transport TLS: no third-party repos, no trusted=yes,
# no AllowInsecure, no --force-yes anywhere. Override with
# --build-arg KALI_MIRROR=<official mirror>/kali.
ARG KALI_MIRROR=http://mirror.wane.kr/kali

# The selected install set comes from one repository-owned profile input;
# the same exact file generates program-manifest.json after the build.
# baseline remains the default. kali-default must be selected explicitly:
#   --build-arg SHELL_IMAGE_PROFILE=kali-default \
#   --build-arg PACKAGE_INPUT=packages-kali-default.txt
# ca-certificates
# is installed FIRST so later stages (and any consumer that switches the
# image to HTTPS) have a real CA bundle; the big install then proceeds on
# the same signed HTTP source.
ARG SHELL_IMAGE_PROFILE=baseline
ARG PACKAGE_INPUT=packages.txt
COPY packages*.txt /tmp/shell-workroom-profiles/

# The workroom tree must serve ANY policy uid, not just 65532: the
# manager runs cap-drop ALL, where even root cannot chmod a path owned
# by another uid, so the writable+exec modes are baked here at build
# time (full privilege) rather than fixed up at runtime.
RUN set -e; \
    case "$SHELL_IMAGE_PROFILE:$PACKAGE_INPUT" in \
      baseline:packages.txt|kali-default:packages-kali-default.txt) ;; \
      *) echo "invalid shell image profile/package input" >&2; exit 1 ;; \
    esac; \
    cp "/tmp/shell-workroom-profiles/$PACKAGE_INPUT" /tmp/shell-workroom-packages.txt; \
    if [ -f /etc/apt/sources.list.d/kali.sources ]; then \
      sed -i "s|^URIs: .*|URIs: $KALI_MIRROR|" /etc/apt/sources.list.d/kali.sources; \
      grep -q "^URIs: $KALI_MIRROR" /etc/apt/sources.list.d/kali.sources; \
    elif [ -f /etc/apt/sources.list ]; then \
      sed -i "s|^deb [^ ]*|deb $KALI_MIRROR|" /etc/apt/sources.list; \
      grep -q "$KALI_MIRROR" /etc/apt/sources.list; \
    else \
      echo "no kali apt source file found" >&2; exit 1; \
    fi; \
    apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates \
    && apt-get install --yes --no-install-recommends $(grep -v '^[[:space:]]*#' /tmp/shell-workroom-packages.txt | grep -v '^[[:space:]]*$' | tr -d '\r' | tr '\n' ' ') \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 65532 redteam \
    && useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin redteam \
    && mkdir -p /workroom /workroom/.local /workroom/.local/bin \
    && chown 65532:65532 /workroom /workroom/.local /workroom/.local/bin \
    && chmod 1777 /workroom /workroom/.local /workroom/.local/bin

# Writable+exec surface: /workroom holds per-instance installs and direct
# binaries (exec allowed, unlike the noexec /tmp tmpfs the manager mounts).
# HOME/PYTHONUSERBASE point at it so `pip install --user` works offline.
ENV HOME=/workroom PYTHONUSERBASE=/workroom/.local

USER 65532:65532
WORKDIR /workroom

CMD ["sleep", "infinity"]
