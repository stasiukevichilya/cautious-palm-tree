ARG SDXL_BASE_IMAGE=pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime@sha256:c16f4c749e2d9e96878875cdf6cc45cddda1d1a36fddd371dd6f2360f1b6e2a2
FROM ${SDXL_BASE_IMAGE}
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/tmp/huggingface HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    SDXL_MODEL_PATH=/models SDXL_OUTPUT_PATH=/outputs
WORKDIR /app
COPY requirements.in requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock && pip check \
    && pip freeze > /opt/sdxl-installed.txt
RUN useradd --uid 1000 --create-home sdxl && mkdir /outputs && chown sdxl:sdxl /outputs
COPY . .
USER sdxl
EXPOSE 8080
CMD ["uvicorn", "app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log"]
