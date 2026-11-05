FROM --platform=linux/amd64 debian:bookworm-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ca-certificates curl xz-utils python3 \
    libpcre3-dev libssl-dev procps util-linux \
    && rm -rf /var/lib/apt/lists/*
ARG NIM_VERSION=2.4.0
ARG NIM_SHA256=23c1c83d9031c498a2b3ad0bce153b8905710d04eb5922cf8ebd8f9d12125743
RUN curl -fsSL "https://nim-lang.org/download/nim-${NIM_VERSION}-linux_x64.tar.xz" -o /tmp/nim.tar.xz \
    && echo "${NIM_SHA256}  /tmp/nim.tar.xz" | sha256sum -c - \
    && mkdir /opt/nim-bootstrap \
    && tar -xJf /tmp/nim.tar.xz --strip-components=1 -C /opt/nim-bootstrap \
    && rm /tmp/nim.tar.xz \
    && useradd -m -s /bin/bash verifier

# Export supplies byte-identical, history-free inputs to both build contexts.
COPY source.tar.gz dependencies.tar.gz /opt/
COPY build-nim /usr/local/bin/build-nim
RUN chmod 755 /usr/local/bin/build-nim \
    && mkdir -p /workspace/Nim /logs/verifier

# Harbor restores only the declared source artifacts under /workspace/Nim.
# Everything used to grade them belongs to this image.
COPY test.sh verify.py case_runner.py ctrf.py scoring.py platform.json manifest.json /tests/
COPY fixtures/ /tests/fixtures/

WORKDIR /workspace/Nim
ENV PATH="/opt/nim-bootstrap/bin:${PATH}"
CMD ["sleep", "infinity"]
