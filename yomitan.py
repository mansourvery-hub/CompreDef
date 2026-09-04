"""
yomitan.py - Minimal Yomitan API fallback for CompreDef.

Uses Yomitan's native-messaging HTTP bridge (yomitan-api) via POST /ankiFields
to fetch beautiful Yomitan HTML directly when CompreDef has no local dictionary
result. This is a fail-safe: if the local ladder yields nothing, we ask Yomitan
instead of returning None.

No second index is built — Yomitan owns the dictionaries. CompreDef stays fast
by caching per-word results and short-circuiting when Yomitan is unavailable.

Performance contract (user explicitly asked to think carefully):
- Runs only in background threads (mw.taskman.run_in_background already).
- Short timeout (2.5s) so a single missing Yomitan does not stall bulk jobs.
- Negative availability cache (30s) so 100 bulk notes don't each wait 2.5s when
  the browser is closed — after the first ECONNREFUSED we skip the rest.
- Per-word cache (5 min) so repeated lookups for the same term are instant.
- Lazy health check: no separate /serverVersion ping on every generate; the
  ankiFields call itself is the probe. Availability is cached from its result.
"""

import json
import time
import threading
import urllib.request
import urllib.error
from typing import List, Dict, Tuple, Optional

# Dual-context sibling imports (see core.py for why)
if __package__:
    from .models import DictionaryEntry
else:
    from models import DictionaryEntry

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
YOMITAN_URL = "http://127.0.0.1:19633"
YOMITAN_TIMEOUT = 2.5  # seconds per request — must stay short
YOMITAN_MAX_ENTRIES = 8  # enough to let ladder scoring see a good candidate
_WORD_CACHE_TTL = 300.0  # seconds
_NEGATIVE_CACHE_TTL = 30.0  # seconds after a failure, skip Yomitan quickly


def _get_configured_url() -> str:
    """Reads yomitan_url from add-on config, falls back to default."""
    try:
        from aqt import mw  # type: ignore
        if mw and hasattr(mw, "addonManager"):
            try:
                name = mw.addonManager.addonFromModule(__name__)
            except Exception:
                name = None
            if not name:
                name = "1619602654"
            cfg = mw.addonManager.getConfig(name)
            if isinstance(cfg, dict) and cfg.get("yomitan_url"):
                url = str(cfg["yomitan_url"]).strip()
                if url:
                    return url.rstrip("/")
    except Exception:
        pass
    return YOMITAN_URL

# ---------------------------------------------------------------------------
# Caches (process-wide, thread-safe)
# ---------------------------------------------------------------------------
_word_cache: Dict[Tuple[str, str, int, str], Tuple[float, List[DictionaryEntry]]] = {}
_word_lock = threading.Lock()

# availability: None = unknown, True = last probe succeeded, False = last probe failed
_availability: Dict[str, float | bool | None] = {"available": None, "checked_at": 0.0}
_avail_lock = threading.Lock()
_last_error: Optional[str] = None
_last_error_lock = threading.Lock()


def _now() -> float:
    return time.monotonic()


def _is_yomitan_recently_unavailable() -> bool:
    """Returns True if we recently proved Yomitan is down — skip fast."""
    with _avail_lock:
        avail = _availability["available"]
        checked_at = float(_availability["checked_at"] or 0.0)
        if avail is False and (_now() - checked_at) < _NEGATIVE_CACHE_TTL:
            return True
    return False


def _mark_availability(success: bool) -> None:
    with _avail_lock:
        _availability["available"] = success
        _availability["checked_at"] = _now()


def _set_last_error(msg: Optional[str]) -> None:
    global _last_error
    with _last_error_lock:
        _last_error = msg


def get_last_yomitan_error() -> Optional[str]:
    with _last_error_lock:
        return _last_error


