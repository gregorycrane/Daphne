# agdt2ud

Convert an **Ancient Greek Dependency Treebank** (AGDT / Perseus, annotated in
the Prague Dependency Treebank "analytical" style) into **Universal
Dependencies** CoNLL-U, with both a basic-tree layer and an optional enhanced
graph, plus automatic flagging of the sentences most likely to need human
review.

Built and tested against `xen_symp_1-2.xml` (Xenophon, *Symposium* 1–2;
149 sentences, 2413 surface tokens, 52 elided nodes).

---

## Usage

```bash
# basic UD (single-rooted projective-ish tree in HEAD/DEPREL)
python3 agdt2ud.py xen_symp_1-2.xml > xen_symp_1-2.conllu

# enhanced UD (adds empty nodes + the enhanced graph in the DEPS column)
python3 agdt2ud.py --enhanced xen_symp_1-2.xml > xen_symp_1-2.enhanced.conllu

# experimental: lexicalize obl/nmod/advcl/acl with the case/mark lemma
python3 agdt2ud.py --enhanced --case-subtypes xen_symp_1-2.xml > out.conllu
```

A triage summary is written to `stderr`:

```
[agdt2ud] converted 149 sentences (enhanced UD)
[agdt2ud] review: 13 high-priority, 50 medium-priority, 86 clean
```

No third-party dependencies; standard-library `xml.etree` only.

---

## What the conversion actually does

The two schemes differ at a structural level, so most of the work is **tree
rotation**, not relabeling.

- **AGDT/PDT is function-head:** prepositions, subordinators, and coordinating
  conjunctions are *heads* of the material they introduce; the copula heads its
  predicate.
- **UD is content-head:** function words attach to the content word they
  accompany (`case`, `mark`, `cc`, `cop`, `aux`), and coordination is annotated
  by chaining conjuncts (`conj`) off the first conjunct.

### Morphology

The 9-position `postag` is decoded to UPOS + FEATS (Person, Number, Tense,
Aspect, Mood, VerbForm, Voice, Gender, Case, Degree, Polarity, PronType). The
original positional tag is preserved in the XPOS column for traceability.

### Tree rotations implemented

| AGDT construction | UD result |
|---|---|
| Preposition (`AuxP`) heads its complement | complement promoted; preposition → `case` |
| Subordinator (`AuxC`) heads the clause | clause verb promoted; subordinator → `mark` |
| Coordinator (`COORD`) heads the conjuncts (`*_CO`) | first conjunct promoted; others → `conj`; conjunction → `cc` |
| Apposition node (`APOS`) heads members (`*_AP`) | first member promoted; others → `appos` |
| Copula heads predicate nominal (`PNOM`) | predicate promoted; copula → `cop` |

`OBJ` is split by morphological case (`obj` for accusative, `obl:arg` for
dative/genitive complements) and by verb form (`ccomp`/`xcomp` for clausal
complements); `ATR` fans out to `det`/`amod`/`nmod`/`nummod`/`acl`/`advmod` by
POS; `ADV` to `advmod`/`obl`/`advcl`; particles (`AuxY`/`AuxZ`) to
`discourse`/`advmod`; negators get `Polarity=Neg`.

A notable structural hazard handled explicitly: a subordinator can head a
*coordination* whose first conjunct is the very node being attached, so the
naïve "who is my external parent" walk resolves back to the node itself. An
`external_attachment` routine climbs *past* any construction that resolves into
a forbidden set, preventing the cycles such nesting would otherwise create.

---

## Enhanced UD layer (`--enhanced`)

The enhanced graph lives in the DEPS column and adds three things the basic
tree cannot express:

1. **Empty nodes for ellipsis.** Each `artificial="elliptic"` node is restored
   as a real empty node with a decimal ID derived from its `insertion_id`
   (`0029e` → `29.1`). It carries the reconstructed form, lemma, UPOS, and
   FEATS; HEAD/DEPREL are `_`; and the dependents that are merely `orphan` in
   the basic tree attach to it with their *true* relations. Empty-node →
   empty-node chains are supported.
2. **Conjunct propagation.** Because AGDT factors shared material out
   explicitly (shared dependents hang off the COORD node, private ones off a
   conjunct), governor propagation and shared-dependent propagation are
   recoverable: e.g. a shared subject receives one `nsubj` edge per coordinated
   verb, and each conjunct inherits the coordination's external relation.
