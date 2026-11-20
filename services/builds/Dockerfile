FROM golang:1.23-bookworm AS build
WORKDIR /app
COPY go.mod .
RUN go mod download
COPY . .
RUN --mount=type=cache,target=/root/.cache/go-build go mod tidy && CGO_ENABLED=1 GOMAXPROCS=2 go build -p 2 -o server . && CGO_ENABLED=1 GOMAXPROCS=2 go build -p 2 -o messaging ./cmd/messaging
FROM debian:bookworm-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl sqlite3 && rm -rf /var/lib/apt/lists/*
COPY --from=build /app/server /app/messaging ./
COPY component.json db-indexes.json telemetry-routes.json ./
CMD ["./server"]
