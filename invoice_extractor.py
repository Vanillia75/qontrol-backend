"""
Extrait montant, date, fournisseur et numero de facture
depuis un PDF, pour pouvoir le passer au moteur de matching.
"""

import re
from datetime import datetime
from typing import Optional

from matching_engine import Invoice


class InvoiceExtractor:

    AMOUNT_PATTERNS = [
        r"total\s*(?:incl\.?\s*vat|ttc|à payer)?\s*:?\s*([\d\s]+[.,]\d{2})\s*€?",
        r"([\d\s]+[.,]\d{2})\s*€",
        r"€\s*([\d\s]+[.,]\d{2})",
        r"\$\s*([\d\s]+[.,]\d{2})",
        r"([\d\s]+[.,]\d{2})\s*\$",
    ]

    DATE_PATTERNS = [
        r"(\d{1,2})[\/\-\.](\d{1,2})[\/\-\.](\d{4})",
        r"(\d{4})[\/\-\.](\d{1,2})[\/\-\.](\d{1,2})",
        r"(\d{1,2})\s+(janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|novembre|décembre)\s+(\d{4})",
    ]

    MONTHS_FR = {
        "janvier": 1, "février": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
        "juillet": 7, "août": 8, "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12,
    }

    INVOICE_NUMBER_PATTERNS = [
        r"\b(F-\d{4}-\d{3,})\b",
        r"(?:numéro de la facture|invoice number)\s*:?\s*([A-Z0-9]{6,})",
        r"(?:facture\s*n[°o]|invoice\s*n[°o])\s*:?\s*([A-Z0-9\-]+)",
    ]

    def extract(self, pdf_path: str) -> Invoice:
        text = self._extract_text(pdf_path)

        amount = self._find_amount(text)
        date = self._find_date(text)
        invoice_number = self._find_invoice_number(text)
        currency = "USD" if "$" in text else "EUR"

        return Invoice(
            id=pdf_path,
            amount=amount or 0.0,
            currency=currency,
            date=date or datetime.now(),
            filename=pdf_path.split("/")[-1],
            invoice_number=invoice_number,
            raw_text=text,
        )

    @staticmethod
    def _extract_text(pdf_path: str) -> str:
        try:
            import pdfplumber
            with pdfplumber.open(pdf_path) as pdf:
                return "\n".join(page.extract_text() or "" for page in pdf.pages)
        except ImportError:
            raise RuntimeError("pdfplumber requis : pip install pdfplumber")

    def _find_amount(self, text: str) -> Optional[float]:
        text_lower = text.lower()
        best_amount = None

        # Priorise les montants pres du mot "total"
        for pattern in self.AMOUNT_PATTERNS:
            matches = re.findall(pattern, text_lower)
            for m in matches:
                clean = m.replace(" ", "").replace(",", ".")
                try:
                    val = float(clean)
                    if val > 0:
                        if best_amount is None or val > best_amount:
                            best_amount = val
                except ValueError:
                    continue
            if best_amount:
                break

        return best_amount

    def _find_date(self, text: str) -> Optional[datetime]:
        for pattern in self.DATE_PATTERNS[:2]:
            match = re.search(pattern, text)
            if match:
                groups = match.groups()
                try:
                    if len(groups[0]) == 4:  # format YYYY-MM-DD
                        return datetime(int(groups[0]), int(groups[1]), int(groups[2]))
                    else:  # format DD-MM-YYYY
                        return datetime(int(groups[2]), int(groups[1]), int(groups[0]))
                except (ValueError, IndexError):
                    continue

        # Format avec mois en lettres (francais)
        match = re.search(self.DATE_PATTERNS[2], text.lower())
        if match:
            day, month_name, year = match.groups()
            month = self.MONTHS_FR.get(month_name)
            if month:
                try:
                    return datetime(int(year), month, int(day))
                except ValueError:
                    pass

        return None

    def _find_invoice_number(self, text: str) -> Optional[str]:
        for pattern in self.INVOICE_NUMBER_PATTERNS:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1)
        return None
