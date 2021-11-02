#!/bin/bash

set -euo pipefail

echo "==> $(date +%H:%M:%S) ==> Checking migrations lock... "
LOCK=`redis-cli -u $REDIS_URL SETNX migrations-lock 1`

if [ $LOCK -eq 1 ]; then
  echo "==> $(date +%H:%M:%S) ==> Got migrations lock... "
  # Expire lock in 20 minutes
  RESULT=`redis-cli -u $REDIS_URL EXPIRE migrations-lock 1200`
  echo "==> $(date +%H:%M:%S) ==> Migrating Django models... "
  python manage.py migrate --noinput

  echo "==> $(date +%H:%M:%S) ==> Setting up service... "
  python manage.py setup_service &
fi

echo "==> $(date +%H:%M:%S) ==> Collecting statics... "
DOCKER_SHARED_DIR=/nginx
rm -rf $DOCKER_SHARED_DIR/*
# STATIC_ROOT=$DOCKER_SHARED_DIR/staticfiles python manage.py collectstatic --noinput &
cp -r staticfiles/ $DOCKER_SHARED_DIR/

echo "==> $(date +%H:%M:%S) ==> Send via Slack info about service version and network"
python manage.py send_slack_notification &

echo "==> $(date +%H:%M:%S) ==> Running Gunicorn... "
exec gunicorn --config gunicorn.conf.py --pythonpath "$PWD" -b unix:$DOCKER_SHARED_DIR/gunicorn.socket -b 0.0.0.0:8888 config.wsgi:application
