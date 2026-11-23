#!/bin/zsh
cd "${0:A:h}"
if curl -fsS --max-time 1 http://127.0.0.1:8847/private-library.json >/dev/null 2>&1; then
  open http://127.0.0.1:8847/
else
  exec python3 tools/serve.py --open
fi
