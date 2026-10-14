# cnav — cluster navigator: cards (cards.yaml) + live status column (status.yaml, /api/status)
#   build + push: mise run build cnav    (from the localdev root)
#   run locally:  docker run --rm -p 8080:8080 localhost:5005/cnav   (status shows "unavailable" outside a cluster)
FROM python:3.13-alpine

RUN pip install --no-cache-dir pyyaml==6.0.3

ADD https://cdn.jsdelivr.net/npm/js-yaml@4.1.0/dist/js-yaml.min.js /app/static/js-yaml.min.js
COPY static/ /app/static/
COPY server.py /app/server.py
# defaults; in-cluster the cnav-config ConfigMap is mounted over /etc/cnav
COPY cards.yaml status.yaml /etc/cnav/
# world-readable files, traversable dirs (ADD from a URL lands as 0600)
RUN chmod -R a+rX /app /etc/cnav

USER 65534
EXPOSE 8080
CMD ["python", "-u", "/app/server.py"]
