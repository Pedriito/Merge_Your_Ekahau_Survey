# esx-merge

Fusionner deux projets Ekahau `.esx` issus d'un même survey commencé sur un terminal et terminé sur un autre.

## Le problème

Vous démarrez un survey sur votre téléphone. Batterie à plat, plantage, ou simplement changement de terminal en cours de journée : vous finissez sur la tablette. À l'arrivée, deux fichiers `.esx`.

Ekahau ne les fusionne pas. Il identifie un projet par son UUID interne, pas par son nom de fichier, donc les deux sessions sont deux forks divergents qui ne se voient pas. Au moment de consolider, vous devez choisir : garder les mesures du téléphone, ou celles de la tablette. Une moitié du survey part à la poubelle.

`esx-merge` recolle les deux dans un seul projet exploitable.

## Ce que fait l'outil

- Inventorie le contenu d'un `.esx` sans le modifier
- Compare deux projets et caractérise les divergences réelles avant toute décision
- Fusionne les objets de survey (mesures, radios mesurées, points de référence, AP découverts) du projet d'apport dans le projet de base
- Déduplique les points d'accès sur leur MAC plutôt que de concaténer, pour éviter les doublons dans les visualisations et les rapports
- Vérifie après fusion qu'aucune référence orpheline n'a été introduite et qu'aucun objet n'est devenu invisible dans Ekahau
- Produit un rapport JSON horodaté avec les empreintes SHA-256 des fichiers d'entrée et de sortie

Le mode simulation est le comportement par défaut. Rien n'est écrit tant que vous n'ajoutez pas `--apply`.

## Prérequis

- Python 3.10 ou supérieur
- Aucune dépendance externe

Testé sur des projets produits par Ekahau AI Pro 11.9.1. Le schéma du `.esx` évolue d'une version à l'autre ; l'extraction est tolérante aux variations de nom de clé, mais un retour vous concernant est le bienvenu si votre version se comporte différemment.

## Installation

```
git clone https://github.com/<VOTRE-COMPTE>/esx-merge.git
cd esx-merge
```

`esx_diff.py` importe `esx_merge.py` et doit rester dans le même dossier que lui.

## Utilisation

### 0. Préparer les fichiers

Exportez le projet depuis chaque terminal, puis **renommez les deux fichiers avant toute autre chose**. Les deux sessions portent le même nom de projet, donc les deux exports sortent avec le même nom de fichier : le second écrase le premier au moment de les rassembler dans un dossier, et vous perdez une moitié du survey avant même d'avoir commencé.

Nommez-les d'après le terminal d'origine, par exemple `telephone.esx` et `tablette.esx`. Travaillez sur des copies et gardez les exports d'origine de côté.

Le nom de fichier ne joue aucun rôle dans la fusion : Ekahau identifie un projet par son UUID interne. Le renommage sert uniquement à ce que vous ne confondiez pas les deux, et à ce que l'un n'écrase pas l'autre.

### 1. Inventorier chaque fichier

```
python esx_merge.py inspect telephone.esx
python esx_merge.py inspect tablette.esx
```

Vous donne le nombre d'objets par type et la version de schéma détectée. Utile pour vérifier d'emblée que les deux fichiers contiennent bien ce que vous croyez.

### 2. Diagnostiquer les divergences

```
python esx_diff.py telephone.esx tablette.esx
```

Cette étape n'est pas optionnelle. Elle vous dit combien d'objets sont partagés à l'identique (signe d'un fork propre depuis un tronc commun), combien divergent réellement, et sur quoi. C'est ce diagnostic qui détermine la politique de conflit à choisir à l'étape suivante.

### 3. Simuler la fusion

```
python esx_merge.py merge tablette.esx telephone.esx -o fusion.esx --on-conflict union
```

Le premier fichier est la **base** : c'est lui qui porte l'identité du projet. Le second est **l'apport** : ses données de survey sont injectées dans la base.

