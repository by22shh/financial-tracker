# Единый артефакт приложения: api, worker и scheduler запускаются из него
# одной командой с разным аргументом (ADR-01, OPS-01).
#
# Сборка:  docker build -t fintracker:local .
# Запуск:  docker run --rm --env-file .env fintracker:local api --port 8080
#          docker run --rm --env-file .env fintracker:local worker
#          docker run --rm --env-file .env fintracker:local scheduler

FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# uv ставится с того же индекса пакетов, что и зависимости: отдельный
# реестр образов для сборки не требуется.
RUN pip install --no-cache-dir "uv>=0.5,<0.9"

WORKDIR /build

# Зависимости ставятся отдельным слоем: изменение исходников не пересобирает их.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable

COPY src ./src
COPY README.md alembic.ini ./
# Пакет ставится копией, а не ссылкой на каталог сборки: исполняемый образ
# не зависит от слоя сборки.
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Приложение не работает от root: файлы вложений и журналы принадлежат ему же.
RUN useradd --system --create-home --uid 10001 fintracker

# Миграции лежат внутри пакета: образ не зависит от исходного дерева.
COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
USER fintracker

# Готовность проверяется отдельно от живости (ADR-13, OPS-02).
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys;\
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health/ready', timeout=3).status == 200 else 1)"

ENTRYPOINT ["fintracker"]
CMD ["api"]
