# One image for both `api` and `testmart`. Pin must match playwright in pyproject.toml.
FROM mcr.microsoft.com/playwright/python:v1.65.2-noble

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml ./
COPY atlas ./atlas
COPY testmart ./testmart
COPY eval ./eval
COPY web ./web
COPY sdk ./sdk
# Serve the SDK as a wheel at /sdk/ (static mount); build leftovers are removed.
RUN pip wheel ./sdk --no-deps -w web/sdk     && python -c "import glob,hashlib,json,os;w=glob.glob('web/sdk/*.whl')[0];n=os.path.basename(w);json.dump({'version':n.split('-')[1],'wheel':n,'sha256':hashlib.sha256(open(w,'rb').read()).hexdigest()},open('web/sdk/version.json','w'))"     && rm -rf sdk
RUN pip install --break-system-packages . \
    && mkdir -p /data/testmart && chown -R pwuser:pwuser /data

USER pwuser
EXPOSE 8000 8100
CMD ["uvicorn", "atlas.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
