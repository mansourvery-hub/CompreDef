"""
db_utils.py - Anki database interaction utilities for CompreDef.

Provides safe, read-only access to Anki's database for analyzing known vocabulary
and kanji, following the "Kanji Grid" approach by using native `mw.col.db` 
wrapper instead of direct SQL access.
"""

from aqt import mw
from typing import List, Set


def get_known_kanji_set() -> Set[str]:
    """
    Scans the Anki database for all kanji present on cards with an interval > 0.

    Returns:
        A set of unique kanji characters considered 'known'.
    """
    # Query to fetch all fields from notes for cards that have been learned (interval > 0)
    # Based on the logic from "Kanji Grid" (ID 1610304449)
    query = """
    SELECT DISTINCT flds
    FROM notes
    JOIN cards ON notes.id = cards.nid
    WHERE cards.ivl > 0
    """
    
    rows = mw.col.db.all(query)
    
    known_kanji = set()
    
    # Iterate through fields and extract kanji
    for row in rows:
        field_text = row[0]
        # Extract only Japanese Kanji characters (Unicode range: 4E00–9FFF)
        for char in field_text:
            if '\u4e00' <= char <= '\u9fff':
                known_kanji.add(char)
                
    return known_kanji


def get_known_vocabulary_set() -> Set[str]:
    """
    Scans the Anki database for all vocabulary (words) present on cards 
    with an interval > 0.
    
    Returns:
        A set of unique vocabulary words considered 'known'.
    """
    # For a simple implementation, we can just use the expression fields.
    # A more robust implementation might require parsing field content.
    # This assumes the 'Expression' field is the first field in the note type.
    query = """
    SELECT DISTINCT flds
    FROM notes
    JOIN cards ON notes.id = cards.nid
    WHERE cards.ivl > 0
    """
    
    rows = mw.col.db.all(query)
    
    known_words = set()
    
    for row in rows:
        # Assuming the first field is the expression/word field
        fields = row[0].split('\x1f')
        if fields:
            known_words.add(fields[0].strip())
            
    return known_words
