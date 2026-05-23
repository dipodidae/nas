FROM nginx:1.27-alpine

RUN apk add --no-cache wget \
 && rm -rf /usr/share/nginx/html/* \
 && rm /etc/nginx/conf.d/default.conf

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY src/ /usr/share/nginx/html/

# nginx:alpine ships with an unprivileged 'nginx' user (uid 101) but the default
# image still runs master as root so it can bind :80. We listen on :8080 and run
# the whole process tree as non-root.
RUN sed -i 's/listen\s*80;/listen 8080;/' /etc/nginx/conf.d/default.conf \
 && chown -R nginx:nginx /var/cache/nginx /var/run /etc/nginx \
 && touch /var/run/nginx.pid \
 && chown nginx:nginx /var/run/nginx.pid

USER nginx
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:8080/healthz || exit 1
