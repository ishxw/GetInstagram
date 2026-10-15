#!/usr/bin/env python3
"""Small, dependency-free Instagram public profile reader.

The service intentionally uses the public HTML pages only. Instagram can change
its page format or require a login at any time, so the response exposes partial
results and per-post fetch errors instead of pretending that missing fields were
read successfully.

Run with::

    python server.py --host 127.0.0.1 --port 8787

The browser endpoint is ``GET /api/profile?url=https://www.instagram.com/name/``.
"""

from __future__ import annotations

import argparse
import calendar
import datetime as _datetime
import html
import json
import logging
import mimetypes
import os
import re
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterable, Iterator, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse
from urllib.request import Request, urlopen


LOGGER = logging.getLogger("instagram-reader")

INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com"}
RESERVED_PROFILE_PATHS = {
    "about",
    "accounts",
    "direct",
    "directory",
    "emails",
    "explore",
    "legal",
    "privacy",
    "reels",
    "session",
    "settings",
    "stories",
    "web",
}
USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]{2,100}$")
POST_PATH_RE = re.compile(r"^/(p|reel|tv)/([A-Za-z0-9_-]+)/?$")
POST_URL_PATH_RE = re.compile(
    r"^/(?:(?P<owner>[A-Za-z0-9._]{1,30})/)?"
    r"(?P<kind>p|reel|tv)/(?P<code>[A-Za-z0-9_-]{2,100})/?$"
)
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
PROFILE_TIMEOUT = 15
POST_TIMEOUT = 15
MAX_POSTS = 50
POST_WORKERS = 4

# Instagram serves a more useful anonymous HTML variant to this conservative
# public-page UA than to a fabricated full browser fingerprint.
USER_AGENT = "Mozilla/5.0"


