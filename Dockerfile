# Runtime image for the webhook server and the console.
#
# Note what this image does NOT do: run the candidate fix. That happens in a
# separate throwaway container the agent starts itself (VALIDATOR_IMAGE), which
# is why compose hands this container a Docker socket.
FROM python:3.11-slim

# git is not optional - the agent clones the failing branch and pushes the fix.
#
# docker-cli, NOT docker.io: Debian splits them, and docker.io is the daemon,
# which ships /usr/bin/docker-init but no `docker` client at all. Installing it
# builds cleanly and then fails at the first validation with "executable file
# not found in $PATH". Only the client is wanted here anyway - the containers
# are created on the host's daemon through the mounted socket.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates docker-cli \
    && rm -rf /var/lib/apt/lists/* \
    && git --version && docker --version

WORKDIR /app

# Copy requirements first so a source edit does not invalidate the pip layer.
COPY agent/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/ ./

# Runs and incident memory live here. Mounted as a volume by compose so they
# survive a rebuild.
ENV MEMORY_DB=/data/memory.db \
    REPOMAP_CACHE_DIR=/data/repomap \
    PYTHONUNBUFFERED=1
RUN mkdir -p /data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health').read()"

CMD ["python", "main.py"]
