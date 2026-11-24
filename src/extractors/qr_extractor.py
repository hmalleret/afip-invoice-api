"""
AFIP QR Code extractor.
Scans images or PDF page renders for official AFIP electronic invoice QR codes.
Extracts 100% ground-truth fiscal data deterministically without LLM.
"""

from __future__ import annotations

import base64
import json
from datetime import date
from decimal import Decimal
from typing import Optional

from PIL import Image
from pyzbar.pyzbar import decode

from ..models import TIPO_COMPROBANTE


def scan_afip_qr(img: Image.Image) -> Optional[dict]:
    """
    Search for and decode an AFIP electronic invoice QR code in an image.
    Uses multi-region scanning (bottom quadrants where AFIP QRs are standardized).
    Returns decoded AFIP dict if found, else None.
    """
    w, h = img.size

    # Check candidate crop regions where AFIP QR codes are placed by regulation
    scan_boxes = [
        # 1. Full image
        None,
        # 2. Bottom right (standard for most AFIP invoices)
        (int(w * 0.50), int(h * 0.65), w, h),
        # 3. Bottom left (alternative layout)
        (0, int(h * 0.65), int(w * 0.55), h),
        # 4. Bottom center
        (int(w * 0.20), int(h * 0.65), int(w * 0.80), h),
        # 5. Lower half
        (0, int(h * 0.50), w, h),
    ]

    for box in scan_boxes:
        target = img.crop(box) if box else img
        try:
            results = decode(target)
        except Exception:
            continue

        for obj in results:
            if obj.type != "QRCODE":
                continue
            raw_text = obj.data.decode("utf-8", errors="ignore")
            if "p=" in raw_text and ("afip.gob.ar" in raw_text or "fe/qr" in raw_text):
                parsed = _parse_afip_qr_payload(raw_text)
                if parsed:
                    return parsed

    return None


def _parse_afip_qr_payload(url: str) -> Optional[dict]:
    """
    Decode the base64 JSON payload from the AFIP QR URL:
    https://www.afip.gob.ar/fe/qr/?p=eyJ2ZXIiOjEsImZlY2hhIjoi...
    """
    try:
        query_part = url.split("p=")[1].split("&")[0]
        # Restore base64 padding
        padding = (4 - len(query_part) % 4) % 4
        padded = query_part + ("=" * padding)
        decoded_bytes = base64.b64decode(padded)
        raw_json = json.loads(decoded_bytes.decode("utf-8"))

        # Map AFIP QR fields to standard invoice fields
        # QR Schema per AFIP RG 4892:
        # {ver, fecha, cuit, ptoVta, tipoCmp, nroCmp, importe, moneda, ctz, tipoDocRec, nroDocRec, tipoCodAut, codAut}
        tipo_cmp = int(raw_json.get("tipoCmp", 0))
        pto_vta = int(raw_json.get("ptoVta", 0))
        nro_cmp = int(raw_json.get("nroCmp", 0))
        total = Decimal(str(raw_json.get("importe", 0)))
        cae = str(raw_json.get("codAut", ""))
        emisor_cuit = str(raw_json.get("cuit", ""))
        fecha_str = str(raw_json.get("fecha", ""))

        fecha_emision = None
        if len(fecha_str) == 10 and "-" in fecha_str:
            parts = fecha_str.split("-")
            fecha_emision = date(int(parts[0]), int(parts[1]), int(parts[2]))

        # Format number: 0001-00000001
        numero_completo = f"{pto_vta:04d}-{nro_cmp:08d}" if pto_vta and nro_cmp else None

        tipo_desc = TIPO_COMPROBANTE.get(tipo_cmp, f"Tipo {tipo_cmp}")

        doc_rec_tipo = raw_json.get("tipoDocRec")
        doc_rec_nro = str(raw_json.get("nroDocRec", "")) if raw_json.get("nroDocRec") else None
        # 80 = CUIT, 96 = DNI
        receptor_cuit = doc_rec_nro if doc_rec_tipo == 80 else None
        receptor_dni = doc_rec_nro if doc_rec_tipo == 96 else None

        return {
            "source": "afip_qr",
            "cae": cae if len(cae) == 14 and cae.isdigit() else None,
            "tipo_comprobante": tipo_cmp,
            "tipo_comprobante_descripcion": tipo_desc,
            "punto_venta": pto_vta,
            "numero_comprobante": nro_cmp,
            "numero_completo": numero_completo,
            "fecha_emision": fecha_emision,
            "total": total,
            "moneda_id": str(raw_json.get("moneda", "PES")),
            "moneda_cotizacion": Decimal(str(raw_json.get("ctz", 1))),
            "emisor_cuit": emisor_cuit,
            "receptor_cuit": receptor_cuit,
            "receptor_dni": receptor_dni,
            "raw_qr": raw_json,
        }
    except Exception:
        return None
