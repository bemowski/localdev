# usage cluster-agent: stdlib only, no dependencies to install.
#   build: mise run image (standalone) or, in localdev, mise run build usage-agent
FROM python:3.13-alpine
WORKDIR /app
COPY agent.py .
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001
CMD ["python", "/app/agent.py"]
