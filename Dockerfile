FROM python:3.13-slim AS builder
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
RUN pip install --no-cache-dir "uv>=0.5,<0.9"
WORKDIR /build
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable
COPY src ./src
COPY README.md ./
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/opt/venv/bin:$PATH" \
    FINTRACKER_SHEETS__STATE_PATH=/var/lib/fintracker/sheetbot.sqlite3
RUN useradd --system --create-home --uid 10001 fintracker \
    && mkdir -p /var/lib/fintracker && chown fintracker:fintracker /var/lib/fintracker
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
USER fintracker
VOLUME ["/var/lib/fintracker"]
ENTRYPOINT ["fintracker"]
CMD ["poll"]
