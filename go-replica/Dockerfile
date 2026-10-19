# Multi-stage build. go.sum is generated at build time by `go mod tidy`
# (the repo ships only go.mod with pinned direct dependencies).
# Go >= 1.25: required by OTel Go >= v1.42.0. Earlier OTel Go versions reject W3C
# traceparent flags `03` (sampled + random, sent by OTel Python >= 1.4x when Django
# starts the trace itself), which broke the Django -> Go link in backend-only traces (S12).
FROM golang:1.25-alpine AS build
WORKDIR /src
COPY go.mod ./
COPY *.go ./
RUN go mod tidy && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/go-replica .

FROM gcr.io/distroless/static-debian12
COPY --from=build /out/go-replica /go-replica
EXPOSE 8090
ENTRYPOINT ["/go-replica"]
