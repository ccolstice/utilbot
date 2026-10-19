#!/usr/bin/env bash
set -euo pipefail

echo "Installing optional system packages for OCR..."
if [ "${INSTALL_MEDIA_DEPS:-0}" = "1" ] && command -v apt-get >/dev/null 2>&1; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y tesseract-ocr tesseract-ocr-rus
else
  echo "Skipping optional OCR system packages"
fi

echo "Installing Python dependencies..."
pip install -r requirements.txt

if [ "${INSTALL_MEDIA_DEPS:-0}" = "1" ]; then
  echo "Installing optional OCR Python dependencies..."
  pip install -r requirements-media.txt
fi
