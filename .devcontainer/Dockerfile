FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# VS Code Server prerequisites plus terminal/Git tools for the test workspace.
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash ca-certificates curl git libstdc++6 procps tar \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt /tmp/salonops-requirements.txt
RUN pip install --disable-pip-version-check -r /tmp/salonops-requirements.txt
# The repository bind mount belongs to the Codespaces host user.
RUN git config --system --add safe.directory /app