3. **Case/mark subtypes** (`--case-subtypes`, off by default). Lexicalizes
   `obl`/`nmod`/`advcl`/`acl` with the governing preposition/subordinator lemma.
   Experimental, because the convention for Ancient Greek is not standardized.

---

## Automatic review flagging

Every sentence is scanned for the patterns where the conversion is least
certain. When any fire, the converter emits CoNLL-U comments (which parsers
ignore):

```
# review_priority = high
# review = multiple competing roots; one chosen heuristically | 2 elided node(s); ...
```

Detected signals and their priority:

| Signal | Priority | Why it matters |
|---|---|---|
| Unannotated source rows (blank head/relation) | high | source data gap; attachment is a guess |
| Multiple competing roots | high | root chosen heuristically among candidates |
| ≥3 relations mapped to generic `dep` | high | several unresolved relations |
| 1–2 relations mapped to `dep` | medium | unresolved relation |
| Elided nodes present | medium | basic tree only approximates (see enhanced DEPS) |
| `ExD` (external-dependency/ellipsis) relations | medium | inherently uncertain attachment |
| Nested function-head cycle-avoidance triggered | medium | tricky attachment, worth a look |
| Nested coordination | medium | propagation is error-prone here |
| Root not labeled `PRED` in source | medium | usually benign (e.g. impersonal verbs) |

On the test file: **13 high, 50 medium, 86 clean.** Most common reasons:
reconstructed ellipsis (34), generic `dep` (23), non-`PRED` root (21), `ExD`
(21), cycle-avoidance (20).

To pull just the work queue:

```bash
grep -B3 'review_priority = high' out.conllu | grep -E '# (sent_id|review)'
```

---

## Known limitations (the manual-correction agenda)

These are intrinsic to AGDT→UD and are **not** solved by any rule-based pass;
the review flags above point at most of them:

- **Coordination of shared modifiers / correlatives** (οὐ μόνον … ἀλλὰ καί,
  τε … καί): scope of shared dependents is not always recoverable.
- **Ellipsis in basic UD:** represented as `orphan` only; the faithful analysis
  lives in the enhanced graph's empty nodes.
- **Relation granularity:** `OBJ`→`obj`/`obl:arg`/`ccomp`/`xcomp` and
  `ATR`→`nmod`/`nmod:poss` cannot always be decided from case + POS alone.
- **Voice ambiguity:** Greek medio-passive (`e`) blocks a reliable
  `nsubj` vs `nsubj:pass` decision, so subjects stay `nsubj`.
- **Particles:** `discourse` vs `advmod` vs `cc` for δέ/μέν/γάρ/τε is a
  convention choice; a different choice shifts hundreds of labels.
- **Not attempted (would need lexical resources / manual review):** `ref` edges
  and antecedent propagation for relative clauses; controlled-subject sharing in
  `xcomp` (object- vs subject-control is verb-specific).

For reference, the official `UD_Ancient_Greek-Perseus` treebank was produced by
an automated AGDT→UD conversion **followed by manual correction** — the same
workflow this tool is designed to support.

---

## Validation

Both outputs pass structural checks on the test file:

| | sentences | tokens | empty nodes | multi-head tokens | structural problems |
|---|---|---|---|---|---|
| basic | 149 | 2413 | 0 | — | 0 |
| enhanced | 149 | 2413 | 52 | 192 | 0 |

Checked: single root per sentence (basic), no dangling heads, no self-loops,
empty nodes carry `_` in HEAD/DEPREL, DEPS sorted and well-formed, every node
reachable from root in the enhanced graph.

> These are internal structural checks. Before publishing, also run the
> official UD validator (`tools/validate.py` from the UniversalDependencies
> repo) for the full guideline conformance suite.

---

## Files

```
agdt2ud/
├── agdt2ud.py                         # the converter
├── README.md                          # this file
└── examples/
    ├── xen_symp_1-2.conllu            # basic UD output
    └── xen_symp_1-2.enhanced.conllu   # enhanced UD output (with --enhanced)
```

## Column reference (CoNLL-U)

`ID  FORM  LEMMA  UPOS  XPOS  FEATS  HEAD  DEPREL  DEPS  MISC`
where XPOS holds the original AGDT positional tag, DEPS holds the enhanced graph
(enhanced runs only), and MISC carries `Ellipsis=Yes` on empty nodes and on
basic-tree `orphan`s.
