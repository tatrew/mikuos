"""Prepare reply text for a Russian TTS voice: drop markup/emoji/links, spell numbers,
roughly transliterate Latin words (Silero ru reads Cyrillic only), split into chunks."""
from __future__ import annotations

import re

_URL = re.compile(r"https?://\S+|www\.\S+")
_MARKUP = re.compile(r"[*_`#>|~\[\]{}<>]")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")
_SPACES = re.compile(r"\s+")
_SENTENCE = re.compile(r"(?<=[.!?…])\s+")

_ONES = ["ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять", "десять",
         "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать", "шестнадцать",
         "семнадцать", "восемнадцать", "девятнадцать"]
_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят",
         "девяносто"]
_HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот",
             "девятьсот"]


def _below_thousand(n: int, feminine: bool = False) -> list[str]:
    words = []
    hundreds, n = divmod(n, 100)
    if hundreds:
        words.append(_HUNDREDS[hundreds])
    if n >= 20:
        words.append(_TENS[n // 10])
        n %= 10
    if n:
        words.append(("одна", "две")[n - 1] if feminine and n in (1, 2) else _ONES[n])
    elif not words:
        words.append(_ONES[0])
    return words


def spell_number(n: int) -> str:
    """Integer 0..999 999 999 in Russian words (nominative)."""
    if n < 0:
        return "минус " + spell_number(-n)
    if n >= 1_000_000_000:
        return " ".join(_ONES[int(d)] for d in str(n))
    words = []
    for value, forms, feminine in ((1_000_000, ("миллион", "миллиона", "миллионов"), False),
                                   (1000, ("тысяча", "тысячи", "тысяч"), True)):
        if n >= value:
            k, n = divmod(n, value)
            words += _below_thousand(k, feminine)
            last2, last = k % 100, k % 10
            words.append(forms[0] if last == 1 and last2 != 11 else
                         forms[1] if 2 <= last <= 4 and not 12 <= last2 <= 14 else forms[2])
    if n or not words:
        words += _below_thousand(n)
    return " ".join(words)


# rough letter-by-letter reading of Latin words (better than silence)
_LAT = [("sh", "ш"), ("ch", "ч"), ("th", "т"), ("ph", "ф"), ("oo", "у"), ("ee", "и"), ("ck", "к"),
        ("qu", "кв"), ("x", "кс"), ("a", "а"), ("b", "б"), ("c", "к"), ("d", "д"), ("e", "е"), ("f", "ф"),
        ("g", "г"), ("h", "х"), ("i", "и"), ("j", "дж"), ("k", "к"), ("l", "л"), ("m", "м"), ("n", "н"),
        ("o", "о"), ("p", "п"), ("q", "к"), ("r", "р"), ("s", "с"), ("t", "т"), ("u", "у"), ("v", "в"),
        ("w", "в"), ("y", "й"), ("z", "з")]
_KNOWN = {"miku": "мику", "hatsune": "хацунэ", "spotify": "спотифай", "steam": "стим", "youtube": "ютуб",
          "google": "гугл", "windows": "виндоус", "lm": "эл эм", "studio": "студио", "ok": "окей",
          "world": "ворлд", "is": "из", "mine": "майн", "the": "зе", "melt": "мелт", "supercell": "суперселл"}


def _translit(word: str) -> str:
    low = word.lower()
    if low in _KNOWN:
        return _KNOWN[low]
    out, i = [], 0
    while i < len(low):
        for lat, cyr in _LAT:
            if low.startswith(lat, i):
                out.append(cyr)
                i += len(lat)
                break
        else:
            i += 1
    return "".join(out)


def prepare(text: str, max_chunk: int = 700) -> list[str]:
    text = _URL.sub(" ссылка ", text or "")
    text = _EMOJI.sub(" ", text)
    text = _MARKUP.sub(" ", text)
    text = re.sub(r"(\d)\s*[xх×]\s*(\d)", r"\1 на \2", text)  # 2560x1392 -> "... на ..."
    text = re.sub(r"\d+", lambda m: " " + spell_number(int(m.group())) + " ", text)
    text = re.sub(r"[A-Za-z]+", lambda m: _translit(m.group()), text)
    text = _SPACES.sub(" ", text).strip()
    chunks, cur = [], ""
    for sentence in _SENTENCE.split(text):
        while len(sentence) > max_chunk:  # very long sentence: cut at a comma or space
            cut = max(sentence.rfind(",", 0, max_chunk), sentence.rfind(" ", 0, max_chunk), 1)
            chunks.append(sentence[:cut + 1].strip())
            sentence = sentence[cut + 1:]
        if len(cur) + len(sentence) + 1 > max_chunk and cur:
            chunks.append(cur)
            cur = ""
        cur = f"{cur} {sentence}".strip()
    if cur:
        chunks.append(cur)
    return [c for c in chunks if re.search(r"[а-яёА-ЯЁ]", c)]
