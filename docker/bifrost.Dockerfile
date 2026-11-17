# Bifrost gateway with the Freshness Guard plugin.
#
# Go plugins can only be loaded by a dynamically linked Bifrost, and only when the plugin is built with
# the same Go version and the same dependency versions. This file follows Bifrost's documented
# "dynamic build" route and compiles the plugin inside Bifrost's own transports module, so the two can
# never drift apart.
#
#   docker build -f docker/bifrost.Dockerfile -t policypulse/bifrost-freshness .
ARG GO_VERSION=1.27.3

FROM golang:${GO_VERSION}-bookworm AS builder
ARG BIFROST_TAG=transports/v2.5.1
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone --depth 1 --branch "${BIFROST_TAG}" https://github.com/maximhq/bifrost.git
WORKDIR /src/bifrost/transports
ENV CGO_ENABLED=1 GOOS=linux GOWORK=off
RUN go mod download

# The gateway does not need the dashboard UI; a placeholder satisfies the go:embed in bifrost-http.
RUN mkdir -p bifrost-http/ui \
 && printf '%s' '<!doctype html><title>Bifrost + Freshness Guard</title><p>Bifrost gateway (dynamic build) with the Freshness Guard plugin. The dashboard UI is not bundled in this image.</p>' \
    > bifrost-http/ui/index.html

# Dynamic build: same flags as the official image minus -extldflags '-static' and the sqlite_static tag.
RUN go build -trimpath -ldflags="-w -s -X main.Version=v2.5.1-dynamic" -o /out/bifrost-http ./bifrost-http

# Build, vet and test the plugin inside the transports module (shared dependency graph).
COPY bifrost-plugin/freshnessguard/main.go bifrost-plugin/freshnessguard/guard.go bifrost-plugin/freshnessguard/guard_test.go ./freshnessguard/
RUN go vet ./freshnessguard \
 && go test -count=1 ./freshnessguard \
 && go build -buildmode=plugin -trimpath -ldflags="-w -s" -o /out/freshness_guard.so ./freshnessguard

FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates wget \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=builder /out/bifrost-http /app/bifrost-http
COPY --from=builder /out/freshness_guard.so /app/plugins/freshness_guard.so
COPY docker/bifrost-config.json /app/data/config.json
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/health || exit 1
CMD ["/app/bifrost-http", "-app-dir", "/app/data", "-host", "0.0.0.0", "-port", "8080"]
