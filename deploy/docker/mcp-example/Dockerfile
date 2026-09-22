FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir "mcp>=2,<3" \
    && useradd --create-home --uid 10001 mcp

COPY server.py .

USER mcp

EXPOSE 8765

CMD ["python", "server.py"]
