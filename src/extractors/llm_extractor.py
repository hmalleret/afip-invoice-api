"""
LLM extractor using Gemini Flash via google-genai SDK (v1+).
Handles PDF text, images, and plain text inputs.
Implements multi-pass extraction with self-correction on validation failure.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional

import pybreaker
import structlog
from google import genai
from google.genai import types as genai_types
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..config import get_gemini_circuit_breaker, get_settings
from ..models import TIPO_COMPROBANTE, IVAItem, InvoiceData, LineItem

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are an expert at extracting structured data from ARCA (ex AFIP) electronic invoices (facturas electrónicas argentinas) and other LATAM tax documents.

RULES — follow strictly:
1. Output ONLY a valid JSON object. No markdown, no code fences, no explanations.
2. Dates: ISO format YYYY-MM-DD only. If a date is ambiguous use null.
3. Monetary values: numeric only (no $ signs, no thousands separators). Round to 4 decimal places.
4. CUIT: 11 digits, strip hyphens. Example: "20123456789".
5. CAE: exactly 14 digits. If not found, null.
6. IVA rates: only 0, 2.5, 5, 10.5, 21, 27 are valid ARCA/AFIP rates.
7. tipo_comprobante: use ARCA/AFIP numeric code (1=FactA, 6=FactB, 11=FactC, 51=FactM, 201=FCE-A...).
8. If a field is not found or uncertain, use null — do not guess.
9. confidence: float 0.0–1.0. Use 0.9+ only if you found CAE + total + emisor_cuit.
10. warnings: list of strings describing any anomalies or missing critical fields.

JSON SCHEMA:
{
  "cae": "14-digit string or null",
  "cae_vencimiento": "YYYY-MM-DD or null",
  "tipo_comprobante": integer_or_null,
  "tipo_comprobante_descripcion": "string or null",
  "punto_venta": integer_or_null,
  "numero_comprobante": integer_or_null,
  "numero_completo": "XXXX-XXXXXXXX or null",
  "fecha_emision": "YYYY-MM-DD or null",
  "concepto": 1_or_2_or_3_or_null,
  "concepto_descripcion": "string or null",
  "fecha_desde": "YYYY-MM-DD or null",
  "fecha_hasta": "YYYY-MM-DD or null",
  "fecha_vto_pago": "YYYY-MM-DD or null",
  "moneda_id": "PES",
  "moneda_descripcion": "Pesos Argentinos",
  "moneda_cotizacion": 1.0,
  "emisor_cuit": "11 digits or null",
  "emisor_razon_social": "string or null",
  "emisor_domicilio": "string or null",
  "emisor_condicion_iva": "string or null",
  "receptor_cuit": "11 digits or null",
  "receptor_razon_social": "string or null",
  "receptor_domicilio": "string or null",
  "receptor_condicion_iva": "string or null",
  "items": [{"codigo": "str or null","descripcion": "str","cantidad": number,"unidad": "str or null","precio_unitario": number,"descuento_porcentaje": null,"descuento_importe": null,"tasa_iva": number,"importe_iva": number,"subtotal": number}],
  "iva_items": [{"tasa": number,"base_imponible": number,"importe": number}],
  "neto_gravado": number_or_null,
  "neto_no_gravado": number_or_null,
  "op_exentas": number_or_null,
  "percepciones_nacionales": number_or_null,
  "percepciones_iibb": number_or_null,
  "percepciones_municipales": number_or_null,
  "impuestos_internos": number_or_null,
  "total": number_or_null,
  "condicion_pago": "string or null",
  "confidence": float,
  "warnings": ["list of strings"]
}"""

_CORRECTION_PREFIX = (
    "Your previous extraction had the following validation errors. "
    "Re-extract the invoice fixing ONLY these issues:\n"
)


# ---------------------------------------------------------------------------
# Gemini client (singleton)
# ---------------------------------------------------------------------------

_gemini_client: Optional[genai.Client] = None


def _get_client() -> genai.Client:
    global _gemini_client
    if _gemini_client is None:
        s = get_settings()
        _gemini_client = genai.Client(api_key=s.google_api_key)
    return _gemini_client


# ---------------------------------------------------------------------------
# Core call — with retry + circuit breaker
# ---------------------------------------------------------------------------

