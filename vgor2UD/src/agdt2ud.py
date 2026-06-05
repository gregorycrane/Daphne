#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agdt2ud.py  —  Convert an Ancient Greek Dependency Treebank (AGDT / Perseus,
annotated in the Prague Dependency Treebank "analytical" style) into
Universal Dependencies (CoNLL-U).

Usage:
    python3 agdt2ud.py input.xml > output.conllu

This is a faithful-but-pragmatic converter. The genuinely hard transformations
(coordination, copula, prepositions, subordinators, ellipsis) are implemented
explicitly; see the module docstring sections and the README discussion for the
linguistic rationale and the residual problems that no rule-based pass solves
cleanly.

The two annotation schemes differ in a fundamental way:

  * PDT/AGDT is a *function-head* (a.k.a. syntactic-head) scheme. Prepositions,
    subordinating conjunctions, and coordinating conjunctions are HEADS of the
    material they introduce. The copula is the head of its predicate.

  * UD is a *content-head* (lexical-head) scheme. Function words attach to the
    content word they accompany (case, mark, cc, cop, aux), and coordination is
    annotated by chaining conjuncts (conj) off the first conjunct.

So the core of the job is a set of *tree rotations*, not a label lookup.
"""

import sys
import xml.etree.ElementTree as ET


# ---------------------------------------------------------------------------
# 1. MORPHOLOGY:  AGDT 9-position postag  ->  UPOS + FEATS
# ---------------------------------------------------------------------------
# Positions: 1 POS | 2 person | 3 number | 4 tense | 5 mood | 6 voice
#            7 gender | 8 case | 9 degree

POS_MAP = {
    "n": "NOUN", "v": "VERB", "a": "ADJ", "d": "ADV", "l": "DET",
    "g": "PART", "c": "CCONJ", "r": "ADP", "p": "PRON", "m": "NUM",
    "i": "INTJ", "u": "PUNCT", "x": "X", "e": "INTJ",
}

PERSON = {"1": "1", "2": "2", "3": "3"}
NUMBER = {"s": "Sing", "d": "Dual", "p": "Plur"}
# (tense + aspect handled jointly below)
MOOD = {"i": "Ind", "s": "Sub", "o": "Opt", "m": "Imp"}
VERBFORM_FROM_MOOD = {"n": "Inf", "p": "Part"}
VOICE = {"a": "Act", "m": "Mid", "p": "Pass", "e": "Mid"}  # e = medio-passive
GENDER = {"m": "Masc", "f": "Fem", "n": "Neut"}
CASE = {"n": "Nom", "g": "Gen", "d": "Dat", "a": "Acc", "v": "Voc", "l": "Loc"}
DEGREE = {"c": "Cmp", "s": "Sup", "p": "Pos"}

# Greek tense -> (Tense, Aspect). This is one of the contested mappings; the
# choices below follow the convention used by UD_Ancient_Greek-Perseus.
TENSE_ASPECT = {
    "p": ("Pres", "Imp"),    # present
    "i": ("Past", "Imp"),    # imperfect
    "a": ("Past", "Perf"),   # aorist  (treated as perfective past)
    "r": ("Past", "Perf"),   # perfect
    "l": ("Pqp",  "Perf"),   # pluperfect
    "f": ("Fut",  None),     # future
    "t": ("Fut",  "Perf"),   # future perfect
}

COORDINATORS = {"καί", "ἤ", "ἢ", "ἤ1", "οὐδέ", "οὔτε", "μήτε", "τε", "ἀλλά",
                "δέ", "ἀτάρ", "αὐτάρ", "ἠδέ", "ἰδέ"}


def decode_morph(postag, lemma, relation):
    """Return (upos, feats_dict) from a 9-char positional tag."""
    p = (postag or "").ljust(9, "-")
    pos = p[0]
    upos = POS_MAP.get(pos, "X")
    feats = {}

    # --- refine UPOS that depends on syntactic role / lemma ---
    if pos == "c":
        # conjunction: subordinator vs coordinator decided by relation/lemma
        if relation == "AuxC":
            upos = "SCONJ"
        elif relation == "COORD" or lemma in COORDINATORS:
            upos = "CCONJ"
        else:
            upos = "SCONJ" if lemma not in COORDINATORS else "CCONJ"
    if pos == "d":
        # "adverbs" in AGDT include many particles. Negators stay ADV (+Polarity).
        if lemma in ("οὐ", "οὐκ", "οὐχ", "μή", "μὴ"):
            upos = "ADV"
            feats["Polarity"] = "Neg"
        elif lemma in COORDINATORS and relation in ("COORD",):
            upos = "CCONJ"
    if pos == "v" and lemma in ("εἰμί",) and relation in ("AuxV",):
        upos = "AUX"

    # --- nominal / verbal morphology ---
    if p[1] in PERSON:
        feats["Person"] = PERSON[p[1]]
    if p[2] in NUMBER:
        feats["Number"] = NUMBER[p[2]]
    if p[3] in TENSE_ASPECT:
        t, a = TENSE_ASPECT[p[3]]
        if t:
            feats["Tense"] = t
        if a:
            feats["Aspect"] = a
    if p[4] in MOOD:
        feats["Mood"] = MOOD[p[4]]
        feats["VerbForm"] = "Fin"
    elif p[4] in VERBFORM_FROM_MOOD:
        feats["VerbForm"] = VERBFORM_FROM_MOOD[p[4]]
    if p[5] in VOICE:
        feats["Voice"] = VOICE[p[5]]
    if p[6] in GENDER:
        feats["Gender"] = GENDER[p[6]]
    if p[7] in CASE:
        feats["Case"] = CASE[p[7]]
    if p[8] in DEGREE and DEGREE[p[8]] != "Pos":
        feats["Degree"] = DEGREE[p[8]]

    if upos == "DET":
        feats.setdefault("PronType", "Art")
    return upos, feats


def fmt_feats(feats):
    if not feats:
        return "_"
    return "|".join(f"{k}={feats[k]}" for k in sorted(feats))


# ---------------------------------------------------------------------------
# 2. TREE MODEL
# ---------------------------------------------------------------------------
class Tok:
    __slots__ = ("idx", "form", "lemma", "postag", "rel", "head",
                 "artificial", "insertion_id", "empty_src", "upos", "feats",
                 "ud_head", "ud_rel", "children", "misc",
                 "enh", "case_marker", "mark_marker")

    def __init__(self, idx, form, lemma, postag, rel, head, artificial,
                 insertion_id=None, empty_src=False):
        self.idx = idx                # original AGDT id (int)
        self.form = form
        self.lemma = lemma
        self.postag = postag
        self.rel = rel or ""          # PDT analytical function
        self.head = head              # original head id (int; 0 = root)
        self.artificial = artificial  # None or "elliptic"
        self.insertion_id = insertion_id  # e.g. "0029e" -> empty node after tok 29
        self.empty_src = empty_src    # True if source HEAD was blank (data noise)
        self.upos, self.feats = decode_morph(postag, lemma, self.rel)
        self.ud_head = None
        self.ud_rel = None
        self.children = []
        self.misc = {}
        # enhanced-UD: list of (head_id, deprel) incoming edges (head_id in
        # AGDT id-space; 0 = root). Filled by convert_sentence.
        self.enh = []
        self.case_marker = None       # lemma of governing AuxP, if any
        self.mark_marker = None       # lemma of governing AuxC, if any


def base_rel(rel):
    """Strip _CO / _AP coordination/apposition suffixes -> bare function."""
    for suf in ("_CO", "_AP_CO", "_AP"):
        if rel.endswith(suf):
            return rel[: -len(suf)]
    return rel


def is_member(rel):
    return rel.endswith("_CO") or rel.endswith("_AP")


def _add_edge(tok, head_id, rel):
    """Add an enhanced incoming edge (head_id, rel) to tok, avoiding dupes
    and self-loops."""
    if head_id == tok.idx:
        return
    if (head_id, rel) not in tok.enh:
        tok.enh.append((head_id, rel))


# ---------------------------------------------------------------------------
# 3. DEPREL of an "ordinary" node, given its (effective) parent
# ---------------------------------------------------------------------------
def map_deprel(tok, parent):
    """Map a bare PDT function to a UD deprel, using POS context.
    `parent` is the effective UD parent Tok (or None for root)."""
    r = base_rel(tok.rel)
    pos = tok.upos
    is_clause = tok.feats.get("VerbForm") in ("Fin", "Inf", "Part")

    if r in ("PRED", "ExD") and (parent is None):
        return "root"

    if r == "SBJ":
        return "csubj" if is_clause and pos == "VERB" else "nsubj"
    if r in ("OBJ", "OCOMP"):
        if pos == "VERB":
            return "xcomp" if tok.feats.get("VerbForm") == "Inf" else "ccomp"
        case = tok.feats.get("Case")
        if case == "Acc":
            return "obj"
        if case in ("Dat", "Gen"):
            return "obl:arg"   # Greek governs many complements in dat/gen
        return "obj"
    if r == "PNOM":
        # only reached if copula rotation did NOT fire (defensive)
        return "nsubj" if pos in ("NOUN", "PRON", "PROPN", "ADJ", "DET") else "dep"
    if r == "ATR":
        if pos == "DET":
            return "det"
        if pos == "ADJ":
            return "amod"
        if pos == "NUM":
            return "nummod"
        if pos == "VERB":
            return "acl"
        if pos in ("NOUN", "PROPN", "PRON"):
            return "nmod"
        if pos in ("ADV", "PART", "CCONJ", "SCONJ"):
            return "advmod"
        return "amod"
    if r in ("ATV", "AtvV"):           # circumstantial / supplementary participle
        return "advcl"
    if r == "ADV":
        if pos == "VERB":
            return "advcl"
        if pos in ("NOUN", "PROPN", "PRON", "DET", "NUM"):
            return "obl"
        return "advmod"
    if r == "AuxV":
        return "aux"
    if r == "AuxZ":
        return "advmod"
    if r == "AuxY":
        # connective particles (δέ, μέν, γάρ) vs others
        if tok.upos == "CCONJ":
            return "cc"
        return "discourse"
    if r in ("AuxX", "AuxK", "AuxG"):
        return "punct"
    if r == "AuxP":
        return "case"
    if r == "AuxC":
        return "mark"
    if r == "PRED":
        return "parataxis"      # non-root predicate not in a coord -> parataxis
    if r == "APOS":
        return "appos"
    return "dep"


# ---------------------------------------------------------------------------
# 4. CORE CONVERSION (per sentence)
# ---------------------------------------------------------------------------
def convert_sentence(toks):
    """toks: list[Tok] in original order, indexed by .idx. Returns
    (surface, artificials, stats) where surface tokens have ud_head/ud_rel set
    and stats records conversion-uncertainty signals for review triage."""

    by_id = {t.idx: t for t in toks}
    by_id[0] = None  # sentinel for root

    # signals gathered during conversion, used by the review-flagging pass
    stats = {"climb": 0, "root_fallback": False}

    # rebuild children lists on the ORIGINAL tree
    for t in toks:
        if t.head in by_id and by_id[t.head] is not None:
            by_id[t.head].children.append(t)

    # --- 4a. effective-parent resolution ----------------------------------
    # Walk up through AuxP / AuxC / COORD / APOS nodes and the copula to find,
    # for any node, the UD attachment point and deprel. We implement this as a
    # recursive "promote" that returns the lexical representative of a subtree.

    def representative(node):
        """The UD head token that should represent this PDT subtree to the
        outside world (used when a parent needs to attach to it)."""
        if node is None:
            return None
        r = node.rel
        # Coordination: the conjunction's representative is its first conjunct.
        if r == "COORD" or base_rel(r) == "COORD":
            conjs = [c for c in node.children if is_member(c.rel)]
            if conjs:
                return representative(conjs[0])
        # Apposition node: first member represents it.
        if r == "APOS":
            members = [c for c in node.children if is_member(c.rel)]
            if members:
                return representative(members[0])
        # Preposition: its nominal complement represents the PP.
        if base_rel(r) == "AuxP" or r == "AuxP":
            comp = _prep_complement(node)
            if comp is not None:
                return representative(comp)
        # Subordinator: the clause head represents the clause.
        if base_rel(r) == "AuxC" or r == "AuxC":
            comp = _clause_head(node)
            if comp is not None:
                return representative(comp)
        # Copula: the predicate nominal represents the clause.
        pnom = _pnom_child(node)
        if pnom is not None:
            return representative(pnom)
        return node

    def _prep_complement(prep):
        kids = [c for c in prep.children if base_rel(c.rel) not in
                ("AuxP", "AuxX", "AuxK", "AuxG", "AuxY", "AuxZ")]
        return kids[0] if kids else None

    def _clause_head(conj):
        kids = [c for c in conj.children if base_rel(c.rel) not in
                ("AuxC", "AuxX", "AuxK", "AuxG", "AuxY", "AuxZ")]
        return kids[0] if kids else None

    def _pnom_child(node):
        if base_rel(node.rel) == "PNOM":
            return None
        if node.lemma not in ("εἰμί", "γίγνομαι", "γίνομαι"):
            return None
        if node.upos not in ("VERB", "AUX"):
            return None
        pnoms = [c for c in node.children if base_rel(c.rel) == "PNOM"]
        return pnoms[0] if pnoms else None

    def external_attachment(node, avoid_ids):
        """Climb the AGDT head chain starting from `node` and return the first
        representative that is NOT in `avoid_ids`. Needed when a construction
        (subordinator/preposition/coordination) resolves back down to the very
        node whose attachment we are computing -- e.g. a subordinator that heads
        a coordination whose first conjunct is the node itself."""
        cur = by_id.get(node.head)
        guard = 0
        while cur is not None and guard < 100:
            rep = representative(cur)
            if rep is not None and rep.idx not in avoid_ids:
                if guard > 0:
                    stats["climb"] += 1   # had to skip a self-resolving construction
                return rep
            cur = by_id.get(cur.head)
            guard += 1
        return None

    # --- 4b. assign ud_head / ud_rel for every node -----------------------
    for t in toks:
        parent = by_id.get(t.head)

        # (i) the node is itself a function head that gets demoted ----------
        # Preposition -> case of its complement
        if base_rel(t.rel) == "AuxP":
            comp = _prep_complement(t)
            if comp is not None:
                rep = representative(comp)
                t.ud_head = rep.idx
                t.ud_rel = "case"
                rep.case_marker = t.lemma
                continue
        # Subordinator -> mark of its clause head
        if base_rel(t.rel) == "AuxC":
            ch = _clause_head(t)
            if ch is not None:
                rep = representative(ch)
                t.ud_head = rep.idx
                t.ud_rel = "mark"
                rep.mark_marker = t.lemma
                continue
        # Coordinating conjunction -> cc of the following conjunct
        if base_rel(t.rel) == "COORD":
            conjs = [c for c in t.children if is_member(c.rel)]
            if conjs:
                # attach cc to the first real conjunct after it (or first conj)
                later = [c for c in conjs if c.idx > t.idx] or conjs
                t.ud_head = representative(later[0]).idx
                t.ud_rel = "cc"
                continue
        # Copula -> cop of its predicate nominal
        pn = _pnom_child(t)
        if pn is not None:
            t.ud_head = representative(pn).idx
            t.ud_rel = "cop"
            continue

        # (ii) the node is a CONJUNCT (X_CO) -------------------------------
        if t.rel.endswith("_CO"):
            coord = parent  # parent is the COORD conjunction
            members = [c for c in coord.children if is_member(c.rel)] if coord else []
            members.sort(key=lambda x: x.idx)
            if members and t is members[0]:
                # first conjunct: inherits the COORD node's external attachment
                avoid = {m.idx for m in members} | {t.idx}
                ext_rep = external_attachment(coord, avoid) if coord else None
                t.ud_head = ext_rep.idx if ext_rep else 0
                t.ud_rel = map_deprel(t, ext_rep) if ext_rep else "root"
            else:
                first = representative(members[0]) if members else None
                t.ud_head = first.idx if first else 0
                t.ud_rel = "conj"
            continue

        # (iii) the node is an apposition member (X_AP) --------------------
        if t.rel.endswith("_AP"):
            apos = parent
            members = [c for c in apos.children if is_member(c.rel)] if apos else []
            members.sort(key=lambda x: x.idx)
            if members and t is members[0]:
                avoid = {m.idx for m in members} | {t.idx}
                ext_rep = external_attachment(apos, avoid) if apos else None
                t.ud_head = ext_rep.idx if ext_rep else 0
                t.ud_rel = map_deprel(t, ext_rep) if ext_rep else "root"
            else:
                first = representative(members[0]) if members else None
                t.ud_head = first.idx if first else 0
                t.ud_rel = "appos"
            continue

        # (iv) ordinary node: attach to the representative of its parent ----
        rep = representative(parent)
        if rep is None:
            t.ud_head = 0
            t.ud_rel = "root"
        else:
            # if the chosen representative is the node itself (e.g. it was the
            # promoted predicate of a copula), climb past the construction
            if rep is t:
                gp = external_attachment(parent, {t.idx}) if parent else None
                t.ud_head = gp.idx if gp else 0
                t.ud_rel = map_deprel(t, gp) if gp else "root"
            else:
                t.ud_head = rep.idx
                t.ud_rel = map_deprel(t, rep)

    # --- 4c. handle the copula-promoted predicate nominal -----------------
    # A PNOM whose parent is a copula must take the copula's external slot.
    for t in toks:
        if base_rel(t.rel) == "PNOM":
            cop = by_id.get(t.head)
            if cop is not None and _pnom_child(cop) is t:
                ext_rep = external_attachment(cop, {t.idx, cop.idx})
                if ext_rep is None:
                    t.ud_head, t.ud_rel = 0, "root"
                else:
                    t.ud_head = ext_rep.idx
                    t.ud_rel = map_deprel(cop, ext_rep)  # inherit cop's role

    # === ENHANCED-UD GRAPH ================================================
    # Snapshot the pre-contraction tree: at this point dependents of elided
    # nodes still point AT those nodes with their true (mapped) relation, and
    # the copula rotation is done. This snapshot IS the enhanced graph modulo
    # the propagation edges added below.
    for t in toks:
        if t.ud_head is not None:
            t.enh = [(t.ud_head, t.ud_rel)]
        else:
            t.enh = [(0, "root")]

    # --- conjunct propagation (governors + shared dependents) -------------
    # UD enhanced graphs propagate (a) the external governor onto every
    # conjunct and (b) shared dependents onto every conjunct. AGDT makes this
    # recoverable: conjuncts are the *_CO children of a COORD node; shared
    # dependents are the COORD node's other (non-member, non-punct) children.
    PROP_SKIP = {"cc", "punct", "conj", "mark", "case", "cop", "aux"}
    for k in toks:
        if base_rel(k.rel) != "COORD":
            continue
        members = sorted([c for c in k.children if is_member(c.rel)],
                         key=lambda x: x.idx)
        if len(members) < 1:
            continue
        first = members[0]
        # (a) governor propagation: the first conjunct's external edge is the
        #     shared role; replicate it onto the other conjuncts.
        ext_head, ext_rel = first.enh[0] if first.enh else (first.ud_head, first.ud_rel)
        for m in members[1:]:
            rep = representative(m)
            if ext_head != rep.idx:
                _add_edge(rep, ext_head, ext_rel)
        # (b) shared-dependent propagation: COORD's non-member children that
        #     are real modifiers get an edge to every conjunct.
        shared = [c for c in k.children
                  if not is_member(c.rel)
                  and base_rel(c.rel) not in ("AuxX", "AuxK", "AuxG", "COORD")]
        for d in shared:
            drep = representative(d)
            for m in members:
                mrep = representative(m)
                if mrep.idx == drep.idx:
                    continue
                _add_edge(drep, mrep.idx, map_deprel(drep, mrep))
    # === end enhanced graph ===============================================

    # --- 4d. remove ARTIFICIAL (elliptic) nodes ---------------------------
    # Reattach their dependents to the artificial node's UD head with `orphan`
    # (a basic-UD approximation of enhanced-UD ellipsis), then drop them.
    artificial = [t for t in toks if t.artificial]
    art_ids = {t.idx for t in artificial}
    for t in toks:
        if t.ud_head in art_ids:
            anc = by_id.get(t.ud_head)
            # climb to first non-artificial UD ancestor
            guard = 0
            while anc is not None and anc.idx in art_ids and guard < 50:
                anc = by_id.get(anc.ud_head)
                guard += 1
            if anc is None:
                t.ud_head, t.ud_rel = 0, "root"
            else:
                t.ud_head = anc.idx
                if t.ud_rel not in ("punct",):
                    t.misc["Ellipsis"] = "Yes"
                    t.ud_rel = "orphan"
    surface = [t for t in toks if not t.artificial]

    # --- 4e. fix empty / dangling heads & guarantee a single root ---------
    surf_ids = {t.idx for t in surface}
    roots = [t for t in surface if t.ud_head in (0, None) or t.ud_head not in surf_ids]
    # promote a deterministic root: prefer an original PRED, else first finite verb
    real_root = None
    for t in surface:
        if base_rel(t.rel) == "PRED" and (t.ud_head in (0, None)):
            real_root = t
            break
    if real_root is None and roots:
        real_root = roots[0]
    # flag for review: distinguish genuine ambiguity from a benign label quirk
    if len([t for t in roots if t.upos != "PUNCT"]) > 1:
        stats["root_multi"] = True          # several competing roots -> high
    if real_root is not None and base_rel(real_root.rel) != "PRED":
        stats["root_nonpred"] = True        # root not labeled PRED -> medium
    for t in roots:
        if t is real_root:
            t.ud_head, t.ud_rel = 0, "root"
        elif t.upos == "PUNCT" or t.ud_rel == "punct":
            t.ud_head = real_root.idx if real_root else 0
            t.ud_rel = "punct"
        else:
            if real_root is not None and t is not real_root:
                t.ud_head = real_root.idx
                t.ud_rel = "parataxis" if t.ud_rel in ("root", "", None) else t.ud_rel
            else:
                t.ud_head, t.ud_rel = 0, "root"
    if real_root is not None:
        real_root.ud_head, real_root.ud_rel = 0, "root"

    # final safety: no self-loops, no dangling
    for t in surface:
        if t.ud_head == t.idx:
            t.ud_head, t.ud_rel = (real_root.idx if real_root and real_root is not t else 0), \
                                   ("dep" if real_root and real_root is not t else "root")
        if t.ud_head not in surf_ids and t.ud_head != 0:
            t.ud_head = real_root.idx if real_root else 0
            t.ud_rel = t.ud_rel or "dep"

    # unannotated source rows (blank HEAD): let the enhanced graph mirror the
    # basic fixup rather than emit a spurious extra root.
    for t in toks:
        if t.empty_src:
            t.enh = [(t.ud_head, t.ud_rel)]

    artificials = [t for t in toks if t.artificial]
    return surface, artificials, stats


# ---------------------------------------------------------------------------
# 5. CoNLL-U OUTPUT
# ---------------------------------------------------------------------------
def _prefix_anchor(insertion_id):
    """'0029e' -> 29 (the surface token the empty node is inserted after)."""
    if not insertion_id:
        return None
    digits = ""
    for ch in insertion_id:
        if ch.isdigit():
            digits += ch
        else:
            break
    return int(digits) if digits else None


def _deps_sort_key(idstr):
    if "." in idstr:
        a, b = idstr.split(".")
        return (int(a), int(b))
    return (int(idstr), 0)


def compute_review(toks, surface, artificials, stats, remap):
    """Identify, per sentence, the spots most likely to need human correction.
    Returns (priority, [reason, ...]) or (None, []) if the sentence looks clean.
    Severity 2 = high (likely wrong / data gap), 1 = medium (approximate)."""
    reasons = []
    rid = lambda t: str(remap.get(t.idx, "?"))

    # (high) unannotated source rows: blank HEAD or blank relation
    blank = [t for t in surface if t.empty_src or t.rel == ""]
    if blank:
        ids = ",".join(rid(t) for t in blank[:12])
        reasons.append((2, f"{len(blank)} unannotated source token(s) "
                           f"(blank head/relation), auto-attached [ids {ids}]"))

    # (high) root genuinely ambiguous: several nodes competed for root
    if stats.get("root_multi"):
        reasons.append((2, "multiple competing roots; one chosen heuristically"))
    # (med) root not labeled PRED in source (often benign, worth a glance)
    if stats.get("root_nonpred"):
        reasons.append((1, "root not labeled PRED in source (chosen heuristically)"))

    # (high/med) relations that fell through to the generic 'dep'
    dep = [t for t in surface if t.ud_rel == "dep"]
    if dep:
        sev = 2 if len(dep) >= 3 else 1
        ids = ",".join(rid(t) for t in dep[:12])
        reasons.append((sev, f"{len(dep)} relation(s) mapped to generic 'dep' "
                             f"[ids {ids}]"))

    # (med) reconstructed ellipsis: basic tree only approximates this
    if artificials:
        reasons.append((1, f"{len(artificials)} elided node(s); basic tree uses "
                           f"'orphan' approximation (full structure in enhanced DEPS)"))

    # (med) ExD = external dependency / ellipsis marker in the source
    exd = [t for t in surface if base_rel(t.rel) == "ExD"]
    if exd:
        ids = ",".join(rid(t) for t in exd[:12])
        reasons.append((1, f"{len(exd)} ExD (external-dependency/ellipsis) "
                           f"relation(s) [ids {ids}]"))

    # (med) nested function-head construction forced cycle-avoidance
    if stats.get("climb"):
        reasons.append((1, f"{stats['climb']} nested function-head construction(s) "
                           f"needed cycle-avoidance (check attachment)"))

    # (med) coordination nested under coordination: propagation is error-prone
    by_id = {t.idx: t for t in toks}
    nested = any(base_rel(t.rel) == "COORD"
                 and by_id.get(t.head) is not None
                 and base_rel(by_id[t.head].rel) == "COORD"
                 for t in toks)
    if nested:
        reasons.append((1, "nested coordination (conjunct propagation may need review)"))

    if not reasons:
        return None, []
    reasons.sort(key=lambda x: -x[0])
    priority = "high" if reasons[0][0] == 2 else "medium"
    return priority, [txt for _, txt in reasons]


def write_conllu(sent_id, subdoc, doc_id, surface, artificials, out,
                 toks=None, stats=None, enhanced=False, case_subtypes=False):
    order = sorted(surface, key=lambda t: t.idx)
    remap = {t.idx: i + 1 for i, t in enumerate(order)}
    remap[0] = 0

    # assign decimal IDs to empty (elliptic) nodes from their insertion_id
    dec = {}
    groups = {}
    for a in artificials:
        prefix = _prefix_anchor(a.insertion_id)
        anchor = remap.get(prefix, len(order)) if prefix is not None else len(order)
        groups.setdefault(anchor, []).append(a)
    for anchor, lst in groups.items():
        lst.sort(key=lambda a: a.insertion_id or "")
        for k, a in enumerate(lst, start=1):
            dec[a.idx] = f"{anchor}.{k}"

    def out_id(idx):
        if idx in remap:
            return str(remap[idx])
        if idx in dec:
            return dec[idx]
        return "0"

    def subtype(tok, rel):
        """Optionally lexicalize obl/nmod/advcl/acl/conj with the case/mark
        marker (experimental; the exact convention for Greek varies)."""
        if not case_subtypes:
            return rel
        base = rel.split(":")[0]
        if base in ("obl", "nmod") and tok.case_marker:
            return f"{base}:{tok.case_marker}"
        if base in ("advcl", "acl") and tok.mark_marker:
            return f"{base}:{tok.mark_marker}"
        return rel

    def deps_field(tok):
        edges = []
        for h, r in (tok.enh or [(tok.ud_head, tok.ud_rel)]):
            edges.append((out_id(h), subtype(tok, r or "dep")))
        # dedupe + sort by head
        seen, uniq = set(), []
        for h, r in edges:
            if (h, r) not in seen:
                seen.add((h, r))
                uniq.append((h, r))
        uniq.sort(key=lambda e: (_deps_sort_key(e[0]), e[1]))
        return "|".join(f"{h}:{r}" for h, r in uniq) if uniq else "_"

    out.write(f"# sent_id = {sent_id}\n")
    if subdoc:
        out.write(f"# subdoc = {subdoc}\n")
    if doc_id and sent_id.endswith("-1"):
        out.write(f"# newdoc id = {doc_id}\n")
    out.write("# text = " + " ".join(t.form for t in order) + "\n")

    priority, reasons = compute_review(toks or [], surface, artificials,
                                       stats or {}, remap)
    if priority:
        out.write(f"# review_priority = {priority}\n")
        out.write("# review = " + " | ".join(reasons) + "\n")

    def emit(tok, idstr, is_empty):
        misc = dict(tok.misc)
        if is_empty:
            misc["Ellipsis"] = "Yes"
        misc_s = "|".join(f"{k}={v}" for k, v in misc.items()) if misc else "_"
        if is_empty:
            head_s, rel_s = "_", "_"          # empty nodes live only in DEPS
        else:
            head_s = str(remap.get(tok.ud_head, 0))
            rel_s = tok.ud_rel or "dep"
        deps_s = deps_field(tok) if enhanced else "_"
        cols = [idstr, tok.form, tok.lemma or "_", tok.upos,
                tok.postag or "_", fmt_feats(tok.feats),
                head_s, rel_s, deps_s, misc_s]
        out.write("\t".join(cols) + "\n")

    for t in order:
        emit(t, str(remap[t.idx]), is_empty=False)
        if enhanced:
            anchor = remap[t.idx]
            for a in sorted([x for x in artificials
                             if dec.get(x.idx, "").split(".")[0] == str(anchor)],
                            key=lambda x: _deps_sort_key(dec[x.idx])):
                emit(a, dec[a.idx], is_empty=True)
    out.write("\n")
    return priority


# ---------------------------------------------------------------------------
# 6. DRIVER
# ---------------------------------------------------------------------------
def main(path, enhanced=False, case_subtypes=False):
    tree = ET.parse(path)
    root = tree.getroot()
    ns_strip = lambda tag: tag.split("}")[-1]

    out = sys.stdout
    n_sent = 0
    tally = {}
    for sent in root.iter():
        if ns_strip(sent.tag) != "sentence":
            continue
        n_sent += 1
        sid = sent.get("id", str(n_sent))
        subdoc = sent.get("subdoc", "")
        doc_id = sent.get("document_id", "")

        toks = []
        for w in sent:
            if ns_strip(w.tag) != "word":
                continue
            try:
                idx = int(w.get("id"))
            except (TypeError, ValueError):
                continue
            head_raw = w.get("head", "")
            empty_src = (head_raw == "" or head_raw is None)
            try:
                head = int(head_raw)
            except (TypeError, ValueError):
                head = 0  # empty head -> detached, fixed up later
            toks.append(Tok(
                idx=idx,
                form=w.get("form", "_"),
                lemma=w.get("lemma", "_"),
                postag=w.get("postag", ""),
                rel=w.get("relation", ""),
                head=head,
                artificial=w.get("artificial"),
                insertion_id=w.get("insertion_id"),
                empty_src=empty_src,
            ))
        if not toks:
            continue
        surface, artificials, stats = convert_sentence(toks)
        priority = write_conllu(f"xen_symp-{sid}", subdoc, doc_id, surface,
                                artificials, out, toks=toks, stats=stats,
                                enhanced=enhanced, case_subtypes=case_subtypes)
        tally[priority] = tally.get(priority, 0) + 1

    mode = "enhanced" if enhanced else "basic"
    flagged = tally.get("high", 0) + tally.get("medium", 0)
    sys.stderr.write(
        f"[agdt2ud] converted {n_sent} sentences ({mode} UD)\n"
        f"[agdt2ud] review: {tally.get('high', 0)} high-priority, "
        f"{tally.get('medium', 0)} medium-priority, "
        f"{n_sent - flagged} clean\n")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:]]
    enhanced = "--enhanced" in args
    case_subtypes = "--case-subtypes" in args
    paths = [a for a in args if not a.startswith("--")]
    if len(paths) != 1:
        sys.stderr.write(
            "usage: python3 agdt2ud.py [--enhanced] [--case-subtypes] "
            "input.xml > out.conllu\n")
        sys.exit(1)
    main(paths[0], enhanced=enhanced, case_subtypes=case_subtypes)
