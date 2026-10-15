FROM python:3.13-slim

LABEL org.opencontainers.image.source="https://github.com/Bizla-My-Coach/Salon-Operation"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN addgroup --system django && adduser --system --home /home/django --ingroup django django

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY --chown=django:django . .
RUN sed -i 's/\r$//' /app/entrypoint.sh && \
    mkdir -p /app/staticfiles && \
    chown django:django /app/staticfiles && \
    chmod +x /app/entrypoint.sh

USER django
EXPOSE 8000
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["gunicorn", "salonops.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "180", "--access-logfile", "-"]
