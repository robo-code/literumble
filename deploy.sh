#!/usr/bin/env bash
# Deploy to the SQLite host on rumble.robowiki.net. Run test_e2e.py first;
# abort the deploy if it fails.
set -euo pipefail

HOST=root@robowiki.net
DIR=/opt/literumble

echo "== running e2e tests =="
venv/bin/python test_e2e.py
venv/bin/python test_concurrency.py

# App Engine is now just the static redirect forwarder; no need to redeploy it here.
# gcloud app deploy --project=literumble app.yaml batchratings.yaml cron.yaml

echo "== deploying to rumble.robowiki.net =="
rsync -az --delete \
  --exclude '.git' --exclude '__pycache__' --exclude 'venv' --exclude 'data' \
  ./ "$HOST:$DIR/"
ssh "$HOST" "cd $DIR \
  && venv/bin/pip install -q -r requirements-local.txt \
  && cp deploy/literumble.service /etc/systemd/system/literumble.service \
  && cp deploy/nginx-rumble-http.conf  /etc/nginx/sites-available/rumble-http \
  && cp deploy/nginx-rumble-https.conf /etc/nginx/sites-available/rumble-https \
  && nginx -t \
  && BEFORE=\$(pgrep -f 'nginx: worker process\$' | sort -n | tr '\n' ' ') \
  && systemctl reload nginx \
  && sleep 2 \
  && AFTER=\$(pgrep -f 'nginx: worker process\$' | sort -n | tr '\n' ' ') \
  && { [ \"\$BEFORE\" != \"\$AFTER\" ] \
       || { echo 'nginx reload REJECTED (workers never respawned):'; \
            tail -3 /var/log/nginx/error.log; exit 1; }; } \
  && systemctl daemon-reload \
  && systemctl restart literumble"

echo "== done =="
