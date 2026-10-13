// go-replica: a small downstream Go service called by the Django API.
//
//   - Spans:  OpenTelemetry SDK + otelhttp, exported over OTLP/HTTP to the collector
//     (endpoint from the standard OTEL_EXPORTER_OTLP_ENDPOINT env var).
//   - Errors: sentry-go, sent straight to Sentry (never sampled). Each error event gets
//     the active OTel trace_id/span_id (via the scope's propagation context) so it
//     lands on the same trace.
//   - Propagation: W3C traceparent/baggage (incoming from Django).
//
// Endpoints:
//
//	GET /replica/status?model_id=N[&fail=1]   fail=1 -> 500 + Sentry error (scenario S4)
//	GET /billing/quote?delay_ms=N             slow "third-party" billing provider (S7)
//	GET /healthz
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"math/rand"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/getsentry/sentry-go"
	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

var tracer = otel.Tracer("go-replica")

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// setupTracing wires the OTel SDK: OTLP/HTTP exporter -> collector, W3C propagation,
// parent-based always-on sampling (the collector does the real, tail-based sampling).
func setupTracing(ctx context.Context) (func(context.Context) error, error) {
	exporter, err := otlptracehttp.New(ctx) // reads OTEL_EXPORTER_OTLP_ENDPOINT etc.
	if err != nil {
		return nil, err
	}
	res, err := resource.New(ctx,
		resource.WithAttributes(attribute.String("service.name", "demo-go-replica")), // default...
		resource.WithFromEnv(), // ...overridable via OTEL_SERVICE_NAME / OTEL_RESOURCE_ATTRIBUTES
		resource.WithTelemetrySDK(),
	)
	if err != nil {
		return nil, err
	}
	tp := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(exporter, sdktrace.WithBatchTimeout(time.Second)),
		sdktrace.WithResource(res),
		sdktrace.WithSampler(sdktrace.ParentBased(sdktrace.AlwaysSample())),
	)
	otel.SetTracerProvider(tp)
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
		propagation.TraceContext{}, propagation.Baggage{},
	))
	tracer = tp.Tracer("go-replica")
	return tp.Shutdown, nil
}

// captureError sends err to Sentry, linked to the current OTel span, and marks that
// span as ERROR so the collector's `status_code: [ERROR]` policy keeps the trace.
//
// Linking: Sentry tracing is off in this service, so sentry-go's Scope.ApplyToEvent
// (v0.31.1) ALWAYS overwrites event.Contexts["trace"] with the scope's propagation
// context. Setting Contexts["trace"] on the event is therefore not enough; instead the
// OTel trace/span id is put into the (cloned) scope's propagation context, which also
// gives the event a dynamic sampling context with the right trace id. sentry.TraceID /
// sentry.SpanID and the OTel trace.TraceID / trace.SpanID are both [16]byte / [8]byte.
// beforeSend (below) re-applies the OTel ids as a second line of defence.
func captureError(ctx context.Context, r *http.Request, err error) {
	span := trace.SpanFromContext(ctx)
	span.RecordError(err)
	span.SetStatus(codes.Error, err.Error())

	hub := sentry.CurrentHub().Clone()
	client := hub.Client()
	if client == nil {
		return // Sentry not configured (no DSN)
	}
	scope := hub.Scope()
	if sc := span.SpanContext(); sc.IsValid() {
		scope.SetPropagationContext(sentry.PropagationContext{
			TraceID: sentry.TraceID(sc.TraceID()),
			SpanID:  sentry.SpanID(sc.SpanID()),
		})
	}
	if r != nil {
		scope.SetRequest(r)
	}
	scope.SetTag("demo_team", "inference")

	event := client.EventFromException(err, sentry.LevelError)
	// The hint carries ctx so beforeSend can find the OTel span.
	client.CaptureEvent(event, &sentry.EventHint{Context: ctx, OriginalException: err}, scope)
}

