#!/usr/bin/env bash
set -euo pipefail

if [ "${INSTALL_MEDIA_DEPS:-0}" = "1" ]; then
  echo "OCR system packages require the Docker runtime. Deploy with the repository Dockerfile."
  exit 1
fi

echo "Installing Python dependencies..."
pip install -r requirements.txt
