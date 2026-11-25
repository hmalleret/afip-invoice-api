"""
Integration tests — run against the live server or testclient.
"""
import base64
import json
import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.validators.invoice_validator import _cuit_valid

client = TestClient(app)

# ---------------------------------------------------------------------------
# Minimal valid AFIP XML fixture
# ---------------------------------------------------------------------------

SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Comprobante>
  <TipoCbte>6</TipoCbte>
  <PtoVta>1</PtoVta>
  <CbteDesde>1</CbteDesde>
  <CbteFch>20241115</CbteFch>
  <Concepto>1</Concepto>
  <DocTipo>80</DocTipo>
  <DocNro>20111111112</DocNro>
  <CuitEmisor>20111111113</CuitEmisor>
  <RazonSocialEmisor>Empresa Test S.A.</RazonSocialEmisor>
  <CondicionIVAEmisor>RI</CondicionIVAEmisor>
  <MonId>PES</MonId>
  <MonCotiz>1</MonCotiz>
  <ImpNeto>1000.00</ImpNeto>
  <ImpTotConc>0</ImpTotConc>
  <ImpOpEx>0</ImpOpEx>
  <ImpTrib>0</ImpTrib>
  <ImpTotal>1210.00</ImpTotal>
  <CAE>12345678901234</CAE>
  <CAEFchVto>20241125</CAEFchVto>
  <Ivas>
    <Alicuota>
      <Id>5</Id>
      <BaseImp>1000.00</BaseImp>
      <Importe>210.00</Importe>
    </Alicuota>
  </Ivas>
</Comprobante>"""


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_schemas():
    r = client.get("/schemas")
    assert r.status_code == 200
    schemas = [s["id"] for s in r.json()["supported"]]
    assert "afip_ar" in schemas


def test_extract_xml_base64():
    encoded = base64.b64encode(SAMPLE_XML.encode()).decode()
    r = client.post("/extract", json={
        "content": encoded,
        "content_type": "application/xml",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    inv = body["invoice"]
    assert inv["tipo_comprobante"] == 6
    assert inv["tipo_comprobante_descripcion"] == "Factura B"
    assert inv["punto_venta"] == 1
    assert inv["numero_comprobante"] == 1
    assert inv["cae"] == "12345678901234"
    assert float(inv["total"]) == 1210.0
    assert inv["extraction_method"] == "xml_parse"
    assert inv["confidence"] >= 0.85


def test_extract_xml_raw_text():
    """Content-type text/plain with raw XML should auto-detect XML."""
    r = client.post("/extract", json={
        "content": SAMPLE_XML,
        "content_type": "text/plain",
    })
    assert r.status_code == 200
    assert r.json()["invoice"]["extraction_method"] == "xml_parse"


def test_invalid_content_type():
    r = client.post("/extract", json={
        "content": "abc",
        "content_type": "application/octet-stream",
    })
    assert r.status_code == 422  # Pydantic validation error


def test_invalid_xml():
    encoded = base64.b64encode(b"not xml at all <broken").decode()
    r = client.post("/extract", json={
        "content": encoded,
        "content_type": "application/xml",
    })
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "INVALID_INPUT"


def test_idempotency():
    encoded = base64.b64encode(SAMPLE_XML.encode()).decode()
    payload = {
        "content": encoded,
        "content_type": "application/xml",
        "idempotency_key": "test-idem-001",
    }
    r1 = client.post("/extract", json=payload)
    r2 = client.post("/extract", json=payload)
    assert r1.status_code == 200
    assert r2.status_code == 200
    # Same request_id on second call means cache hit
    assert r1.json()["request_id"] == r2.json()["request_id"]


# ---------------------------------------------------------------------------
# Real sample tests
# ---------------------------------------------------------------------------

def test_extract_real_sample_telecom_pdf():
    import os
    sample_path = "/home/hernan/Documentos/WORKSPACE/afip-invoice-api/samples/05000-24527132.pdf"
    if not os.path.exists(sample_path):
        pytest.skip("Sample PDF not present")

    with open(sample_path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode()

    r = client.post("/extract", json={
        "content": encoded,
        "content_type": "application/pdf",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["billed"] is True
    inv = body["invoice"]
    assert inv["cae"] == "86451763467519"
    assert inv["tipo_comprobante"] == 6
    assert inv["punto_venta"] == 5000
    assert inv["numero_comprobante"] == 24527132
    assert inv["emisor_cuit"] == "30639453738"
    assert float(inv["total"]) == 39772.06
    assert inv["confidence"] >= 0.95


# ---------------------------------------------------------------------------
# CUIT validator unit tests
# ---------------------------------------------------------------------------

def test_cuit_valid():
    # AFIP's own CUIT: 33-69345023-9  (Módulo 11 verified)
    assert _cuit_valid("33-69345023-9") is True
    assert _cuit_valid("33693450239") is True
    # Persona física: 20-12345678-6
    assert _cuit_valid("20123456786") is True


def test_cuit_invalid():
    assert _cuit_valid("12345678901") is False   # invalid prefix
    assert _cuit_valid("00000000000") is False   # all zeros
    assert _cuit_valid("abc") is False           # not digits
    assert _cuit_valid("2012345678") is False    # too short
    assert _cuit_valid("20123456785") is False   # wrong check digit


# ---------------------------------------------------------------------------
# Auth unit & integration tests
# ---------------------------------------------------------------------------

def test_api_key_auth_flow(monkeypatch):
    from src.config import get_settings
    s = get_settings()

    # Enable auth requirement for this test
    monkeypatch.setattr(s, "require_api_key", True)
    monkeypatch.setattr(s, "master_api_key", "secret_master_123")

    # 1. No key -> 401
    r = client.post("/extract", json={
        "content": SAMPLE_XML,
        "content_type": "text/plain",
    })
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "API_KEY_REQUIRED"

    # 2. Master key -> 200
    r = client.post("/extract", json={
        "content": SAMPLE_XML,
        "content_type": "text/plain",
    }, headers={"X-API-Key": "secret_master_123"})
    assert r.status_code == 200

    # 3. Arca prefix key -> 200
    r = client.post("/extract", json={
        "content": SAMPLE_XML,
        "content_type": "text/plain",
    }, headers={"X-API-Key": "arca_live_key_999"})
    assert r.status_code == 200

    # 4. Bogus key -> 401
    r = client.post("/extract", json={
        "content": SAMPLE_XML,
        "content_type": "text/plain",
    }, headers={"X-API-Key": "invalid_key"})
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "INVALID_API_KEY"

