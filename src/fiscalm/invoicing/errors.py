"""Refusals shared by invoice validation and rendering."""

from __future__ import annotations


class InvoiceError(ValueError):
    """An invoice cannot safely be saved or issued; the message explains the correction."""