// beforeSend runs after the scope was applied. For events captured with a context in
// the hint (captureError above, or sentry-go integrations that set EventHint.Context,
// e.g. the panic recovery of sentryhttp / RecoverWithContext):
//   - it stamps contexts.trace with the active OTel trace/span id, and
//   - for any event at level error/fatal (not only exceptions, e.g. CaptureMessage at
//     LevelError) it marks that OTel span ERROR, so the tail sampler keeps the trace.
//
// Events captured WITHOUT a context in the hint (plain sentry.CaptureMessage(...)) are not
// linked; capture through captureError or pass the context in the hint.
func beforeSend(event *sentry.Event, hint *sentry.EventHint) *sentry.Event {
	if event == nil || hint == nil || hint.Context == nil {
		return event
	}
	span := trace.SpanFromContext(hint.Context)
	sc := span.SpanContext()
	if !sc.IsValid() {
		return event
	}
	if event.Contexts == nil {
		event.Contexts = map[string]sentry.Context{}
	}
	event.Contexts["trace"] = sentry.Context{
		"trace_id": sc.TraceID().String(),
		"span_id":  sc.SpanID().String(),
	}
	if event.Level == sentry.LevelError || event.Level == sentry.LevelFatal {
		msg := event.Message
		if len(event.Exception) > 0 {
			last := event.Exception[len(event.Exception)-1]
			msg = last.Type + ": " + last.Value
		}
		span.SetStatus(codes.Error, msg)
	}
	return event
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func replicaStatus(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	modelID := r.URL.Query().Get("model_id")

	// A child span for the (simulated) replica-pool lookup.
	ctx, span := tracer.Start(ctx, "replica.pool.lookup", trace.WithAttributes(attribute.String("model.id", modelID)))
	time.Sleep(time.Duration(15+rand.Intn(25)) * time.Millisecond)

	if r.URL.Query().Get("fail") == "1" {
		err := fmt.Errorf("replica pool for model %s unreachable: %w", modelID, errors.New("dial tcp 10.0.3.17:9000: connect: connection refused"))
		captureError(ctx, r, err) // marks the lookup span ERROR + Sentry issue
		span.End()
		// otelhttp marks the server span ERROR for 5xx responses.
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "replica pool unreachable"})
		return
	}
	span.End()

	writeJSON(w, http.StatusOK, map[string]any{
		"model_id": modelID,
		"replicas": []map[string]any{
			{"id": "replica-a", "state": "ACTIVE", "gpu": "A10G"},
			{"id": "replica-b", "state": "ACTIVE", "gpu": "A10G"},
		},
	})
}

func billingQuote(w http.ResponseWriter, r *http.Request) {
	delayMs, err := strconv.Atoi(r.URL.Query().Get("delay_ms"))
	if err != nil || delayMs < 0 {
		delayMs = 1800
	}
	_, span := tracer.Start(r.Context(), "billing.provider.quote")
	time.Sleep(time.Duration(delayMs) * time.Millisecond) // the slow third party
	span.End()
	writeJSON(w, http.StatusOK, map[string]any{"currency": "USD", "monthly_estimate": 1234.56, "delay_ms": delayMs})
}

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	if err := sentry.Init(sentry.ClientOptions{
		Dsn:              os.Getenv("SENTRY_DSN_BE"), // empty DSN = Sentry disabled
		Environment:      envOr("SENTRY_ENVIRONMENT", "demo"),
		Release:          envOr("SENTRY_RELEASE", "tail-sample-demo@0.1.0"),
		ServerName:       "go-replica",
		AttachStacktrace: true,
		BeforeSend:       beforeSend,
	}); err != nil {
		log.Printf("sentry.Init: %v", err)
	}
	defer sentry.Flush(2 * time.Second)

	shutdown, err := setupTracing(ctx)
	if err != nil {
		log.Fatalf("otel setup: %v", err)
	}
	defer func() {
		c, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = shutdown(c)
	}()

	mux := http.NewServeMux()
	mux.HandleFunc("/replica/status", replicaStatus)
	mux.HandleFunc("/billing/quote", billingQuote)
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
	})

	// otelhttp: SERVER span per request, extracts the incoming W3C traceparent.
	handler := otelhttp.NewHandler(mux, "go-replica",
		otelhttp.WithSpanNameFormatter(func(_ string, r *http.Request) string {
			return r.Method + " " + r.URL.Path
		}),
		otelhttp.WithFilter(func(r *http.Request) bool { return r.URL.Path != "/healthz" }),
	)

	srv := &http.Server{Addr: ":" + envOr("PORT", "8090"), Handler: handler, ReadHeaderTimeout: 5 * time.Second}
	go func() {
		log.Printf("go-replica listening on %s", srv.Addr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Printf("listen: %v", err)
			stop() // log.Fatalf here would skip the deferred sentry.Flush / tracer shutdown
		}
	}()

	<-ctx.Done()
	c, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = srv.Shutdown(c)
}
