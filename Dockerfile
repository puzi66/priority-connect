FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

USER nobody
ENV PRIORITY_MCP_TRANSPORT=http \
    PRIORITY_MCP_HOST=0.0.0.0 \
    PRIORITY_MCP_PORT=8741
EXPOSE 8741
ENTRYPOINT ["priority-mcp"]
