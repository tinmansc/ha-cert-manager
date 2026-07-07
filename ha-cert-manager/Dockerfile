ARG BUILD_FROM=python:3.12-alpine
FROM ${BUILD_FROM}

# System deps for cryptography, paramiko, lxml
RUN apk add --no-cache \
    gcc musl-dev libffi-dev openssl-dev cargo \
    openssh-client openssl sshpass curl

WORKDIR /app

# Python backend
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Pre-built frontend static files
COPY frontend/dist /app/static

# Backend source
COPY backend /app/backend

# Bundled helper scripts (e.g. HPE 1950 deployer)
COPY scripts /app/scripts

COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
