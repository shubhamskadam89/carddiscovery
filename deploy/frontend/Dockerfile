# Frontend: a thin test page served by nginx. It holds no data and no secrets: everything it shows comes
# from the backend's API (/api/*), which nginx forwards. Replace it with any other frontend later; the
# engine (backend) doesn't change.
FROM nginx:1.27-alpine
COPY deploy/nginx/default.conf /etc/nginx/conf.d/default.conf
COPY cdserver/static/app.html /usr/share/nginx/html/index.html
