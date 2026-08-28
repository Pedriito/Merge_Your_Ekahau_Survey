#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
esx_merge — fusion encadree de deux projets Ekahau (.esx).

Cas d'usage : un meme site releve depuis deux terminaux (telephone + tablette),
donnant deux projets divergents qui portent le meme nom mais ne se voient pas.

Principes de conception
-----------------------
1. Schema-agnostique. Le format .esx evolue d'une version a l'autre : on ne
   code en dur aucun nom de collection. On decouvre les fichiers JSON, on
   identifie les identifiants declares (cle "id" portant un UUID) et les
   references (tout autre UUID present dans l'arbre).
2. Les entrees ne sont JAMAIS modifiees. On lit, on ecrit ailleurs.
3. Dry-run par defaut. Il faut --apply pour produire un fichier.
4. Preflight bloquant : version de schema, correspondance des plans, conflits
   d'identifiants. On refuse plutot que de produire un projet silencieusement
   casse.
5. Tracabilite : rapport JSON horodate avec les SHA-256 des deux entrees.

Usage
-----
    python esx_merge.py inspect projet.esx
    python esx_merge.py merge base.esx apport.esx -o fusion.esx
    python esx_merge.py merge base.esx apport.esx -o fusion.esx --apply

Copyright (c) 2026 Pierre-Elie Romer. Publie sous licence MIT (voir LICENSE).
"""

from __future__ import annotations

import argparse
import copy
import datetime as _dt
import hashlib
import json
import os
import re
import sys
import zipfile
from collections import Counter, defaultdict
from typing import Any

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# Collections structurelles : en cas d'identifiant identique des deux cotes,
# il s'agit du meme objet (projet forke), pas d'une collision fortuite.
STRUCTURAL_HINTS = (
    "floorplan", "building", "floor", "walltype", "antenna",
    "tagkey", "requirement", "aptype", "vendor", "channel",
)


# --------------------------------------------------------------------------
# Utilitaires d'arbre JSON
# --------------------------------------------------------------------------

def walk(node: Any, path: str = ""):
    """Parcours recursif : rend (chemin, cle, valeur) pour chaque scalaire."""
    if isinstance(node, dict):
        for k, v in node.items():
            sub = f"{path}.{k}" if path else k
            if isinstance(v, (dict, list)):
                yield from walk(v, sub)
            else:
                yield sub, k, v
    elif isinstance(node, list):
        for i, v in enumerate(node):
            sub = f"{path}[{i}]"
            if isinstance(v, (dict, list)):
                yield from walk(v, sub)
            else:
                yield sub, None, v


def is_uuid(v: Any) -> bool:
    return isinstance(v, str) and bool(UUID_RE.match(v))


def declared_ids(node: Any) -> set[str]:
    """Identifiants declares : cle 'id' portant un UUID."""
    out: set[str] = set()
    if isinstance(node, dict):
        v = node.get("id")
        if is_uuid(v):
            out.add(v)
        for sub in node.values():
            if isinstance(sub, (dict, list)):
                out |= declared_ids(sub)
    elif isinstance(node, list):
        for sub in node:
            out |= declared_ids(sub)
    return out


def all_uuids(node: Any) -> set[str]:
    return {v for _, _, v in walk(node) if is_uuid(v)}


def remap_tree(node: Any, mapping: dict[str, str]) -> Any:
    """Reecrit recursivement toute chaine UUID presente dans mapping."""
    if isinstance(node, dict):
        return {k: remap_tree(v, mapping) for k, v in node.items()}
    if isinstance(node, list):
        return [remap_tree(v, mapping) for v in node]
    if isinstance(node, str) and node in mapping:
        return mapping[node]
    return node


def canonical(node: Any) -> Any:
    """Forme canonique pour COMPARAISON uniquement : listes triees.

    Ekahau ne garantit pas l'ordre de serialisation des listes. Deux objets
    identiques au sens metier peuvent donc differer octet a octet. On ne
    reecrit jamais le projet sous cette forme, on ne s'en sert que pour
    decider si deux objets sont reellement en conflit.
    """
    if isinstance(node, dict):
        return {k: canonical(v) for k, v in node.items()}
    if isinstance(node, list):
        items = [canonical(v) for v in node]
        try:
            return sorted(items, key=lambda x: json.dumps(
                x, sort_keys=True, ensure_ascii=False))
        except TypeError:
            return items
    return node


def _sig(node: Any) -> str:
    return json.dumps(canonical(node), sort_keys=True, ensure_ascii=False)


def same_content(a: Any, b: Any) -> bool:
    return _sig(a) == _sig(b)


def union_merge(base: dict, add: dict) -> tuple[dict, list[str], list[str]]:
    """Fusionne 'add' dans 'base' : union des listes, base prioritaire ailleurs.

    Retourne (objet, champs_unis, champs_arbitres_en_faveur_de_base).
    """
    out = copy.deepcopy(base)
    unioned: list[str] = []
    kept: list[str] = []
    for k, v in add.items():
        if k not in out:
            out[k] = copy.deepcopy(v)
            unioned.append(k)
            continue
        if same_content(out[k], v):
            continue
        if isinstance(out[k], list) and isinstance(v, list):
            seen = {_sig(x) for x in out[k]}
            merged = list(out[k])
            for item in v:
                s = _sig(item)
                if s not in seen:
                    merged.append(copy.deepcopy(item))
                    seen.add(s)
            if len(merged) != len(out[k]):
                out[k] = merged
                unioned.append(k)
        else:
            kept.append(k)
    return out, unioned, kept


def objects_of(doc: Any) -> list[dict]:
    """Objets de premier niveau d'un document de collection .esx.

    Format usuel : {"<collection>": [ {...}, {...} ]}
    """
    if isinstance(doc, dict):
        for v in doc.values():
            if isinstance(v, list) and all(isinstance(x, dict) for x in v):
                return v
    if isinstance(doc, list):
        return [x for x in doc if isinstance(x, dict)]
    return []


def collection_key(doc: Any) -> str | None:
    if isinstance(doc, dict):
        for k, v in doc.items():
            if isinstance(v, list):
                return k
    return None


# --------------------------------------------------------------------------
# Archive
# --------------------------------------------------------------------------

class EsxArchive:
    """Representation memoire d'un .esx. Lecture seule sur le fichier source."""

    def __init__(self, path: str):
        self.path = path
        self.docs: dict[str, Any] = {}      # nom -> arbre JSON
        self.blobs: dict[str, bytes] = {}   # nom -> contenu binaire
        self.order: list[str] = []
        self.sha256 = self._sha256(path)
        self._load()

    @staticmethod
    def _sha256(path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def _load(self):
        with zipfile.ZipFile(self.path, "r") as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                name = info.filename
                self.order.append(name)
                raw = z.read(name)
                if name.lower().endswith(".json"):
                    try:
                        self.docs[name] = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise ValueError(
                            f"{os.path.basename(self.path)} : {name} illisible ({exc})"
                        ) from exc
                else:
                    self.blobs[name] = raw

    # -- introspection ------------------------------------------------------

    @property
    def project(self) -> dict:
        for name, doc in self.docs.items():
            if os.path.basename(name).lower() == "project.json":
                return doc if isinstance(doc, dict) else {}
        return {}

    @property
    def schema_version(self):
        return self.project.get("version")

    def declared(self) -> dict[str, str]:
        """id -> fichier qui le declare."""
        out: dict[str, str] = {}
        for name, doc in self.docs.items():
            for i in declared_ids(doc):
                out.setdefault(i, name)
        return out

    def referenced(self) -> set[str]:
        out: set[str] = set()
        for doc in self.docs.values():
            out |= all_uuids(doc)
        return out

    def object_index(self) -> dict[str, dict]:
        """id -> objet de premier niveau, pour les collections."""
        idx: dict[str, dict] = {}
        for name, doc in self.docs.items():
            objs = objects_of(doc)
            for obj in objs:
                oid = obj.get("id")
                if is_uuid(oid):
                    idx[oid] = obj
            # Document autonome (project.json et assimiles) : la racine est
            # elle-meme l'objet porteur de l'identifiant.
            if not objs and isinstance(doc, dict) and is_uuid(doc.get("id")):
                idx[doc["id"]] = doc
        return idx

    def inventory(self) -> dict[str, int]:
        inv = {}
        for name, doc in self.docs.items():
            objs = objects_of(doc)
            if objs:
                inv[os.path.basename(name)] = len(objs)
        return inv

    def floorplans(self) -> list[dict]:
        out = []
        for name, doc in self.docs.items():
            if "floorplan" not in os.path.basename(name).lower():
                continue
            for obj in objects_of(doc):
                img = obj.get("imageId")
                blob_hash = None
                for bname, data in self.blobs.items():
                    if img and img in bname:
                        blob_hash = hashlib.sha256(data).hexdigest()[:16]
                        break
                out.append({
                    "id": obj.get("id"),
                    "name": obj.get("name"),
                    "width": obj.get("width"),
                    "height": obj.get("height"),
                    "metersPerUnit": obj.get("metersPerUnit"),
                    "imageId": img,
                    "imageSha16": blob_hash,
                })
        return out

    def orphans(self) -> set[str]:
        return self.referenced() - set(self.declared())


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------

class Preflight:
    def __init__(self, a: EsxArchive, b: EsxArchive, aliases: dict[str, str]):
        self.a, self.b = a, b
        self.aliases = aliases
        self.blocking: list[str] = []
        self.warnings: list[str] = []
        self.notes: list[str] = []
        self.shared_ids: dict[str, str] = {}   # id -> "identical" | "conflict"
        self.floor_matches: list[tuple] = []

    def run(self) -> "Preflight":
        self._check_version()
        self._check_ids()
        self._check_floorplans()
        self._check_orphans()
        return self

    def _check_version(self):
        va, vb = self.a.schema_version, self.b.schema_version
        if va is None or vb is None:
            self.warnings.append(
                "Version de schema absente de project.json : verification impossible."
            )
        elif va != vb:
            self.blocking.append(
                f"Versions de schema differentes : base={va} apport={vb}. "
                "Reenregistre les deux projets dans la meme version d'Ekahau AI Pro "
                "avant de fusionner."
            )
        else:
            self.notes.append(f"Version de schema commune : {va}")

    def _check_ids(self):
        da, db = self.a.declared(), self.b.declared()
        ia, ib = self.a.object_index(), self.b.object_index()
        shared = set(da) & set(db)
        for oid in sorted(shared):
            if oid in self.aliases:
                continue
            oa, ob = ia.get(oid), ib.get(oid)
            if oa is None or ob is None:
                self.shared_ids[oid] = "conflict"
            elif oa == ob:
                self.shared_ids[oid] = "identical"
            elif same_content(oa, ob):
                # Meme contenu, ordre de serialisation different : sans objet.
                self.shared_ids[oid] = "order-only"
            else:
                self.shared_ids[oid] = "conflict"
        n_id = sum(1 for v in self.shared_ids.values() if v == "identical")
        n_or = sum(1 for v in self.shared_ids.values() if v == "order-only")
        n_cf = sum(1 for v in self.shared_ids.values() if v == "conflict")
        if n_id:
            self.notes.append(
                f"{n_id} objet(s) partage(s) a l'identique : projets issus d'un "
                "meme tronc, fusion structurelle triviale."
            )
        if n_or:
            self.notes.append(
                f"{n_or} objet(s) identiques a l'ordre de serialisation pres : "
                "traites comme identiques."
            )
        if n_cf:
            self.warnings.append(
                f"{n_cf} conflit(s) reel(s) de contenu. Arbitrage requis "
                "(--on-conflict union recommande)."
            )

    def _check_floorplans(self):
        fa, fb = self.a.floorplans(), self.b.floorplans()
        if not fa or not fb:
            self.warnings.append("Aucun plan detecte d'un cote au moins.")
            return
        by_id_a = {f["id"]: f for f in fa}
        unmatched = []
        for f in fb:
            target = self.aliases.get(f["id"], f["id"])
            if target in by_id_a:
                ref = by_id_a[target]
                self.floor_matches.append((f["id"], target, "id"))
                if ref.get("metersPerUnit") != f.get("metersPerUnit"):
                    self.blocking.append(
                        f"Plan '{f.get('name')}' : echelle divergente "
                        f"({ref.get('metersPerUnit')} vs {f.get('metersPerUnit')}). "
                        "Les points de mesure atterriraient decales."
                    )
                if (ref.get("width"), ref.get("height")) != (f.get("width"), f.get("height")):
                    self.warnings.append(
                        f"Plan '{f.get('name')}' : dimensions divergentes "
                        "(recadrage ?). Verifie l'alignement avant fusion."
                    )
                continue
            same_img = [g for g in fa if g["imageSha16"]
                        and g["imageSha16"] == f["imageSha16"]]
            if same_img:
                self.floor_matches.append((f["id"], same_img[0]["id"], "image-sha"))
                self.notes.append(
                    f"Plan '{f.get('name')}' apparie par empreinte d'image sur "
                    f"'{same_img[0].get('name')}'."
                )
                continue
            unmatched.append(f)
        if unmatched:
            names = ", ".join(str(f.get("name")) for f in unmatched)
            self.blocking.append(
                f"Plan(s) de l'apport sans correspondance dans la base : {names}. "
                "Utilise --alias <id_apport>=<id_base>, ou "
                "--allow-unmatched-floorplans pour les ajouter comme plans distincts."
            )

    def _check_orphans(self):
        oa, ob = self.a.orphans(), self.b.orphans()
        if oa:
            self.warnings.append(f"{len(oa)} reference(s) orpheline(s) deja dans la base.")
        if ob:
            self.warnings.append(f"{len(ob)} reference(s) orpheline(s) deja dans l'apport.")


# --------------------------------------------------------------------------
# Fusion
# --------------------------------------------------------------------------

class Merger:
    def __init__(self, a: EsxArchive, b: EsxArchive, pre: Preflight,
                 on_conflict: str = "abort", dedupe_key: str | None = "mac",
                 accept_unreachable: bool = False):
        self.a, self.b, self.pre = a, b, pre
        self.on_conflict = on_conflict
        self.dedupe_key = dedupe_key
        self.accept_unreachable = accept_unreachable
        self.mapping: dict[str, str] = dict(pre.aliases)
        self.stats: dict[str, Any] = {}
        self.duplicates: list[dict] = []
        self.union_fields: Counter = Counter()
        self.arbitrated_fields: Counter = Counter()

    def build_mapping(self):
        for src, dst, how in self.pre.floor_matches:
            if src != dst:
                self.mapping[src] = dst
        # Les identifiants partages a l'identique sont deja alignes : la
        # projection est l'identite, on se contente d'ecarter le doublon.
        return self.mapping

    def merge(self) -> tuple[dict[str, Any], dict[str, bytes]]:
        self.build_mapping()
        conflicts = [i for i, s in self.pre.shared_ids.items() if s == "conflict"]
        if conflicts and self.on_conflict == "abort":
            raise RuntimeError(
                f"{len(conflicts)} conflit(s) d'identifiant non arbitre(s). "
                "Relance avec --on-conflict keep-a ou keep-b."
            )

        docs = {n: copy.deepcopy(d) for n, d in self.a.docs.items()}
        blobs = dict(self.a.blobs)

        # Index par nom de base pour rapprocher les fichiers des deux archives
        by_base = {os.path.basename(n).lower(): n for n in docs}

        added = Counter()
        skipped = Counter()

        for name, doc in self.b.docs.items():
            base = os.path.basename(name).lower()
            if base == "project.json":
                continue  # identite du projet = celle de la base
            objs = objects_of(doc)
            if not objs:
                if base not in by_base:
                    docs[name] = copy.deepcopy(doc)
                continue

            target_name = by_base.get(base)
            if target_name is None:
                docs[name] = remap_tree(copy.deepcopy(doc), self.mapping)
                added[base] += len(objs)
                by_base[base] = name
                continue

            tdoc = docs[target_name]
            tlist = objects_of(tdoc)
            existing = {o.get("id") for o in tlist}

            for obj in objs:
                oid = obj.get("id")
                projected = self.mapping.get(oid, oid)
                if projected in existing:
                    state = self.pre.shared_ids.get(oid)
                    if state == "conflict" and self.on_conflict in ("keep-b", "union"):
                        radd = remap_tree(copy.deepcopy(obj), self.mapping)
                        for k, o in enumerate(tlist):
                            if o.get("id") != projected:
                                continue
                            if self.on_conflict == "keep-b":
                                tlist[k] = radd
                            else:
                                merged, uni, kept = union_merge(o, radd)
                                tlist[k] = merged
                                for f in uni:
                                    self.union_fields[f"{base}:{f}"] += 1
                                for f in kept:
                                    self.arbitrated_fields[f"{base}:{f}"] += 1
                            break
                    skipped[base] += 1
                    continue
                tlist.append(remap_tree(copy.deepcopy(obj), self.mapping))
                existing.add(projected)
                added[base] += 1

        # Blobs (images de plans, etc.)
        for bname, data in self.b.blobs.items():
            new = bname
            for src, dst in self.mapping.items():
                if src in new:
                    new = new.replace(src, dst)
            if new in blobs:
                continue
            blobs[new] = data

        self.stats = {
            "objets_ajoutes": dict(added),
            "objets_ecartes_doublon": dict(skipped),
            "alias_appliques": self.mapping,
            "conflits_reels": len(conflicts),
            "politique_conflit": self.on_conflict,
            "champs_unis": dict(self.union_fields),
            "champs_arbitres_base": dict(self.arbitrated_fields),
        }
        self._detect_duplicates(docs)
        self._verify(docs)
        return docs, blobs

    def _detect_duplicates(self, docs):
        """Signale les objets fusionnes partageant une meme adresse MAC."""
        if not self.dedupe_key:
            return
        buckets: dict[tuple, list] = defaultdict(list)
        for name, doc in docs.items():
            for obj in objects_of(doc):
                for k, v in obj.items():
                    if self.dedupe_key in k.lower() and isinstance(v, str) and len(v) >= 12:
                        buckets[(os.path.basename(name), v.lower())].append(obj.get("id"))
        for (fname, mac), ids in buckets.items():
            if len(ids) > 1:
                self.duplicates.append({"fichier": fname, "cle": mac, "ids": ids})

    @staticmethod
    def _reachability(docs) -> tuple[set[str], set[str]]:
        """(declares, references-par-un-autre-objet).

        On exclut l'auto-declaration : un objet qui ne se cite que lui-meme
        n'est reference par personne, donc invisible dans Ekahau.
        """
        declared: set[str] = set()
        referenced: set[str] = set()
        for doc in docs.values():
            objs = objects_of(doc)
            if not objs and isinstance(doc, dict):
                objs = [doc]
            for obj in objs:
                if not isinstance(obj, dict):
                    continue
                own = obj.get("id")
                declared |= declared_ids(obj)
                referenced |= (all_uuids(obj) - ({own} if is_uuid(own) else set()))
        return declared, referenced

    def _verify(self, docs):
        declared, referenced = self._reachability(docs)

        # 1. References pendantes introduites par la fusion
        pre_orphans = self.a.orphans() | self.b.orphans()
        new_orphans = (referenced - declared) - pre_orphans
        self.stats["orphelins_introduits"] = sorted(new_orphans)
        if new_orphans:
            raise RuntimeError(
                f"Verification post-fusion : {len(new_orphans)} reference(s) "
                "orpheline(s) introduite(s). Fusion abandonnee, rien n'a ete ecrit."
            )

        # 2. Donnees devenues inatteignables : presentes dans le fichier mais
        #    citees par aucun objet, donc invisibles dans Ekahau. C'est le
        #    risque exact d'un arbitrage keep-a sur un objet porteur de
        #    references vers les mesures de l'apport.
        da, ra = self._reachability(self.a.docs)
        db, rb = self._reachability(self.b.docs)
        base_unreachable = (da - ra) | (db - rb)
        unreachable = (declared - referenced) - base_unreachable
        self.stats["objets_inatteignables_introduits"] = sorted(unreachable)
        if unreachable:
            details = self._describe_unreachable(docs, unreachable)
            self.stats["inatteignables_detail"] = details
            lines = []
            for d in details:
                lines.append(
                    f"    - {d['collection']} : {d['libelle']}\n"
                    f"      id {d['id']}\n"
                    f"      {d['verdict']}"
                )
            benign = all(d["doublon_benin"] for d in details)
            if self.accept_unreachable:
                self.warnings_late = [
                    f"ACCEPTE explicitement : {len(unreachable)} objet(s) "
                    "inatteignable(s). Detail au rapport."
                ]
                return
            hint = ("Tous ces objets ont un homologue de meme MAC qui survit : "
                    "il s'agit de doublons inter-sessions, la perte est sans "
                    "consequence. Relance avec --accept-unreachable."
                    if benign else
                    "Au moins un objet n'a pas d'homologue : verifie avant "
                    "d'accepter. --accept-unreachable pour forcer.")
            raise RuntimeError(
                f"Verification post-fusion : {len(unreachable)} objet(s) de "
                "l'apport ne seraient cites par aucun objet, donc invisibles "
                f"dans Ekahau.\n{chr(10).join(lines)}\n  {hint}\n"
                "  Rien n'a ete ecrit."
            )

    @staticmethod
    def _describe_unreachable(docs, ids: set[str]) -> list[dict]:
        """Identifie chaque objet inatteignable et cherche un homologue MAC."""
        located: dict[str, tuple[str, dict]] = {}
        by_coll_mac: dict[tuple[str, str], list[str]] = defaultdict(list)
        for name, doc in docs.items():
            coll = os.path.basename(name)
            for obj in objects_of(doc):
                oid = obj.get("id")
                if not is_uuid(oid):
                    continue
                if oid in ids:
                    located[oid] = (coll, obj)
                for k, v in obj.items():
                    if "mac" in k.lower() and isinstance(v, str):
                        by_coll_mac[(coll, v.lower())].append(oid)

        out = []
        for oid in sorted(ids):
            coll, obj = located.get(oid, ("?", {}))
            mac = next((v for k, v in obj.items()
                        if "mac" in k.lower() and isinstance(v, str)), None)
            label = " / ".join(
                str(obj[k]) for k in ("name", "ssid", "mac") if obj.get(k)
            ) or "<sans libelle>"
            twin = False
            if mac:
                peers = [x for x in by_coll_mac.get((coll, mac.lower()), [])
                         if x != oid and x not in ids]
                twin = bool(peers)
            out.append({
                "id": oid,
                "collection": coll,
                "libelle": label,
                "doublon_benin": twin,
                "verdict": ("doublon inter-sessions : un objet de meme MAC "
                            "survit dans la fusion, perte sans consequence"
                            if twin else
                            "aucun homologue de meme MAC : a verifier"),
            })
        return out


def write_esx(path: str, docs: dict[str, Any], blobs: dict[str, bytes]):
    tmp = path + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for name, doc in docs.items():
            z.writestr(name, json.dumps(doc, ensure_ascii=False, indent=1))
        for name, data in blobs.items():
            z.writestr(name, data)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def cmd_inspect(args):
    arc = EsxArchive(args.esx)
    print(f"Fichier   : {arc.path}")
    print(f"SHA-256   : {arc.sha256}")
    print(f"Projet    : {arc.project.get('name')}  (version schema {arc.schema_version})")
    print(f"Documents : {len(arc.docs)} JSON, {len(arc.blobs)} binaire(s)")
    print("\nInventaire des collections")
    for k, v in sorted(arc.inventory().items(), key=lambda x: -x[1]):
        print(f"  {v:>7}  {k}")
    fps = arc.floorplans()
    if fps:
        print("\nPlans")
        for f in fps:
            print(f"  - {f['name']}  id={f['id']}  "
                  f"{f['width']}x{f['height']}  mpu={f['metersPerUnit']}  "
                  f"img={f['imageSha16']}")
    orph = arc.orphans()
    print(f"\nReferences orphelines : {len(orph)}")
    return 0


def _print_preflight(pre: Preflight):
    print("\n=== PREFLIGHT ===")
    for n in pre.notes:
        print(f"  [ok]      {n}")
    for w in pre.warnings:
        print(f"  [attention] {w}")
    for b in pre.blocking:
        print(f"  [BLOQUANT] {b}")


def cmd_merge(args):
    aliases = {}
    for spec in args.alias or []:
        if "=" not in spec:
            print(f"Alias invalide : {spec} (attendu id_apport=id_base)", file=sys.stderr)
            return 2
        src, dst = spec.split("=", 1)
        aliases[src.strip()] = dst.strip()

    a = EsxArchive(args.base)
    b = EsxArchive(args.apport)
    print(f"Base    : {os.path.basename(a.path)}  sha256={a.sha256[:16]}...")
    print(f"Apport  : {os.path.basename(b.path)}  sha256={b.sha256[:16]}...")

    pre = Preflight(a, b, aliases).run()
    if args.allow_unmatched_floorplans:
        pre.blocking = [x for x in pre.blocking if "sans correspondance" not in x]
    _print_preflight(pre)

    if pre.blocking:
        print("\nFusion refusee : corrige les points bloquants ci-dessus.")
        return 1

    merger = Merger(a, b, pre, on_conflict=args.on_conflict,
                    dedupe_key=args.dedupe_key,
                    accept_unreachable=args.accept_unreachable)
    try:
        docs, blobs = merger.merge()
    except RuntimeError as exc:
        print(f"\n[ECHEC] {exc}")
        return 1

    print("\n=== SIMULATION ===" if not args.apply else "\n=== FUSION ===")
    for k, v in sorted(merger.stats["objets_ajoutes"].items(), key=lambda x: -x[1]):
        print(f"  +{v:>6}  {k}")
    for k, v in sorted(merger.stats["objets_ecartes_doublon"].items()):
        print(f"   ={v:>6}  {k} (deja present)")
    if merger.union_fields:
        print("\n  Champs unis (donnees des deux sessions conservees) :")
        for k, v in merger.union_fields.most_common():
            print(f"    {k}  x{v}")
    if merger.arbitrated_fields:
        print("\n  Champs arbitres en faveur de la base :")
        for k, v in merger.arbitrated_fields.most_common():
            print(f"    {k}  x{v}")
    for w in getattr(merger, "warnings_late", []):
        print(f"\n  [attention] {w}")
    if merger.duplicates:
        print(f"\n  {len(merger.duplicates)} doublon(s) potentiel(s) sur '{args.dedupe_key}' :")
        for d in merger.duplicates[:10]:
            print(f"    {d['fichier']} : {d['cle']} -> {d['ids']}")
        print("  A arbitrer dans Ekahau AI Pro apres ouverture.")

    report = {
        "genere_le": _dt.datetime.now().astimezone().isoformat(),
        "outil": "esx_merge",
        "base": {"fichier": a.path, "sha256": a.sha256,
                 "version": a.schema_version, "inventaire": a.inventory()},
        "apport": {"fichier": b.path, "sha256": b.sha256,
                   "version": b.schema_version, "inventaire": b.inventory()},
        "preflight": {"notes": pre.notes, "avertissements": pre.warnings,
                      "bloquants": pre.blocking},
        "fusion": merger.stats,
        "doublons_potentiels": merger.duplicates,
        "applique": bool(args.apply),
    }

    if not args.apply:
        print("\nMode simulation. Rien n'a ete ecrit. Ajoute --apply pour produire le fichier.")
    else:
        write_esx(args.output, docs, blobs)
        report["sortie"] = {"fichier": args.output,
                            "sha256": EsxArchive._sha256(args.output)}
        print(f"\nEcrit : {args.output}")

    rpath = (args.output if args.apply else args.output + ".dryrun") + ".rapport.json"
    with open(rpath, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(f"Rapport : {rpath}")
    print("\nRappel : ouvre puis reenregistre le resultat dans Ekahau AI Pro "
          "avant exploitation, il normalise le projet.")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="esx_merge",
        description="Fusion encadree de deux projets Ekahau (.esx).")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("inspect", help="Inventorier un .esx")
    pi.add_argument("esx")
    pi.set_defaults(func=cmd_inspect)

    pm = sub.add_parser("merge", help="Fusionner apport dans base")
    pm.add_argument("base", help=".esx servant de reference (identite du projet)")
    pm.add_argument("apport", help=".esx dont on injecte les donnees")
    pm.add_argument("-o", "--output", default="fusion.esx")
    pm.add_argument("--apply", action="store_true",
                    help="Ecrire reellement le fichier (sinon simulation)")
    pm.add_argument("--alias", action="append", metavar="ID_APPORT=ID_BASE",
                    help="Forcer une correspondance d'objet (repetable)")
    pm.add_argument("--on-conflict", choices=("abort", "keep-a", "keep-b", "union"),
                    default="abort",
                    help="union : unit les listes, base prioritaire ailleurs")
    pm.add_argument("--allow-unmatched-floorplans", action="store_true",
                    help="Ajouter les plans non apparies comme plans distincts")
    pm.add_argument("--accept-unreachable", action="store_true",
                    help="Accepter la perte d'objets non rattaches (documentee "
                         "au rapport)")
    pm.add_argument("--dedupe-key", default="mac",
                    help="Champ servant a detecter les doublons (defaut: mac)")
    pm.set_defaults(func=cmd_merge)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, zipfile.BadZipFile) as exc:
        print(f"[ERREUR] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
