# Runs both processes: the 5-minute updater and the web server.
FROM python:3.12-slim

# tzdata is required -- the board renders Visnjan local time.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persist the database, the ephemeris cache and the nightly plan files.
VOLUME ["/app/data", "/app/plans"]

ENV WHICHNEO_HOST=0.0.0.0 WHICHNEO_PORT=8080
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=120s \
  CMD python3 -c "import urllib.request,sys; \
      sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/status', timeout=8).status==200 else 1)"

CMD ["./run.sh"]
