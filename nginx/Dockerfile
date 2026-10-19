# Builds the React app and serves it with nginx (build context = repo root).
FROM node:22-alpine AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# Vite inlines VITE_* at build time (the DSN is public by design).
ARG VITE_SENTRY_DSN_FE=""
ARG VITE_SENTRY_ENVIRONMENT=demo
ARG VITE_SENTRY_RELEASE=tail-sample-demo@0.1.0
ARG VITE_SENTRY_ORG=""
ARG VITE_JAEGER_URL=http://localhost:16686
# otlp (split transport -> collector) | sentry (stock transport -> Sentry). See frontend/src/sentry.js.
ARG VITE_FE_SPANS=otlp
ENV VITE_SENTRY_DSN_FE=$VITE_SENTRY_DSN_FE VITE_SENTRY_ENVIRONMENT=$VITE_SENTRY_ENVIRONMENT \
    VITE_SENTRY_RELEASE=$VITE_SENTRY_RELEASE VITE_SENTRY_ORG=$VITE_SENTRY_ORG VITE_JAEGER_URL=$VITE_JAEGER_URL \
    VITE_FE_SPANS=$VITE_FE_SPANS
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
# Demo-only: publish the FE_SPANS this build uses, so the loadgen can record the real mode.
ARG VITE_FE_SPANS=otlp
RUN echo "$VITE_FE_SPANS" > /usr/share/nginx/html/fe-spans.txt
