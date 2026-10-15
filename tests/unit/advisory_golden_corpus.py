"""PR F / F3.6a: production-shaped golden corpus for the advisory suggestion engine.

The corpus mirrors the production PRODUCT_NOT_FOUND lines of 2026-10-15 (ICT Bulut TFZP/TGDR,
VİTEL ManageEngine and Ruijie hardware lines, the Fabric reservation line, generic cloud,
Azure, GitHub, İADE and e-charging lines) plus one line per variant-catalog guard trigger and
the ManageEngine / Microsoft CSP / T2 / T3 safeguards, on the F1 production-shaped catalog.

``tests/fixtures/advisory_engine_golden_f1_2.json`` is the frozen output of engine ``f1.2``
(captured before F3.6a). F3.6a may change only the variant-catalog note text.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.advisory.product_suggestions import PriorMapping, ProductSuggestionResult, suggest_products
from tests.unit.test_pr_f1_product_suggestions import ICT_BULUT, OTHER, POLICY, VITEL, _catalog, _line

GOLDEN_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "advisory_engine_golden_f1_2.json"
#: The f1.2 wording F3.6a replaces (kept here, not imported: the constant now holds the new text).
F1_2_VARIANT_CATALOG_NOTE = "Varyantlı lisans kataloğu satırı: benzerlik önerisi yapılmaz, yalnız kesin kod eşleşmesi."

PRIORS = (
    PriorMapping(
        supplier_partner_id=ICT_BULUT, product_id=394, description="Microsoft 365 Business Standard", source="ledger 23"
    ),
)

#: name -> (description, supplier, seller code, manufacturer code)
CORPUS: dict[str, tuple[str | None, int, str | None, str | None]] = {
    # production lines (shape of the 19 current sets)
    "wb25_tfzp_t0": ("MS Windows Server Standart Edition Lisans per VM", ICT_BULUT, "TFZP", None),
    "wb23_tgdr": ("MS SQL Server Standart Edition 2 Core Lisans", ICT_BULUT, "TGDR", None),
    "wb21_generic_cloud": ("CLOUD HİZMETİ", ICT_BULUT, "01.0001", None),
    "wb2_iade": ("İADE", ICT_BULUT, "İADE", None),
    "wb17_vitel_me_sku_description": ("85710.1SS3", VITEL, "1531016053", None),
    "wb20_vitel_ruijie_hardware": ("RG-ES205C-P 5 Port Gigabit Unmanaged PoE Switch", VITEL, "1531015246", None),
    "wb33_vitel_ruijie_ap": ("Ruijie RG-RAP2200(E) Wi-Fi 5 Access Point", VITEL, "1531015093", None),
    "wb48_fabric_reservation": ("Microsoft Fabric Capacity F4 Reservation", ICT_BULUT, "DZH318Z0KCZ7:00KV", None),
    "wb48_azure_plan": ("Azure plan", ICT_BULUT, "DZH318Z0BPS60001", None),
    "wb47_github": ("GitHub Enterprise", ICT_BULUT, None, None),
    "wb58_esarj": ("Elektrikli araç şarj hizmeti", OTHER, None, None),
    # one line per variant-catalog guard trigger, each firing alone
    "trigger_manual_only_supplier": ("Ruijie RG-ES209GC-P Gigabit Smart PoE Switch", VITEL, "1531012111", None),
    "trigger_description_sku_token": ("ManageEngine Endpoint Central 85710.1SS3 lisans", OTHER, None, None),
    "trigger_sku_shaped_seller_code": ("Endpoint yönetim lisansı", OTHER, "67225.5M", None),
    # safeguards that must not move
    "me_exact_sku_t1": ("ManageEngine Endpoint Central", OTHER, "85710.1S1", None),
    "me_other_sku_conflict": ("ManageEngine Endpoint Central 50 Endpoints", OTHER, None, "85710.1S2"),
    "csp_exact_t1": ("Microsoft 365 Business Basic", ICT_BULUT, "CFQ7TTC0LH18:0001", None),
    "csp_product_id_only": ("Microsoft 365", ICT_BULUT, "CFQ7TTC0LH18:0002", None),
    "t2_prior_mapping": ("Microsoft 365 Business Standard", ICT_BULUT, "100099", None),
    "t3_name_similarity": ("Windows Server Standard lisansı per VM", OTHER, None, None),
    "fabric_payg_t3": ("Microsoft Fabric Capacity F2 Pay-as-you-go", OTHER, None, None),
}


def run_corpus() -> dict[str, ProductSuggestionResult]:
    catalog = _catalog()
    return {
        name: suggest_products(
            _line(description, supplier=supplier, code=code, manufacturer=manufacturer),
            catalog,
            prior_mappings=PRIORS,
            policy=POLICY,
        )
        for name, (description, supplier, code, manufacturer) in CORPUS.items()
    }


def snapshot(results: dict[str, ProductSuggestionResult]) -> dict[str, Any]:
    return {
        name: {
            "suggestions": [
                {
                    "rank": suggestion.rank,
                    "product_id": suggestion.product_id,
                    "tier": suggestion.tier.value,
                    "score": str(suggestion.score),
                    "reasons": list(suggestion.reasons),
                    "warnings": list(suggestion.warnings),
                }
                for suggestion in result.suggestions
            ],
            "reclassification_recommended": result.reclassification_recommended,
            "ambiguous": result.ambiguous,
            "notes": list(result.notes),
        }
        for name, result in results.items()
    }


def load_golden() -> dict[str, Any]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


if __name__ == "__main__":  # pragma: no cover - regenerates the frozen f1.2 snapshot (run on f1.2 only)
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_PATH.write_text(
        json.dumps(snapshot(run_corpus()), ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
