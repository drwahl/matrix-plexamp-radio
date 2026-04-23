# Single-container radio station
# Base: official Liquidsoap image (Debian-based, all audio plugins pre-built)
# Adds: Icecast2, nginx, Python, supervisord

FROM savonet/liquidsoap:main

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        icecast2 \
        nginx \
        python3 \
        python3-pip \
        supervisor \
        gettext-base \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/nginx/sites-enabled/default

# Python dependencies
COPY radio-service/requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

# Python app
COPY radio-service/app /app/app/

# Tests (run with: docker exec radio python3 -m pytest /app/tests/ -v)
COPY radio-service/tests /app/tests/
COPY radio-service/pytest.ini /app/pytest.ini

# Web player
COPY web /app/web/

# Config templates (rendered at container startup via entrypoint)
COPY config /etc/radio/

# Process supervisor config
COPY supervisord.conf /etc/supervisor/conf.d/radio.conf

# Entrypoint: render configs from env, init /data, exec supervisord
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

VOLUME ["/data"]

# All traffic in through nginx (web player, /api, /stream proxy)
EXPOSE 80

ENTRYPOINT ["/entrypoint.sh"]
