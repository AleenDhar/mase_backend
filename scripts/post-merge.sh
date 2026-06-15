#!/bin/bash
set -e

echo "[post-merge] Installing Python dependencies..."
pip install -r requirements.txt --quiet

echo "[post-merge] Done."
