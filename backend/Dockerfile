FROM python:3.12-slim
WORKDIR /srv
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
# Ohne die Revisionen startet die App nicht: das Schema kommt ausschliesslich
# von dort, es gibt kein create_all als Notnagel.
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./alembic.ini
COPY frontend ./frontend
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
