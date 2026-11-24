"""
Deterministic XML extractor for AFIP electronic invoices.

This path requires NO LLM — it parses the structured XML directly.
Confidence is always >= 0.90 when a valid CAE is found.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional

import xmltodict

from ..models import (
    ALICIVA_TASA,
    MONEDA,
    TIPO_COMPROBANTE,
    IVAItem,
    InvoiceData,
    LineItem,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _str(v) -> str:
    return str(v).strip() if v is not None else ""


def _decimal(v, default: Decimal = Decimal("0")) -> Decimal:
    try:
        return Decimal(str(v)).quantize(Decimal("0.0001"))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _int(v) -> Optional[int]:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _afip_date(v) -> Optional[date]:
    """Parse YYYYMMDD or YYYY-MM-DD."""
    if not v:
        return None
    s = str(v).strip().replace("-", "")
    if len(s) != 8:
        return None
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None


def _navigate(d: dict, *keys):
    """Return first matching key in d, case-insensitively."""
    lower_map = {k.lower(): v for k, v in d.items()}
    for key in keys:
        v = lower_map.get(key.lower())
        if v is not None:
            return v
    return None


# ---------------------------------------------------------------------------
# Main extractor
# ---------------------------------------------------------------------------

def extract_from_xml(xml_content: str) -> InvoiceData:
    """
    Parse AFIP XML and return an InvoiceData object.
    Supports both AFIP WS response format and the PDF-embedded XML variant.
    Raises ValueError if the content is not parseable XML or has no invoice data.
    """
    warnings: list[str] = []

    try:
        raw = xmltodict.parse(xml_content, force_list=("Alicuota", "Item", "Tributo"))
    except Exception as exc:
        raise ValueError(f"XML parse error: {exc}") from exc

    # --- Locate the invoice root ---
    # AFIP WS returns: {"FECompConsultarResponse": {"FECompConsultarResult": {"ResultGet": {...}}}}
    # Simplified: {"Comprobante": {...}}  or  {"invoice": {...}}
    comp = _find_comprobante(raw)
    if comp is None:
        raise ValueError("No recognizable AFIP invoice structure found in XML")

    # --- CAE ---
    cae = _str(_navigate(comp, "CAE", "NroCae"))
    cae_venc = _afip_date(_navigate(comp, "CAEFchVto", "FchVtoCae", "VencimientoCAE"))

    # --- Tipo comprobante ---
    tipo_int = _int(_navigate(comp, "CbteTipo", "TipoCbte", "TipoComprobante"))
    tipo_desc = TIPO_COMPROBANTE.get(tipo_int, f"Tipo {tipo_int}") if tipo_int else None

    # --- Punto de venta / número ---
    pv = _int(_navigate(comp, "PtoVta", "PuntoVenta", "NroPtoVta"))
    nro_desde = _int(_navigate(comp, "CbteDesde", "NroCbte", "NumeroComprobante"))
    numero_completo = f"{pv:04d}-{nro_desde:08d}" if pv and nro_desde else None

    # --- Fecha ---
    fecha = _afip_date(_navigate(comp, "CbteFch", "FchCbte", "FechaComprobante"))

    # --- Concepto ---
    concepto = _int(_navigate(comp, "Concepto"))
    concepto_desc = {1: "Productos", 2: "Servicios", 3: "Productos y Servicios"}.get(concepto)
    fecha_desde = _afip_date(_navigate(comp, "FchServDesde"))
    fecha_hasta = _afip_date(_navigate(comp, "FchServHasta"))
    fecha_vto = _afip_date(_navigate(comp, "FchVtoPago"))

    # --- Moneda ---
    moneda_id = _str(_navigate(comp, "MonId", "Moneda")) or "PES"
    moneda_desc = MONEDA.get(moneda_id, moneda_id)
    moneda_ctz = _decimal(_navigate(comp, "MonCotiz", "Cotizacion"), Decimal("1"))

    # --- Emisor ---
    emisor_cuit = _str(_navigate(comp, "CuitEmisor", "CUIT"))
    emisor_rs = _str(_navigate(comp, "RazonSocialEmisor", "Nombre"))
    emisor_dom = _str(_navigate(comp, "DomicilioEmisor", "Domicilio"))
    emisor_cond = _str(_navigate(comp, "CondicionIVAEmisor", "CondIvaEmisor"))

    # --- Receptor ---
    doc_tipo = _int(_navigate(comp, "DocTipo"))
    doc_nro = _str(_navigate(comp, "DocNro"))
    receptor_cuit = doc_nro if doc_tipo == 80 else None  # 80 = CUIT
    receptor_rs = _str(_navigate(comp, "RazonSocialReceptor", "RazonSocial"))
    receptor_dom = _str(_navigate(comp, "DomicilioReceptor"))
    receptor_cond = _str(_navigate(comp, "CondicionIVAReceptor", "CondIvaReceptor"))

    # --- IVA breakdown (Alicuotas) ---
    iva_items: list[IVAItem] = []
    alicuotas_raw = _navigate(comp, "Ivas", "AlicIvas")
    if isinstance(alicuotas_raw, dict):
        alicuota_list = alicuotas_raw.get("Alicuota") or []
        if isinstance(alicuota_list, dict):
            alicuota_list = [alicuota_list]
    elif isinstance(alicuotas_raw, list):
        alicuota_list = alicuotas_raw
    else:
        alicuota_list = []

    for aliq in alicuota_list:
        if not isinstance(aliq, dict):
            continue
        try:
            aliciva_id = _int(_navigate(aliq, "Id", "AlicIva"))
            # Map AFIP AlicIva ID to percentage
            if aliciva_id and aliciva_id in ALICIVA_TASA:
                tasa = ALICIVA_TASA[aliciva_id]
            else:
                tasa = float(_decimal(_navigate(aliq, "Tasa", "AlicIva"), Decimal("21")))
            iva_items.append(IVAItem(
                tasa=tasa,
                base_imponible=_decimal(_navigate(aliq, "BaseImp")),
                importe=_decimal(_navigate(aliq, "Importe")),
            ))
        except Exception as exc:
            warnings.append(f"IVA item parse error: {exc}")

    # --- Line items (optional in AFIP WS, present in extended formats) ---
    items: list[LineItem] = []
    items_raw = _navigate(comp, "Items", "Detalle")
    if isinstance(items_raw, dict):
        item_list = items_raw.get("Item") or []
        if isinstance(item_list, dict):
            item_list = [item_list]
    elif isinstance(items_raw, list):
        item_list = items_raw
    else:
        item_list = []

    for item in item_list:
        if not isinstance(item, dict):
            continue
        try:
            tasa_raw = _navigate(item, "AlicIva", "TasaIva", "Iva")
            aliciva_id = _int(tasa_raw)
            if aliciva_id and aliciva_id in ALICIVA_TASA:
                tasa = ALICIVA_TASA[aliciva_id]
            else:
                tasa = float(_decimal(tasa_raw, Decimal("21")))

            items.append(LineItem(
                codigo=_str(_navigate(item, "Pro_codigo", "Codigo")) or None,
                descripcion=_str(_navigate(item, "Pro_ds", "Descripcion", "Ds")) or "Sin descripción",
                cantidad=_decimal(_navigate(item, "Pro_qty", "Cantidad", "Qty"), Decimal("1")),
                unidad=_str(_navigate(item, "Pro_uom", "Unidad")) or None,
                precio_unitario=_decimal(_navigate(item, "Pro_precio_uni", "PrecioUnitario", "PUnt")),
                descuento_porcentaje=None,
                descuento_importe=_decimal(_navigate(item, "Pro_total_descuento")) or None,
                tasa_iva=tasa,
                importe_iva=_decimal(_navigate(item, "ImptoLiq", "ImporteIva")),
                subtotal=_decimal(_navigate(item, "Imp", "Subtotal", "Total")),
            ))
        except Exception as exc:
            warnings.append(f"Item parse error: {exc}")

    # --- Totals ---
    neto_gravado = _decimal(_navigate(comp, "ImpNeto")) or None
    neto_no_gravado = _decimal(_navigate(comp, "ImpTotConc")) or None
    op_exentas = _decimal(_navigate(comp, "ImpOpEx")) or None
    percepciones_nac = _decimal(_navigate(comp, "ImpTrib")) or None
    total = _decimal(_navigate(comp, "ImpTotal")) or None

    # --- Confidence ---
    # XML is deterministic. Full confidence if CAE is valid, slightly less otherwise.
    has_cae = bool(cae and len(cae) == 14 and cae.isdigit())
    confidence = 1.0 if (has_cae and tipo_int and fecha and total) else 0.85

    if not has_cae:
        warnings.append("CAE not found or invalid — this may not be a valid AFIP e-invoice")
    if not tipo_int:
        warnings.append("Tipo de comprobante not found")

    return InvoiceData(
        cae=cae if has_cae else None,
        cae_vencimiento=cae_venc,
        tipo_comprobante=tipo_int,
        tipo_comprobante_descripcion=tipo_desc,
        punto_venta=pv,
        numero_comprobante=nro_desde,
        numero_completo=numero_completo,
        fecha_emision=fecha,
        concepto=concepto,
        concepto_descripcion=concepto_desc,
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
        fecha_vto_pago=fecha_vto,
        moneda_id=moneda_id,
        moneda_descripcion=moneda_desc,
        moneda_cotizacion=moneda_ctz,
        emisor_cuit=emisor_cuit or None,
        emisor_razon_social=emisor_rs or None,
        emisor_domicilio=emisor_dom or None,
        emisor_condicion_iva=emisor_cond or None,
        receptor_cuit=receptor_cuit or None,
        receptor_razon_social=receptor_rs or None,
        receptor_domicilio=receptor_dom or None,
        receptor_condicion_iva=receptor_cond or None,
        items=items or None,
        iva_items=iva_items or None,
        neto_gravado=neto_gravado,
        neto_no_gravado=neto_no_gravado,
        op_exentas=op_exentas,
        percepciones_nacionales=percepciones_nac,
        total=total,
        confidence=confidence,
        source_format="xml",
        extraction_method="xml_parse",
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Internal: locate the invoice object inside any AFIP XML structure
# ---------------------------------------------------------------------------

def _find_comprobante(d: dict, depth: int = 0) -> Optional[dict]:
    if depth > 6:
        return None

    target_keys = {
        "comprobante", "cbte", "resultget", "fedetresp",
        "invoice", "factura", "voucher",
    }

    for k, v in d.items():
        if k.lower() in target_keys and isinstance(v, dict):
            return v
        # Try to detect by presence of AFIP-specific fields
        if isinstance(v, dict):
            keys_lower = {kk.lower() for kk in v}
            afip_markers = {"cbtipo", "cbtetipo", "ptoVta", "ptovta", "imptotal", "cae"}
            if keys_lower & afip_markers:
                return v
            # Recurse
            found = _find_comprobante(v, depth + 1)
            if found:
                return found

    return None
