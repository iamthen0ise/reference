FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        procps psmisc strace util-linux iproute2 lsof python3 curl sysstat tini busybox-static \
    && rm -rf /var/lib/apt/lists/*
