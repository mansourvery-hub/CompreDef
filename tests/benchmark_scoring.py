import re
import html
import time
from typing import Set

# Current Logic (from generator.py)
_KANJI_RE = re.compile(r'[\u4e00-\u9fff]')
_RT_RE = re.compile(r'<rt\b[^>]*>.*?</rt>', flags=re.DOTALL | re.IGNORECASE)
_RP_RE = re.compile(r'<rp\b[^>]*>.*?</rp>', flags=re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r'<[^>]+>')

def _extract_base_text_slow(html_or_text: str) -> str:
    if not html_or_text: return ""
    no_rt = _RT_RE.sub("", html_or_text)
    no_rp = _RP_RE.sub("", no_rt)
    plain = _TAG_RE.sub("", no_rp)
    return html.unescape(plain).strip()

def score_slow(html_text: str, known_kanji: Set[str]) -> float:
    clean_text = _extract_base_text_slow(html_text)
    kanji_in_text = _KANJI_RE.findall(clean_text)
    if not kanji_in_text: return 1.0
    known_count = sum(1 for char in kanji_in_text if char in known_kanji)
    return known_count / len(kanji_in_text)

def score_fast(precalculated_text: str, known_kanji: Set[str]) -> float:
    kanji_in_text = _KANJI_RE.findall(precalculated_text)
    if not kanji_in_text: return 1.0
    known_count = sum(1 for char in kanji_in_text if char in known_kanji)
    return known_count / len(kanji_in_text)

# Mock Data: A large Yomitan-style definition
sample_html = """
<span class="structured-content">
    <div data-sc-name="definition">
        <ruby>漢<rt>かん</rt></ruby><ruby>字<rt>じ</rt></ruby>は、
        中国で生まれた文字であり、その後、日本や韓国、ベトナムなどで
        導入され、それぞれ独自の発展を遂げた。
        <span class="gloss-sc-span" style="font-size: 0.8em">
            例：<ruby>教育<rt>きょういく</rt></ruby>、<ruby>学習<rt>がくしゅう</rt></ruby>
        </span>
    </div>
    <div data-sc-name="example">
        <ruby>漢字<rt>かんじ</rt></ruby>を練習する。
    </div>
</span>
""" * 5 # Simulate a very long definition
known_kanji = {"漢", "字", "中", "国", "生", "本"}

ITERATIONS = 10000
precalculated = _extract_base_text_slow(sample_html)

print(f"Running benchmark with {ITERATIONS} iterations...")

start = time.perf_counter()
for _ in range(ITERATIONS):
    score_slow(sample_html, known_kanji)
end = time.perf_counter()
slow_time = end - start

start = time.perf_counter()
for _ in range(ITERATIONS):
    score_fast(precalculated, known_kanji)
end = time.perf_counter()
fast_time = end - start

print(f"Slow Scoring: {slow_time:.4f}s")
print(f"Fast Scoring: {fast_time:.4f}s")
print(f"Speedup: {slow_time / fast_time:.2f}x")
