"""
Pydantic models for AFIP electronic invoice extraction.
Covers all invoice types: A, B, C, M, Crédito Electrónica, Notas de crédito/débito.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import IntEnum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# AFIP lookup tables
# ---------------------------------------------------------------------------

TIPO_COMPROBANTE = {
    1: "Factura A",
    2: "Nota de Débito A",
    3: "Nota de Crédito A",
    4: "Recibo A",
    5: "Nota de Venta al Contado A",
    6: "Factura B",
    7: "Nota de Débito B",
    8: "Nota de Crédito B",
    9: "Recibo B",
    10: "Nota de Venta al Contado B",
    11: "Factura C",
    12: "Nota de Débito C",
    13: "Nota de Crédito C",
    15: "Recibo C",
    49: "Bienes Usados - Consumidor Final",
    51: "Factura M",
    52: "Nota de Débito M",
    53: "Nota de Crédito M",
    54: "Recibo M",
    60: "Cuenta de Venta y Líquido Producto A",
    61: "Cuenta de Venta y Líquido Producto B",
    63: "Liquidación A",
    64: "Liquidación B",
    91: "Remito R",
    201: "Factura de Crédito Electrónica A",
    202: "Nota de Débito Electrónica A",
    203: "Nota de Crédito Electrónica A",
    206: "Factura de Crédito Electrónica B",
    207: "Nota de Débito Electrónica B",
    208: "Nota de Crédito Electrónica B",
    211: "Factura de Crédito Electrónica C",
    212: "Nota de Débito Electrónica C",
    213: "Nota de Crédito Electrónica C",
}

CONDICION_IVA = {
    "RI": "Responsable Inscripto",
    "M": "Monotributo",
    "EX": "Exento",
    "CF": "Consumidor Final",
    "RSNR": "Responsable no Inscripto",
    "PS": "Proveedor del Exterior",
    "CE": "Cliente del Exterior",
    "SS": "Sujeto Simplificado",
}

# AFIP AlicIva codes → percentage
ALICIVA_TASA = {
    3: 0.0,
    4: 10.5,
    5: 21.0,
    6: 27.0,
    8: 5.0,
    9: 2.5,
}

VALID_IVA_RATES = {0.0, 2.5, 5.0, 10.5, 21.0, 27.0}

MONEDA = {
    "PES": "Pesos Argentinos",
    "DOL": "Dólar Estadounidense",
    "EUR": "Euro",
    "BRL": "Real Brasileño",
    "CLP": "Peso Chileno",
    "UYU": "Peso Uruguayo",
}


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

class IVAItem(BaseModel):
    """IVA breakdown by rate."""
    tasa: float = Field(..., description="IVA rate: 0, 2.5, 5, 10.5, 21, 27")
    base_imponible: Decimal = Field(..., ge=Decimal("0"))
    importe: Decimal = Field(..., ge=Decimal("0"))

    @field_validator("tasa")
    @classmethod
    def validate_tasa(cls, v: float) -> float:
        if v not in VALID_IVA_RATES:
            raise ValueError(f"Invalid IVA rate {v}. Must be one of {VALID_IVA_RATES}")
        return v


class LineItem(BaseModel):
    """Single invoice line item."""
    codigo: Optional[str] = Field(None, max_length=50)
    descripcion: str = Field(..., min_length=1, max_length=1000)
    cantidad: Decimal = Field(..., gt=Decimal("0"))
    unidad: Optional[str] = Field(None, max_length=20)
    precio_unitario: Decimal = Field(..., ge=Decimal("0"))
    descuento_porcentaje: Optional[Decimal] = Field(None, ge=Decimal("0"), le=Decimal("100"))
    descuento_importe: Optional[Decimal] = Field(None, ge=Decimal("0"))
    tasa_iva: float = Field(..., description="IVA rate: 0, 2.5, 5, 10.5, 21, 27")
    importe_iva: Decimal = Field(..., ge=Decimal("0"))
    subtotal: Decimal = Field(..., ge=Decimal("0"))

    @field_validator("tasa_iva")
    @classmethod
    def validate_tasa_iva(cls, v: float) -> float:
        if v not in VALID_IVA_RATES:
            raise ValueError(f"Invalid IVA rate {v}")
        return v


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------

class ExtractionRequest(BaseModel):
    """Payload sent by the caller."""
    content: str = Field(
        ...,
        description="Base64-encoded document (PDF/XML/image) or raw XML/text string",
        min_length=1,
    )
    content_type: str = Field(
        ...,
        description=(
            "MIME type: application/xml | application/pdf | "
            "image/jpeg | image/png | image/webp | text/plain"
        ),
    )
    country_schema: str = Field(
        "afip_ar",
        description="Schema: afip_ar | sat_mx | sri_ec",
    )
    idempotency_key: Optional[str] = Field(
        None,
        max_length=128,
        description="Optional idempotency key to avoid double-billing on retries",
    )

    @field_validator("content_type")
    @classmethod
    def validate_content_type(cls, v: str) -> str:
        allowed = {
            "application/xml", "text/xml",
            "application/pdf",
            "image/jpeg", "image/png", "image/webp",
            "text/plain",
        }
        if v.lower() not in allowed:
            raise ValueError(f"Unsupported content_type '{v}'. Allowed: {allowed}")
        return v.lower()

    @field_validator("country_schema")
    @classmethod
    def validate_schema(cls, v: str) -> str:
        allowed = {"afip_ar", "sat_mx", "sri_ec"}
        if v not in allowed:
            raise ValueError(f"Unsupported schema '{v}'. Allowed: {allowed}")
        return v


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------

class InvoiceData(BaseModel):
    """
    Fully extracted invoice data.
    All fields optional — confidence and warnings indicate extraction quality.
    """

    # --- Identification ---
    cae: Optional[str] = Field(None, description="AFIP CAE: 14 digits", pattern=r"^\d{14}$")
    cae_vencimiento: Optional[date] = None
    tipo_comprobante: Optional[int] = Field(None, description="AFIP tipo comprobante code")
    tipo_comprobante_descripcion: Optional[str] = None
    punto_venta: Optional[int] = Field(None, ge=1, le=99999)
    numero_comprobante: Optional[int] = Field(None, ge=1)
    numero_completo: Optional[str] = Field(None, description="'XXXX-XXXXXXXX' formatted")
    fecha_emision: Optional[date] = None

    # --- Concept / Period ---
    concepto: Optional[int] = Field(None, ge=1, le=3, description="1=Productos 2=Servicios 3=Ambos")
    concepto_descripcion: Optional[str] = None
    fecha_desde: Optional[date] = None
    fecha_hasta: Optional[date] = None
    fecha_vto_pago: Optional[date] = None

    # --- Currency ---
    moneda_id: str = Field("PES")
    moneda_descripcion: str = Field("Pesos Argentinos")
    moneda_cotizacion: Decimal = Field(Decimal("1.00"))

    # --- Issuer (Emisor) ---
    emisor_cuit: Optional[str] = Field(None, description="11-digit CUIT")
    emisor_razon_social: Optional[str] = None
    emisor_domicilio: Optional[str] = None
    emisor_condicion_iva: Optional[str] = None
    emisor_inicio_actividades: Optional[date] = None

    # --- Recipient (Receptor) ---
    receptor_cuit: Optional[str] = Field(None, description="11-digit CUIT or DNI")
    receptor_razon_social: Optional[str] = None
    receptor_domicilio: Optional[str] = None
    receptor_condicion_iva: Optional[str] = None

    # --- Line Items ---
    items: Optional[list[LineItem]] = None

    # --- IVA Breakdown ---
    iva_items: Optional[list[IVAItem]] = None

    # --- Totals ---
    neto_gravado: Optional[Decimal] = Field(None, ge=Decimal("0"))
    neto_no_gravado: Optional[Decimal] = Field(None, ge=Decimal("0"))
    op_exentas: Optional[Decimal] = Field(None, ge=Decimal("0"))
    percepciones_nacionales: Optional[Decimal] = Field(None, ge=Decimal("0"))
    percepciones_iibb: Optional[Decimal] = Field(None, ge=Decimal("0"))
    percepciones_municipales: Optional[Decimal] = Field(None, ge=Decimal("0"))
    impuestos_internos: Optional[Decimal] = Field(None, ge=Decimal("0"))
    total: Optional[Decimal] = Field(None, ge=Decimal("0"))

    # --- Payment ---
    condicion_pago: Optional[str] = None

    # --- Metadata ---
    confidence: float = Field(..., ge=0.0, le=1.0, description="Extraction confidence 0–1")
    source_format: str = Field(..., description="xml | pdf | image | text")
    extraction_method: str = Field(..., description="xml_parse | llm_gemini_flash | hybrid")
    validation_passed: bool = True
    warnings: list[str] = Field(default_factory=list)
    country_schema: str = Field("afip_ar")

    @field_validator("emisor_cuit", "receptor_cuit", mode="before")
    @classmethod
    def normalize_cuit(cls, v: Optional[str]) -> Optional[str]:
        """Strip hyphens and spaces from CUIT."""
        if v is None:
            return None
        cleaned = str(v).replace("-", "").replace(" ", "").strip()
        return cleaned if cleaned else None


class ExtractionResponse(BaseModel):
    """Full API response envelope."""
    success: bool
    invoice: Optional[InvoiceData] = None
    error: Optional[dict] = None
    request_id: str
    processing_time_ms: int
    billed: bool = True
