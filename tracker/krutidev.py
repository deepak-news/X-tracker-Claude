"""Turns Hindi typed in the Kruti Dev font back into real Hindi text.

Many Hindi-language government offices still type in Kruti Dev 010, an old
font that draws Devanagari shapes on top of ordinary English keys. The PDF
looks right to a reader, but the text inside it is the keys that were
pressed: मंत्रिपरिषद comes out as `eaf=ifj"kn`. Nothing -- no search, no AI --
can read that, so a Kruti Dev document is converted before it is judged.

The mapping follows the Kruti Dev 010 keyboard. Two keys need moving rather
than swapping: "f" (the short-i sign) is typed BEFORE its consonant but
belongs after it in real text, and "Z" (the half-r on top) is typed AFTER
its syllable but belongs before it.
"""

import re

# Longer key sequences first: "vks" (ओ) must win over "vk" (आ) and "v" (अ).
_SEQUENCES = [
    ("vkS", "औ"), ("vks", "ओ"), ("vk", "आ"), (",s", "ऐ"), ("bZ", "ई"),
    ("Ùk", "त्त"), ("Ù", "त्त्"),
    ("Ø", "क्र"), ("Ñ", "कृ"), ("ô", "क्क"), ("ê", "ट्ट"), ("ë", "ट्ठ"),
    ("ì", "ड्ड"), ("ï", "ड्ढ"), ("í", "द्द"), ("æ", "द्र"), ("ç", "प्र"),
    ("Á", "प्र"), ("Ý", "फ्र"), ("à", "ह्न"), ("á", "ह्य"), ("â", "हृ"),
    ("ã", "ह्म"), ("è", "ध"), ("Ä", "घ"), ("Nî", "छ्य"), ("Vª", "ट्र"),
    ("Mª", "ड्र"), ("<ª", "ढ्र"), ("Nª", "छ्र"), ("xz", "ग्र"),
    ("#", "रु"), (":", "रू"), ("}", "द्व"), ("K", "ज्ञ"), ("J", "श्र"),
    ("¯", "ऊ"), ("Å", "ऊ"), ("M+", "ड़"), ("<+", "ढ़"), ("+", "़"),
]

_KEYS = {
    # vowels
    "v": "अ", "b": "इ", "m": "उ", ",": "ए",
    # consonants, full
    "d": "क", "x": "ग", "p": "च", "N": "छ", "t": "ज", ">": "झ",
    "V": "ट", "B": "ठ", "M": "ड", "<": "ढ", "r": "त", "n": "द",
    "u": "न", "i": "प", "Q": "फ", "c": "ब", "e": "म", ";": "य",
    "j": "र", "y": "ल", "o": "व", "l": "स", "g": "ह", "G": "ळ",
    "=": "त्र",
    # consonants that only appear as half forms; "k" after them makes them full
    "[": "ख्", "?": "घ्", "'": "श्", '"': "ष्", "/": "ध्", ".": "ण्",
    "F": "थ्", "H": "भ्", "{": "क्ष्",
    "D": "क्", "X": "ग्", "P": "च्", "T": "ज्", "R": "त्", "U": "न्",
    "I": "प्", "C": "ब्", "E": "म्", "Y": "ल्", "O": "व्", "L": "स्",
    # vowel signs and marks
    "k": "ा", "h": "ी", "q": "ु", "w": "ू", "`": "ृ", "s": "े", "S": "ै",
    "a": "ं", "¡": "ँ", "W": "ॅ", "z": "्र", "~": "्",
    # punctuation
    "A": "।", "%": ":", "&": "-", "]": ",", "*": "?", "¼": "(", "½": ")",
    "f": "ि", "Z": "\x00",          # placeholders, moved into place below
}

_CONSONANT = "कखगघङचछजझञटठडढणतथदधनपफबभमयरलवशषसहळ"
_SIGNS = "ािीुूृेैोौंँॅ़"


def _place_short_i(text: str) -> str:
    """ि is typed before its consonant; it belongs after the whole cluster."""
    out = list(text)
    i = 0
    while i < len(out):
        if out[i] == "ि" and i + 1 < len(out) and out[i + 1] in _CONSONANT:
            j = i + 1
            # carry it past the consonant and any half-consonants joined to it
            while j + 2 < len(out) and out[j + 1] == "्" and out[j + 2] in _CONSONANT:
                j += 2
            if j + 1 < len(out) and out[j + 1] == "़":
                j += 1
            # popping the sign first shifts the cluster one place left, so it
            # now ends at j - 1 and the sign goes in at j
            out.insert(j, out.pop(i))
            i = j + 1
        else:
            i += 1
    return "".join(out)


def _place_reph(text: str) -> str:
    """The half-r on top ("Z") is typed after its syllable; it belongs before."""
    out = list(text)
    i = 0
    while i < len(out):
        if out[i] != "\x00":
            i += 1
            continue
        out.pop(i)
        j = i - 1
        while j >= 0 and out[j] in _SIGNS:
            j -= 1                                   # skip the syllable's vowel signs
        while j >= 2 and out[j - 1] == "्" and out[j - 2] in _CONSONANT:
            j -= 2                                   # and any half-consonants before it
        if j >= 0 and out[j] in _CONSONANT:
            out[j:j] = ["र", "्"]
            i += 2
    return "".join(out)


def to_unicode(text: str) -> str:
    # PDFs hand back typographic quotes where the keys were plain ones.
    text = text.replace("’", "'").replace("‘", "'").replace("”", '"').replace("“", '"')
    for old, new in _SEQUENCES:
        text = text.replace(old, new)
    text = "".join(_KEYS.get(ch, ch) for ch in text)
    # A half consonant followed by the ा key is the full consonant ("[k" = ख).
    text = text.replace("ाे", "ो").replace("ाै", "ौ")
    text = text.replace("्ा", "").replace("्ो", "ो").replace("्ौ", "ौ")
    text = _place_short_i(text)
    text = _place_reph(text)
    text = text.replace("अा", "आ").replace("अो", "ओ").replace("अौ", "औ")
    return text


# Words that are extremely common in Hindi and look like nothing in English
# once typed in Kruti Dev: के में की है और से का को पर लिए.
_TELLTALES = re.compile(r"(?<![A-Za-z])(ds|esa|dh|gS|vkSj|ls|dk|dks|ij|fy,|fd|;g)(?![A-Za-z])")


def looks_like_krutidev(text: str) -> bool:
    """True when text is Kruti Dev rather than English or real Hindi."""
    if not text or len(re.findall(r"[ऀ-ॿ]", text)) > len(text) * 0.05:
        return False
    return len(_TELLTALES.findall(text)) >= 5