def _post_json(path: str, payload: dict, timeout: float, base_url: Optional[str] = None) -> Optional[dict]:
    """POSTs JSON to Yomitan bridge and returns parsed response, or None on failure.

    Must never raise — Anki must never crash because Yomitan is closed.
    """
    url = (base_url or _get_configured_url()).rstrip("/") + path
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            _mark_availability(True)
            _set_last_error(None)
            try:
                return json.loads(body) if body else {}
            except Exception as je:
                _set_last_error(f"Yomitan returned invalid JSON on {path}: {je}")
                print(f"CompreDef Yomitan: invalid JSON {path}: {je}")
                return {}
    except urllib.error.HTTPError as e:
        # HTTPError is a subclass of URLError but contains a valid response body.
        # For 502 (Yomitan not connected) we want to surface the JSON error, not treat as network failure.
        try:
            body = e.read().decode("utf-8") if hasattr(e, "read") else ""
            data = json.loads(body) if body else {}
            # 502 from bridge means Yomitan not connected — don't mark as network unavailable for Test
            if e.code == 502:
                msg = data.get("error") if isinstance(data, dict) else str(data)
                if not msg:
                    msg = f"Yomitan not connected (HTTP 502) at {url}{path}"
                _set_last_error(msg)
                print(f"CompreDef Yomitan: {msg}")
                # Return the data so caller can see the error, but also cache as empty for ankiFields
                return data
            # Other HTTP errors are treated as network failure
            _mark_availability(False)
            reason = getattr(e, "reason", str(e))
            msg = f"Yomitan HTTP {e.code} at {url}{path}: {reason}"
            _set_last_error(msg)
            print(f"CompreDef Yomitan: {msg} ({e})")
            return None
        except Exception as je:
            _mark_availability(False)
            _set_last_error(f"Yomitan HTTP {e.code} read failed: {je}")
            return None
    except urllib.error.URLError as e:
        _mark_availability(False)
        reason = getattr(e, "reason", str(e))
        msg = f"Yomitan not reachable at {url}{path}: {reason}. Is browser open + Yomitan API enabled + bridge installed? Click 'Install / Repair Bridge' then restart browser."
        _set_last_error(msg)
        print(f"CompreDef Yomitan: {msg} ({e})")
        return None
    except Exception as e:
        # Any other error (timeout, etc.) counts as unavailable for negative cache,
        # but we distinguish timeout as transient — still mark unavailable to avoid
        # hammering the browser in bulk jobs.
        _mark_availability(False)
        msg = f"Yomitan error {url}{path}: {e}"
        _set_last_error(msg)
        print(f"CompreDef Yomitan: {msg}")
        return None


def _normalize_reading(reading: str) -> str:
    """Yomitan reading normalization — katakana->hiragana + strip decorations."""
    # Inline minimal normalize to avoid circular import; mirrors utils.normalize_reading
    if not reading:
        return ""
    out = []
    for ch in reading:
        code = ord(ch)
        if 0x30A1 <= code <= 0x30F6 or 0x30FD <= code <= 0x30FC:
            out.append(chr(code - 0x60))
        else:
            out.append(ch)
    import re
    return re.sub(r"[\s\-・.。_ー()()「」【】]", "", "".join(out))


def fetch_yomitan_definitions(
    word: str,
    reading: str = "",
    max_entries: int = YOMITAN_MAX_ENTRIES,
    timeout: float = YOMITAN_TIMEOUT,
    base_url: Optional[str] = None,
) -> List[DictionaryEntry]:
    """Fetches definitions from Yomitan via POST /ankiFields.

    Returns a list of DictionaryEntry (definition = Yomitan's beautiful HTML).
    Returns [] on any failure or when Yomitan has no entry — never raises.

    Caching:
    - Per-word (word, reading, max_entries) cached 5 min.
    - Negative availability (30s) skips network entirely when browser is closed.
    """
    if not word or not word.strip():
        return []

    word = word.strip()
    reading_norm = _normalize_reading(reading) if reading else ""
    effective_url = base_url or _get_configured_url()

    cache_key = (word, reading_norm, max_entries, effective_url)

    # Fast-path: per-word cache hit
    with _word_lock:
        hit = _word_cache.get(cache_key)
        if hit:
            ts, entries = hit
            if (_now() - ts) < _WORD_CACHE_TTL:
                return entries
            else:
                # Expired — evict
                _word_cache.pop(cache_key, None)

    # Fast-path: recently unavailable — don't even try network
    if _is_yomitan_recently_unavailable():
        return []

    # Build ankiFields request — this is Yomitan's rendered HTML path
    # so CompreDef gets the beautiful glossary without re-implementing rendering.
    # We request reading+expression markers to allow reading disambiguation.
    payload = {
        "text": word,
        "type": "term",
        "markers": ["expression", "reading", "glossary"],
        "maxEntries": max_entries,
        "includeMedia": False,
    }

    data = _post_json("/ankiFields", payload, timeout=timeout, base_url=effective_url)
    if data is None:
        # Network / Yomitan not running — cache empty result briefly to avoid
        # hammering in bulk (word cache with empty list)
        with _word_lock:
            _word_cache[cache_key] = (_now(), [])
        return []

    # Response: {"fields": [{"expression": "...", "reading": "...", "glossary": "<div>..."}]}
    fields = data.get("fields") if isinstance(data, dict) else None
    # Fallback for single kanji like "口": Yomitan may store it as kanji entry, not term.
    # If term search returned nothing and the query is a single kanji, retry as kanji.
    if (not isinstance(fields, list) or not fields) and len(word) == 1:
        import re
        if re.match(r"[\u4e00-\u9fff]", word):
            k_payload = {
                "text": word,
                "type": "kanji",
                "markers": ["character", "glossary"],
                "maxEntries": max_entries,
                "includeMedia": False,
            }
            k_data = _post_json("/ankiFields", k_payload, timeout=timeout, base_url=effective_url)
            if k_data is not None:
                k_fields = k_data.get("fields") if isinstance(k_data, dict) else None
                if isinstance(k_fields, list) and k_fields:
                    fields = k_fields
                # Also try /kanjiEntries as last resort if ankiFields kanji still empty
                if not isinstance(fields, list) or not fields:
                    k2 = _post_json("/kanjiEntries", {"character": word}, timeout=timeout, base_url=effective_url)
                    if isinstance(k2, dict) and k2.get("character") == word:
                        # Convert kanjiEntries format to a fake ankiFields glossary
                        defs = k2.get("definitions") or k2.get("defintions") or []
                        if isinstance(defs, list) and defs:
                            glos = "<div class=\"yomitan-glossary\">" + "<br>".join(str(d) for d in defs) + "</div>"
                            fields = [{"character": word, "glossary": glos}]
    if not isinstance(fields, list) or not fields:
        with _word_lock:
            _word_cache[cache_key] = (_now(), [])
        return []

    result: List[DictionaryEntry] = []
    for field in fields:
        if not isinstance(field, dict):
            continue
        glossary = field.get("glossary")
        if not glossary or not isinstance(glossary, str):
            continue
        glossary = glossary.strip()
        if not glossary:
            continue

        # Reading disambiguation: if caller supplied reading, skip entries
        # whose Yomitan reading does not match (handles 先ず まず vs せんず).
        if reading_norm:
            yomitan_reading = field.get("reading") or field.get("reading-plain") or ""
            # Some Yomitan templates may not include reading marker — if missing,
            # don't filter aggressively.
            if yomitan_reading:
                if _normalize_reading(str(yomitan_reading)) != reading_norm:
                    continue

        # Use expression as term if available, else the queried word
        expr = field.get("expression")
        term = str(expr).strip() if expr and isinstance(expr, str) and expr.strip() else word

        # Yomitan glossary is already beautiful HTML — store as-is
        result.append(DictionaryEntry(
            word=term,
            reading=reading,
            definition=glossary,
            dictionary_title="Yomitan",
            dictionary_path="yomitan://api",
        ))

    # Cache result (even if empty)
    with _word_lock:
        _word_cache[cache_key] = (_now(), result)

    return result


