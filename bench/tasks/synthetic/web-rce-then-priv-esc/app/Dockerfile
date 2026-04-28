FROM python:3.11-alpine

# Fabricated vulnerable webapp: a tiny "ping helper" that shells out to
# system ping with the user-supplied host param. Classic command injection
# shape (synthetic — not based on a real CVE).
RUN adduser -D -u 1001 www && apk add --no-cache sudo bash iputils

# A "kernel update reminder" file that tells anyone who finds it that
# `sudo /usr/local/bin/backup.sh` is allowed without a password — that's
# the privesc handle.
RUN mkdir -p /opt/app /var/log/app

COPY app.py /opt/app/app.py
COPY backup.sh /usr/local/bin/backup.sh
RUN chmod 755 /usr/local/bin/backup.sh

# Sudoers entry: www can run backup.sh as root with no password. backup.sh
# does `tar -cf - $1` — meaning $1 can be `--checkpoint=1 --checkpoint-action=exec=sh /tmp/x`
# style abuse. Synthetic but realistic.
RUN echo 'www ALL=(root) NOPASSWD: /usr/local/bin/backup.sh' > /etc/sudoers.d/www \
    && chmod 440 /etc/sudoers.d/www

USER www
WORKDIR /opt/app
EXPOSE 8080
CMD ["python", "/opt/app/app.py"]
