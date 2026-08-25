from uuid import uuid4

from fastapi import FastAPI, Request

from .observability import configure_logging, trace_id_var
from .routes import health_router, twilio_router, vapi_router

configure_logging("api")

app = FastAPI(title="RPT Agent API", version="0.2.0")
app.include_router(health_router)
app.include_router(vapi_router)
app.include_router(twilio_router)


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    trace_id = request.headers.get("x-trace-id") or uuid4().hex
    token = trace_id_var.set(trace_id)
    try:
        response = await call_next(request)
        response.headers["X-Trace-ID"] = trace_id
        return response
    finally:
        trace_id_var.reset(token)
