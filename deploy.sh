#!/usr/bin/env bash
# Deploy to both targets (for now): App Engine and the SQLite host on
# rumble.robowiki.net. Run test_e2e.py first; abort the whole deploy if it fails.
set -euo pipefail

HOST=root@robowiki.net
DIR=/opt/literumble

echo "== running e2e tests =="
venv/bin/python test_e2e.py

echo "== deploying to App Engine =="
gcloud app deploy --project=literumble app.yaml batchratings.yaml cron.yaml

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
