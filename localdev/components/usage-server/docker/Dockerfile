# usage server. Build with ../build.sh, which stages ../openapi.yaml into the context.
FROM python:3.13-alpine
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY openapi.yaml .
COPY usage_server ./usage_server
ENV USAGE_SPEC=/app/openapi.yaml PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001
EXPOSE 8080 9090
CMD ["python", "-m", "usage_server"]
