#!/bin/bash
set -euo pipefail
mkdir -p /logs/verifier
chmod 700 /logs/verifier
rm -f /logs/verifier/reward.txt /logs/verifier/reward.json /logs/verifier/ctrf.json
exec python3 /tests/verify.py
