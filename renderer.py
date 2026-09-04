import html
from typing import Any, List, Optional, Dict

def _style_to_css(style: dict) -> str:
    """Converts a Yomitan structured-content style dictionary into an inline CSS string."""
    if not isinstance(style, dict):
        return ""

    css_rules = []
    for prop, val in style.items():
        kebab = "".join(["-" + c.lower() if c.isupper() else c for c in prop]).lstrip("-")

        if isinstance(val, (int, float)) and kebab in (
            "margin-top", "margin-left", "margin-right", "margin-bottom",
            "padding-top", "padding-left", "padding-right", "padding-bottom",
            "width", "height"
        ):
            css_rules.append(f"{kebab}: {val}em")
        elif isinstance(val, list):
            css_rules.append(f"{kebab}: {' '.join(str(x) for x in val)}")
        elif val is not None:
            css_rules.append(f"{kebab}: {val}")

    return "; ".join(css_rules)

def render_structured_content_node(node: Any) -> str:
    """Renders a Yomitan structured-content node to semantic HTML."""
    if isinstance(node, str):
        return html.escape(node)

    if isinstance(node, list):
        return "".join(render_structured_content_node(child) for child in node)

    if isinstance(node, dict):
        tag = node.get("tag", "span")
        classes = [f"gloss-sc-{tag}"]
        attrs = []

        data = node.get("data")
        if isinstance(data, dict):
            for dk, dv in data.items():
                attrs.append(f'data-sc-{html.escape(dk.lower())}="{html.escape(str(dv))}"')

        style = node.get("style")
        if isinstance(style, dict):
            css = _style_to_css(style)
            if css:
                attrs.append(f'style="{html.escape(css)}"')

        lang = node.get("lang")
        if lang:
            attrs.append(f'lang="{html.escape(str(lang))}"')

        title = node.get("title")
        if title:
            attrs.append(f'title="{html.escape(str(title))}"')

        href = node.get("href")
        if href:
            attrs.append(f'href="{html.escape(str(href))}"')

        for cell_attr in ("colSpan", "rowSpan"):
            val = node.get(cell_attr)
            if isinstance(val, int):
                attrs.append(f'{cell_attr.lower()}="{val}"')

        if node.get("open") is True:
            attrs.append("open")

        attrs.insert(0, f'class="{" ".join(classes)}"')
        attr_str = " " + " ".join(attrs) if attrs else ""

        if tag == "br":
            return f"<br{attr_str}>"

        if tag == "img":
            src = node.get("path") or node.get("src", "")
            alt = node.get("title") or node.get("alt", "image")
            img_attrs = ['class="gloss-image"']
            if src:
                img_attrs.append(f'src="{html.escape(str(src))}"')
            if alt:
                img_attrs.append(f'alt="{html.escape(str(alt))}"')
            return f'<img {" ".join(img_attrs)}>'

        content = node.get("content", "")
        inner = render_structured_content_node(content) if content is not None else ""
        return f"<{tag}{attr_str}>{inner}</{tag}>"

    return ""

def _extract_plain_text_node(node: Any) -> str:
    """Recursively extracts visible plain text from a Yomitan structured-content node.

    Unlike render_structured_content_node this does NOT build HTML — it walks
    the tree and concatenates only text, skipping all tags/attributes/styles.
    This is the zero-HTML path used when the user opts for plain-text
    definitions (avoids generating HTML at all).
    """
    if isinstance(node, str):
        return node

    if isinstance(node, list):
        return "".join(_extract_plain_text_node(child) for child in node)

    if isinstance(node, dict):
        # Structured node: recurse into its 'content'. Ignore tag/data/style/
        # lang/title/href/colSpan/img etc. — only visible text matters.
        # For <img> preserve alt/title as plain text (same as Yomitan shows).
        tag = node.get("tag", "")
        # Furigana readings (<rt>) and <rp> are ruby annotations, not base
        # text — skip them so plain output matches scoring's base_text
        # (e.g. 先[rt]ま[/rt]ず -> "先ず", not "先まず"). Same for <rp>.
        if tag in ("rt", "rp"):
            return ""
        if tag == "img":
            alt = node.get("title") or node.get("alt", "")
            return str(alt) if alt else ""
        if tag == "br":
            return "\n"
        content = node.get("content", "")
        if content is not None:
            return _extract_plain_text_node(content)
        return ""

    return ""


def render_yomitan_definition_html(def_block: Any) -> str:
    """Renders a Yomitan definition block into Anki-ready HTML."""
    if isinstance(def_block, str):
        return html.escape(def_block.strip()).replace("\n", "<br>")

    if isinstance(def_block, dict):
        if def_block.get("type") == "text":
            return html.escape(str(def_block.get("text", "")).strip()).replace("\n", "<br>")
        if def_block.get("type") == "structured-content" or "content" in def_block:
            content = def_block.get("content", [])
            rendered = render_structured_content_node(content)
            return f'<span class="structured-content">{rendered}</span>'

    return ""


def render_yomitan_definition_text(def_block: Any) -> str:
    """Renders a Yomitan definition block to plain text (no HTML).

    This is the efficient plain-text path: it extracts visible text directly
    from the structured content without ever building an HTML string. The
    result is stripped but preserves internal newlines from <br> / text blocks.
    """
    if isinstance(def_block, str):
        return def_block.strip()

    if isinstance(def_block, dict):
        if def_block.get("type") == "text":
            return str(def_block.get("text", "")).strip()
        if def_block.get("type") == "structured-content" or "content" in def_block:
            content = def_block.get("content", [])
            return _extract_plain_text_node(content).strip()

    return ""
