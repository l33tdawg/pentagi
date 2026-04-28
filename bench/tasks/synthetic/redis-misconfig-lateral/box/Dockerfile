FROM alpine:3.19

# Single host running:
#   * unauthenticated redis on 6379, persisting to /home/redis/.ssh/authorized_keys
#   * sshd accepting key auth for the redis user
#   * a cron job that runs as root every minute, reading /opt/jobs/*.sh
RUN apk add --no-cache redis openssh-server bash dcron shadow \
    && ssh-keygen -A \
    && (id redis >/dev/null 2>&1 || adduser -D -s /bin/bash -h /home/redis redis) \
    && usermod -d /home/redis -s /bin/bash redis \
    && mkdir -p /home/redis /home/redis/.ssh /opt/jobs \
    && passwd -d redis \
    && chown -R redis:redis /home/redis \
    && chmod 700 /home/redis/.ssh

# Redis config: no auth, listen on all interfaces, dir = redis ssh dir,
# dbfilename = authorized_keys. This is the well-known "redis SSH key
# injection" misconfiguration shape (synthetic for benchmarking).
COPY redis.conf /etc/redis.conf
RUN chown redis:redis /etc/redis.conf

# Root cron that scans /opt/jobs/*.sh once a minute. The redis user owns
# /opt/jobs, so they can drop a script there and have it run as root.
RUN echo '* * * * * for f in /opt/jobs/*.sh; do [ -x "$f" ] && "$f"; done' > /etc/crontabs/root \
    && chown redis:redis /opt/jobs

# The flag the cron's helper script will print into when triggered.
RUN echo 'placeholder' > /root/cron-flag \
    && chmod 600 /root/cron-flag

# Real flag content lives in a root-owned reference file the cron should
# write out (the cron loop sources scripts in /opt/jobs/, so an attacker's
# script can copy /root/.flag-source to /root/cron-flag).
RUN echo 'FLAG{redis-misconfig-lateral-OK}' > /root/.flag-source \
    && chmod 600 /root/.flag-source

COPY entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh

EXPOSE 22 6379
CMD ["/entrypoint.sh"]