class InstagramReadError(Exception):
    """An expected upstream or input failure that should become JSON."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class _HTMLCapture:
    """Very small HTML parser used to avoid a third-party dependency."""

    def __init__(self) -> None:
        from html.parser import HTMLParser

        class Parser(HTMLParser):
            def __init__(self, outer: "_HTMLCapture") -> None:
                super().__init__(convert_charrefs=False)
                self.outer = outer
                self.in_script = False
                self.script_attrs: dict[str, str] = {}
                self.script_parts: list[str] = []

            def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
                attrs_map = {str(k).lower(): (v or "") for k, v in attrs}
                if tag.lower() == "script":
                    self.in_script = True
                    self.script_attrs = attrs_map
                    self.script_parts = []
                elif tag.lower() == "meta":
                    self.outer.meta.append(attrs_map)
                elif tag.lower() == "link":
                    self.outer.links.append(attrs_map)

            def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
                self.handle_starttag(tag, attrs)
                if tag.lower() == "script":
                    self.handle_endtag(tag)

            def handle_data(self, data: str) -> None:
                if self.in_script:
                    self.script_parts.append(data)

            def handle_entityref(self, name: str) -> None:
                if self.in_script:
                    self.script_parts.append(f"&{name};")

            def handle_charref(self, name: str) -> None:
                if self.in_script:
                    self.script_parts.append(f"&#{name};")

            def handle_comment(self, data: str) -> None:
                if self.in_script:
                    self.script_parts.append(f"<!--{data}-->")

            def handle_endtag(self, tag: str) -> None:
                if tag.lower() == "script" and self.in_script:
                    self.outer.scripts.append((self.script_attrs, "".join(self.script_parts)))
                    self.in_script = False
                    self.script_attrs = {}
                    self.script_parts = []

        self.scripts: list[tuple[dict[str, str], str]] = []
        self.meta: list[dict[str, str]] = []
        self.links: list[dict[str, str]] = []
        self.parser = Parser(self)

    def feed(self, text: str) -> None:
        self.parser.feed(text)
        self.parser.close()


def _json_load(value: str) -> Any | None:
    """Decode a JSON string after removing a BOM and common HTML escaping."""

    value = value.strip().lstrip("\ufeff")
    if not value:
        return None
    # Script text is normally not entity encoded, but this helps with copied
    # fixtures and old pages without changing normal JSON punctuation.
    candidates = (value, html.unescape(value))
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return None


def _balanced_json(text: str, start: int) -> str | None:
    """Return a balanced JSON object/array starting at *start*.

    Instagram embeds JSON in JavaScript assignments. A brace counter alone
    breaks when a caption contains braces, so strings and escapes are tracked.
    """

    if start < 0 or start >= len(text) or text[start] not in "[{":
        return None
    opening = text[start]
    closing = "]" if opening == "[" else "}"
    stack: list[str] = [closing]
    in_string = False
    escaped = False
    for index in range(start + 1, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            stack.append("]" if char == "[" else "}")
        elif char in "]}":
            if not stack or char != stack[-1]:
                return None
            stack.pop()
            if not stack:
                return text[start : index + 1]
    return None


def _extract_script_json(scripts: Sequence[tuple[Mapping[str, str], str]]) -> list[Any]:
    values: list[Any] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        if not isinstance(value, (dict, list)):
            return
        try:
            key = json.dumps(value, sort_keys=True, ensure_ascii=False)[:4000]
        except (TypeError, ValueError):
            key = repr(value)[:4000]
        if key not in seen:
            seen.add(key)
            values.append(value)

    for attrs, source in scripts:
        script_id = attrs.get("id", "").lower()
        script_type = attrs.get("type", "").lower()
        direct = _json_load(source)
        if script_id == "__next_data__" or script_type == "application/ld+json":
            if direct is not None:
                add(direct)
        # Legacy pages assign a complete object to window._sharedData.
        for marker in ("window._sharedData", "window.__sharedData", "__additionalDataLoaded"):
            marker_index = source.find(marker)
            if marker_index < 0:
                continue
            object_start = source.find("{", marker_index)
            if object_start < 0:
                object_start = source.find("[", marker_index)
            raw = _balanced_json(source, object_start)
            decoded = _json_load(raw or "")
            if decoded is not None:
                add(decoded)
        # Some deployments omit __NEXT_DATA__'s id but still put raw JSON in a
        # script tag. Only accept objects that look like Instagram payloads to
        # avoid treating analytics configuration as page data.
        if direct is not None and isinstance(direct, (dict, list)):
            serialised = source.lower()
            if any(token in serialised for token in (
                "shortcode",
                "edge_owner_to_timeline_media",
                "polaris_ordered_timeline_connection",
                "graphql",
                "taken_at_timestamp",
            )):
                add(direct)
    return values


def _walk(value: Any) -> Iterator[Any]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _first(value: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in value and value[key] not in (None, ""):
            return value[key]
    return None


def _as_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        text = _first(value, "text", "content", "value")
        return text if isinstance(text, str) else None
    return None


def _clean_caption_text(value: str) -> str:
    """Remove the engagement prefix Instagram adds to Open Graph text."""

    text = html.unescape(value).strip()
    prefix, separator, remainder = text.partition(': "')
    if separator and "likes" in prefix.lower() and "comments" in prefix.lower():
        text = remainder.strip()
        if text.endswith('".'):
            text = text[:-2]
        elif text.endswith('"'):
            text = text[:-1]
    # Keep Instagram's paragraph breaks, but discard malformed pages that
    # repeat empty lines hundreds of times around the caption.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _post_path_info(url: str) -> tuple[str | None, str, str] | None:
    """Return optional owner, route kind, and shortcode from an Instagram path."""

    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    # Embedded GraphQL objects sometimes expose ``/p/CODE/`` rather than an
    # absolute URL. Treat that as an Instagram path; absolute URLs remain
    # restricted to the allowed hosts.
    if parsed.netloc and parsed.netloc.lower().split(":", 1)[0] not in INSTAGRAM_HOSTS:
        return None
    match = POST_URL_PATH_RE.match(parsed.path or "")
    if not match:
        return None
    return match.group("owner"), match.group("kind"), match.group("code")


def _post_path(url: str) -> tuple[str, str] | None:
    info = _post_path_info(url)
    return (info[1], info[2]) if info else None


def _post_kind(node: Mapping[str, Any], fallback: str = "p") -> str:
    for key in ("permalink", "url", "link", "seo_canonical_url", "canonical_url"):
        value = node.get(key)
        if isinstance(value, str):
            parsed = _post_path_info(value)
            if parsed:
                return parsed[1]
    if bool(_first(node, "is_reel", "is_clips")):
        return "reel"
    product_type = _first(node, "product_type", "productType")
    if isinstance(product_type, str) and product_type.casefold() in {"clips", "reels"}:
        return "reel"
    typename = _first(node, "__typename")
    if isinstance(typename, str) and "reel" in typename.casefold():
        return "reel"
    return fallback if fallback in {"p", "reel", "tv"} else "p"


def _normalise_post_url(
    value: Any,
    shortcode: str | None = None,
    kind: str = "p",
    owner: str | None = None,
) -> str | None:
    if isinstance(value, str):
        value = html.unescape(value).strip()
        parsed = _post_path_info(value if "://" in value else urljoin("https://www.instagram.com", value))
        if parsed:
            owner, kind, code = parsed
            owner_path = f"{owner}/" if owner else ""
            return f"https://www.instagram.com/{owner_path}{kind}/{code}/"
    if shortcode and SHORTCODE_RE.match(shortcode):
        owner_path = f"{owner}/" if isinstance(owner, str) and USERNAME_RE.fullmatch(owner) else ""
        route = kind if kind in {"p", "reel", "tv"} else "p"
        return f"https://www.instagram.com/{owner_path}{route}/{shortcode}/"
    return None


def _shortcode(node: Mapping[str, Any]) -> str | None:
    direct = _first(node, "shortcode", "short_code", "code")
    if isinstance(direct, str) and SHORTCODE_RE.match(direct):
        return direct
    for key in ("permalink", "url", "link", "seo_canonical_url", "canonical_url"):
        path = _post_path(str(node[key])) if isinstance(node.get(key), str) else None
        if path:
            return path[1]
    return None


def _media_urls(node: Mapping[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Return image preview, video URL, and best preview URL."""

    image_url: str | None = None
    video_url: str | None = None
    image_candidates: list[Any] = []
    video_candidates: list[Any] = []

    for key in (
        "display_url",
        "display_uri",
        "displayUrl",
        "thumbnail_src",
        "image_url",
        "thumbnail_url",
        "display_src",
    ):
        image_candidates.append(node.get(key))
    for key in ("image_versions2", "thumbnail_resources", "display_resources", "images"):
        nested = node.get(key)
        if isinstance(nested, Mapping):
            image_candidates.extend(nested.get("candidates", []) if isinstance(nested.get("candidates"), list) else [])
            image_candidates.extend(nested.get("resources", []) if isinstance(nested.get("resources"), list) else [])
            additional = nested.get("additional_candidates")
            if isinstance(additional, Mapping):
                image_candidates.extend(additional.values())
        elif isinstance(nested, list):
            image_candidates.extend(nested)
    for key in ("video_url", "video_src"):
        video_candidates.append(node.get(key))
    for key in ("video_versions", "videos"):
        nested = node.get(key)
        if isinstance(nested, list):
            video_candidates.extend(nested)

    # Newer Polaris responses sometimes nest the media fields several levels
    # below the post node. Walk those descendants as a fallback, and recover
    # a progressive URL from the DASH manifest when ``video_versions`` is
    # omitted.
    for child in _walk(node):
        if child is node or not isinstance(child, Mapping):
            continue
        for key in ("video_url", "video_src", "video_versions", "videos"):
            nested = child.get(key)
            if isinstance(nested, list):
                video_candidates.extend(nested)
            elif nested is not None:
                video_candidates.append(nested)
        manifest = child.get("video_dash_manifest")
        if isinstance(manifest, str):
            video_candidates.extend(re.findall(r"<BaseURL[^>]*>\s*(.*?)\s*</BaseURL>", manifest, re.IGNORECASE | re.DOTALL))

    def url_from(item: Any) -> str | None:
        if isinstance(item, str):
            if item.startswith(("http://", "https://")):
                return html.unescape(item)
            if item.startswith("//"):
                return html.unescape("https:" + item)
            return None
        if isinstance(item, Mapping):
            candidate = _first(item, "url", "src")
            return url_from(candidate)
        return None

    # ``display_uri`` is often a square cover crop. Prefer the candidate with
    # the largest advertised dimensions so portrait images and Reel covers
    # retain their complete frame.
    ranked_images: list[tuple[int, str]] = []
    for item in image_candidates:
        candidate_url = url_from(item)
        if not candidate_url:
            continue
        if isinstance(item, Mapping):
            try:
                score = int(item.get("width") or 0) * int(item.get("height") or 0)
            except (TypeError, ValueError):
                score = 0
        else:
            score = 1
        ranked_images.append((score, candidate_url))
    if ranked_images:
        image_url = max(ranked_images, key=lambda entry: entry[0])[1]
    for item in video_candidates:
        video_url = url_from(item)
        if video_url:
            break
    # Prefer a still preview for videos. Browsers can show that URL in an
    # ``<img>`` element without needing a separate video player.
    return image_url, video_url, image_url or video_url


