ARG QWEN_IMAGE_BASE=pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime@sha256:c16f4c749e2d9e96878875cdf6cc45cddda1d1a36fddd371dd6f2360f1b6e2a2
FROM ${QWEN_IMAGE_BASE}
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/tmp/huggingface HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
COPY fetch_sources.py /opt/fetch_sources.py
RUN python /opt/fetch_sources.py
WORKDIR /opt/ComfyUI
COPY constraints.txt requirements.lock /opt/
RUN pip install --no-cache-dir -c /opt/constraints.txt -c /opt/requirements.lock \
      -r requirements.txt -r custom_nodes/ComfyUI-GGUF/requirements.txt \
      prometheus_client==0.26.0 \
    && pip check && pip list --format=freeze > /opt/qwen-image-installed.txt
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home qwenimage
COPY local_nodes /opt/ComfyUI/custom_nodes/local_qwen_image
COPY launch.py models.json /opt/qwen-image/
COPY workflows /opt/qwen-image/workflows
COPY extra_model_paths.yaml /opt/ComfyUI/extra_model_paths.yaml
COPY smoke.py /opt/qwen-image/smoke.py
USER qwenimage
EXPOSE 8188
CMD ["python", "/opt/qwen-image/launch.py"]
