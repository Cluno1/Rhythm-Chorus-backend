FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/pip pip install .
RUN useradd --create-home --uid 10001 rhythm \
    && mkdir -p /data \
    && chown -R rhythm:rhythm /data

EXPOSE 8000
USER rhythm
CMD ["uvicorn", "rhythm_metadata_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
