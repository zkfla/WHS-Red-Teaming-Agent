FROM debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251

ARG COREUTILS_VERSION=9.1-1
ARG GREP_VERSION=3.8-5
ARG JQ_VERSION=1.6-2.1+deb12u2

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends \
        coreutils="${COREUTILS_VERSION}" \
        grep="${GREP_VERSION}" \
        jq="${JQ_VERSION}" \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 65532 redteam \
    && useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin redteam

USER 65532:65532
WORKDIR /workspace

