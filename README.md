# ARCA (ex AFIP) & LATAM Invoice Extraction API

> **API REST de alta precisión para extraer datos estructurados (JSON) de facturas electrónicas de ARCA (ex AFIP) en Argentina y comprobantes fiscales de Latinoamérica.**
> Soporta **PDF**, **XML**, **Imágenes escaneadas (JPG/PNG)** y texto plano.

---

## ⚡ Características Principales

- **Lectura Nativa del QR de ARCA (RG 4892)**: Decodifica de forma determinística el CAE, CUIT Emisor, Punto de Venta, Número de Comprobante, Fecha y Total exactos sin costo de cómputo ni alucinaciones.
- **Parseo Determinístico de XML ($0 costo LLM)**: Para XMLs directos de facturación electrónica.
- **Soporte de Visión Multimodal**: Convierte y extrae datos de facturas escaneadas o fotografías mediante Gemini Flash.
- **Validaciones Fiscales Argentinas**:
  - Algoritmo Módulo 11 para validación de CUIT.
  - Verificación aritmética de alícuotas de IVA (21%, 10.5%, 27%, etc.).
  - Cruce de totales: Neto Gravado + IVA + Percepciones == Total.
- **Billing Gate Ético**: Si un documento no es una factura válida o la confianza es baja (`confidence < 0.50`), la API responde pero marca `"billed": false` (no se factura).

---

## 🔗 Endpoints Públicos

- **Base URL**: `https://afip-invoice-api.onrender.com`
- **Swagger / OpenAPI interactivo**: [https://afip-invoice-api.onrender.com/docs](https://afip-invoice-api.onrender.com/docs)
- **Health Check**: `GET /health`
- **Extracción**: `POST /extract`

---

## 💻 Ejemplos de Integración

### 1. cURL (Extraer Factura en PDF)

```bash
curl -X POST "https://afip-invoice-api.onrender.com/extract" \
  -H "Content-Type: application/json" \
  -d '{
    "content": "'$(base64 -w 0 factura.pdf)'",
    "content_type": "application/pdf",
    "country_schema": "afip_ar"
  }'
```

### 2. Python

```python
import base64
import requests

with open("factura.pdf", "rb") as f:
    pdf_b64 = base64.b64encode(f.read()).decode("utf-8")

response = requests.post(
    "https://afip-invoice-api.onrender.com/extract",
    json={
        "content": pdf_b64,
        "content_type": "application/pdf",
        "country_schema": "afip_ar"
    }
)

data = response.json()
if data["success"]:
    inv = data["invoice"]
    print(f"Comprobante: {inv['tipo_comprobante_descripcion']} {inv['numero_completo']}")
    print(f"CAE: {inv['cae']}")
    print(f"CUIT Emisor: {inv['emisor_cuit']}")
    print(f"Total: ${inv['total']}")
```

### 3. Node.js / TypeScript

```typescript
import fs from 'fs';

const pdfBase64 = fs.readFileSync('factura.pdf', { encoding: 'base64' });

const res = await fetch('https://afip-invoice-api.onrender.com/extract', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    content: pdfBase64,
    content_type: 'application/pdf',
    country_schema: 'afip_ar'
  })
});

const data = await res.json();
console.log(data.invoice);
```

---

## 📋 Estructura de Respuesta JSON

```json
{
  "success": true,
  "invoice": {
    "cae": "86451763467519",
    "cae_vencimiento": "2026-11-17",
    "tipo_comprobante": 6,
    "tipo_comprobante_descripcion": "Factura B",
    "punto_venta": 5000,
    "numero_comprobante": 24527132,
    "numero_completo": "5000-24527132",
    "fecha_emision": "2026-11-07",
    "moneda_id": "PES",
    "moneda_cotizacion": "1.0",
    "emisor_cuit": "30639453738",
    "emisor_razon_social": "TELECOM ARGENTINA S.A.",
    "total": "39772.0600",
    "confidence": 0.98,
    "source_format": "pdf",
    "extraction_method": "hybrid_qr_llm",
    "validation_passed": true,
    "warnings": []
  },
  "request_id": "88d41c21-96df-4695-984a-d00069c66005",
  "processing_time_ms": 320,
  "billed": true
}
```

---

## 🛠️ Tecnologías

- **FastAPI** + **Pydantic v2**
- **Google Gemini 3.1 Flash-Lite** (Vision & OCR)
- **PyZBar** & **OpenCV** (Decodificación QR RG 4892)
- **PDFPlumber** & **Poppler Utils**
