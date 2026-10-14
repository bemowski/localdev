# localdev docs — markdown (localdev's docs/) rendered with MkDocs Material at build time, served by nginx.
#   build + push: mise run build docs   (components/docs/build.sh stages docs/ into the context)
#   run locally:  docker run --rm -p 8080:8080 localhost:5005/docs   -> http://localhost:8080/docs/

FROM python:3.13-alpine AS build
COPY requirements.txt /tmp/
RUN pip install --no-cache-dir --root-user-action=ignore -r /tmp/requirements.txt
COPY docs /src
# --strict: broken links and missing nav pages fail the build
RUN mkdocs build --strict -f /src/mkdocs.yml -d /site

FROM nginxinc/nginx-unprivileged:1.31.5-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /site /usr/share/nginx/html/docs
