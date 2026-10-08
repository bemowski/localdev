# cnav — cluster navigator: static page rendering cards from /etc/cnav/cards.yaml
#   build + push: mise run build cnav    (from kube/localdev)
#   run locally:  docker run --rm -p 8080:8080 localhost:5005/cnav
FROM nginxinc/nginx-unprivileged:1.30-alpine

ADD --chmod=644 https://cdn.jsdelivr.net/npm/js-yaml@4.1.0/dist/js-yaml.min.js /usr/share/nginx/html/js-yaml.min.js
COPY --chmod=644 index.html /usr/share/nginx/html/index.html
COPY --chmod=644 nginx.conf /etc/nginx/conf.d/default.conf
# default cards; in-cluster a ConfigMap is mounted over /etc/cnav
COPY --chmod=644 cards.yaml /etc/cnav/cards.yaml
