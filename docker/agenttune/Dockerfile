FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        sshpass \
        sysbench \
        postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# docker CLI (static binary) so this container can control the sibling
# postgres container through the shared /var/run/docker.sock socket.
ARG DOCKER_CLI_VERSION=29.2.1
RUN curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz" -o /tmp/docker.tgz \
    && tar -xzf /tmp/docker.tgz -C /tmp \
    && mv /tmp/docker/docker /usr/local/bin/docker \
    && rm -rf /tmp/docker /tmp/docker.tgz

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

ENV PYTHONUNBUFFERED=1
CMD ["tail", "-f", "/dev/null"]