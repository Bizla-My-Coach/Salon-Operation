FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# VS Code Server prerequisites and a non-root development user with sudo access.
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash ca-certificates curl git libstdc++6 procps sudo tar \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 vscode \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash vscode \
    && printf 'vscode ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/vscode \
    && chmod 0440 /etc/sudoers.d/vscode
WORKDIR /workspaces/Salon-Operation
COPY requirements.txt /tmp/salonops-requirements.txt
RUN pip install --disable-pip-version-check -r /tmp/salonops-requirements.txt
COPY --chmod=0644 .devcontainer/bash-prompt.sh /etc/salonops-bash-prompt.sh
RUN printf '\nsource /etc/salonops-bash-prompt.sh\n' >> /home/vscode/.bashrc
# The repository bind mount belongs to the Codespaces host user.
RUN git config --system --add safe.directory /workspaces/Salon-Operation
