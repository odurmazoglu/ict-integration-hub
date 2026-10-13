"""Advisory-only helpers (ADR-0014).

Nothing in this package decides, matches, maps, creates or writes anything. Its results are
shown to an operator, who stays the only one to choose; deterministic matching, decisions,
execution and every ERP write path must never import it (enforced by
``tests/unit/test_pr_f1_product_suggestions.py``).
"""
