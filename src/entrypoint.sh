#!/bin/bash
set -e

# Generate configs from environment variables at startup.
# envsubst is used with an explicit variable list to avoid clobbering
# nginx's own $host / $uri / etc. variables.

envsubst '${ICECAST_SOURCE_PASSWORD} ${ICECAST_ADMIN_PASSWORD}' \
    < /etc/radio/icecast.xml.tmpl > /etc/radio/icecast.xml

envsubst '${STREAM_BITRATE} ${ICECAST_SOURCE_PASSWORD} ${STREAM_MOUNT} ${STREAM_NAME} ${STREAM_DESCRIPTION}' \
    < /etc/radio/radio.liq.tmpl > /etc/radio/radio.liq

envsubst '${STREAM_MOUNT}' \
    < /etc/radio/nginx.conf.tmpl > /etc/nginx/conf.d/default.conf

# Ensure shared data dir has a playlist file so Liquidsoap doesn't crash on startup
mkdir -p /data
if [ ! -f /data/background.m3u ]; then
    touch /data/background.m3u
fi

exec supervisord -n -c /etc/supervisor/conf.d/radio.conf
