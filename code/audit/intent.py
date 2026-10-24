"""Does a request authorise deletion? (AGENTS.md §6.5: otherwise NOT_APPLICABLE.)

Deterministic and explainable. A request authorises deletion when it asks to
delete, erase, remove or close the account or its personal data, in any of
the supported languages, and does not also say it is not asking for that.
Anything else (access, copy or export, correction, login help, marketing
opt-out) is not a deletion request. The verdict carries the matched phrases,
so every decision can be reviewed.

Rules, applied in order:
  1. An explicit negation of deletion ("not asking for deletion", "do not
     close", "don't delete", "without removing", "keep the account",
     "leave ... in place / active", "continue using", "not a request to
     erase") means NOT a deletion request.
  2. A removal limited to marketing or promotional messages ("remove me from
     marketing lists only", "stop the promotional emails") is an opt-out,
     not a deletion request.
  3. Otherwise, a deletion phrase in any supported language means a deletion
     request.
  4. With no deletion phrase, the request is not a deletion request.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# deletion of the account or personal data, per language
DELETION = [
    r"\b(delet(e|ed|es|ing|ion)|eras(e|ed|es|ing|ure)|remov(e|ed|es|ing|al))\b",
    r"\b(account|profile|data|records?)\b[^.]*\b(is|are|be|been) gone\b",  # "confirm the profile is gone"
    r"\bclose (my|this|the) account\b", r"\bclose this account\b",
    r"\baccount[- ]deletion\b",
    r"\bsuppression\b", r"\bsupprim",                      # French
    r"\beliminaci[oó]n\b", r"\beliminar\b", r"\bborra",    # Spanish
    r"\bexclus[aã]o\b", r"\bexcluir\b", r"\bapagar\b",     # Portuguese
    r"\bl[oö]sch", r"\bentfern",                           # German
    r"\bcancella", r"\brimuov",                            # Italian
    "削除", "삭제", "删除", "удал",                          # Japanese, Korean, Chinese, Russian
]
# an explicit statement that deletion is NOT wanted
NEGATION = [
    r"\bnot asking for (deletion|removal|erasure)\b",
    r"\bnot a request to (erase|delete|remove|close)\b",
    r"\b(do not|don'?t|never) (close|delete|erase|remove)\b",
    r"\bwithout (removing|deleting|erasing|closing)\b",
    r"\bkeep (the|my) account\b", r"\bkeep the account as it is\b",
    r"\bleave (the|my) (account|profile)[^.]*\b(active|in place|as it is)\b",
    r"\bcontinue using\b", r"\bstill (use|need|want) (it|the account|my account)\b",
    # the same negations in the other supported languages
    r"\bne (demande|souhaite|veux) pas (la )?suppression\b", r"\bne (supprimez|fermez) pas\b",
    r"\bsans (supprimer|effacer)\b",                                                    # French
    r"\bno (quiero|solicito|pido) (la )?(eliminaci[oó]n|eliminar|borrar)\b",
    r"\bno (eliminen|borren|cierren)\b", r"\bsin (eliminar|borrar)\b",                   # Spanish
    r"\bn[aã]o (quero|solicito|pe[cç]o) (a )?(exclus[aã]o|excluir|apagar)\b",
    r"\bn[aã]o (excluam|apaguem)\b", r"\bsem (excluir|apagar)\b",                        # Portuguese
    r"\bkeine l[oö]schung\b", r"\bnicht l[oö]schen\b", r"\bohne zu l[oö]schen\b",          # German
    r"\bnon (cancellate|eliminate)\b", r"\bsenza (cancellare|eliminare)\b",              # Italian
    "削除しないで", "削除は不要", "削除を希望しません",                                       # Japanese
    "삭제하지 마", "삭제를 원하지 않", "삭제 요청이 아닙",                                    # Korean
]
# removal limited to marketing / promotional contact
OPT_OUT = [
    r"\b(remove|unsubscribe|take) me from (the )?(marketing|promotional|mailing)[^.]*\bonly\b",
    r"\bstop (the |all )?(promotional|marketing) (emails?|messages?)\b",
]


@dataclass(frozen=True)
class IntentVerdict:
    authorises_deletion: bool
    rule: str            # which rule decided
    matched: tuple[str, ...]


def _find(patterns, text) -> tuple[str, ...]:
    hits = []
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            hits.append(match.group(0))
    return tuple(hits)


def classify_request(text: str | None) -> IntentVerdict:
    if not text or not text.strip():
        return IntentVerdict(False, "empty request text", ())
    normalised = unicodedata.normalize("NFC", text).replace("’", "'")
    negation = _find(NEGATION, normalised)
    if negation:
        return IntentVerdict(False, "explicit negation of deletion", negation)
    opt_out = _find(OPT_OUT, normalised)
    if opt_out:
        return IntentVerdict(False, "marketing opt-out only", opt_out)
    deletion = _find(DELETION, normalised)
    if deletion:
        return IntentVerdict(True, "deletion requested", deletion)
    return IntentVerdict(False, "no deletion phrase", ())
