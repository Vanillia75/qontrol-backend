"""
Moteur de matching automatique transactions <-> factures
Le coeur du produit : prend une transaction Qonto sans justificatif,
et trouve la facture correspondante parmi les sources connectées
(emails, uploads, Drive).
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional


@dataclass
class Transaction:
    id: str
    amount: float
    currency: str
    date: datetime
    label: str
    counterparty: Optional[str] = None
    reference: Optional[str] = None


@dataclass
class Invoice:
    id: str
    amount: float
    currency: str
    date: datetime
    filename: str
    vendor: Optional[str] = None
    invoice_number: Optional[str] = None
    raw_text: Optional[str] = None


@dataclass
class MatchResult:
    transaction: Transaction
    invoice: Optional[Invoice]
    confidence: float  # 0.0 a 1.0
    reasons: list[str]


class MatchingEngine:
    """
    Logique de matching en cascade, du plus fiable au moins fiable :
    1. Match exact (montant + date a 1 jour pres) -> confiance tres haute
    2. Match montant exact + date large (7 jours) -> confiance haute
    3. Match montant approche (tolerance devise/frais) + date -> confiance moyenne
    4. Match par numero de facture trouve dans le texte -> confiance haute
    """

    AMOUNT_TOLERANCE_STRICT = 0.01  # 1 centime
    AMOUNT_TOLERANCE_LOOSE = 1.50   # frais de change / arrondis
    DATE_WINDOW_STRICT = 1   # jours
    DATE_WINDOW_LOOSE = 14   # jours

    def __init__(self, transactions: list[Transaction], invoices: list[Invoice]):
        self.transactions = transactions
        self.invoices = invoices

    def match_all(self) -> list[MatchResult]:
        results = []
        used_invoice_ids = set()

        for tx in self.transactions:
            best_match, confidence, reasons = self._find_best_match(tx, used_invoice_ids)
            if best_match:
                used_invoice_ids.add(best_match.id)
            results.append(MatchResult(
                transaction=tx,
                invoice=best_match,
                confidence=confidence,
                reasons=reasons,
            ))
        return results

    def _find_best_match(
        self, tx: Transaction, exclude_ids: set
    ) -> tuple[Optional[Invoice], float, list[str]]:
        candidates = []

        for inv in self.invoices:
            if inv.id in exclude_ids:
                continue

            score, reasons = self._score_match(tx, inv)
            if score > 0:
                candidates.append((inv, score, reasons))

        if not candidates:
            return None, 0.0, ["Aucune facture correspondante trouvee"]

        candidates.sort(key=lambda c: c[1], reverse=True)
        best_inv, best_score, best_reasons = candidates[0]
        return best_inv, best_score, best_reasons

    def _score_match(self, tx: Transaction, inv: Invoice) -> tuple[float, list[str]]:
        reasons = []
        score = 0.0

        # --- Montant ---
        amount_diff = abs(tx.amount - inv.amount)
        same_currency = tx.currency == inv.currency

        if amount_diff <= self.AMOUNT_TOLERANCE_STRICT:
            score += 0.5
            reasons.append(f"Montant exact ({inv.amount} {inv.currency})")
        elif amount_diff <= self.AMOUNT_TOLERANCE_LOOSE:
            score += 0.3
            reasons.append(f"Montant proche (ecart {amount_diff:.2f})")
        elif not same_currency:
            # Tolerance plus large si devises differentes (conversion possible)
            ratio = max(tx.amount, inv.amount) / max(min(tx.amount, inv.amount), 0.01)
            if 0.85 <= ratio <= 1.20:
                score += 0.2
                reasons.append("Montant compatible avec conversion de devise")
            else:
                return 0.0, []
        else:
            return 0.0, []

        # --- Date ---
        date_diff = abs((tx.date - inv.date).days)
        if date_diff <= self.DATE_WINDOW_STRICT:
            score += 0.35
            reasons.append(f"Date quasi identique ({date_diff}j d'ecart)")
        elif date_diff <= self.DATE_WINDOW_LOOSE:
            score += 0.15
            reasons.append(f"Date dans la fenetre ({date_diff}j d'ecart)")
        else:
            score *= 0.5  # penalite forte mais pas elimination

        # --- Numero de facture dans le texte ---
        if inv.invoice_number and tx.reference:
            if inv.invoice_number.lower() in tx.reference.lower():
                score += 0.4
                reasons.append(f"Numero de facture trouve : {inv.invoice_number}")

        # --- Nom du vendeur / contrepartie ---
        if inv.vendor and tx.counterparty:
            if self._fuzzy_name_match(inv.vendor, tx.counterparty):
                score += 0.25
                reasons.append(f"Fournisseur correspondant : {inv.vendor}")

        return min(score, 1.0), reasons

    @staticmethod
    def _fuzzy_name_match(name1: str, name2: str) -> bool:
        n1 = re.sub(r"[^a-z0-9]", "", name1.lower())
        n2 = re.sub(r"[^a-z0-9]", "", name2.lower())
        return n1 in n2 or n2 in n1

    @staticmethod
    def confidence_label(confidence: float) -> str:
        if confidence >= 0.85:
            return "auto"        # attache automatiquement, zero intervention
        elif confidence >= 0.5:
            return "a_verifier"  # propose a l'utilisateur, validation 1 clic
        else:
            return "manquant"    # rien trouve, notification


if __name__ == "__main__":
    # Exemple d'usage avec les donnees du cas SJ FOR INTERNET / Binance
    transactions = [
        Transaction(
            id="tx_001",
            amount=6037.76,
            currency="EUR",
            date=datetime(2026, 6, 8),
            label="VANILLIA",
            counterparty="SJ FOR INTERNET",
            reference="FD1749052135833856",
        ),
        Transaction(
            id="tx_002",
            amount=549.99,
            currency="EUR",
            date=datetime(2026, 4, 14),
            label="Amazon.fr",
            counterparty="Amazon",
        ),
    ]

    invoices = [
        Invoice(
            id="inv_001",
            amount=6037.76,
            currency="EUR",
            date=datetime(2026, 6, 8),
            filename="F-2026-026.pdf",
            vendor="SJ FOR INTERNET CONTENT PROVIDER",
            invoice_number="F-2026-026",
        ),
        Invoice(
            id="inv_002",
            amount=549.99,
            currency="EUR",
            date=datetime(2026, 4, 14),
            filename="facture_amazon.pdf",
            vendor="Amazon EU",
        ),
    ]

    engine = MatchingEngine(transactions, invoices)
    results = engine.match_all()

    for r in results:
        label = MatchingEngine.confidence_label(r.confidence)
        print(f"\n--- {r.transaction.label} ({r.transaction.amount} {r.transaction.currency}) ---")
        print(f"  Statut: {label} (confiance: {r.confidence:.0%})")
        if r.invoice:
            print(f"  Facture: {r.invoice.filename}")
        for reason in r.reasons:
            print(f"  - {reason}")
