# Minni evaluation image (PACKAGING_PLAN.md §1, decision §6.5).
#
# Purpose: let a stranger evaluate the daemon with one command and zero local
# Python/Node setup:
#
#   docker run --rm -it ghcr.io/infektyd/minni:latest
#
# This is the demo/eval channel. Minni's real habitat is your own machine
# (per-agent vaults, launchd, editors reading the Markdown) — see the README
# Quickstart for the supported local install.
#
# Embedding models (~320 MB) are NOT baked into the image; the daemon
# announces and downloads them on first recall, cached in the mounted volume.

FROM python:3.14-slim

# The daemon is pure Python; build tools are only needed if a wheel is missing
# for this platform (faiss-cpu and torch ship manylinux wheels).
RUN useradd --create-home --shell /usr/sbin/nologin minni

WORKDIR /app

# Engine only: the Node plugin surface is for wiring real agent runtimes on a
# host, which is out of scope for the eval container.
COPY engine/requirements* ./engine/
RUN pip install --no-cache-dir -r \
    "$( [ -f engine/requirements.lock ] && echo engine/requirements.lock || echo engine/requirements.txt )"

COPY engine/ ./engine/
COPY scripts/ ./scripts/
COPY LICENSE README.md ./

RUN mkdir -p /home/minni/.minni/run && \
    chown -R minni:minni /home/minni/.minni /app
USER minni
ENV HOME=/home/minni

# Vault/DB and the HF model cache live here; mount it to persist memory:
#   docker run -v minni-data:/home/minni ghcr.io/infektyd/minni
VOLUME /home/minni

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s \
  CMD python engine/minnid_client.py --socket /home/minni/.minni/run/minnid.sock ping || exit 1

CMD ["python", "-u", "engine/minnid.py", "--socket", "/home/minni/.minni/run/minnid.sock"]
