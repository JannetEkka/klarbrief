# Klarbrief app image. Standard library only: nothing is installed at build or run time,
# which keeps the image small and auditable for on-premise and air-gapped deployments.
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app
COPY src/ src/
COPY data/ data/
COPY tests/ tests/

RUN useradd --create-home --uid 10001 klarbrief && chown -R klarbrief /app
USER klarbrief

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" || exit 1

ENTRYPOINT ["python", "-m", "klarbrief"]
CMD ["run"]
