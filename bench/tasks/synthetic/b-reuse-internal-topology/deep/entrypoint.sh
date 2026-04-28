#!/bin/sh
set -e
crond -b -L /var/log/cron.log
exec /usr/sbin/sshd -D -e
