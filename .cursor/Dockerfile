# Base image for Cloud Agents on this repo.
# Cursor checks out the selected git revision separately — do not COPY the project here.
FROM python:3.12-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
        build-essential \
        xz-utils \
        sudo \
    && rm -rf /var/lib/apt/lists/*

ARG NODE_VERSION=20.18.2
RUN curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
    | tar -xJ -C /usr/local --strip-components=1 \
    && node --version \
    && npm --version

RUN if ! id ubuntu >/dev/null 2>&1; then \
      useradd --create-home --shell /bin/bash ubuntu; \
    fi \
    && echo "ubuntu ALL=(ALL) NOPASSWD:ALL" >/etc/sudoers.d/ubuntu \
    && chmod 0440 /etc/sudoers.d/ubuntu

USER ubuntu
WORKDIR /home/ubuntu
