#!/bin/sh
set -e

# Prepare the database before the API accepts traffic.
#
# Both compose instances share one SQLite file. create_all runs in an
# explicit BEGIN IMMEDIATE transaction with retries (app/database.py:init_db),
# so a concurrent second instance either finds the complete schema or waits
# for the writer instead of racing on half-created tables.
python -m app.db_init

exec "$@"
