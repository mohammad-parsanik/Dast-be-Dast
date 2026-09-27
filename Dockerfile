FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install . && useradd --create-home app
COPY manage.py ./

USER app
RUN DJANGO_SECRET_KEY=build python manage.py collectstatic --noinput

EXPOSE 8000
CMD ["gunicorn", "dastbedast.wsgi", "--bind", "0.0.0.0:8000", "--workers", "2"]
