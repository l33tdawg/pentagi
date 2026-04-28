#!/bin/sh
set -e

# Start sshd
/usr/sbin/sshd -e

# Start cron in background
crond -b -L /var/log/cron.log

# Run redis as the redis user so the dump file is owned by redis:redis
exec su -s /bin/sh redis -c '/usr/bin/redis-server /etc/redis.conf'
