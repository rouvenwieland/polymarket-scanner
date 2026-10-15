"""Gemeinsame Pruefungen fuer die Fonds-Dateien (von mehreren Testmodulen genutzt)."""
import os

import bereinigung as bd


def position_id(position, idcol):
    return position.get(idcol) or position.get("pair_id") or position.get("market_id")


def check_archive(d):
    """Die Sicherung muss fuer sich konsistent bleiben: sie dient spaeteren Laeufen (und den Tests) als 'unbereinigter
    Stand'. Kasse = Start + Summe ihrer Handelsliste; Positionen = offene Lebenszyklen, MIT Vielfachheit: dieselbe
    Paar-/Markt-ID kann mehrfach offen sein (Paar nach dem Entfernen des ersten Kaufs erneut gekauft), dann sind es
    auch zwei Positionen - zugeordnet ueber (ID, Einstiegszeit == Zeit des KAUF)."""
    arch = os.path.join(d, bd.ARCHIVE_SUB)
    for f in bd.FUNDS:
        st = bd.read_json(os.path.join(arch, f["state"]))
        if st is None:
            continue
        rows, _ = bd.read_csv(os.path.join(arch, f["trades"]))
        ledger = f["start"] + sum(float(r["betrag"]) for r in rows)
        assert abs(st["cash"] - ledger) < bd.TOLERANCE, (f["key"], "Sicherung: Kasse", st["cash"], ledger)
        open_lcs = [lc for lc in bd.lifecycles(rows, f["idcol"]) if not lc["closed"]]
        opn = sorted(lc["id"] for lc in open_lcs)
        pos = sorted(position_id(p, f["idcol"]) for p in st["positions"])
        assert opn == pos, (f["key"], "Sicherung: Positionen")
        if all(p.get("eingestiegen") for p in st["positions"]):
            assert sorted((lc["id"], lc["rows"][0][1]["timestamp"]) for lc in open_lcs if lc["rows"]) == \
                sorted((position_id(p, f["idcol"]), p["eingestiegen"]) for p in st["positions"]), \
                (f["key"], "Sicherung: Positionen nach Einstiegszeit")
