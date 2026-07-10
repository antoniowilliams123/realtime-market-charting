#!/usr/bin/env bash
# Concurrent SSE server. gevent worker handles many EventSource streams via
# greenlets; one worker is enough for a single operator. (spec §5.4.3)
#
# Feeds boot once when the app module loads under gunicorn (CHARTS_BOOT=1), not
# under __main__. Importing the module WITHOUT CHARTS_BOOT is side-effect-free.
cd "$(dirname "$0")"
exec gunicorn -k gevent -w 1 --worker-connections 128 \
  --timeout 0 --bind 127.0.0.1:${PORT:-8010} \
  --env CHARTS_BOOT=1 \
  "server:app"
