#!/bin/sh
cd "$(dirname "$0")"
export HF_HOME="$PWD/.hf-cache"
export HF_HUB_CACHE="$PWD/.hf-cache/hub"
export HF_ENDPOINT=https://hf-mirror.com
export TRANSFORMERS_CACHE="$PWD/.hf-cache"
exec ./.venv/bin/python smoke.py
