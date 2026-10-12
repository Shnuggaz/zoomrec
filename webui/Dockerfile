# Web interface of zoomrec. Build context is the repository root (shares zoomrec_core.py).
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Europe/Vienna \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Flask and waitress from the Ubuntu repositories, no pip needed
RUN apt-get update && \
    apt-get install --no-install-recommends -y python3 python3-flask python3-waitress tzdata && \
    ln -snf /usr/share/zoneinfo/${TZ} /etc/localtime && echo ${TZ} > /etc/timezone && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && \
    userdel -r ubuntu 2>/dev/null || true

WORKDIR /app
COPY zoomrec_core.py transcription.py /app/
COPY webui/app.py webui/wsgi.py /app/
COPY webui/templates /app/templates
COPY webui/static /app/static

# Same uid as the recorder so both can read and write the shared volumes
USER 1000
EXPOSE 8080
CMD ["sh", "-c", "umask ${UMASK:-0002} && exec python3 -m waitress --listen=0.0.0.0:8080 --threads=8 wsgi:app"]
