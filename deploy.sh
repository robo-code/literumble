#!/usr/bin/env bash
# Deploy to the SQLite host on rumble.robowiki.net. Run test_e2e.py first;
# abort the deploy if it fails.
set -euo pipefail

HOST=root@robowiki.net
DIR=/opt/literumble

echo "== running e2e tests =="
venv/bin/python test_e2e.py

# App Engine is now just the static redirect forwarder; no need to redeploy it here.
# gcloud app deploy --project=literumble app.yaml batchratings.yaml cron.yaml

echo "== deploying to rumble.robowiki.net =="
rsync -az --delete \
  --exclude '.git' --exclude '__pycache__' --exclude 'venv' --exclude 'data' \
  ./ "$HOST:$DIR/"
ssh "$HOST" "cd $DIR \
  && venv/bin/pip install -q -r requirements-local.txt \
  && cp deploy/literumble.service /etc/systemd/system/literumble.service \
  && systemctl daemon-reload \
  && systemctl restart literumble"

echo "== done =="
