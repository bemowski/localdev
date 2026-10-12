# api-auth: API-key check for api.localhost (Traefik forwardAuth). Stdlib only.
#   build + push: mise run build api-auth    (from kube/localdev)
FROM python:3.13-alpine
COPY server.py /app/server.py
USER 65534
EXPOSE 8080
CMD ["python", "-u", "/app/server.py"]
