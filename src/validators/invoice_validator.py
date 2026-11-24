"""
Post-extraction business rule validator.
Verifies mathematical consistency and AFIP-specific rules.
Does NOT reject — it annotates warnings and lowers confidence.
"""

from __future__ import annotations

from decimal import Decimal

from ..models import InvoiceData

_TOLERANCE = Decimal("0.10")  # 10 centavos tolerance for rounding


def validate(invoice: InvoiceData) -> InvoiceData:
    """
    Run all validation rules against the invoice.
    Returns the same invoice object, mutated with warnings and adjusted confidence.
    Never raises — this is an annotation pass, not a gate.
    """
    warnings = list(invoice.warnings)
    confidence_penalty = 0.0

    # --- CUIT checksum (Módulo 11) ---
    if invoice.emisor_cuit:
        if not _cuit_valid(invoice.emisor_cuit):
            warnings.append(f"emisor_cuit '{invoice.emisor_cuit}' fails Módulo-11 checksum")
            confidence_penalty += 0.05

    if invoice.receptor_cuit:
        if not _cuit_valid(invoice.receptor_cuit):
            warnings.append(f"receptor_cuit '{invoice.receptor_cuit}' fails Módulo-11 checksum")
            confidence_penalty += 0.03

    # --- CAE length ---
    if invoice.cae and len(invoice.cae) != 14:
        warnings.append(f"CAE '{invoice.cae}' is not 14 digits")
        confidence_penalty += 0.10

    # --- Date consistency ---
    if invoice.fecha_emision and invoice.cae_vencimiento:
        if invoice.cae_vencimiento < invoice.fecha_emision:
            warnings.append("CAE vencimiento is before fecha_emision — likely extraction error")
            confidence_penalty += 0.05

    if invoice.fecha_desde and invoice.fecha_hasta:
        if invoice.fecha_hasta < invoice.fecha_desde:
            warnings.append("fecha_hasta is before fecha_desde")
            confidence_penalty += 0.03

    # --- IVA arithmetic ---
    if invoice.iva_items:
        total_iva = sum(item.importe for item in invoice.iva_items)
        total_base = sum(item.base_imponible for item in invoice.iva_items)

        for item in invoice.iva_items:
            if item.tasa > 0:
                expected_iva = (item.base_imponible * Decimal(str(item.tasa)) / Decimal("100")).quantize(Decimal("0.01"))
                diff = abs(item.importe - expected_iva)
                if diff > _TOLERANCE:
                    warnings.append(
                        f"IVA {item.tasa}%: base={item.base_imponible} → "
                        f"expected IVA={expected_iva}, got {item.importe} (diff={diff})"
                    )
                    confidence_penalty += 0.03

    # --- Total consistency ---
    if invoice.total is not None:
        computed = Decimal("0")
        if invoice.neto_gravado:
            computed += invoice.neto_gravado
        if invoice.neto_no_gravado:
            computed += invoice.neto_no_gravado
        if invoice.op_exentas:
            computed += invoice.op_exentas
        if invoice.iva_items:
            computed += sum(i.importe for i in invoice.iva_items)
        if invoice.percepciones_nacionales:
            computed += invoice.percepciones_nacionales
        if invoice.percepciones_iibb:
            computed += invoice.percepciones_iibb
        if invoice.percepciones_municipales:
            computed += invoice.percepciones_municipales
        if invoice.impuestos_internos:
            computed += invoice.impuestos_internos

        if computed > Decimal("0"):
            diff = abs(invoice.total - computed)
            if diff > _TOLERANCE:
                warnings.append(
                    f"Total mismatch: declared={invoice.total}, computed={computed} (diff={diff})"
                )
                confidence_penalty += 0.05

    # --- Items vs IVA totals ---
    if invoice.items and invoice.iva_items:
        item_iva_total = sum(i.importe_iva for i in invoice.items)
        breakdown_total = sum(i.importe for i in invoice.iva_items)
        diff = abs(item_iva_total - breakdown_total)
        if diff > _TOLERANCE:
            warnings.append(
                f"Items IVA sum={item_iva_total} vs IVA breakdown sum={breakdown_total} (diff={diff})"
            )
            confidence_penalty += 0.03

    # --- tipo_comprobante vs condicion IVA consistency ---
    if invoice.tipo_comprobante in (1, 2, 3, 201, 202, 203):  # Type A
        if invoice.receptor_condicion_iva and invoice.receptor_condicion_iva.upper() == "CF":
            warnings.append("Factura A issued to Consumidor Final — unusual")

    if invoice.tipo_comprobante in (6, 7, 8, 206, 207, 208):  # Type B
        if invoice.receptor_condicion_iva and invoice.receptor_condicion_iva.upper() == "RI":
            warnings.append("Factura B issued to Responsable Inscripto — should be Factura A")

    # --- Apply penalty ---
    new_confidence = max(0.0, invoice.confidence - confidence_penalty)
    invoice.confidence = round(new_confidence, 4)
    invoice.warnings = list(dict.fromkeys(warnings))  # deduplicate, preserve order
    invoice.validation_passed = confidence_penalty < 0.15

    return invoice


# ---------------------------------------------------------------------------
# CUIT Módulo 11 checksum
# ---------------------------------------------------------------------------

def _cuit_valid(cuit: str) -> bool:
    digits = cuit.replace("-", "").replace(" ", "")
    if len(digits) != 11 or not digits.isdigit():
        return False
    # All-zeros is structurally invalid even if Módulo 11 passes
    if digits == "0" * 11:
        return False
    # Valid CUIT prefixes (tipo de persona)
    prefix = digits[:2]
    if prefix not in {"20", "23", "24", "27", "30", "33", "34"}:
        return False
    weights = [5, 4, 3, 2, 7, 6, 5, 4, 3, 2]
    total = sum(int(d) * w for d, w in zip(digits[:10], weights))
    remainder = total % 11
    if remainder == 0:
        check = 0
    elif remainder == 1:
        check = 9
    else:
        check = 11 - remainder
    return check == int(digits[10])
