# Multi-stage build. go.sum is generated at build time by `go mod tidy`
# (the repo ships only go.mod with pinned direct dependencies).
FROM golang:1.23-alpine AS build
WORKDIR /src
COPY go.mod ./
COPY *.go ./
RUN go mod tidy && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/go-replica .

FROM gcr.io/distroless/static-debian12
COPY --from=build /out/go-replica /go-replica
EXPOSE 8090
ENTRYPOINT ["/go-replica"]
