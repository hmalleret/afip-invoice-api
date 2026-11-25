"""
FastAPI application — AFIP Invoice Extraction API.

Endpoints:
  POST /extract          → extract invoice from document
  GET  /health           → health check + circuit breaker status
  GET  /schemas          → list supported schemas
"""

from __future__ import annotations

import base64
import io
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from PIL import Image

from google import genai
import pybreaker
import structlog
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import get_gemini_circuit_breaker, get_settings
from .extractors.llm_extractor import extract_with_llm
from .extractors.pdf_extractor import process_pdf
from .extractors.qr_extractor import scan_afip_qr
from .extractors.xml_extractor import extract_from_xml
from .models import ExtractionRequest, ExtractionResponse, InvoiceData
from .validators.invoice_validator import validate

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# Idempotency cache (in-memory, good enough for single instance)
# In production: replace with Redis
# ---------------------------------------------------------------------------
_idempotency_cache: dict[str, ExtractionResponse] = {}

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    if s.google_api_key:
        genai.Client(api_key=s.google_api_key)
        log.info("gemini_configured", model=s.gemini_model)
    else:
        log.warning("google_api_key_missing", msg="XML-only mode active")
    yield
    log.info("shutdown")


app = FastAPI(
    title=get_settings().api_title,
    version=get_settings().api_version,
    description=(
        "Extract structured JSON data from ARCA (ex AFIP) and LATAM electronic invoices. "
        "Supports XML (direct parse), PDF, and image inputs. "
        "Includes native QR code decoding (RG 4892), CAE validation, and CUIT checksums."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Global error handler — never expose 500 internals
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    log.error("unhandled_exception", path=request.url.path, error=str(exc), exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "An unexpected error occurred. This request was NOT billed.",
                "request_id": str(uuid.uuid4()),
            },
            "billed": False,
        },
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health", tags=["System"])
async def health():
    cb = get_gemini_circuit_breaker()
    return {
        "status": "ok",
        "version": get_settings().api_version,
        "gemini_circuit_breaker": cb.current_state,
        "gemini_fail_count": cb.fail_counter,
    }


@app.get("/schemas", tags=["System"])
async def list_schemas():
    return {
        "supported": [
            {
                "id": "afip_ar",
                "name": "ARCA (ex AFIP) Argentina",
                "description": "Facturas electrónicas ARCA (ex AFIP): tipos A, B, C, M, FCE, Notas de crédito y débito",
                "formats": ["application/xml", "application/pdf", "image/jpeg", "image/png", "text/plain"],
            },
            {
                "id": "sat_mx",
                "name": "SAT México (CFDI)",
                "description": "Comprobantes Fiscales Digitales por Internet (CFDI 4.0)",
                "formats": ["application/xml", "application/pdf"],
                "status": "beta",
            },
            {
                "id": "sri_ec",
                "name": "SRI Ecuador",
                "description": "Facturas electrónicas SRI Ecuador",
                "formats": ["application/xml"],
                "status": "beta",
            },
        ]
    }


@app.post(
    "/extract",
    response_model=ExtractionResponse,
    tags=["Extraction"],
    summary="Extract structured data from an invoice document",
    responses={
        200: {"description": "Successful extraction"},
        400: {"description": "Invalid input (not billed)"},
        402: {"description": "Low confidence result — not billed"},
        429: {"description": "Rate limited"},
        503: {"description": "Upstream service unavailable (not billed)"},
    },
)
async def extract_invoice(
    body: ExtractionRequest,
    x_rapidapi_proxy_secret: Optional[str] = Header(None),
) -> ExtractionResponse:
    request_id = str(uuid.uuid4())
    t_start = time.monotonic()
    s = get_settings()

    # --- RapidAPI proxy secret validation (optional) ---
    if s.rapidapi_proxy_secret and x_rapidapi_proxy_secret != s.rapidapi_proxy_secret:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FORBIDDEN", "message": "Invalid proxy secret"},
        )

    # --- Idempotency ---
    if body.idempotency_key and body.idempotency_key in _idempotency_cache:
        log.info("idempotency_hit", key=body.idempotency_key)
        return _idempotency_cache[body.idempotency_key]

    # --- Content size limit ---
    content_size_mb = len(body.content.encode()) / (1024 * 1024)
    if content_size_mb > s.max_content_size_mb:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "CONTENT_TOO_LARGE",
                "message": f"Content exceeds {s.max_content_size_mb}MB limit",
                "billed": False,
            },
        )

    # --- Route to extractor ---
    invoice: Optional[InvoiceData] = None
    billed = True

    try:
        ct = body.content_type.lower()

        # ── XML path (deterministic, no LLM) ──────────────────────────────
        if "xml" in ct:
            # Content can be base64-encoded or raw XML
            xml_str = _decode_content(body.content)
            invoice = extract_from_xml(xml_str)

        # ── PDF path ───────────────────────────────────────────────────────
        elif ct == "application/pdf":
            pdf_res = process_pdf(body.content)
            qr = pdf_res.qr_data

            if qr and not s.google_api_key:
                # LLM not configured, but AFIP QR has full fiscal ground-truth
                invoice = InvoiceData(
                    cae=qr["cae"],
                    tipo_comprobante=qr["tipo_comprobante"],
                    tipo_comprobante_descripcion=qr["tipo_comprobante_descripcion"],
                    punto_venta=qr["punto_venta"],
                    numero_comprobante=qr["numero_comprobante"],
                    numero_completo=qr["numero_completo"],
                    fecha_emision=qr["fecha_emision"],
                    total=qr["total"],
                    moneda_id=qr["moneda_id"],
                    moneda_cotizacion=qr["moneda_cotizacion"],
                    emisor_cuit=qr["emisor_cuit"],
                    receptor_cuit=qr["receptor_cuit"],
                    confidence=0.98,
                    source_format="pdf",
                    extraction_method="afip_qr",
                )
            elif not s.google_api_key:
                raise HTTPException(
                    503,
                    detail={
                        "code": "LLM_UNAVAILABLE",
                        "message": "Google API key is not configured and no AFIP QR was detected",
                        "billed": False,
                    },
                )
            elif pdf_res.is_scanned:
                # Scanned PDF: convert page 1 to base64 PNG for Vision LLM
                if not pdf_res.page_images:
                    raise HTTPException(
                        400,
                        detail={"code": "INVALID_PDF", "message": "Could not render scanned PDF", "billed": False},
                    )
                buf = io.BytesIO()
                pdf_res.page_images[0].save(buf, format="PNG")
                img_b64 = base64.b64encode(buf.getvalue()).decode()
                invoice = extract_with_llm(
                    text=None,
                    image_b64=img_b64,
                    image_mime="image/png",
                    source_format="pdf_scanned",
                )
            else:
                # Digital text PDF
                invoice = extract_with_llm(
                    text=pdf_res.text,
                    image_b64=None,
                    image_mime=None,
                    source_format="pdf",
                )

            # If QR was found, overlay ground-truth fiscal numbers
            if qr and invoice:
                if qr.get("cae") and not invoice.cae:
                    invoice.cae = qr["cae"]
                if qr.get("total") and (not invoice.total or invoice.total == 0):
                    invoice.total = qr["total"]
                if qr.get("tipo_comprobante") and not invoice.tipo_comprobante:
                    invoice.tipo_comprobante = qr["tipo_comprobante"]
                    invoice.tipo_comprobante_descripcion = qr["tipo_comprobante_descripcion"]
                if qr.get("emisor_cuit") and not invoice.emisor_cuit:
                    invoice.emisor_cuit = qr["emisor_cuit"]
                invoice.extraction_method = "hybrid_qr_llm"
                invoice.confidence = max(invoice.confidence, 0.98)

        # ── Image path ─────────────────────────────────────────────────────
        elif ct.startswith("image/"):
            raw_img_bytes = base64.b64decode(body.content)
            pil_img = Image.open(io.BytesIO(raw_img_bytes)).convert("RGB")
            qr = scan_afip_qr(pil_img)

            if qr and not s.google_api_key:
                invoice = InvoiceData(
                    cae=qr["cae"],
                    tipo_comprobante=qr["tipo_comprobante"],
                    tipo_comprobante_descripcion=qr["tipo_comprobante_descripcion"],
                    punto_venta=qr["punto_venta"],
                    numero_comprobante=qr["numero_comprobante"],
                    numero_completo=qr["numero_completo"],
                    fecha_emision=qr["fecha_emision"],
                    total=qr["total"],
                    moneda_id=qr["moneda_id"],
                    moneda_cotizacion=qr["moneda_cotizacion"],
                    emisor_cuit=qr["emisor_cuit"],
                    receptor_cuit=qr["receptor_cuit"],
                    confidence=0.98,
                    source_format="image",
                    extraction_method="afip_qr",
                )
            elif not s.google_api_key:
                raise HTTPException(
                    503,
                    detail={"code": "LLM_UNAVAILABLE", "message": "Google API key is not configured and no AFIP QR was detected", "billed": False},
                )
            else:
                invoice = extract_with_llm(
                    text=None,
                    image_b64=body.content,
                    image_mime=ct,
                    source_format="image",
                )
                if qr and invoice:
                    if qr.get("cae"):
                        invoice.cae = qr["cae"]
                    if qr.get("total"):
                        invoice.total = qr["total"]
                    if qr.get("tipo_comprobante"):
                        invoice.tipo_comprobante = qr["tipo_comprobante"]
                    invoice.extraction_method = "hybrid_qr_llm"
                    invoice.confidence = max(invoice.confidence, 0.98)

        # ── Plain text path ────────────────────────────────────────────────
        elif ct == "text/plain":
            # Could be raw XML embedded as text — try XML first
            decoded = _decode_content(body.content)
            if decoded.strip().startswith("<"):
                try:
                    invoice = extract_from_xml(decoded)
                except ValueError:
                    pass  # Fall through to LLM

            if invoice is None:
                if not s.google_api_key:
                    raise HTTPException(503, detail={"code": "LLM_UNAVAILABLE", "billed": False})
                invoice = extract_with_llm(
                    text=decoded,
                    image_b64=None,
                    image_mime=None,
                    source_format="text",
                )

        else:
            raise HTTPException(
                400,
                detail={"code": "UNSUPPORTED_CONTENT_TYPE", "billed": False, "message": ct},
            )

    except HTTPException:
        raise
    except pybreaker.CircuitBreakerError:
        raise HTTPException(
            503,
            detail={
                "code": "UPSTREAM_UNAVAILABLE",
                "message": "Gemini API is temporarily unavailable. Retry after 60 seconds.",
                "retry_after": 60,
                "billed": False,
            },
        )
    except ValueError as exc:
        # Input format errors — not billed
        raise HTTPException(
            400,
            detail={"code": "INVALID_INPUT", "message": str(exc), "billed": False},
        )
    except RuntimeError as exc:
        raise HTTPException(
            503,
            detail={"code": "EXTRACTION_FAILED", "message": str(exc), "billed": False},
        )

    # --- Post-extraction validation ---
    invoice.country_schema = body.country_schema
    invoice = validate(invoice)

    # --- Billing gate: don't charge for very low confidence ---
    if invoice.confidence < s.min_confidence_to_bill:
        billed = False
        invoice.warnings.append(
            f"Confidence {invoice.confidence:.2f} < {s.min_confidence_to_bill} threshold — not billed"
        )

    processing_ms = int((time.monotonic() - t_start) * 1000)

    response = ExtractionResponse(
        success=True,
        invoice=invoice,
        request_id=request_id,
        processing_time_ms=processing_ms,
        billed=billed,
    )

    # --- Cache idempotency ---
    if body.idempotency_key:
        _idempotency_cache[body.idempotency_key] = response
        # Prevent unbounded growth (simple LRU-lite)
        if len(_idempotency_cache) > 10_000:
            oldest = next(iter(_idempotency_cache))
            del _idempotency_cache[oldest]

    log.info(
        "extraction_complete",
        request_id=request_id,
        source=invoice.source_format,
        method=invoice.extraction_method,
        confidence=invoice.confidence,
        billed=billed,
        ms=processing_ms,
    )

    return response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _decode_content(content: str) -> str:
    """Try base64 decode first; fall back to treating as raw string."""
    try:
        decoded = base64.b64decode(content).decode("utf-8", errors="replace")
        # Sanity check: base64 round-trip shouldn't produce junk
        if "\x00" not in decoded:
            return decoded
    except Exception:
        pass
    return content


# ---------------------------------------------------------------------------
# Dev entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run("src.main:app", host="0.0.0.0", port=8000, reload=True)