def _media_items(node: Mapping[str, Any]) -> list[dict[str, str]]:
    """Return every image/video in a carousel, or the single post media."""

    children: list[Mapping[str, Any]] = []
    for key in ("carousel_media", "carouselMedia", "sidecar_media", "media_items"):
        value = node.get(key)
        if isinstance(value, list):
            children.extend(item for item in value if isinstance(item, Mapping))
    sidecar = node.get("edge_sidecar_to_children")
    if isinstance(sidecar, Mapping):
        edges = sidecar.get("edges")
        if isinstance(edges, list):
            for edge in edges:
                if isinstance(edge, Mapping) and isinstance(edge.get("node"), Mapping):
                    children.append(edge["node"])
    # Some Polaris payloads put the child list under a generic ``items`` key.
    if not children and isinstance(node.get("items"), list) and "carousel" in str(node.get("product_type", "")).lower():
        children.extend(item for item in node["items"] if isinstance(item, Mapping))
    if not children:
        children = [node]

    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for child in children:
        image_url, video_url, preview_url = _media_urls(child)
        if not preview_url and not video_url:
            continue
        kind = "video" if video_url else "image"
        identity = (kind, video_url or image_url or preview_url or "")
        if identity in seen:
            continue
        seen.add(identity)
        result.append({
            "type": kind,
            "image_url": image_url or "",
            "video_url": video_url or "",
            "preview_url": preview_url or image_url or "",
        })
    return result


def _caption(node: Mapping[str, Any]) -> str | None:
    edge = node.get("edge_media_to_caption")
    if isinstance(edge, Mapping) and isinstance(edge.get("edges"), list):
        for item in edge["edges"]:
            if isinstance(item, Mapping):
                text = _as_text(item.get("node"))
                if text:
                    return _clean_caption_text(text)
    for key in ("caption", "title", "description"):
        text = _as_text(node.get(key))
        if text:
            return _clean_caption_text(text)
    return None


def _timestamp(node: Mapping[str, Any]) -> Any:
    value = _first(node, "taken_at_timestamp", "taken_at", "timestamp", "created_time", "created_at", "date")
    if isinstance(value, Mapping):
        value = _first(value, "timestamp", "value")
    return value


def _owner_username(node: Mapping[str, Any]) -> str | None:
    for key in ("owner", "user", "author", "from"):
        owner = node.get(key)
        if isinstance(owner, Mapping):
            username = _first(owner, "username", "user_name", "handle")
            if isinstance(username, str):
                return username
        elif isinstance(owner, str):
            return owner
    username = _first(node, "owner_username", "username")
    return username if isinstance(username, str) else None


def _is_pinned(node: Mapping[str, Any]) -> bool | None:
    """Read the optional pinned marker exposed by newer profile payloads."""

    value = _first(node, "is_pinned", "isPinned", "pinned", "is_pin")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return None


