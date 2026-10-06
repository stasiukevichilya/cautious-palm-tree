ARG BILI_BASE_IMAGE=node:22-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392
FROM ${BILI_BASE_IMAGE}
ARG BILI_VERSION=0.1.185
# Pinned version, installed root-owned: the proxy runs as node and cannot self-update even if a check slipped through.
RUN npm install -g --omit=optional --no-fund --no-audit billion-context@${BILI_VERSION} \
    && npm cache clean --force && bili --help >/dev/null
ENV HOME=/data XDG_STATE_HOME=/data/state XDG_CONFIG_HOME=/data/config \
    ACP_AUTO_UPDATE=0 BILI_ADVISORY_CHECK=0 ACP_AUTO_RESTART_ON_UPDATE=0
USER node
WORKDIR /data
EXPOSE 8787
# --no-auto-update is required; ACP_AUTO_UPDATE=0 and BILI_ADVISORY_CHECK=0 also stop the forced
# "critical advisory" installs, which ignore the auto-update setting.
ENTRYPOINT ["bili", "--no-auto-update", "--host", "0.0.0.0", "--port", "8787"]
