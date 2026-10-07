ARG IMAGES_BASE_IMAGE=python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
FROM ${IMAGES_BASE_IMAGE}
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.in requirements.lock ./
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.lock && pip check \
    && pip freeze > /opt/images-installed.txt
RUN useradd --uid 1000 --create-home images
# The generator client is shared with the Telegram bot (compose additional context "tgbot").
COPY --from=tgbot images.py llm.py ./
COPY app.py ./
COPY tests ./tests
USER images
EXPOSE 8080
CMD ["uvicorn", "app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log"]
