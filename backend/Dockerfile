FROM python:3.12-slim
WORKDIR /srv
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
# Ohne die Revisionen startet die App nicht: das Schema kommt ausschliesslich
# von dort, es gibt kein create_all als Notnagel.
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./alembic.ini
COPY VERSION ./VERSION
COPY frontend ./frontend
# Für die Betriebsskripte per `docker exec` (Ladepunkte importieren,
# VAPID-Schlüssel erzeugen) - siehe tools/*.py.
COPY tools ./tools
# Last, so that a new commit does not invalidate the layers above.
ARG GIT_SHA=dev
ENV GIT_SHA=$GIT_SHA
# Not as root: a bug that allows writing files or running code then does not
# own the container. Nothing is written to /srv at runtime (data lives in
# Postgres).
RUN useradd --system --no-create-home --uid 10001 jolt
USER jolt
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
