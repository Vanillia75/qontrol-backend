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

import requests


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
    Logique de matching :
    1. On calcule un score pour CHAQUE paire (transaction, facture) possible.
    2. On assigne en partant des meilleurs scores globaux en premier, pour
       eviter qu'une transaction mediocre (mais traitee en premier) ne
       "vole" une facture a une autre transaction qui matcherait bien mieux.
    3. Les montants en devise etrangere sont convertis en EUR (taux de
       change recupere en direct, avec une tolerance plus large pour
       absorber les frais/marge de change appliques par la banque).
    """

    AMOUNT_TOLERANCE_STRICT = 0.01  # 1 centime (apres conversion en EUR)
    AMOUNT_TOLERANCE_LOOSE = 1.50   # arrondis, petits frais
    CROSS_CURRENCY_TOLERANCE_PCT = 0.12  # 12% : marge de change + frais bancaires
    DATE_WINDOW_STRICT = 1   # jours
    DATE_WINDOW_LOOSE = 14   # jours

    # En dessous de ce score, on ne propose RIEN plutot qu'un match
    # douteux : mieux vaut signaler une absence de match que de risquer
    # de coller la mauvaise facture sur la mauvaise transaction.
    MIN_MATCH_CONFIDENCE = 0.5

    _fx_cache: dict[str, float] = {}

    # Taux de secours si l'API de change est injoignable (approximatifs,
    # mieux que rien mais a ne pas considerer comme exacts)
    FALLBACK_RATES_TO_EUR = {
        "USD": 0.92,
        "GBP": 1.17,
        "CHF": 1.04,
        "EUR": 1.0,
    }

    def __init__(self, transactions: list[Transaction], invoices: list[Invoice]):
        self.transactions = transactions
        self.invoices = invoices

    @classmethod
    def _rate_to_eur(cls, currency: str) -> float:
        """Retourne combien vaut 1 unite de `currency` en EUR."""
        if currency == "EUR":
            return 1.0
        if currency in cls._fx_cache:
            return cls._fx_cache[currency]
        try:
            resp = requests.get(
                "https://api.frankfurter.app/latest",
                params={"from": currency, "to": "EUR"},
                timeout=3,
            )
            rate = resp.json()["rates"]["EUR"]
            cls._fx_cache[currency] = rate
            return rate
        except Exception:
            return cls.FALLBACK_RATES_TO_EUR.get(currency, 1.0)

    @classmethod
    def _to_eur(cls, amount: float, currency: str) -> float:
        return amount * cls._rate_to_eur(currency)

    def match_all(self) -> list[MatchResult]:
        # 1. Score de chaque paire transaction <-> facture possible
        candidates = []  # (score, tx_index, inv_index, reasons)
        for ti, tx in enumerate(self.transactions):
            for ii, inv in enumerate(self.invoices):
                score, reasons = self._score_match(tx, inv)
                if score >= self.MIN_MATCH_CONFIDENCE:
                    candidates.append((score, ti, ii, reasons))

        # 2. Assignation gloutonne en partant des MEILLEURS scores globaux,
        #    pas dans l'ordre des transactions -> evite qu'un match mediocre
        #    bloque un meilleur match potentiel trouve plus tard.
        candidates.sort(key=lambda c: c[0], reverse=True)

        assigned: dict[int, tuple[int, float, list[str]]] = {}
        used_invoices: set[int] = set()
        for score, ti, ii, reasons in candidates:
            if ti in assigned or ii in used_invoices:
                continue
            assigned[ti] = (ii, score, reasons)
            used_invoices.add(ii)

        # 3. Construction des resultats dans l'ordre d'origine des transactions
        results = []
        for ti, tx in enumerate(self.transactions):
            if ti in assigned:
                ii, score, reasons = assigned[ti]
                results.append(MatchResult(tx, self.invoices[ii], score, reasons))
            else:
                results.append(MatchResult(tx, None, 0.0, ["Aucune facture correspondante trouvee"]))
        return results

    def _score_match(self, tx: Transaction, inv: Invoice) -> tuple[float, list[str]]:
        reasons = []
        score = 0.0
        same_currency = tx.currency == inv.currency

        # --- Montant (converti en EUR pour pouvoir comparer toutes les devises) ---
        tx_eur = self._to_eur(tx.amount, tx.currency)
        inv_eur = self._to_eur(inv.amount, inv.currency)
        amount_diff = abs(tx_eur - inv_eur)

        if amount_diff <= self.AMOUNT_TOLERANCE_STRICT:
            score += 0.5
            if same_currency:
                reasons.append(f"Montant exact ({inv.amount} {inv.currency})")
            else:
                reasons.append(
                    f"Montant exact apres conversion ({inv.amount} {inv.currency} "
                    f"\u2248 {inv_eur:.2f} EUR)"
                )
        elif amount_diff <= self.AMOUNT_TOLERANCE_LOOSE:
            score += 0.3
            reasons.append(f"Montant proche (ecart {amount_diff:.2f} EUR)")
        elif not same_currency:
            # Tolerance plus large specifique aux devises etrangeres, pour
            # absorber la marge de change appliquee par la banque/carte
            pct_diff = amount_diff / max(tx_eur, 0.01)
            if pct_diff <= self.CROSS_CURRENCY_TOLERANCE_PCT:
                score += 0.2
                reasons.append(
                    f"Montant compatible avec conversion de devise "
                    f"({inv.amount} {inv.currency} \u2248 {inv_eur:.2f} EUR, "
                    f"ecart {pct_diff:.1%})"
                )
            else:
                return 0.0, []
        else:
            return 0.0, []

        # --- Date ---
        # Logique : une facture ne peut etre payee qu'apres avoir ete emise,
        # jamais avant. On compare donc l'ecart de facon DIRECTIONNELLE
        # (tx.date - inv.date), pas en valeur absolue.
        days_after = (tx.date - inv.date).days

        if days_after < -2:
            # La transaction a eu lieu avant meme l'emission de la facture :
            # incoherent, on rejette ce match completement.
            return 0.0, []
        elif 0 <= days_after <= self.DATE_WINDOW_STRICT:
            score += 0.35
            reasons.append(f"Date quasi identique ({days_after}j apres la facture)")
        elif 0 <= days_after <= self.DATE_WINDOW_LOOSE:
            score += 0.15
            reasons.append(f"Date coherente ({days_after}j apres la facture)")
        elif -2 <= days_after < 0:
            # Tolerance minime pour decalage horaire/journee de traitement
            score += 0.10
            reasons.append(f"Date quasi identique ({-days_after}j avant, marge de traitement)")
        else:
            score *= 0.5  # penalite forte mais pas elimination
            reasons.append(f"Date eloignee ({days_after}j apres la facture)")

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
