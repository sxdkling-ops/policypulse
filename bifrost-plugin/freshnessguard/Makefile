.PHONY: help test test-local image e2e

GO_IMAGE ?= golang:1.27.3-bookworm
ROOT := $(abspath ../..)

help: ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z0-9_-]+:.*?## / {printf "  %-12s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

test: ## go vet + go test -race in a Go container (works on any host with Docker)
	docker run --rm -v "$(CURDIR):/src" -w /src $(GO_IMAGE) sh -c "go vet ./... && go test -count=1 -race ./..."

test-local: ## go vet + go test -race with the local Go toolchain (Linux or macOS)
	go vet ./... && go test -count=1 -race ./...

image: ## Build Bifrost v2.5.1 (dynamic) with freshness_guard.so
	docker build -f $(ROOT)/docker/bifrost.Dockerfile -t policypulse/bifrost-freshness:2.5.1 $(ROOT)

e2e: ## Start the keyless demo stack and run the end-to-end test against it
	cd $(ROOT) && docker compose -f docker-compose.demo.yml up --build -d \
		&& python scripts/e2e_test.py http://localhost:8080 http://localhost:8000