def _looks_like_post(node: Mapping[str, Any]) -> bool:
    code = _shortcode(node)
    if not code:
        return False
    media_keys = {
        "display_url",
        "display_uri",
        "thumbnail_src",
        "image_versions2",
        "video_versions",
        "video_url",
        "is_video",
        "media_type",
        "taken_at_timestamp",
        "taken_at",
        "caption",
        "product_type",
        "edge_media_to_caption",
    }
    return bool(media_keys.intersection(node.keys()))


def _iter_post_nodes(payloads: Iterable[Any]) -> Iterator[Mapping[str, Any]]:
    seen: set[tuple[str, str]] = set()
    for payload in payloads:
        for item in _walk(payload):
            if not isinstance(item, Mapping) or not _looks_like_post(item):
                continue
            code = _shortcode(item)
            if not code:
                continue
            key = (code, str(_first(item, "id", "pk") or ""))
            if key in seen:
                continue
            seen.add(key)
            yield item


def _html_post_links(source: str) -> list[tuple[str | None, str, str]]:
    """Read visible post anchors as a fallback and as an ordering hint."""

    links: list[tuple[str | None, str, str]] = []
    seen: set[tuple[str | None, str, str]] = set()
    # Keep the username segment when it is present. The profile timeline can
    # contain collaboration links whose owner differs from the profile, so
    # callers must not discard those links solely by owner.
    patterns = (
        re.compile(
            r"(?:https?://(?:www\\.)?instagram\\.com)?/"
            r"(?:(?P<owner>[A-Za-z0-9._]{1,30})/)?"
            r"(?P<kind>p|reel|tv)/(?P<code>[A-Za-z0-9_-]{2,100})/?"
        ),
        re.compile(
            r"(?:https?:\\/\\/(?:www\\.)?instagram\\\\.com)?\\/"
            r"(?:(?P<owner>[A-Za-z0-9._]{1,30})\\/)?"
            r"(?P<kind>p|reel|tv)\\/(?P<code>[A-Za-z0-9_-]{2,100})\\/?"
        ),
    )
    for pattern in patterns:
        for match in pattern.finditer(source):
            item = (match.group("owner"), match.group("kind"), match.group("code"))
            if item not in seen:
                seen.add(item)
                links.append(item)
    return links


def _meta_values(capture: _HTMLCapture) -> dict[str, str]:
    values: dict[str, str] = {}
    for attrs in capture.meta:
        key = attrs.get("property") or attrs.get("name") or attrs.get("itemprop")
        value = attrs.get("content")
        if key and value:
            values[key.lower()] = html.unescape(value)
    for attrs in capture.links:
        if attrs.get("rel", "").lower() == "canonical" and attrs.get("href"):
            values.setdefault("canonical", html.unescape(attrs["href"]))
    return values


def _meta_timestamp(meta: Mapping[str, str]) -> int | None:
    """Extract the calendar date Instagram puts in OG descriptions."""

    text = meta.get("og:description") or meta.get("description") or meta.get("og:title") or ""
    match = re.search(r"\bon\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})\b", text, re.IGNORECASE)
    if not match:
        return None
    try:
        value = _datetime.datetime.strptime(match.group(1), "%B %d, %Y").replace(tzinfo=_datetime.timezone.utc)
    except ValueError:
        try:
            value = _datetime.datetime.strptime(match.group(1), "%b %d, %Y").replace(tzinfo=_datetime.timezone.utc)
        except ValueError:
            return None
    return calendar.timegm(value.utctimetuple())


def _meta_post_node(meta: Mapping[str, str], shortcode: str | None) -> dict[str, Any] | None:
    """Build a minimal post when Instagram withholds its JSON payload."""

    if not shortcode:
        return None
    canonical = meta.get("og:url") or meta.get("canonical")
    post_url = _normalise_post_url(canonical, shortcode)
    if not post_url:
        return None
    node: dict[str, Any] = {"shortcode": shortcode, "permalink": post_url}
    if meta.get("og:image"):
        node["display_url"] = meta["og:image"]
    if meta.get("og:video"):
        node["video_url"] = meta["og:video"]
    description = meta.get("og:description") or meta.get("description") or meta.get("og:title")
    if description:
        node["caption"] = _clean_caption_text(description)
    timestamp = _meta_timestamp(meta)
    if timestamp is not None:
        node["taken_at_timestamp"] = timestamp
    return node


def _profile_username_from_payload(payloads: Iterable[Any]) -> str | None:
    for payload in payloads:
        for item in _walk(payload):
            if not isinstance(item, Mapping):
                continue
            username = item.get("username")
            if not isinstance(username, str) or not USERNAME_RE.match(username):
                continue
            if any(key in item for key in ("edge_owner_to_timeline_media", "edge_felix_video_timeline", "edge_user_to_photos", "profile_pic_url", "profile_pic_url_hd")):
                return username
    return None


