# PI WEB (web UI for Pi Coding Agent): https://pi-web.dev
# Modelled on the upstream docker/ runtime (github.com/jmfederico/pi-web), but Debian-based
# and wired into this compose stack: one image, two services (piweb-sessiond + piweb).
ARG NODE_IMAGE=node:22-bookworm-slim
ARG DOCKER_CLI_IMAGE=docker:29-cli

FROM ${DOCKER_CLI_IMAGE} AS docker-cli

FROM ${NODE_IMAGE}

ARG PIWEB_VERSION=1.202610.1
ARG PIWEB_UID=1000
ARG PIWEB_GID=1000

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Build tools are needed for node-pty and for whatever the agent builds in the projects.
RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    tini ca-certificates curl wget git git-lfs openssh-client gnupg \
    build-essential python3 python3-pip python3-venv python3-dev \
    jq ripgrep fd-find fzf less vim-tiny procps iproute2 iputils-ping \
    unzip zip xz-utils bzip2 file tree rsync \
  && ln -s /usr/bin/fdfind /usr/local/bin/fd \
  && rm -rf /var/lib/apt/lists/*

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins /usr/local/libexec/docker/cli-plugins

# Pi Coding Agent comes in as PI WEB's peer dependency; expose its `pi` binary on PATH.
ENV NPM_CONFIG_UPDATE_NOTIFIER=false
RUN npm install -g --omit=dev --include=peer --no-audit --no-fund \
    "@jmfederico/pi-web@${PIWEB_VERSION}" --allow-scripts=node-pty \
  && peer_pi="$(npm root -g)/@jmfederico/pi-web/node_modules/.bin/pi" \
  && if [ -x "$peer_pi" ]; then ln -sf "$peer_pi" "$(npm prefix -g)/bin/pi"; fi \
  && pi --version \
  && npm cache clean --force

# The node image already owns UID 1000; replace it with the pi user so shells get a passwd entry.
RUN userdel -r node \
  && groupadd -g "${PIWEB_GID}" pi \
  && useradd -u "${PIWEB_UID}" -g "${PIWEB_GID}" -d /data/home -M -s /bin/bash pi

COPY entrypoint.sh /usr/local/bin/piweb-entrypoint
RUN chmod 0755 /usr/local/bin/piweb-entrypoint

ENV HOME=/data/home \
  XDG_CONFIG_HOME=/data/config \
  PI_WEB_CONFIG=/data/config/pi-web/config.json \
  PI_WEB_DATA_DIR=/data/pi-web \
  PI_WEB_SESSIOND_SOCKET=/data/pi-web/sessiond.sock \
  PI_CODING_AGENT_DIR=/data/pi-agent \
  PI_WEB_HOST=0.0.0.0 \
  PI_WEB_PORT=8504 \
  SHELL=/bin/bash \
  TERM=xterm-256color

WORKDIR /workspace
USER pi
EXPOSE 8504
ENTRYPOINT ["tini", "--", "piweb-entrypoint"]
CMD ["pi-web-server"]
