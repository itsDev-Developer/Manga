def extract_og_image(bs):
    """Most manga sites serve a small thumbnail on their search/listing page
    but set a full-resolution `og:image` (or `twitter:image`) meta tag on
    the manga's own detail page for link-preview purposes. Preferring that
    over the listing thumbnail gets a much higher quality poster for posts,
    without needing a site-specific CSS selector that breaks the moment the
    page's markup changes.

    `bs` is the BeautifulSoup-parsed detail page. Returns the image URL, or
    None if no such tag was found."""
    if not bs:
        return None
    for attrs in (
        {"property": "og:image"}, {"name": "og:image"},
        {"property": "twitter:image"}, {"name": "twitter:image"},
    ):
        tag = bs.find("meta", attrs=attrs)
        url = tag.get("content", "").strip() if tag else ""
        if url:
            return url
    return None


DEAULT_MSG_FORMAT = """
<blockquote><b>{title}</b></blockquote>

**Status**: `{status}`
**Genres**: `{genres}`

**Description**: <blockquote expandable><i>{summary}...</i></blockquote>

**<a href={url}>Read More</a>**
"""

T_MSG_FORMAT = """<blockquote><b>{title}</b></blockquote>

**Status**: `{status}`
**Genres**: `{genres}`
**Language**: `{language}`

**Description**: <blockquote expandable><i>{summary}...</i></blockquote>

**<a href={url}>Read More</a>**
"""