def _profile_info_from_html(source: str, username: str, posts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Extract public profile header fields from the anonymous page payload."""

    def string_field(name: str, start: int = 0, end: int | None = None) -> str | None:
        window = source[start:end]
        match = re.search(r'"' + re.escape(name) + r'"\s*:\s*"((?:\\.|[^"\\])*)"', window)
        if not match:
            return None
        try:
            value = json.loads('"' + match.group(1) + '"')
        except (TypeError, ValueError, json.JSONDecodeError):
            value = match.group(1)
        return value if isinstance(value, str) else None

    bio_index = source.find('"biography"')
    context_start = max(0, bio_index - 5000) if bio_index >= 0 else 0
    context_end = min(len(source), bio_index + 6000) if bio_index >= 0 else len(source)
    profile_pic = string_field("profile_pic_url", context_start, context_end)
    profile_pic_hd = string_field("profile_pic_url_hd", context_start, context_end) or profile_pic
    biography = string_field("biography", context_start, context_end)
    full_name = string_field("full_name", context_start, context_end)
    external_url = None
    bio_links_start = source.find('"bio_links"', context_start, context_end)
    if bio_links_start >= 0:
        external_url = string_field("url", bio_links_start, context_end)

    def number_field(name: str) -> int | None:
        match = re.search(r'"' + re.escape(name) + r'"\s*:\s*(\d+)', source[context_start:context_end])
        return int(match.group(1)) if match else None

    followers = number_field("follower_count")
    following = number_field("following_count")
    media_count = number_field("all_media_count")
    verified_match = re.search(r'"is_verified"\s*:\s*(true|false)', source[context_start:context_end], re.IGNORECASE)
    verified = verified_match.group(1).lower() == "true" if verified_match else None
    return {
        "username": username,
        "full_name": full_name or username,
        "biography": biography or "",
        "avatar": profile_pic_hd or profile_pic or "",
        "is_verified": verified,
        "followers": followers,
        "following": following,
        "media_count": media_count or len(posts),
        "external_url": external_url or "",
    }


def _node_to_post(node: Mapping[str, Any], order: int, fallback_username: str | None = None) -> dict[str, Any] | None:
    shortcode = _shortcode(node)
    if not shortcode:
        return None
    source_url = _first(node, "permalink", "url", "link", "seo_canonical_url", "canonical_url")
    owner_username = _owner_username(node) or fallback_username
    post_url = _normalise_post_url(source_url, shortcode, _post_kind(node), owner_username)
    image_url, video_url, preview_url = _media_urls(node)
    media_items = _media_items(node)
    route_info = _post_path_info(post_url or "")
    route_kind = route_info[1] if route_info else None
    is_video_raw = _first(node, "is_video", "isVideo")
    media_type = _first(node, "media_type", "mediaType", "__typename")
    is_video = (
        bool(is_video_raw)
        or route_kind == "reel"
        or media_type in (2, "2", "GraphVideo", "GraphSidecarVideo")
        or bool(video_url)
    )
    if len(media_items) > 1 or (isinstance(media_type, str) and "carousel" in media_type.lower()) or media_type in (8, "8", "GraphSidecar"):
        media_kind = "carousel"
    elif is_video:
        media_kind = "video"
    elif image_url:
        media_kind = "image"
    else:
        media_kind = "unknown"
    return {
        "order": order,
        "index": order,
        "id": _first(node, "id", "pk"),
        "shortcode": shortcode,
        "url": post_url,
        "permalink": post_url,
        "media_type": media_kind,
        "mediaType": media_kind,
        "is_video": is_video,
        "preview_url": preview_url,
        "mediaUrl": preview_url,
        "image_url": image_url,
        "video_url": video_url,
        "media_items": media_items,
        "mediaItems": media_items,
        "caption": _caption(node),
        "timestamp": _timestamp(node),
        "owner_username": owner_username,
        "is_pinned": _is_pinned(node),
        "detail_fetch": {"ok": False, "status": "not_requested"},
    }


def _merge_post(base: dict[str, Any], node: Mapping[str, Any]) -> None:
    image_url, video_url, preview_url = _media_urls(node)
    media_items = _media_items(node)
    values = {
        "id": _first(node, "id", "pk"),
        "caption": _caption(node),
        "timestamp": _timestamp(node),
        "owner_username": _owner_username(node),
        "is_pinned": _is_pinned(node),
        "image_url": image_url,
        "video_url": video_url,
        "preview_url": preview_url,
    }
    for key, value in values.items():
        if value not in (None, "", []):
            base[key] = value
    if video_url:
        base["is_video"] = True
        base["media_type"] = "video"
    elif base.get("media_type") == "unknown" and image_url:
        base["media_type"] = "image"
    if base.get("preview_url"):
        base["mediaUrl"] = base["preview_url"]
    base["mediaType"] = base.get("media_type", "unknown")
    if len(media_items) > 1 or not base.get("media_items"):
        base["media_items"] = media_items
        base["mediaItems"] = media_items


def _parse_html(source: str, requested_shortcode: str | None = None) -> tuple[list[Mapping[str, Any]], dict[str, str], list[Any]]:
    capture = _HTMLCapture()
    capture.feed(source)
    payloads = _extract_script_json(capture.scripts)
    nodes = list(_iter_post_nodes(payloads))
    meta = _meta_values(capture)
    # An OG description/image page is still useful when its JSON is omitted,
    # and can also fill a missing requested node in an otherwise noisy payload.
    if requested_shortcode and not any(_shortcode(node) == requested_shortcode for node in nodes):
        fallback = _meta_post_node(meta, requested_shortcode)
        if fallback:
            nodes.append(fallback)
    return nodes, meta, payloads


def _fetch_html(url: str, timeout: int) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.8,zh-CN;q=0.6",
            "Cache-Control": "no-cache",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            charset = response.headers.get_content_charset() or "utf-8"
            if len(body) > MAX_RESPONSE_BYTES:
                raise InstagramReadError("upstream_too_large", "Instagram returned a page larger than 8 MB", 502)
            return body.decode(charset, errors="replace")
    except InstagramReadError:
        raise
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise InstagramReadError("login_required", "Instagram refused the unauthenticated request", 502) from exc
        if exc.code == 404:
            raise InstagramReadError("not_found", "Instagram profile or post was not found", 502) from exc
        if exc.code == 429:
            raise InstagramReadError("rate_limited", "Instagram rate-limited the request; try again later", 502) from exc
        raise InstagramReadError("upstream_http_error", f"Instagram returned HTTP {exc.code}", 502) from exc
    except (TimeoutError, URLError) as exc:
        reason = getattr(exc, "reason", None)
        detail = str(reason or exc)
        raise InstagramReadError("upstream_unavailable", f"Could not read Instagram: {detail}", 502) from exc


def validate_profile_url(raw_url: str) -> tuple[str, str]:
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise InstagramReadError("missing_url", "Query parameter 'url' is required")
    value = raw_url.strip()
    try:
        parsed = urlparse(value)
    except ValueError as exc:
        raise InstagramReadError("invalid_url", "The URL could not be parsed") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() not in ("http", "https") or host not in INSTAGRAM_HOSTS:
        raise InstagramReadError("invalid_url", "Only an Instagram profile URL is accepted")
    if parsed.query or parsed.fragment:
        raise InstagramReadError("invalid_url", "The profile URL must not contain a query or fragment")
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) != 1 or not USERNAME_RE.fullmatch(parts[0]) or parts[0].casefold() in RESERVED_PROFILE_PATHS:
        raise InstagramReadError("invalid_profile_url", "Use a URL like https://www.instagram.com/username/")
    username = parts[0]
    return f"https://www.instagram.com/{quote(username, safe='._')}/", username


def validate_target_url(raw_url: str) -> tuple[str, str, str | None]:
    """Validate either a profile URL or a public post/reel URL.

    Returns ``(kind, canonical_url, username)``. The username is optional for
    bare Instagram routes such as ``/reel/CODE/`` and is filled from the post
    payload when Instagram exposes it.
    """

    if not isinstance(raw_url, str) or not raw_url.strip():
        raise InstagramReadError("missing_url", "Query parameter 'url' is required")
    value = raw_url.strip()
    try:
        parsed = urlparse(value)
    except ValueError as exc:
        raise InstagramReadError("invalid_url", "The URL could not be parsed") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() not in ("http", "https") or host not in INSTAGRAM_HOSTS:
        raise InstagramReadError("invalid_url", "Only an Instagram URL is accepted")
    if parsed.query or parsed.fragment:
        raise InstagramReadError("invalid_url", "The Instagram URL must not contain a query or fragment")
    post_info = _post_path_info(parsed.path)
    if post_info:
        owner, route_kind, shortcode = post_info
        owner_path = f"{owner}/" if owner else ""
        return (
            "post",
            f"https://www.instagram.com/{owner_path}{route_kind}/{quote(shortcode, safe='-_')}/",
            owner,
        )
    profile_url, username = validate_profile_url(value)
    return "profile", profile_url, username


def _profile_posts(source: str, requested_username: str) -> tuple[list[dict[str, Any]], list[str], str | None]:
    nodes, meta, payloads = _parse_html(source)
    profile_username = _profile_username_from_payload(payloads) or requested_username
    links = _html_post_links(source)
    # A profile timeline can contain a collaboration or cross-account Reel.
    # Those anchors are part of the visible timeline and must not be discarded
    # just because their URL owner differs from the requested profile.
    by_shortcode: dict[str, Mapping[str, Any]] = {}
    for node in nodes:
        code = _shortcode(node)
        if code and code not in by_shortcode:
            by_shortcode[code] = node
    ordered_posts: list[tuple[str, str, str | None]] = []
    # Embedded timeline data carries the ordering used by Instagram. Visible
    # anchors are a fallback for pages that omit that data and a way to append
    # links whose node details were not embedded.
    for node in nodes:
        code = _shortcode(node)
        if not code or any(existing_code == code for _, existing_code, _ in ordered_posts):
            continue
        source_url = _first(node, "permalink", "url", "link", "seo_canonical_url", "canonical_url")
        parsed_info = _post_path_info(source_url) if isinstance(source_url, str) else None
        ordered_posts.append((parsed_info[1] if parsed_info else "p", code, parsed_info[0] if parsed_info else profile_username))
    for owner, kind, code in links:
        if not any(existing_code == code for _, existing_code, _ in ordered_posts):
            ordered_posts.append((kind, code, owner or profile_username))
    # A profile can expose an og:image but no timeline data. That is a valid
    # profile response, just with zero posts visible to an anonymous client.
    posts: list[dict[str, Any]] = []
    for kind, code, owner in ordered_posts[:MAX_POSTS]:
        node = by_shortcode.get(
            code,
            {"shortcode": code, "permalink": f"/{owner or profile_username}/{kind}/{code}/"},
        )
        post = _node_to_post(node, len(posts) + 1, profile_username)
        if post:
            posts.append(post)
    warnings: list[str] = []
    if len(ordered_posts) > MAX_POSTS:
        warnings.append(f"Only the first {MAX_POSTS} posts were requested to limit upstream load")
    if not payloads:
        warnings.append("Instagram did not expose embedded timeline JSON; results came from visible links only")
    if not posts:
        if "login" in source.lower() or "challenge" in source.lower():
            warnings.append("Instagram returned a login or challenge page; no public posts were visible")
        else:
            warnings.append("No public posts were found in the profile HTML")
    return posts, warnings, profile_username


def _fetch_post_detail(post: dict[str, Any]) -> None:
    url = post.get("url")
    shortcode = post.get("shortcode")
    if not isinstance(url, str):
        post["detail_fetch"] = {"ok": False, "status": "missing_url"}
        return
    try:
        source = _fetch_html(url, POST_TIMEOUT)
        nodes, meta, payloads = _parse_html(source, str(shortcode) if shortcode else None)
        node: Mapping[str, Any] | None = None
        if shortcode:
            matching = [candidate for candidate in nodes if _shortcode(candidate) == shortcode]
            if matching:
                # The same shortcode can occur in a lightweight link record
                # and a full media record. Prefer the record with actual
                # media fields so Reels retain their playable source.
                media_keys = {
                    "video_versions", "video_url", "video_dash_manifest",
                    "image_versions2", "carousel_media", "edge_sidecar_to_children",
                    "display_uri", "display_url",
                }
                node = max(matching, key=lambda candidate: len(media_keys.intersection(candidate.keys())))
        node = node or (nodes[0] if nodes else None)
        metadata_node = _meta_post_node(meta, str(shortcode) if shortcode else None)
        if node:
            _merge_post(post, node)
            # A partial Polaris node can carry media but omit its caption or
            # timestamp. Fill only those gaps from the Open Graph metadata.
            if metadata_node and (not post.get("caption") or not post.get("timestamp")):
                _merge_post(post, metadata_node)
        elif metadata_node:
            _merge_post(post, metadata_node)
        post["detail_fetch"] = {"ok": True, "status": "ok"}
    except InstagramReadError as exc:
        post["detail_fetch"] = {"ok": False, "status": exc.code, "message": exc.message}
    except Exception as exc:  # Defensive: one malformed post must not abort all results.
        LOGGER.warning("Could not parse post %s: %s", url, exc)
        post["detail_fetch"] = {"ok": False, "status": "parse_error", "message": "Could not parse post HTML"}


def _post_sort_timestamp(post: Mapping[str, Any]) -> float:
    """Return a comparable timestamp; undated posts stay at the end."""

    value = post.get("timestamp")
    if isinstance(value, Mapping):
        value = _first(value, "timestamp", "value")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if text:
            try:
                return float(text)
            except ValueError:
                try:
                    parsed = _datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=_datetime.timezone.utc)
                    return parsed.timestamp()
                except ValueError:
                    pass
    return float("-inf")


def _renumber_posts(posts: list[dict[str, Any]]) -> None:
    for index, post in enumerate(posts, 1):
        post["order"] = index
        post["index"] = index


def read_profile(raw_url: str, pinned: bool = False, details: bool = True) -> tuple[dict[str, Any], int]:
    profile_url, requested_username = validate_profile_url(raw_url)
    source = _fetch_html(profile_url, PROFILE_TIMEOUT)
    posts, warnings, profile_username = _profile_posts(source, requested_username)
    # Fetch details concurrently but write only to each independent dict. The
    # fast links mode intentionally skips this network-heavy step.
    if details and posts:
        with ThreadPoolExecutor(max_workers=min(POST_WORKERS, len(posts)), thread_name_prefix="ig-post") as pool:
            futures = [pool.submit(_fetch_post_detail, post) for post in posts]
            for future in futures:
                future.result()
    # Instagram's profile HTML usually places pinned items first, followed by
    # the rest of the grid. Keep that order only when explicitly requested;
    # the default view is more useful as a chronological newest-first list.
    if details and not pinned:
        posts.sort(key=_post_sort_timestamp, reverse=True)
    _renumber_posts(posts)
    profile_info = _profile_info_from_html(source, profile_username, posts)
    if details:
        response_posts: list[dict[str, Any]] = posts
    else:
        response_posts = [
            {
                "order": post.get("order"),
                "index": post.get("index"),
                "shortcode": post.get("shortcode"),
                "url": post.get("url"),
                "owner_username": post.get("owner_username"),
                "is_pinned": post.get("is_pinned"),
            }
            for post in posts
        ]
    result = {
        "ok": True,
        "profile": {"username": profile_username, "url": profile_url, **profile_info},
        "source_url": profile_url,
        "posts": response_posts,
        "links": [post.get("url") for post in response_posts if post.get("url")],
        "count": len(response_posts),
        "mode": "details" if details else "links",
        "sort": "pinned" if pinned else "latest",
        "pinned_order": pinned,
        "warnings": warnings,
    }
    return result, HTTPStatus.OK


def read_post(raw_url: str, canonical_url: str, username: str | None) -> tuple[dict[str, Any], int]:
    """Read one public post or Reel and return it in the same shape as a profile response."""

    _, _, shortcode = _post_path_info(canonical_url) or (None, None, None)
    if not shortcode:
        raise InstagramReadError("invalid_post_url", "Use a URL like https://www.instagram.com/reel/CODE/")
    post: dict[str, Any] = _node_to_post(
        {"shortcode": shortcode, "permalink": canonical_url},
        1,
        username,
    ) or {
        "order": 1,
        "index": 1,
        "shortcode": shortcode,
        "url": canonical_url,
        "permalink": canonical_url,
        "owner_username": username,
    }
    _fetch_post_detail(post)
    resolved_username = post.get("owner_username") or username or "unknown"
    profile_url = (
        f"https://www.instagram.com/{quote(str(resolved_username), safe='._')}/"
        if resolved_username != "unknown"
        else canonical_url
    )
    warnings: list[str] = []
    if not post.get("caption"):
        warnings.append("Instagram did not expose a public caption for this post")
    if not post.get("timestamp"):
        warnings.append("Instagram did not expose a public publish time for this post")
    return {
        "ok": True,
        "profile": {"username": resolved_username, "url": profile_url},
        "source_url": canonical_url,
        "posts": [post],
        "count": 1,
        "warnings": warnings,
    }, HTTPStatus.OK


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class InstagramHandler(BaseHTTPRequestHandler):
    server_version = "InstagramReader/1.0"

    def _send_json(self, payload: Mapping[str, Any], status: int = 200) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, request_path: str) -> None:
        """Serve the small browser client from the same origin as the API."""

        relative = request_path.lstrip("/") or "index.html"
        # The route is intentionally constrained to the tool directory. This
        # prevents path traversal while still allowing CSS and JavaScript.
        if any(part in ("", ".", "..") and part == ".." for part in relative.split("/")):
            self._send_json({"ok": False, "error": {"code": "not_found", "message": "Not found"}}, HTTPStatus.NOT_FOUND)
            return
        root = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.abspath(os.path.join(root, relative))
        if not file_path.startswith(root + os.sep) or not os.path.isfile(file_path):
            self._send_json({"ok": False, "error": {"code": "not_found", "message": "Not found"}}, HTTPStatus.NOT_FOUND)
            return
        try:
            with open(file_path, "rb") as file:
                body = file.read()
        except OSError:
            self._send_json({"ok": False, "error": {"code": "read_error", "message": "Could not read file"}}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith(("text/", "application/javascript")) else content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802 - required BaseHTTPRequestHandler name
        self._send_json({"ok": True})

    @staticmethod
    def _flag(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().casefold() in {"1", "true", "yes", "on"}
        return default

    def _handle_api_values(self, values: Mapping[str, Any]) -> None:
        raw_url = values.get("url", "")
        if not isinstance(raw_url, str):
            raw_url = str(raw_url or "")
        pinned = self._flag(values.get("pinned"), False)
        sort_value = str(values.get("sort", "")).strip().casefold()
        pinned = pinned or sort_value in {"pinned", "pin", "profile", "page"}
        links_only = self._flag(values.get("links_only"), False)
        detail_value = values.get("details", values.get("detail"))
        details = not links_only if detail_value is None else self._flag(detail_value, True)
        target_kind, canonical_url, username = validate_target_url(raw_url)
        if target_kind == "post":
            result, status = read_post(raw_url, canonical_url, username)
        else:
            result, status = read_profile(canonical_url, pinned=pinned, details=details)
        self._send_json(result, status)

    def do_GET(self) -> None:  # noqa: N802 - required BaseHTTPRequestHandler name
        parsed = urlparse(self.path)
        if parsed.path == "/" or (parsed.path and not parsed.path.startswith("/api/")):
            self._send_static(parsed.path)
            return
        if parsed.path != "/api/profile":
            self._send_json(
                {"ok": False, "error": {"code": "not_found", "message": "Use GET /api/profile?url=..."}},
                HTTPStatus.NOT_FOUND,
            )
            return
        query = parse_qs(parsed.query, keep_blank_values=True)
        try:
            values = {key: items[-1] for key, items in query.items()}
            self._handle_api_values(values)
        except InstagramReadError as exc:
            self._send_json({"ok": False, "error": {"code": exc.code, "message": exc.message}}, exc.status)
        except Exception:
            LOGGER.exception("Unexpected profile request failure")
            self._send_json(
                {"ok": False, "error": {"code": "internal_error", "message": "Unexpected server error"}},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def do_POST(self) -> None:  # noqa: N802 - JSON API compatibility
        parsed = urlparse(self.path)
        if parsed.path != "/api/profile":
            self._send_json(
                {"ok": False, "error": {"code": "not_found", "message": "Use POST /api/profile with a JSON body"}},
                HTTPStatus.NOT_FOUND,
            )
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 64 * 1024:
                raise InstagramReadError("invalid_body", "JSON request body must be smaller than 64 KB")
            body = self.rfile.read(length)
            payload = json.loads(body.decode("utf-8")) if body else {}
            if not isinstance(payload, Mapping):
                raise InstagramReadError("invalid_body", "JSON request body must be an object")
            self._handle_api_values(payload)
        except InstagramReadError as exc:
            self._send_json({"ok": False, "error": {"code": exc.code, "message": exc.message}}, exc.status)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._send_json({"ok": False, "error": {"code": "invalid_json", "message": "Request body must be valid UTF-8 JSON"}}, HTTPStatus.BAD_REQUEST)
        except Exception:
            LOGGER.exception("Unexpected profile POST failure")
            self._send_json(
                {"ok": False, "error": {"code": "internal_error", "message": "Unexpected server error"}},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.info("%s - %s", self.address_string(), fmt % args)


def create_server(host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), InstagramHandler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read public Instagram profile posts")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8787, help="Bind port (default: 8787)")
    parser.add_argument("--debug", action="store_true", help="Enable verbose request logs")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = create_server(args.host, args.port)
    LOGGER.info("Instagram reader listening on http://%s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Stopping")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
