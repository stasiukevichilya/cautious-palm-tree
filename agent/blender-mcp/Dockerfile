ARG BLENDER_MCP_BASE_IMAGE=python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
FROM ${BLENDER_MCP_BASE_IMAGE}
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    DISABLE_TELEMETRY=1 BLENDER_MCP_DISABLE_TELEMETRY=1 BLENDERMCP_NO_UPDATE_CHECK=1
WORKDIR /app
COPY requirements.in requirements.lock ./
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.lock && pip check \
    && pip freeze > /opt/blender-mcp-installed.txt
RUN useradd --uid 1000 --create-home blender
COPY run.py ./
USER blender
EXPOSE 8080
CMD ["python", "run.py"]
