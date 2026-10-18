FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_ENV=development \
    DEMO_MODE=true \
    DATABASE_URL=sqlite:////app/state/aerointel_demo.db \
    PORT=7860 \
    HOME=/home/aero

WORKDIR /app
RUN useradd --uid 1000 --user-group --create-home --home-dir /home/aero aero \
    && mkdir -p /app/state \
    && chown -R 1000:1000 /app /home/aero

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY --chown=1000:1000 backend /app/backend
COPY --chown=1000:1000 frontend /app/frontend
COPY --chown=1000:1000 data /app/data
COPY --chown=1000:1000 pyproject.toml README.md /app/

# Recreate the ignored demo classifier from the checked-in CSV during image build.
RUN python -m backend.scripts.train_maintenance_model \
    && chown -R 1000:1000 /app /home/aero

EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','7860')+'/api/health', timeout=4)" || exit 1

USER 1000:1000
CMD if [ -z "${JWT_SECRET_KEY:-}" ]; then JWT_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"; export JWT_SECRET_KEY; echo "JWT_SECRET_KEY was empty; generated a random key for this container."; fi; python -c 'from backend.app.database import Base, engine; Base.metadata.create_all(bind=engine)' && exec uvicorn backend.app.main:app --host 0.0.0.0 --port "${PORT:-7860}"
