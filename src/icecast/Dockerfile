FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends icecast2 \
    && rm -rf /var/lib/apt/lists/*

EXPOSE 8000

ENTRYPOINT ["icecast2", "-c", "/etc/icecast2/icecast.xml"]
