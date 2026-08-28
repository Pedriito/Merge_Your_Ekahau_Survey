#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
esx_diff — caracterise les conflits entre deux projets .esx avant fusion.

A poser dans le meme dossier que esx_merge.py, dont il reutilise les
primitives de lecture.

    python esx_diff.py tablette.esx telephone.esx

Repond a la seule question qui compte avant de choisir --on-conflict :
garder la version de la base, est-ce que ca rend des donnees de l'apport
inatteignables ?
"""

from __future__ import annotations

import os
import sys
from collections import Counter, defaultdict

from esx_merge import EsxArchive, all_uuids, is_uuid, objects_of, same_content


def collection_of(idx_src: dict[str, str], oid: str) -> str:
    return os.path.basename(idx_src.get(oid, "?"))


def main(argv=None):
    argv = argv or sys.argv[1:]
    if len(argv) != 2:
        print(__doc__)
        return 2
    a = EsxArchive(argv[0])
    b = EsxArchive(argv[1])

    print(f"Base   : {os.path.basename(a.path)}")
    print(f"Apport : {os.path.basename(b.path)}")

    # --- 1. Generation de schema : comparaison des jeux de fichiers ---------
    def is_survey(n):
        return os.path.basename(n).startswith("survey-")

    fa = {x for x in a.docs if x.endswith(".json")}
    fb = {x for x in b.docs if x.endswith(".json")}
    sa, sb = {x for x in fa if is_survey(x)}, {x for x in fb if is_survey(x)}
    ca, cb = fa - sa, fb - sb          # collections fixes

    print("\n=== 1. Structure de l'archive ===")
    only_a, only_b = sorted(ca - cb), sorted(cb - ca)
    if not only_a and not only_b:
        print(f"  Collections fixes identiques ({len(ca)} fichiers) "
              "-> meme generation de schema.")
    else:
        if only_a:
            print(f"  Collection presente seulement dans la base  : {only_a}")
        if only_b:
            print(f"  Collection presente seulement dans l'apport : {only_b}")
        print("  -> generations de schema possiblement differentes, prudence.")
    print(f"\n  Fichiers de session survey-*.json :")
    print(f"    communs aux deux      : {len(sa & sb):>5}")
    print(f"    propres a la base     : {len(sa - sb):>5}")
    print(f"    propres a l'apport    : {len(sb - sa):>5}   <- a recuperer")

    # --- 2. Volumetrie par collection --------------------------------------
    print("\n=== 2. Volumetrie ===")
    inv_a, inv_b = a.inventory(), b.inventory()
    print(f"  {'collection':<34}{'base':>8}{'apport':>9}")
    for k in sorted(set(inv_a) | set(inv_b)):
        print(f"  {k:<34}{inv_a.get(k, 0):>8}{inv_b.get(k, 0):>9}")

    # --- 3. Caracterisation des conflits -----------------------------------
    ia, ib = a.object_index(), b.object_index()
    src_a, src_b = a.declared(), b.declared()
    shared = set(ia) & set(ib)
    differing = [i for i in shared if ia[i] != ib[i]]
    order_only = [i for i in differing if same_content(ia[i], ib[i])]
    conflicts = [i for i in differing if not same_content(ia[i], ib[i])]

    print(f"\n=== 3. Divergences ===")
    print(f"  {len(order_only):>6}  identiques a l'ordre de serialisation pres "
          "(sans objet)")
    print(f"  {len(conflicts):>6}  conflits reels de contenu")
    print()
    per_coll = Counter(collection_of(src_a, i) for i in conflicts)
    for coll, n in per_coll.most_common():
        print(f"  {n:>6}  {coll}")

    # Quelles cles divergent, par collection
    keys_by_coll = defaultdict(Counter)
    for i in conflicts:
        oa, ob = ia[i], ib[i]
        for k in set(oa) | set(ob):
            if oa.get(k) != ob.get(k):
                keys_by_coll[collection_of(src_a, i)][k] += 1
    print("\n  Champs qui divergent :")
    for coll, ctr in keys_by_coll.items():
        champs = ", ".join(f"{k}({n})" for k, n in ctr.most_common(6))
        print(f"    {coll:<32} {champs}")

    # --- 4. Le point decisif : perte de donnees -----------------------------
    # Une reference presente cote apport mais absente cote base, qui pointe
    # vers un objet reellement declare dans l'apport, serait perdue si l'on
    # garde la version de la base.
    at_risk = defaultdict(set)
    for i in conflicts:
        lost = (all_uuids(ib[i]) - all_uuids(ia[i])) & set(src_b)
        for t in lost:
            # on nomme la collection de la CIBLE perdue : un plan reste
            # atteignable par ailleurs, une mesure non.
            at_risk[f"{collection_of(src_a, i)} -> {collection_of(src_b, t)}"].add(t)
    print("\n=== 4. Impact de --on-conflict keep-a ===")
    if not at_risk:
        print("  Aucune reference de l'apport ne serait perdue.")
        print("  -> keep-a est sans risque : les divergences ne portent que sur")
        print("     des champs propres (nom, couleur, statut, horodatage).")
    else:
        total = sum(len(v) for v in at_risk.values())
        print(f"  {total} objet(s) de l'apport deviendraient inatteignables :")
        for coll, ids in at_risk.items():
            ex = ", ".join(list(ids)[:2])
            print(f"    via {coll:<28} {len(ids):>6} objet(s)   ex: {ex}")
        print("  -> keep-a perdrait ces donnees. Il faut un mode 'union'")
        print("     (fusion des listes de references au sein de l'objet).")

    # --- 5. Echantillon lisible -------------------------------------------
    print("\n=== 5. Echantillon (2 conflits) ===")
    for i in conflicts[:2]:
        oa, ob = ia[i], ib[i]
        print(f"\n  id {i}  ({collection_of(src_a, i)})")
        for k in sorted(set(oa) | set(ob)):
            va, vb = oa.get(k, "<absent>"), ob.get(k, "<absent>")
            if va != vb:
                sa, sb = repr(va)[:70], repr(vb)[:70]
                print(f"    {k}:")
                print(f"      base   = {sa}")
                print(f"      apport = {sb}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
