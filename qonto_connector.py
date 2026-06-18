"""
Connecteur Qonto : recupere les transactions sans justificatif
et envoie les pieces jointes une fois matchees.
"""

import requests
import uuid
from datetime import datetime
from dataclasses import dataclass

from matching_engine import Transaction


QONTO_BASE_URL = "https://thirdparty.qonto.com/v2"


class QontoConnector:
    def __init__(self, organization_slug: str, secret_key: str):
        """
        organization_slug: ex "vanillia-6921"
        secret_key: la cle API generee dans Qonto > Parametres > API
        Ces deux valeurs forment l'auth header "slug:secret_key"
        Stockees chiffrees cote backend, jamais en clair en base.
        """
        self.headers = {"Authorization": f"{organization_slug}:{secret_key}"}

    def test_connection(self) -> bool:
        """Verifie que la cle API est valide avant de continuer."""
        resp = requests.get(f"{QONTO_BASE_URL}/organizations/-", headers=self.headers)
        return resp.status_code == 200

    def get_unjustified_transactions(self, bank_account_id: str, since: datetime = None) -> list[Transaction]:
        """Recupere toutes les transactions qui n'ont pas de piece jointe."""
        transactions = []
        page = 1

        params = {"bank_account_id": bank_account_id, "per_page": 100}
        if since:
            params["emitted_at_from"] = since.isoformat()

        while True:
            params["page"] = page
            resp = requests.get(
                f"{QONTO_BASE_URL}/transactions", headers=self.headers, params=params
            )
            data = resp.json()

            for t in data.get("transactions", []):
                if t.get("attachment_ids"):
                    continue  # deja justifiee, on ignore

                counterparty = None
                if t.get("income"):
                    counterparty = t.get("clean_counterparty_name") or t.get("label")
                elif t.get("label"):
                    counterparty = t.get("clean_counterparty_name") or t.get("label")

                transactions.append(Transaction(
                    id=t["id"],
                    amount=float(t["amount"]),
                    currency=t["currency"],
                    date=datetime.fromisoformat(t["emitted_at"].replace("Z", "+00:00")).replace(tzinfo=None),
                    label=t.get("label", ""),
                    counterparty=counterparty,
                    reference=t.get("reference"),
                ))

            meta = data.get("meta", {})
            if not meta.get("next_page"):
                break
            page += 1

        return transactions

    def attach_file(self, transaction_id: str, file_path: str, filename: str) -> bool:
        """Attache un fichier a une transaction. Retourne True si succes."""
        idempotency_key = str(uuid.uuid4())
        with open(file_path, "rb") as f:
            resp = requests.post(
                f"{QONTO_BASE_URL}/transactions/{transaction_id}/attachments",
                headers={**self.headers, "X-Qonto-Idempotency-Key": idempotency_key},
                files={"file": (filename, f, "application/pdf")},
            )
        return resp.status_code in (200, 201, 204)

    def list_bank_accounts(self) -> list[dict]:
        """Liste les comptes bancaires de l'organisation, pour le selecteur dans l'app."""
        resp = requests.get(f"{QONTO_BASE_URL}/organizations/-", headers=self.headers)
        data = resp.json()
        return data.get("organization", {}).get("bank_accounts", [])
