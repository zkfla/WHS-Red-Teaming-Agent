FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY --chown=pwuser:pwuser pyproject.toml README.md ./
COPY --chown=pwuser:pwuser src ./src
COPY --chown=pwuser:pwuser configs ./configs
RUN python -m pip install .

USER pwuser
ENTRYPOINT ["whs-redteam"]
CMD ["--help"]