Rien n'est écrit à ce stade. Vous obtenez le détail de ce qui serait ajouté, de ce qui serait écarté comme doublon, et un rapport `fusion.esx.dryrun.rapport.json`.

### 4. Appliquer

```
python esx_merge.py merge tablette.esx telephone.esx -o fusion.esx --on-conflict union --apply
```

Ouvrez ensuite `fusion.esx` dans Ekahau AI Pro et réenregistrez-le avant exploitation. Ekahau normalise le projet à l'enregistrement, et c'est cette étape qui valide que la fusion est réellement exploitable.

## Politiques de conflit

| Valeur | Comportement |
|---|---|
| `abort` | Interrompt à la première divergence. Par défaut. |
| `union` | Fusionne les listes de références des deux côtés au lieu d'arbitrer. C'est la politique adaptée au cas téléphone/tablette. |
| `keep-a` | Conserve la version de la base. |
| `keep-b` | Conserve la version de l'apport. |

`keep-a` et `keep-b` sont à manier avec précaution : arbitrer en faveur d'un côté sur un objet qui porte des références vers les mesures de l'autre rend ces mesures invisibles dans Ekahau, même si elles sont physiquement présentes dans le fichier. La vérification post-fusion détecte ce cas et interrompt.

## Autres options

- `--allow-unmatched-floorplans` : ajoute les plans non appariés comme plans distincts au lieu d'interrompre
- `--alias ID_APPORT=ID_BASE` : force une correspondance d'objet, répétable
- `--dedupe-key` : champ servant à détecter les doublons, `mac` par défaut
- `--accept-unreachable` : passe outre la vérification d'atteignabilité. À n'utiliser qu'après avoir lu le rapport et identifié l'objet concerné comme un doublon inter-session bénin. La décision est tracée dans le rapport JSON.

## Tester sans données réelles

Le dépôt embarque un générateur de projets `.esx` synthétiques reproduisant le fork téléphone/tablette, avec un faux conflit dû à l'ordre de sérialisation, un conflit réel de références, et un point d'accès vu par les deux terminaux sous deux identifiants.

```
python tests/make_fixtures.py
python tests/smoke_test.py
```

Le test de bout en bout vérifie que le diagnostic distingue les vrais conflits des faux, que la fusion est refusée sans arbitrage explicite, que le dry-run n'écrit rien, et qu'après fusion en mode `union` les deux sessions de relevé sont présentes et référencées.

## Limites connues

- `project.json` n'est jamais fusionné : le projet garde l'identité de la base. Si votre version d'Ekahau y stocke des références vers des objets de survey, celles du projet d'apport ne seront pas reportées. Vérifiez ce point avec `esx_diff` si le diagnostic signale un conflit sur `project.json`.
- Si les plans d'étage ont été recréés de zéro sur le second terminal, leurs UUID diffèrent et il faut vérifier manuellement que l'échelle et l'orientation sont identiques des deux côtés. Sans quoi les points de mesure atterrissent décalés.
- Les doublons d'AP détectés sur la MAC sont signalés mais restent à arbitrer dans Ekahau après ouverture.
- L'outil ne réconcilie pas des projets Ekahau Cloud. Pour du survey à plusieurs terminaux prévu à l'avance, le mode team survey d'Ekahau reste la bonne réponse.

## Avertissement

Travaillez sur des copies. L'outil n'écrit jamais sur les fichiers d'entrée, mais un `.esx` de survey représente parfois plusieurs jours de terrain et mérite une sauvegarde avant toute manipulation.

Fourni tel quel, sans garantie. Vérifiez le résultat dans Ekahau avant de le livrer à un client.

## Licence

MIT.

## Un retour ?

Si l'outil vous a fait gagner du temps, ou s'il casse sur vos fichiers, dites-le moi sur LinkedIn : [Pierre-Elie Romer](https://www.linkedin.com/in/pierreelie/). Je publie régulièrement mes outils et mes retours de terrain Wi-Fi.