def clear_yomitan_cache() -> None:
    """Clears all Yomitan caches — useful for tests or manual refresh."""
    with _word_lock:
        _word_cache.clear()
    with _avail_lock:
        _availability["available"] = None
        _availability["checked_at"] = 0.0


# ---------------------------------------------------------------------------
# Provider wrapper (optional — for future explicit Yomitan mode)
# ---------------------------------------------------------------------------
# For now the engine fallbacks via fetch_yomitan_definitions() directly.
# This provider is kept for potential future GUI toggle ("Local" vs "Yomitan" vs "Auto").
try:
    import abc
    if __package__:
        from .provider import DictionaryProvider
    else:
        from provider import DictionaryProvider

    class YomitanApiProvider(DictionaryProvider):
        """DictionaryProvider that talks to Yomitan's HTTP bridge.

        All lifecycle methods are no-ops (no index). Only lookup does work.
        Kept minimal for now — the fail-safe path in engine.py uses the
        free function above; this class is for a future explicit toggle.
        """

        def __init__(self, base_url: str = YOMITAN_URL, timeout: float = YOMITAN_TIMEOUT, max_entries: int = YOMITAN_MAX_ENTRIES):
            self.base_url = base_url
            self.timeout = timeout
            self.max_entries = max_entries

        def lookup(self, word: str, reading: str = "") -> List[DictionaryEntry]:
            return fetch_yomitan_definitions(word, reading, max_entries=self.max_entries, timeout=self.timeout, base_url=self.base_url)

        def lookup_by_path(self, path: str, word: str, reading: str = "") -> List[DictionaryEntry]:
            # path is ignored — Yomitan owns all dictionaries
            return self.lookup(word, reading)

        def get_title(self, path: str) -> str:
            return "Yomitan"

        def is_installed(self, path: str) -> bool:
            # If Yomitan responds, treat as installed
            # Quick check: if recently unavailable, report not installed to grey out UI
            if _is_yomitan_recently_unavailable():
                return False
            # Probe with short timeout
            data = _post_json("/serverVersion", {}, timeout=1.0, base_url=self.base_url)
            return data is not None

        def get_entry_count(self, path: str) -> int:
            return 0

        def install(self, path: str, progress_cb=None, cancel_check=None, plain_text=None) -> int:
            return 0

        def uninstall(self, path: str) -> None:
            return None

        def is_index_current(self, path: str) -> bool:
            return True

except Exception:
    # In headless test stub where DictionaryProvider may not import, ignore
    YomitanApiProvider = None  # type: ignore
