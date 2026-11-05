#!/bin/bash
set -euo pipefail
cd /workspace/Nim
patch --batch --forward -p1 < /solution/implementation.diff
