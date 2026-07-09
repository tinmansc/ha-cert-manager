#!/bin/sh

export PORT=8099
export OPTIONS_FILE=/config/certfleet/config.json

echo "[INFO] Starting CertFleet on port ${PORT}..."

exec python3 /app/backend/main.py
