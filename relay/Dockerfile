FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RELAY_HOST=0.0.0.0 \
    RELAY_PORT=8790

WORKDIR /app

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin relay

COPY pyproject.toml README.md ./
COPY __init__.py config.py app.py limits.py tokens.py ./
COPY logging ./logging/
COPY auth ./auth/
COPY brain ./brain/
COPY storage ./storage/
COPY api ./api/
COPY speech ./speech/

RUN pip install --no-cache-dir .

USER relay

EXPOSE 8790

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8790/health', timeout=3).status==200 else 1)"

CMD ["uvicorn", "relay.app:app", "--host", "0.0.0.0", "--port", "8790"]
