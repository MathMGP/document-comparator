"""Headless smoke test: deterministic status recompute + the compact UI render
paths, with a synthetic comparison. No Gemini calls. Window withdrawn."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tray_app as t
from compare import engine

def _f(label, kind, should, equiv, cells, note="", relation="igual"):
    # cells items: (doc, value) or (doc, value, role)
    out = []
    for c in cells:
        d, v = c[0], c[1]
        role = c[2] if len(c) > 2 else ""
        out.append({"doc": d, "value": v, "present": v != "—", "role": role,
                    "source": v})
    return {"label": label, "kind": kind, "should_match": should,
            "equivalent": equiv, "note": note, "relation": relation,
            "cells": out}


FAKE = {
    "docs": [
        {"id": "D1", "filename": "csi_pt.pdf", "doc_type": "CSI (PT)"},
        {"id": "D2", "filename": "csi_en.pdf", "doc_type": "CSI (EN)"},
        {"id": "D3", "filename": "RAST.pdf", "doc_type": "RASTREABILIDADE"},
    ],
    "fields": [
        # REAL numeric divergence -> mismatch (deterministic, ignores model)
        _f("Total de caixas", "numero", True, True,
           [("D1", "980"), ("D2", "980"), ("D3", "1214")], "D3 difere"),
        _f("Peso líquido total", "numero", True, True,
           [("D1", "22.748,07"), ("D2", "22748,07"), ("D3", "28.003,04")]),
        # language pair -> equivalent True -> must NOT be a mismatch
        _f("País de destino", "texto", True, True,
           [("D1", "Egito"), ("D2", "Egypt"), ("D3", "—")]),
        _f("Condições de transporte", "texto", True, True,
           [("D1", "CONGELADO(-18°C)"), ("D2", "FROZEN(-18°C)"), ("D3", "—")]),
        # same entity + group prefix -> equivalent True -> not a mismatch
        _f("Exportador", "texto", True, True,
           [("D1", "RIO GRANDE COMERCIO DE CARNES LTDA"),
            ("D2", "RIO GRANDE COMERCIO DE CARNES LTDA"),
            ("D3", "1-GRUPO FRIBAL / Filial: 2-Rio Grande comercio de carnes Ltda")]),
        # each doc's own number -> should_match False -> info, not mismatch
        _f("Nº do documento (próprio)", "texto", False, False,
           [("D1", "10-00271227/2431/26"), ("D2", "10-00271249/2431/26"),
            ("D3", "1002/2431/2026")]),
        # SPLIT SHIPMENT: two CSIs are parts; sum (980+234) == 1214 -> consistent
        _f("Total de caixas (soma)", "numero", True, True,
           [("D3", "1214", "total"), ("D1", "980", "parte"), ("D2", "234", "parte")],
           relation="soma"),
        # split where the parts do NOT add up -> real mismatch
        _f("Peso líquido (soma errada)", "numero", True, True,
           [("D3", "28003,04", "total"), ("D1", "22748,07", "parte"),
            ("D2", "1000,00", "parte")], relation="soma"),
        # SAFETY NET: model FORGOT relation/roles (relation='igual', no roles),
        # but 700+300 == 1000 -> must be auto-detected as split, NOT a divergence
        _f("Peças (split sem tag)", "numero", True, True,
           [("D3", "1000"), ("D1", "700"), ("D2", "300")], relation="igual"),
        # AGREEING TOTALS but incomplete parts: 4 docs say ~28094, only 2 of 3
        # drafts present (sum falls short) -> totals agree -> NOT a divergence
        _f("Peso (totais batem, partes incompletas)", "numero", True, True,
           [("D1", "28094,765", "total"), ("D2", "28094,77", "total"),
            ("D3", "28094,765", "total"), ("D4", "28094,77", "total"),
            ("X1", "10600,00", "parte"), ("X2", "16494,77", "parte")],
           relation="soma"),
    ],
    "alerts": [{"severity": "alta", "field": "CSI",
                "text": "Há 2 CSI para o mesmo embarque — confira aceitação."}],
    "summary": "Caixas e peso divergem (D3).",
    "_pdf_paths": ["a", "b", "c"],
}


def main() -> int:
    engine.classify(FAKE)
    by = {f["label"]: f["status"] for f in FAKE["fields"]}
    # numeric divergences caught
    assert by["Total de caixas"] == "mismatch", by
    assert by["Peso líquido total"] == "mismatch", by
    # language / formatting / prefix -> NOT mismatch (the false positives we fixed)
    assert by["País de destino"] != "mismatch", by
    assert by["Condições de transporte"] != "mismatch", by
    assert by["Exportador"] != "mismatch", by
    # each doc's own number -> not a hard divergence
    assert by["Nº do documento (próprio)"] != "mismatch", by
    # split shipment that ADDS UP -> NOT a divergence (the bug this round)
    assert by["Total de caixas (soma)"] != "mismatch", by
    sumf = next(f for f in FAKE["fields"] if f["label"] == "Total de caixas (soma)")
    assert sumf["_sum"]["ok"] and sumf["_sum"]["soma"] == 1214, sumf["_sum"]
    assert engine.minority_docs(sumf) == set()        # parts not marked odd
    # split shipment that does NOT add up -> mismatch
    assert by["Peso líquido (soma errada)"] == "mismatch", by
    # safety net: untagged split auto-detected -> NOT a mismatch
    assert by["Peças (split sem tag)"] != "mismatch", by
    netf = next(f for f in FAKE["fields"] if f["label"] == "Peças (split sem tag)")
    assert netf.get("_sum", {}).get("ok") and netf["relation"] == "soma"
    # agreeing totals + incomplete parts -> NOT red, but honest 'short' warning
    agf = next(f for f in FAKE["fields"]
               if f["label"].startswith("Peso (totais batem"))
    assert agf["status"] == "short", agf["status"]
    assert not agf["_sum"]["ok"] and agf["_sum"]["short"]
    assert FAKE["_counts"]["mismatch"] == 3, FAKE["_counts"]
    assert FAKE["_counts"].get("short", 0) == 1, FAKE["_counts"]
    assert FAKE["fields"][0]["status"] == "mismatch"          # sorted first

    # 2) UI renders without error
    app = t.App()
    app.root.withdraw()
    app._show = lambda *a: None

    # staging queue: add from "different folders", Comparar gates on >= 2
    assert str(app.compare_btn["state"]) == "disabled"
    app._staged = [r"C:\a\RAST.pdf"]
    app._refresh_stage()
    assert str(app.compare_btn["state"]) == "disabled"   # only 1 -> can't compare
    app._staged.append(r"C:\b\CSI.pdf")                  # different folder
    app._refresh_stage()
    assert str(app.compare_btn["state"]) == "normal"
    assert "Comparar (2)" in app.compare_btn.cget("text")
    assert app.stage.size() == 2                          # two real rows
    app._clear_stage()
    assert len(app._staged) == 0                          # source of truth empty
    assert app.stage.size() == 1                          # one placeholder row
    assert str(app.compare_btn["state"]) == "disabled"

    app._on_done(FAKE)
    app.root.update()
    txt = app.out.get("1.0", "end")
    assert "Total de caixas" in txt and "Peso líquido total" in txt
    assert "Egito" not in txt or "País de destino" not in txt.split("campos que batem")[0]
    assert "🟥 3 divergência" in app.verdict.cget("text")
    # alert and the consistent split-shipment arithmetic are both surfaced
    assert "Conferir" in txt and "2 CSI" in txt
    assert "soma confere" in txt and "= 1214" in txt
    assert str(app.send_btn["state"]) == "normal"
    # toggle shows the equivalent (non-divergent) fields
    app.show_all.set(True)
    app._render()
    assert "Exportador" in app.out.get("1.0", "end")
    app.root.destroy()
    print("SMOKE OK - recompute + compact UI render fine")
    return 0


if __name__ == "__main__":
    sys.exit(main())
