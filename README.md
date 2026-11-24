# AFIP Invoice Extraction API

**Extrae datos estructurados de facturas electrónicas AFIP/LATAM.**
Soporta XML, PDF e imagen. Respuesta tipada, validada y auditada.

## Stack
- **FastAPI** + Pydantic v2
- **Gemini Flash** (PDF/imagen) · extractor XML determinístico (sin LLM)
- **Fly.io** (deploy, $0 free tier inicial)
- **RapidAPI** (distribución y billing)

## Rutas
| Método | Path | Descripción |
|--------|------|-------------|
| POST | `/extract` | Extrae factura → JSON |
| GET | `/health` | Health + estado circuit breaker |
| GET | `/schemas` | Schemas soportados |

## Request
```json
{
  "content": "<base64 o XML crudo>",
  "content_type": "application/xml",
  "country_schema": "afip_ar",
  "idempotency_key": "opcional-para-no-cobrar-doble"
}
```

### `content_type` soportados
| Tipo | Extractor | Costo LLM |
|------|-----------|-----------|
| `application/xml` | XML parse determinístico | $0 |
| `text/plain` | Auto-detect (XML o LLM) | $0 / mínimo |
| `application/pdf` | pdfplumber → Gemini Flash | ~$0.001 |
| `image/jpeg`, `image/png` | Gemini Vision | ~$0.002 |

## Response
```json
{
  "success": true,
  "billed": true,
  "request_id": "uuid",
  "processing_time_ms": 180,
  "invoice": {
    "cae": "12345678901234",
    "tipo_comprobante": 6,
    "tipo_comprobante_descripcion": "Factura B",
    "punto_venta": 1,
    "numero_comprobante": 1,
    "numero_completo": "0001-00000001",
    "fecha_emision": "2024-11-15",
    "emisor_cuit": "20111111113",
    "emisor_razon_social": "Empresa Test S.A.",
    "total": "1210.0000",
    "confidence": 1.0,
    "extraction_method": "xml_parse",
    "validation_passed": true,
    "warnings": []
  }
}
```

### Billing gate
Si `confidence < 0.50` → `"billed": false`. El cliente puede reintentar con mejor calidad de documento.

## Setup local
```bash
cp .env.example .env
# Editar .env con tu GOOGLE_API_KEY

python3.12 -m venv .venv && source .venv/bin/activate
pip install uv && uv pip install -e ".[dev]"

uvicorn src.main:app --reload
# → http://localhost:8000/docs
```

## Tests
```bash
pytest tests/ -v
```

## Deploy en Fly.io
```bash
fly auth login
fly launch --no-deploy
fly secrets set GOOGLE_API_KEY=your_key
fly deploy
```

## Pricing en RapidAPI (sugerido)
| Tier | Precio | Descripción |
|------|--------|-------------|
| Freemium | $0 | 100 req/mes (XML only) |
| Basic | $0.03/req | XML + PDF |
| Pro | $0.08/req | XML + PDF + imagen |

## Robustez implementada
- **XML**: parse determinístico, confidence=1.0 con CAE válido
- **PDF/imagen**: multi-pass LLM con auto-corrección en 1 intento
- **Validación**: CUIT Módulo 11, aritmética IVA, consistencia de totales
- **Circuit breaker**: Gemini se abre tras 3 fallos → responde 503 inmediato
- **Retry**: tenacity con exponential backoff (3 intentos, 1→8s)
- **Idempotencia**: cache en memoria con `idempotency_key`
- **Billing gate**: no cobra si confidence < 0.50
- **Never 500**: todos los errores son JSON estructurados con `billed: false`
