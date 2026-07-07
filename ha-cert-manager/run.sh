#!/usr/bin/with-contenv bashio

export PORT=8099
export OPTIONS_FILE=/config/ha_cert_manager/config.json

bashio::log.info "Starting HA Cert Manager on port ${PORT}..."

exec python3 /app/backend/main.py
