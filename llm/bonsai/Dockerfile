# llama-server from PrismML's llama.cpp fork (branch "prism"). Upstream llama.cpp
# cannot load Ternary-Bonsai: PQ2_0/PTQ1_0 quant types and Hadamard-folded weights
# (prism.hadamard) exist only in the fork. Release b10743 already contains the
# MTP token-embedding Hadamard-inverse fix (PR #205), required by the -MTP GGUFs.
# The release is built on Ubuntu 22.04 with CUDA 12.8 for all supported archs.
ARG BONSAI_BASE_IMAGE=nvidia/cuda:12.8.1-runtime-ubuntu22.04@sha256:4a801ef9232d2b05e69df4eb8aa054dbbe2824e5499e1e6e857320bb01ac41a9
FROM ${BONSAI_BASE_IMAGE}
ARG PRISM_RELEASE=prism-b10743-adfffbe
ARG PRISM_SHA256=43b73a24d5cd83c4482750ee52e59afac497c669c008a319e44e43a0033757e2
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 libssl3 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --no-create-home --home-dir /tmp bonsai
ADD --checksum=sha256:${PRISM_SHA256} \
    https://github.com/PrismML-Eng/llama.cpp/releases/download/${PRISM_RELEASE}/llama-${PRISM_RELEASE}-bin-linux-cuda-12.8-x64.tar.gz \
    /tmp/prism.tar.gz
RUN mkdir /app && tar -xzf /tmp/prism.tar.gz -C /app --strip-components=1 && rm /tmp/prism.tar.gz \
    && /app/llama-server --version
ENV LD_LIBRARY_PATH=/app LLAMA_ARG_HOST=0.0.0.0 LLAMA_ARG_PORT=8080
USER bonsai
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=180s --retries=4 \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1
ENTRYPOINT ["/app/llama-server"]
