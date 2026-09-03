import os
from typing import List, Optional, Set
from provider import DictionaryProvider
from scoring import calculate_kanji_score, is_reference_title
from utils import extract_clean_word
from models import DictionaryEntry

class DefinitionGenerator:
    """
    Orchestrates the definition generation process.
    Separates the 'Ladder' algorithm from the 'Provider' (data access).
    """
    def __init__(self, provider: DictionaryProvider, known_kanji: Set[str]):
        self.provider = provider
        self.known_kanji = known_kanji

    def generate(
        self,
        target_word: str,
        ladder_paths: List[str],
        reading: str = ""
    ) -> Optional[str]:
        """
        Core Dictionary Ladder algorithm:
        1. Walk dictionaries in order.
        2. Early Exit: if a definition is 100% known, return it immediately.
        3. Fallback: return the highest scoring definition across all dictionaries.
        """
        word = extract_clean_word(target_word)
        if not word:
            return None

        best_definition: Optional[str] = None
        best_score: float = -1.0

        for path in ladder_paths:
            if hasattr(self.provider, 'lookup_by_path'):
                entries = self.provider.lookup_by_path(path, word, reading)
            else:
                entries = self.provider.lookup(word, reading)

            if not entries:
                continue

            non_ref = [e for e in entries if not is_reference_title(e.definition)]
            valid = non_ref if non_ref else (entries if len(entries) == 1 else [])
            
            if not valid:
                continue

            for entry in valid:
                res = calculate_kanji_score(entry.definition, self.known_kanji)
                
                if res.is_perfect:
                    return res.definition
                
                if res.score > best_score:
                    best_score = res.score
                    best_definition = res.definition

        return best_definition
