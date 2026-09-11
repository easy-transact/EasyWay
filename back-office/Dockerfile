# Multi-stage build: compile the Vite/React app, then serve the static
# output with nginx. VITE_API_URL is baked into the JS bundle at build time
# (Vite env vars are compile-time, not runtime) -- pass it as a build arg
# from docker-compose.prod.yml (BACK_OFFICE_API_URL there), pointing at the
# public API domain, e.g. https://easyway-api.easy-transact.net.
FROM node:20-alpine AS build
WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
ARG VITE_API_URL
ENV VITE_API_URL=${VITE_API_URL}
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
    CMD wget -qO- http://localhost/ >/dev/null || exit 1
