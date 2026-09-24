FROM python:3.13-slim

WORKDIR /app
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/pip pip install .
COPY scripts/provision_client_image_cos.py ./scripts/provision_client_image_cos.py
RUN useradd --create-home --uid 10001 rhythm \
    && mkdir -p /data \
    && chown -R rhythm:rhythm /data

EXPOSE 8000
USER rhythm
CMD ["uvicorn", "rhythm_metadata_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
