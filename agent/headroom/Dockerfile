ARG HEADROOM_BASE_IMAGE=python:3.13-slim@sha256:bf44cdfcb76cd3b41e879bc058fc37ec5872002ccfde7fcb765e218cde0cd79c
FROM ${HEADROOM_BASE_IMAGE}
ARG HEADROOM_VERSION=0.40.0
# Pinned version: [proxy] brings the ONNX Kompress text compressor (no torch), [code] the AST code compressor.
RUN pip install --no-cache-dir "headroom-ai[proxy,code]==${HEADROOM_VERSION}" && headroom --version
RUN useradd --uid 1000 --create-home --home-dir /data headroom
# Kompress weights (~500 MB) download from Hugging Face on first use and stay in /data/hf.
ENV HOME=/data HF_HOME=/data/hf \
    HEADROOM_BEACON=off HEADROOM_UPDATE_CHECK=off DO_NOT_TRACK=1
USER headroom
WORKDIR /data
EXPOSE 8787
ENTRYPOINT ["headroom", "proxy", "--host", "0.0.0.0", "--port", "8787", "--no-telemetry"]
