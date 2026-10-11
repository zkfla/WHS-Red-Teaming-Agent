FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends nftables iproute2 python3 python3-pip tcpdump gcc python3-dev libnetfilter-queue-dev libnfnetlink-dev && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --break-system-packages NetfilterQueue
COPY src/redteam/capability/gate_daemon.py src/redteam/capability/gate_wire.py src/redteam/capability/gate_nfqueue.py /opt/gate/
COPY deploy/docker/gate-sidecar/gate-daemon /usr/local/bin/gate-daemon
RUN chmod +x /usr/local/bin/gate-daemon
ENTRYPOINT ["sleep", "infinity"]
