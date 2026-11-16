# One image for both `api` and `testmart`. Pin must match playwright in pyproject.toml.
FROM mcr.microsoft.com/playwright/python:v1.65.2-noble

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml ./
COPY atlas ./atlas
COPY testmart ./testmart
COPY eval ./eval
COPY web ./web
RUN pip install --break-system-packages . \
    && mkdir -p /data/testmart && chown -R pwuser:pwuser /data

USER pwuser
EXPOSE 8000 8100
CMD ["uvicorn", "atlas.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
