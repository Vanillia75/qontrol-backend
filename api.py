"""
API principale : orchestre la connexion Qonto, l'extraction de factures,
le matching automatique et l'attachement.

Lancer avec : uvicorn api:app --reload
"""

import os
import shutil
import tempfile
from datetime import datetime

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from matching_engine import MatchingEngine, Invoice
from qonto_connector import QontoConnector
from invoice_extractor import InvoiceExtractor


app = FastAPI(title="Qontrol API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # MVP : ouvert a tous. A restreindre au domaine Vercel une fois connu.
    allow_methods=["*"],
    allow_headers=["*"],
)

# Extensions de facture acceptees a l'upload (PDF + photos/scans d'image)
ALLOWED_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp")

# --- Stockage en memoire pour le MVP ---
# En production : remplacer par une vraie base (Postgres) avec cles API chiffrees
SESSIONS: dict[str, dict] = {}


class ConnectRequest(BaseModel):
    organization_slug: str
    secret_key: str


class ConnectResponse(BaseModel):
    session_id: str
    bank_accounts: list[dict]


@app.post("/connect", response_model=ConnectResponse)
def connect_qonto(req: ConnectRequest):
    """Etape 1 : le client connecte son compte Qonto avec sa cle API."""
    connector = QontoConnector(req.organization_slug, req.secret_key)

    if not connector.test_connection():
        raise HTTPException(status_code=401, detail="Cle API invalide. Verifie qu'elle est correcte et qu'elle a les droits necessaires.")

    bank_accounts = connector.list_bank_accounts()
    session_id = f"sess_{req.organization_slug}"

    # IMPORTANT : en prod, chiffrer secret_key avant stockage (ex: Fernet/AES)
    SESSIONS[session_id] = {
        "connector": connector,
        "invoices": [],
    }

    return ConnectResponse(session_id=session_id, bank_accounts=bank_accounts)


@app.post("/sessions/{session_id}/invoices/upload")
async def upload_invoices(session_id: str, files: list[UploadFile] = File(...)):
    """Etape 2 : le client uploade ses factures en vrac (PDF, JPG ou PNG)."""
    if session_id not in SESSIONS:
        raise HTTPException(status_code=404, detail="Session introuvable")

    extractor = InvoiceExtractor()
    extracted = []
    skipped = []

    tmp_dir = tempfile.mkdtemp()
    for file in files:
        if not file.filename.lower().endswith(ALLOWED_EXTENSIONS):
            skipped.append(file.filename)
            continue

        file_path = os.path.join(tmp_dir, file.filename)
        with open(file_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        try:
            invoice = extractor.extract(file_path)
            invoice.id = file_path  # garde le chemin pour l'attachement ulterieur
            extracted.append(invoice)
        except Exception as e:
            skipped.append(file.filename)
            continue

    SESSIONS[session_id]["invoices"].extend(extracted)

    return {
        "uploaded": len(extracted),
        "skipped": skipped,
        "invoices": [
            {
                "filename": inv.filename,
                "amount": inv.amount,
                "currency": inv.currency,
                "date": inv.date.isoformat(),
                "invoice_number": inv.invoice_number,
            }
            for inv in extracted
        ],
    }


@app.post("/sessions/{session_id}/match")
def run_matching(session_id: str, bank_account_id: str):
    """
    Etape 3 : lance le matching entre les transactions sans justificatif
    et les factures uploadees. Retourne les resultats pour validation
    avant attachement (les match >85% peuvent etre auto-attaches).
    """
    if session_id not in SESSIONS:
        raise HTTPException(status_code=404, detail="Session introuvable")

    session = SESSIONS[session_id]
    connector: QontoConnector = session["connector"]
    invoices: list[Invoice] = session["invoices"]

    transactions = connector.get_unjustified_transactions(bank_account_id)

    engine = MatchingEngine(transactions, invoices)
    results = engine.match_all()

    session["last_match_results"] = results

    output = []
    for r in results:
        label = MatchingEngine.confidence_label(r.confidence)
        output.append({
            "transaction_id": r.transaction.id,
            "transaction_label": r.transaction.label,
            "amount": r.transaction.amount,
            "currency": r.transaction.currency,
            "date": r.transaction.date.isoformat(),
            "matched_invoice": r.invoice.filename if r.invoice else None,
            "confidence": round(r.confidence, 2),
            "status": label,
            "reasons": r.reasons,
        })

    auto_count = sum(1 for r in results if MatchingEngine.confidence_label(r.confidence) == "auto")
    review_count = sum(1 for r in results if MatchingEngine.confidence_label(r.confidence) == "a_verifier")
    missing_count = sum(1 for r in results if MatchingEngine.confidence_label(r.confidence) == "manquant")

    return {
        "total": len(results),
        "auto": auto_count,
        "a_verifier": review_count,
        "manquant": missing_count,
        "results": output,
    }


@app.post("/sessions/{session_id}/attach")
def attach_matched(session_id: str, auto_only: bool = True):
    """
    Etape 4 : attache reellement les factures matchees sur Qonto.
    Par defaut n'attache que les matchs haute confiance (>85%),
    les autres restent en attente de validation manuelle.
    """
    if session_id not in SESSIONS:
        raise HTTPException(status_code=404, detail="Session introuvable")

    session = SESSIONS[session_id]
    connector: QontoConnector = session["connector"]
    results = session.get("last_match_results", [])

    attached = []
    failed = []

    for r in results:
        if not r.invoice:
            continue

        status = MatchingEngine.confidence_label(r.confidence)
        if auto_only and status != "auto":
            continue

        success = connector.attach_file(
            transaction_id=r.transaction.id,
            file_path=r.invoice.id,  # chemin stocke dans .id lors de l'upload
            filename=r.invoice.filename,
        )
        if success:
            attached.append(r.transaction.id)
        else:
            failed.append(r.transaction.id)

    return {"attached": len(attached), "failed": len(failed)}


@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}
