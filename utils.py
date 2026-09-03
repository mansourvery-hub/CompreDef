import re
import html
import os
import zipfile
from typing import List, Optional

_RT_RE = re.compile(r'<rt\b[^>]*>.*?</rt>', flags=re.DOTALL | re.IGNORECASE)
_RP_RE = re.compile(r'<rp\b[^>]*>.*?</rp>', flags=re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r'<[^>]+>')

def extract_base_text(html_or_text: str) -> str:
    """Extracts visible base text from HTML, stripping ruby furigana (<rt> tags)."""
    if not html_or_text:
        return ""
    no_rt = _RT_RE.sub("", html_or_text)
    no_rp = _RP_RE.sub("", no_rt)
    plain = _TAG_RE.sub("", no_rp)
    return html.unescape(plain).strip()

def extract_clean_word(field_text: str) -> str:
    """Extracts the clean target word/expression from a note field."""
    if not field_text:
        return ""

    text = field_text.strip()
    if not text:
        return ""

    text = _RT_RE.sub("", text)
    text = _RP_RE.sub("", text)
    text = _TAG_RE.sub("", text).strip()
    text = html.unescape(text).strip()

    if "[" in text or "［" in text:
        s = text.replace("［", "[").replace("］", "]")
        whole = re.fullmatch(r"([^\[\]]+)\[([^\[\]]+)\]", s)
        if whole:
            text = whole.group(1).strip()
        else:
            text = re.sub(r"\[[^\]]*\]", "", s).strip()

    return text

def parse_furigana_field(field_text: str) -> str:
    """Extracts the pure kana reading from a note field."""
    if not field_text:
        return ""
    text = field_text.strip()

    if "<ruby" in text or "<rt" in text:
        def _ruby_sub(match: re.Match) -> str:
            inner = match.group(0)
            rt = re.search(r"<rt\b[^>]*>(.*?)</rt>", inner, flags=re.DOTALL)
            return re.sub(r"<[^>]+>", "", rt.group(1)) if rt else ""
        kana = re.sub(r"<ruby\b[^>]*>.*?</ruby>", _ruby_sub, text, flags=re.DOTALL)
        kana = re.sub(r"<[^>]+>", "", kana)
        return normalize_reading(kana)

    if "[" in text or "［" in text:
        s = text.replace("［", "[").replace("］", "]")
        whole = re.fullmatch(r"([^\[\]]+)\[([^\[\]]+)\]", s)
        if whole:
            return normalize_reading(whole.group(2))
        result: list = []
        tokens = re.split(r"(\[[^\]]*\])", s)
        for part in tokens:
            if part.startswith("[") and part.endswith("]"):
                if result:
                    prev = result[-1]
                    trimmed = re.sub(r"[\u4e00-\u9fff]+$", "", prev)
                    result[-1] = trimmed
                result.append(part[1:-1])
            else:
                result.append(part)
        return normalize_reading("".join(result))

    if re.fullmatch(r"[\u3040-\u30ff\u30fc\s\-・]+", text):
        return normalize_reading(text)
    if not re.search(r"[\u3040-\u30ff]", text):
        return ""
    if re.search(r"[\u4e00-\u9fff]", text):
        return ""
    return normalize_reading(text)

def normalize_reading(reading: str) -> str:
    if not reading: return ""
    out = []
    for ch in reading:
        code = ord(ch)
        if 0x30A1 <= code <= 0x30F6 or 0x30FD <= code <= 0x30FC:
            out.append(chr(code - 0x60))
        else:
            out.append(ch)
    return re.sub(r"[\s\-・.。_ー()()「」【】]", "", "".join(out))

def resolve_ladder_paths(
    dictionaries: Optional[List[str]],
    dictionary_folder: str,
    disabled_dictionaries: Optional[List[str]],
) -> List[str]:
    """Resolves the ordered ladder of dictionary paths from user config."""
    ladder_paths: List[str] = []

    if dictionaries and isinstance(dictionaries, list):
        ladder_paths = [str(p).strip() for p in dictionaries if p and str(p).strip()]

    if not ladder_paths and dictionary_folder:
        ladder_paths = find_dictionary_folders(dictionary_folder)

    if disabled_dictionaries and ladder_paths:
        disabled = {os.path.realpath(os.path.expanduser(str(p))) for p in disabled_dictionaries}
        ladder_paths = [
            p for p in ladder_paths
            if os.path.realpath(os.path.expanduser(p)) not in disabled
        ]

    return ladder_paths

def is_zip_dictionary(path: str) -> bool:
    """Checks if path points to a valid Yomitan dictionary zip archive."""
    if not (path.endswith(".zip") and os.path.isfile(path)):
        return False
    try:
        with zipfile.ZipFile(path, "r") as z:
            names = z.namelist()
            return "index.json" in names or any("term_bank" in n and n.endswith(".json") for n in names)
    except Exception:
        return False

def is_directory_dictionary(path: str) -> bool:
    """Checks if path points to an unzipped Yomitan dictionary directory."""
    if not os.path.isdir(path):
        return False
    try:
        names = os.listdir(path)
        return "index.json" in names or any(n.startswith("term_bank") and n.endswith(".json") for n in names)
    except Exception:
        return False

def find_dictionary_folders(parent_or_dict_path: str) -> List[str]:
    """Discovers all dictionary archives (.zip) and unzipped folders in a path."""
    if not parent_or_dict_path:
        return []

    norm = os.path.realpath(os.path.expanduser(parent_or_dict_path))

    if is_zip_dictionary(norm) or is_directory_dictionary(norm):
        return [norm]

    if not os.path.isdir(norm):
        return []

    found: List[str] = []
    seen_titles: set = set()

    def get_simple_title(p):
        return os.path.basename(p.rstrip("/\\"))

    for entry in sorted(os.listdir(norm)):
        sub = os.path.join(norm, entry)
        if is_directory_dictionary(sub):
            title = get_simple_title(sub)
            if title not in seen_titles:
                found.append(sub)
                seen_titles.add(title)

    for entry in sorted(os.listdir(norm)):
        sub = os.path.join(norm, entry)
        if is_zip_dictionary(sub):
            title = get_simple_title(sub)
            if title not in seen_titles:
                found.append(sub)
                seen_titles.add(title)

    return found
