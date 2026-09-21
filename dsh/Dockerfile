ARG NODE_IMAGE=node:24-bookworm-slim
FROM ${NODE_IMAGE}
ARG DSH_VERSION
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git ripgrep python3 make g++ \
    && rm -rf /var/lib/apt/lists/*
RUN npm install --global "@deepseek-ai/dsh@${DSH_VERSION}"
RUN mkdir -p /home/node/.dsh /workspace && chown -R node:node /home/node/.dsh /workspace
RUN npm install --global pnpm@10.28.2
USER node
ENV DSH_HOME=/home/node/.dsh
WORKDIR /workspace
CMD ["dsh", "web", "--no-open"]