@retry(
    retry=retry_if_exception_type((json.JSONDecodeError, ValueError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)
def _call_gemini(contents: list, cb: pybreaker.CircuitBreaker) -> dict:
    s = get_settings()
    fallback_models = [s.gemini_model, "gemini-2.5-flash", "gemini-2.5-flash-lite"]
    # Remove duplicates while preserving order
    models_to_try = list(dict.fromkeys(fallback_models))

    last_error = None
    raw_text = None

    for model_name in models_to_try:
        def _do_call():
            response = _get_client().models.generate_content(
                model=model_name,
                contents=contents,
                config=genai_types.GenerateContentConfig(
                    system_instruction=_SYSTEM_PROMPT,
                    temperature=0.0,
                    response_mime_type="application/json",
                ),
            )
            return response.text

        try:
            raw_text = cb.call(_do_call)
            if raw_text:
                break
        except pybreaker.CircuitBreakerError:
            raise
        except Exception as exc:
            log.warning("gemini_model_attempt_failed", model=model_name, error=str(exc))
            last_error = exc
            continue

    if not raw_text and last_error:
        raise last_error

    raw = (raw_text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("gemini_json_parse_failed", error=str(exc), snippet=raw[:200])
        raise


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract_with_llm(
    text: Optional[str],
    image_b64: Optional[str],
    image_mime: Optional[str],
    source_format: str,
    previous_errors: Optional[list[str]] = None,
) -> InvoiceData:
    cb = get_gemini_circuit_breaker()
    warnings: list[str] = []

    # Build contents list
    contents: list = []

    if previous_errors:
        contents.append(_CORRECTION_PREFIX + "\n".join(f"- {e}" for e in previous_errors) + "\n\n")

    if image_b64 and image_mime:
        contents.append(
            genai_types.Part.from_bytes(
                data=base64.b64decode(image_b64),
                mime_type=image_mime,
            )
        )
        contents.append("Extract all invoice data from this image.")
    elif text:
        contents.append(f"Extract all invoice data from the following text:\n\n{text}")
    else:
        raise ValueError("Either text or image_b64 must be provided")

    # First extraction
    try:
        raw = _call_gemini(contents, cb)
    except pybreaker.CircuitBreakerError:
        raise RuntimeError("Gemini API temporarily unavailable (circuit breaker open). Retry in 60s.")
    except (json.JSONDecodeError, RetryError) as exc:
        raise RuntimeError(f"LLM returned unparseable JSON after 3 attempts: {exc}") from exc

    invoice, parse_errors = _parse_llm_response(raw, source_format)

    # Self-correction pass
    if parse_errors and not previous_errors:
        log.info("llm_self_correction", errors=parse_errors)
        warnings.extend([f"[auto-corrected] {e}" for e in parse_errors])
        try:
            corrected_raw = _call_gemini(
                contents + [_CORRECTION_PREFIX + "\n".join(f"- {e}" for e in parse_errors)],
                cb,
            )
            corrected_invoice, remaining_errors = _parse_llm_response(corrected_raw, source_format)
            if len(remaining_errors) < len(parse_errors):
                invoice = corrected_invoice
                parse_errors = remaining_errors
        except Exception as exc:
            warnings.append(f"Self-correction attempt failed: {exc}")

    if invoice is None:
        raise RuntimeError(f"Could not build a valid InvoiceData: {parse_errors}")

    invoice.warnings = list(dict.fromkeys(invoice.warnings + warnings))
    return invoice


# ---------------------------------------------------------------------------
# Parser: raw LLM dict → InvoiceData
# ---------------------------------------------------------------------------

def _parse_llm_response(raw: dict, source_format: str) -> tuple[Optional[InvoiceData], list[str]]:
    errors: list[str] = []
    warnings: list[str] = list(raw.get("warnings") or [])

    def _d(key, default=None):
        v = raw.get(key, default)
        return v if v is not None else default

    def _decimal_safe(key) -> Optional[Decimal]:
        v = _d(key)
        if v is None:
            return None
        try:
            return Decimal(str(v)).quantize(Decimal("0.0001"))
        except InvalidOperation:
            errors.append(f"Field '{key}' has invalid decimal value: {v!r}")
            return None

    def _date_safe(key) -> Optional[date]:
        v = _d(key)
        if not v:
            return None
        try:
            if isinstance(v, str) and len(v) == 10:
                parts = v.split("-")
                return date(int(parts[0]), int(parts[1]), int(parts[2]))
        except Exception:
            pass
        errors.append(f"Field '{key}' has invalid date: {v!r}")
        return None

    _VALID_RATES = {0.0, 2.5, 5.0, 10.5, 21.0, 27.0}

    items: list[LineItem] = []
    for i, item in enumerate(raw.get("items") or []):
        try:
            tasa = float(item.get("tasa_iva", 21))
            if tasa not in _VALID_RATES:
                tasa = min(_VALID_RATES, key=lambda x: abs(x - tasa))
                warnings.append(f"Item {i}: IVA rate snapped to {tasa}%")
            items.append(LineItem(
                codigo=item.get("codigo"),
                descripcion=str(item.get("descripcion") or "Sin descripción"),
                cantidad=Decimal(str(item.get("cantidad", 1))),
                unidad=item.get("unidad"),
                precio_unitario=Decimal(str(item.get("precio_unitario", 0))),
                descuento_porcentaje=Decimal(str(item["descuento_porcentaje"])) if item.get("descuento_porcentaje") else None,
                descuento_importe=Decimal(str(item["descuento_importe"])) if item.get("descuento_importe") else None,
                tasa_iva=tasa,
                importe_iva=Decimal(str(item.get("importe_iva", 0))),
                subtotal=Decimal(str(item.get("subtotal", 0))),
            ))
        except Exception as exc:
            errors.append(f"Item {i} parse error: {exc}")

    iva_items: list[IVAItem] = []
    for i, iva in enumerate(raw.get("iva_items") or []):
        try:
            tasa = float(iva.get("tasa", 21))
            if tasa not in _VALID_RATES:
                tasa = min(_VALID_RATES, key=lambda x: abs(x - tasa))
            iva_items.append(IVAItem(
                tasa=tasa,
                base_imponible=Decimal(str(iva.get("base_imponible", 0))),
                importe=Decimal(str(iva.get("importe", 0))),
            ))
        except Exception as exc:
            errors.append(f"IVA item {i} parse error: {exc}")

    confidence_raw = _d("confidence", 0.5)
    try:
        confidence = max(0.0, min(1.0, float(confidence_raw)))
    except (TypeError, ValueError):
        confidence = 0.5
        errors.append("confidence field is not a valid float")

    try:
        invoice = InvoiceData(
            cae=_d("cae"),
            cae_vencimiento=_date_safe("cae_vencimiento"),
            tipo_comprobante=_d("tipo_comprobante"),
            tipo_comprobante_descripcion=_d("tipo_comprobante_descripcion"),
            punto_venta=_d("punto_venta"),
            numero_comprobante=_d("numero_comprobante"),
            numero_completo=_d("numero_completo"),
            fecha_emision=_date_safe("fecha_emision"),
            concepto=_d("concepto"),
            concepto_descripcion=_d("concepto_descripcion"),
            fecha_desde=_date_safe("fecha_desde"),
            fecha_hasta=_date_safe("fecha_hasta"),
            fecha_vto_pago=_date_safe("fecha_vto_pago"),
            moneda_id=_d("moneda_id", "PES"),
            moneda_descripcion=_d("moneda_descripcion", "Pesos Argentinos"),
            moneda_cotizacion=_decimal_safe("moneda_cotizacion") or Decimal("1"),
            emisor_cuit=_d("emisor_cuit"),
            emisor_razon_social=_d("emisor_razon_social"),
            emisor_domicilio=_d("emisor_domicilio"),
            emisor_condicion_iva=_d("emisor_condicion_iva"),
            receptor_cuit=_d("receptor_cuit"),
            receptor_razon_social=_d("receptor_razon_social"),
            receptor_domicilio=_d("receptor_domicilio"),
            receptor_condicion_iva=_d("receptor_condicion_iva"),
            items=items or None,
            iva_items=iva_items or None,
            neto_gravado=_decimal_safe("neto_gravado"),
            neto_no_gravado=_decimal_safe("neto_no_gravado"),
            op_exentas=_decimal_safe("op_exentas"),
            percepciones_nacionales=_decimal_safe("percepciones_nacionales"),
            percepciones_iibb=_decimal_safe("percepciones_iibb"),
            percepciones_municipales=_decimal_safe("percepciones_municipales"),
            impuestos_internos=_decimal_safe("impuestos_internos"),
            total=_decimal_safe("total"),
            condicion_pago=_d("condicion_pago"),
            confidence=confidence,
            source_format=source_format,
            extraction_method=f"llm_{get_settings().gemini_model.replace('-', '_')}",
            warnings=warnings,
        )
        return invoice, errors
    except Exception as exc:
        errors.append(f"InvoiceData construction failed: {exc}")
        return None, errors
