FROM --platform=linux/amd64 ghcr.io/astral-sh/uv:0.12.1@sha256:cf4eedcaa81655197f625739489effcbe71b61ceb1506f332c3facae5deceded AS uv
FROM --platform=linux/amd64 debian:bookworm-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3

# These shared layers are identical across tasks. Downloads happen only at build time.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ca-certificates curl xz-utils patch python3 ripgrep \
    libpcre3-dev libssl-dev nodejs procps util-linux \
    && rm -rf /var/lib/apt/lists/*
ARG NIM_VERSION=2.4.0
ARG NIM_SHA256=23c1c83d9031c498a2b3ad0bce153b8905710d04eb5922cf8ebd8f9d12125743
# Newer compiler sources and tests can reveal the task's solution.
RUN curl -fsSL "https://nim-lang.org/download/nim-${NIM_VERSION}-linux_x64.tar.xz" -o /tmp/nim.tar.xz \
    && echo "${NIM_SHA256}  /tmp/nim.tar.xz" | sha256sum -c - \
    && mkdir /opt/nim-bootstrap \
    && tar -xJf /tmp/nim.tar.xz --strip-components=1 -C /opt/nim-bootstrap "nim-${NIM_VERSION}/bin/nim" \
    && rm /tmp/nim.tar.xz \
    && useradd -m -s /bin/bash agent

# Harbor's native agent is installed once at image build time.
COPY --from=uv /uv /usr/local/bin/uv
RUN runuser -u agent -- env HOME=/home/agent UV_PYTHON_DOWNLOADS=never \
    uv tool install --python /usr/bin/python3 mini-swe-agent==2.4.6 \
    --with 'litellm[mcp]==1.108.0' --with 'orjson==3.11.5' --with 'fastapi==0.162.0'

COPY build-nim /usr/local/bin/build-nim
RUN chmod 755 /usr/local/bin/build-nim
COPY dependencies.tar.gz /opt/dependencies.tar.gz
RUN mkdir -p /opt/nim-deps && tar -xzf /opt/dependencies.tar.gz -C /opt/nim-deps
COPY source.tar.gz /opt/source.tar.gz
WORKDIR /workspace/Nim
RUN tar -xzf /opt/source.tar.gz -C /workspace/Nim \
    && tar -xzf /opt/source.tar.gz -C /opt/nim-bootstrap lib config \
    && tar -xzf /opt/dependencies.tar.gz -C /opt/nim-bootstrap \
    && cp -a /opt/nim-deps/. /workspace/Nim/ \
    && build-nim \
    && chown -R agent:agent /workspace/Nim

# Git is available to the coding agent; upstream repository history is absent.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/home/agent/.local/bin:/workspace/Nim/bin:/opt/nim-bootstrap/bin:${PATH}"
ENV UV_OFFLINE=1
ENV LITELLM_LOCAL_MODEL_COST_MAP=True
COPY {issue_id}.md /workspace/{issue_id}.md
CMD ["sleep", "infinity"]
