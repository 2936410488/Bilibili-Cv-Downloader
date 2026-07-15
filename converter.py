"""
B站文集 → EPUB 转换器
当前策略：
1. 只请求 CV 页面，不使用 API 兜底。
2. 正文优先解析页面 DOM，失败时解析页面内的 __INITIAL_STATE__。
3. 每章提取 banner 图并写入 EPUB，重复 banner 只保留一份。
4. 文章和图片按顺序抓取，网络请求间隔 1 秒，缓存命中时不再请求网络。
"""

import os
import re
import json
import argparse
import configparser
import hashlib
import ipaddress
import tempfile
import time
import unicodedata
from html import escape
from urllib.parse import urljoin, urlparse
import sys

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from ebooklib import epub

BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0",
    "Referer": "https://www.bilibili.com/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 正文有效的最低字符数（纯文本，去空格后）
MIN_CONTENT_CHARS = 200
REQUEST_TIMEOUT = 20
REQUEST_DELAY_SECONDS = 1.0
MAX_IMAGE_BYTES = 50 * 1024 * 1024
CACHE_DIR = "./.cache"
IMAGE_CACHE_DIR = os.path.join(CACHE_DIR, "imgs")
IMAGE_EXTS = (".avif", ".webp", ".jpg", ".jpeg", ".png", ".gif")
DEFAULT_COOKIE_FILE = "./cookies.txt"
CONFIG_SECTION = "converter"
COOKIE_SECTION = "cookies"

_authenticated_session: requests.Session | None = None
_public_session: requests.Session | None = None
_cookie_pairs: list[tuple[str, str]] = []
_last_request_at = 0.0


def parse_cookie_pairs(cookie_text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for part in normalize_cookie_text(cookie_text).split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if name:
            pairs.append((name, value.strip()))
    return pairs


def build_session(*, authenticated: bool = False) -> requests.Session:
    session = requests.Session()
    session.headers.update(BASE_HEADERS)
    if authenticated:
        # 使用 CookieJar 的域规则，而不是全局 Cookie 请求头。这样即使请求被重定向，
        # 登录 Cookie 也不会发送给 bilibili.com 以外的站点。
        for name, value in _cookie_pairs:
            session.cookies.set(
                name,
                value,
                domain=".bilibili.com",
                path="/",
                secure=True,
            )
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=32, pool_maxsize=32)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def get_session(*, authenticated: bool = False) -> requests.Session:
    global _authenticated_session, _public_session
    if authenticated:
        if _authenticated_session is None:
            _authenticated_session = build_session(authenticated=True)
        return _authenticated_session
    if _public_session is None:
        _public_session = build_session(authenticated=False)
    return _public_session


def throttled_get(
    url: str,
    *,
    timeout: int | float,
    authenticated: bool = False,
    stream: bool = False,
) -> requests.Response:
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < REQUEST_DELAY_SECONDS:
        time.sleep(REQUEST_DELAY_SECONDS - elapsed)
    try:
        return get_session(authenticated=authenticated).get(
            url,
            timeout=timeout,
            stream=stream,
        )
    finally:
        _last_request_at = time.monotonic()


def normalize_cookie_text(cookie_text: str) -> str:
    cookie_parts = []
    lines = cookie_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.lower().startswith("cookie:"):
            line = line.split(":", 1)[1].strip()
        for part in line.split(";"):
            part = part.strip()
            if part:
                cookie_parts.append(part)
    return "; ".join(cookie_parts)


def resolve_config_path(path: str, config_path: str | None = None) -> str:
    expanded = os.path.expanduser(path)
    if os.path.isabs(expanded):
        return expanded
    base_dir = os.path.dirname(config_path) if config_path else os.getcwd()
    return os.path.abspath(os.path.join(base_dir, expanded))


def load_config(path: str | None) -> tuple[configparser.ConfigParser | None, str | None]:
    if not path:
        return None, None

    config_path = os.path.abspath(os.path.expanduser(path))
    config = configparser.ConfigParser(interpolation=None)
    config.optionxform = str
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config.read_file(f)
    except OSError as e:
        print(f"读取配置文件失败：{e}")
        sys.exit(1)
    except configparser.Error as e:
        print(f"解析配置文件失败：{e}")
        sys.exit(1)
    return config, config_path


def config_value(
    config: configparser.ConfigParser | None,
    sections: list[str],
    names: list[str],
) -> str:
    if config is None:
        return ""

    wanted = {name.lower() for name in names}
    for section in sections:
        if section == "DEFAULT":
            items = config.defaults().items()
        elif config.has_section(section):
            items = config._sections.get(section, {}).items()
        else:
            continue

        for key, value in items:
            if key == "__name__":
                continue
            if key.lower() in wanted:
                return str(value or "").strip()
    return ""


def cookie_pairs_from_sections(config: configparser.ConfigParser | None, sections: list[str]) -> str:
    if config is None:
        return ""

    reserved_names = {
        "cookie",
        "cookies",
        "cookie_text",
        "cookie_file",
        "cookies_file",
        "cookie_path",
        "cookies_path",
        "file",
        "path",
        "readlist_id",
        "read_list_id",
        "id",
    }
    cookie_lines = []
    for section in sections:
        if not config.has_section(section):
            continue
        for key, value in config._sections.get(section, {}).items():
            if key == "__name__" or key.lower() in reserved_names:
                continue
            value = str(value or "").strip()
            if value:
                cookie_lines.append(f"{key}={value}")
    return normalize_cookie_text("\n".join(cookie_lines))


def load_cookie_file(path: str, *, explicit: bool = False) -> str:
    if not os.path.isfile(path):
        if explicit:
            print(f"  [警告] Cookie 文件不存在：{path}")
        return ""
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return normalize_cookie_text(f.read())


def apply_cookie_config(config: configparser.ConfigParser | None, config_path: str | None):
    global _authenticated_session, _public_session, _cookie_pairs
    inline_cookie = config_value(
        config,
        [CONFIG_SECTION, COOKIE_SECTION, "DEFAULT"],
        ["cookies", "cookie", "cookie_text"],
    )
    cookie_header = normalize_cookie_text(inline_cookie) if inline_cookie else ""
    if not cookie_header:
        cookie_header = cookie_pairs_from_sections(config, [COOKIE_SECTION, CONFIG_SECTION])

    explicit_cookie_file = config_value(
        config,
        [CONFIG_SECTION, COOKIE_SECTION, "DEFAULT"],
        ["cookie_file", "cookies_file", "cookie_path", "cookies_path", "file", "path"],
    )
    if not cookie_header:
        cookie_file = explicit_cookie_file or DEFAULT_COOKIE_FILE
        cookie_path = resolve_config_path(cookie_file, config_path)
        cookie_header = load_cookie_file(cookie_path, explicit=bool(explicit_cookie_file))

    _cookie_pairs = parse_cookie_pairs(cookie_header) if cookie_header else []
    _authenticated_session = None
    _public_session = None


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────

WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_filename(
    value: str,
    fallback: str = "output",
    max_length: int = 120,
    max_bytes: int = 180,
) -> str:
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = fallback
    if value.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        value = f"_{value}"
    value = value[:max_length].rstrip(" .")
    while value and len(value.encode("utf-8")) > max_bytes:
        value = value[:-1].rstrip(" .")
    return value or fallback


def image_extension_from_url(url: str) -> str:
    parsed_path = urlparse(url).path
    ext = os.path.splitext(parsed_path)[1].lower()
    if "@" in parsed_path:
        transformed_ext = os.path.splitext(parsed_path.split("@", 1)[1])[1].lower()
        if transformed_ext in IMAGE_EXTS:
            ext = transformed_ext
    if ext == ".jpeg":
        return ".jpg"
    return ext if ext in IMAGE_EXTS else ".jpg"


def get_image_filename(url: str) -> str:
    normalized = normalize_image_url(url, strip_transform=False)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    basename = os.path.basename(urlparse(normalized).path).split("@", 1)[0]
    stem = safe_filename(os.path.splitext(basename)[0], "image", max_length=48)
    return f"{digest}_{stem}{image_extension_from_url(normalized)}"


def normalize_image_url(url: str, *, strip_transform: bool = True) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    url = url.split(",", 1)[0].strip()
    url = url.split()[0]
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = "https://www.bilibili.com" + url
    elif url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    if strip_transform and "@" in url:
        url = url.split("@", 1)[0]
    return url


def is_safe_image_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return True
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def sanitize_xhtml_fragment(fragment: str, *, base_url: str) -> str:
    soup = BeautifulSoup(f'<div data-epub-root="1">{fragment or ""}</div>', "html.parser")
    root = soup.find("div", attrs={"data-epub-root": "1"})
    if root is None:
        return ""

    for element in root.find_all(
        ["script", "iframe", "object", "embed", "form", "input", "button", "video", "audio", "source"]
    ):
        element.decompose()

    for element in root.find_all(True):
        for attr in list(element.attrs):
            attr_lower = attr.lower()
            if attr_lower.startswith("on") or attr_lower in {
                "contenteditable",
                "data-src",
                "decoding",
                "loading",
                "srcset",
                "style",
            }:
                del element.attrs[attr]

        if element.name == "a":
            href = str(element.get("href") or "").strip()
            if href.startswith("#"):
                continue
            absolute_href = urljoin(base_url, href)
            scheme = urlparse(absolute_href).scheme.lower()
            if not href or scheme not in {"http", "https", "mailto"}:
                element.attrs.pop("href", None)
            else:
                element["href"] = absolute_href

        if element.name == "img":
            src = str(element.get("src") or "")
            if not src.startswith("images/"):
                element.decompose()

    return "".join(str(child) for child in root.contents)


def extract_initial_state(html: str) -> dict | None:
    marker = "window.__INITIAL_STATE__="
    start = html.find(marker)
    if start < 0:
        return None
    start += len(marker)
    try:
        data, _ = json.JSONDecoder().raw_decode(html[start:])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def opus_node_text(node: dict) -> str:
    if not isinstance(node, dict):
        return ""
    if node.get("node_type") == 1:
        return str(node.get("word", {}).get("words", ""))
    if node.get("node_type") == 4:
        return str(node.get("link", {}).get("show_text", ""))
    return ""


def opus_paragraph_text(paragraph: dict) -> str:
    text_data = paragraph.get("text") or {}
    nodes = text_data.get("nodes") or []
    return "".join(opus_node_text(node) for node in nodes)


def is_tag_line(text: str) -> bool:
    return bool(re.fullmatch(r"(#[^#\s]+#\s*)+", text.strip()))


# ─────────────────────────────────────────────
# 数据类
# ─────────────────────────────────────────────

class ReadList:
    def __init__(self, readlist_id: str):
        self.readlist_id = readlist_id
        self.articles_meta: list[dict] = []
        self.cover_url: str | None = None
        self.author: str | None = None
        self.name: str | None = None

    def fetch(self):
        url = f"https://api.bilibili.com/x/article/list/web/articles?id={self.readlist_id}"
        try:
            response = throttled_get(url, timeout=15, authenticated=True)
            response.raise_for_status()
            data = response.json()
            if data["code"] == 0:
                self.name      = str(data["data"]["list"]["name"])
                self.cover_url = str(data["data"]["list"]["image_url"])
                self.author    = str(data["data"]["author"]["name"])
                for i in data["data"]["articles"]:
                    self.articles_meta.append({
                        "cv_id": str(i.get("id", "")),
                    })
                print(f"文集《{self.name}》共 {len(self.articles_meta)} 篇")
            else:
                print(f"API 返回错误：{data}")
                sys.exit(1)
        except Exception as e:
            print(f"获取文集信息失败：{e}")
            sys.exit(1)


class CV_Article:
    def __init__(self, cv_id: str):
        self.images: list[str] = []
        self.banner_url: str | None = None
        self.context: str | None = None
        self.title: str | None = None
        self.cv_id = cv_id

    # ------------------------------------------------------------------
    # 图片处理
    # ------------------------------------------------------------------
    def _process_images(self, content_div):
        for img in content_div.find_all("img"):
            src = normalize_image_url(img.get("data-src") or img.get("src") or "")
            if not src or src.startswith("data:"):
                img.decompose()
                continue
            src_clean = normalize_image_url(src)
            self.images.append(src_clean)
            img["src"] = f"images/{get_image_filename(src_clean)}"
            for attr in ["data-src", "loading", "decoding"]:
                if img.get(attr):
                    del img[attr]

    def _extract_banner_from_dom(self, soup: BeautifulSoup):
        # 新版页面通常会在同一个 picture 中提供 AVIF、WebP 和 img 回退。
        # 优先选择兼容性更好的 JPEG/PNG/WebP，只在没有其他候选时使用 AVIF。
        priorities = {
            "image/jpeg": 0,
            "image/png": 1,
            "image/webp": 2,
            "image/avif": 3,
        }
        for picture in soup.find_all("picture"):
            sources = picture.find_all("source")
            if not any("image/avif" in (source.get("type") or "").lower() for source in sources):
                continue

            candidates: list[tuple[int, str]] = []
            for source in sources:
                source_type = (source.get("type") or "").lower()
                srcset = source.get("srcset") or ""
                if srcset and source_type in priorities:
                    candidates.append((priorities[source_type], srcset))

            image = picture.find("img")
            if image:
                image_url = image.get("data-src") or image.get("src") or ""
                if image_url:
                    candidates.append((1, image_url))

            for _, candidate in sorted(candidates, key=lambda item: item[0]):
                banner_url = normalize_image_url(candidate, strip_transform=False)
                if banner_url:
                    self.banner_url = banner_url
                    return

    def _extract_banner_from_state(self, read_info: dict):
        if self.banner_url:
            return

        cover = (((read_info.get("opus") or {}).get("article") or {}).get("cover") or [])
        if cover and isinstance(cover[0], dict):
            banner_url = normalize_image_url(str(cover[0].get("url") or ""))
            if banner_url:
                self.banner_url = banner_url
                return

        for key in ["banner_url", "origin_image_urls", "image_urls"]:
            value = read_info.get(key)
            if isinstance(value, list) and value:
                banner_url = normalize_image_url(str(value[0]))
            else:
                banner_url = normalize_image_url(str(value or ""))
            if banner_url:
                self.banner_url = banner_url
                return

    def _parse_initial_state(self, html: str, state: dict | None = None) -> bool:
        state = state or extract_initial_state(html)
        if not state:
            return False

        read_info = state.get("readInfo") or {}
        self._extract_banner_from_state(read_info)
        title = str(read_info.get("title") or "").strip()
        content_html = str(read_info.get("content") or "").strip()
        paragraphs = (((read_info.get("opus") or {}).get("content") or {}).get("paragraphs") or [])

        if content_html and "<" in content_html:
            soup = BeautifulSoup(f'<div class="article-content">{content_html}</div>', "html.parser")
            div = soup.find("div")
            text = div.get_text(strip=True).replace("\xa0", "") if div else ""
            if div and len(text) >= MIN_CONTENT_CHARS:
                self.title = title or self.title or f"cv{self.cv_id}"
                self._process_images(div)
                self.context = str(div)
                return True

        blocks = []
        text_parts = []
        for idx, paragraph in enumerate(paragraphs):
            text = opus_paragraph_text(paragraph).replace("\xa0", " ").strip()
            if not text or is_tag_line(text):
                continue
            tag = "h1" if paragraph.get("para_type") == 9 and idx == 0 else "p"
            blocks.append(f"<{tag}>{escape(text)}</{tag}>")
            text_parts.append(text)

        plain_text = "".join(text_parts)
        if len(plain_text) < MIN_CONTENT_CHARS:
            if content_html:
                plain_lines = [
                    line.strip()
                    for line in content_html.replace("\r\n", "\n").replace("\r", "\n").split("\n")
                    if line.strip() and not is_tag_line(line)
                ]
                blocks = [f"<p>{escape(line)}</p>" for line in plain_lines]
                plain_text = "".join(plain_lines)
            if len(plain_text) < MIN_CONTENT_CHARS:
                return False

        self.title = title or self.title or f"cv{self.cv_id}"
        soup = BeautifulSoup(f'<div class="article-content">{"".join(blocks)}</div>', "html.parser")
        div = soup.find("div")
        self._process_images(div)
        self.context = str(div)
        return True

    # ------------------------------------------------------------------
    # 解析 CV 页面 HTML（方式1 使用）
    # ------------------------------------------------------------------
    def _parse_cv_html(self, html: str) -> bool:
        soup = BeautifulSoup(html, "html.parser")
        self._extract_banner_from_dom(soup)
        state = None
        if not self.banner_url and "window.__INITIAL_STATE__" in html:
            state = extract_initial_state(html)
            if state:
                self._extract_banner_from_state(state.get("readInfo") or {})

        # 标题
        for tag, kwargs in [
            ("h1", {"class_": re.compile(r"title")}),
            ("h1", {}),
        ]:
            el = soup.find(tag, **kwargs)
            if el and el.get_text(strip=True):
                self.title = el.get_text(strip=True)
                break
        if not self.title:
            title_tag = soup.find("title")
            if title_tag:
                self.title = title_tag.get_text(strip=True).split(" - ")[0].strip()
        if not self.title:
            self.title = f"cv{self.cv_id}"

        # 正文：按优先级依次尝试
        for selector in [
            {"id": "article-content"},
            {"class_": re.compile(r"read-article-holder")},
            {"class_": re.compile(r"article-content")},
            {"class_": re.compile(r"bili-rich-text")},
        ]:
            el = soup.find("div", **selector)
            if not el:
                continue
            text = el.get_text(strip=True).replace("\xa0", "")
            if len(text) < MIN_CONTENT_CHARS:
                # 正文字数不足，说明拿到的是空壳，不算成功
                continue
            self._process_images(el)
            self.context = str(el)
            return True

        if self._parse_initial_state(html, state):
            print(f"  [CV 初始数据] cv{self.cv_id}")
            return True

        print(f"  [CV 正文字数不足] cv{self.cv_id}（可能被反爬/限流，或需要登录）")
        return False

    # ------------------------------------------------------------------
    # 方式1：直接 requests 请求专栏 CV 页面（服务端渲染，无需浏览器）
    # ------------------------------------------------------------------
    def _fetch_via_cv(self) -> bool:
        os.makedirs(CACHE_DIR, exist_ok=True)
        fn = os.path.join(CACHE_DIR, f"cv_{self.cv_id}.html")

        if os.path.isfile(fn) and os.path.getsize(fn) >= 10000:
            with open(fn, "r", encoding="utf-8", errors="ignore") as f:
                html = f.read()
            if self._parse_cv_html(html):
                print(f"  [缓存-cv] cv{self.cv_id}")
                return True
            os.remove(fn)

        url = f"https://www.bilibili.com/read/cv{self.cv_id}/?from=readlist&opus_fallback=1"
        print(f"  [CV请求] {url}")
        try:
            resp = throttled_get(url, timeout=REQUEST_TIMEOUT, authenticated=True)
            if resp.status_code != 200:
                print(f"  [CV请求] HTTP {resp.status_code}")
                return False
            html = resp.text
            parsed_ok = self._parse_cv_html(html)
            if parsed_ok:
                with open(fn, "w", encoding="utf-8") as f:
                    f.write(html)
            return parsed_ok
        except Exception as e:
            print(f"  [CV请求失败] cv{self.cv_id}：{e}")
            return False

    # ------------------------------------------------------------------
    # 主入口：只使用 CV 页面，全程只用 requests
    # ------------------------------------------------------------------
    def fetch_content(self) -> bool:
        try:
            if self._fetch_via_cv():
                print(f"  [OK-cv]  cv{self.cv_id}《{self.title}》图片 {len(self.images)} 张")
                return True

            print(f"  [警告] cv{self.cv_id} CV 页面抓取失败，保留空章节")
            self.title = self.title or f"文章 cv{self.cv_id}"
            self.context = f"<p>（正文无法获取，请访问：https://www.bilibili.com/read/cv{self.cv_id}）</p>"
            return False

        except Exception as e:
            print(f"  [异常] cv{self.cv_id}：{e}，跳过")
            self.title = self.title or f"文章 cv{self.cv_id}"
            self.context = "<p>（抓取时发生异常）</p>"
            return False


# ─────────────────────────────────────────────
# 图片下载
# ─────────────────────────────────────────────

def download_image(url: str) -> bytes | None:
    if not is_safe_image_url(url):
        print(f"  [跳过不安全图片地址] {url}")
        return None

    img_filename = get_image_filename(url)
    os.makedirs(IMAGE_CACHE_DIR, exist_ok=True)
    fn = os.path.join(IMAGE_CACHE_DIR, img_filename)

    if os.path.exists(fn):
        with open(fn, "rb") as f:
            cached = f.read(MAX_IMAGE_BYTES + 1)
        if 0 < len(cached) <= MAX_IMAGE_BYTES and sniff_image_format(cached):
            print(f"  [缓存图片] {img_filename}")
            return cached
        print(f"  [损坏图片缓存] {img_filename}，重新下载")
        try:
            os.remove(fn)
        except OSError:
            pass

    temp_path: str | None = None
    try:
        response = throttled_get(url, timeout=REQUEST_TIMEOUT, stream=True)
        with response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            if not content_type.startswith("image/"):
                print(f"  [非图片] {url}（Content-Type: {content_type}）")
                return None

            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > MAX_IMAGE_BYTES:
                print(f"  [图片过大] {url}（上限 {MAX_IMAGE_BYTES // 1024 // 1024} MB）")
                return None

            content = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                content.extend(chunk)
                if len(content) > MAX_IMAGE_BYTES:
                    print(f"  [图片过大] {url}（上限 {MAX_IMAGE_BYTES // 1024 // 1024} MB）")
                    return None

        if not content:
            print(f"  [空图片] {url}")
            return None

        image_data = bytes(content)
        if not sniff_image_format(image_data):
            print(f"  [无法识别的图片格式] {url}")
            return None
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".image-",
            dir=IMAGE_CACHE_DIR,
            delete=False,
        ) as temp_file:
            temp_file.write(image_data)
            temp_path = temp_file.name
        os.replace(temp_path, fn)
        return image_data
    except Exception as e:
        print(f"  [图片下载失败] {url}：{e}")
        return None
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def sniff_image_format(image_data: bytes) -> tuple[str, str] | None:
    if image_data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if image_data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if image_data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if len(image_data) >= 12 and image_data[:4] == b"RIFF" and image_data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if len(image_data) >= 12 and image_data[4:8] == b"ftyp":
        brands = image_data[8:32]
        if b"avif" in brands or b"avis" in brands:
            return "image/avif", ".avif"
    return None


def mime_and_ext(url: str, image_data: bytes | None = None) -> tuple[str, str]:
    if image_data:
        detected = sniff_image_format(image_data)
        if detected:
            return detected

    ext = image_extension_from_url(url)
    mapping = {
        ".avif": ("image/avif", ".avif"),
        ".jpg":  ("image/jpeg", ".jpg"),
        ".jpeg": ("image/jpeg", ".jpg"),
        ".png":  ("image/png",  ".png"),
        ".gif":  ("image/gif",  ".gif"),
        ".webp": ("image/webp", ".webp"),
    }
    return mapping.get(ext, ("image/jpeg", ".jpg"))


def epub_image_path(url: str) -> str:
    return f"images/{get_image_filename(url)}"


def banner_dedupe_key(url: str) -> str:
    normalized = normalize_image_url(url)
    parsed = urlparse(normalized)
    path = parsed.path.split("@", 1)[0]
    return f"{parsed.netloc}{path}".lower()


def make_banner_html(article: CV_Article, banner_path: str | None) -> str:
    if not article.banner_url or not banner_path:
        return ""
    src = banner_path
    alt = escape(article.title or "")
    return f'<p class="chapter-banner"><img src="{src}" alt="{alt}"/></p>'


BOOK_CSS = b"""
body { font-family: sans-serif; line-height: 1.7; margin: 5%; }
h1 { line-height: 1.3; }
img { display: block; max-width: 100%; height: auto; margin: 1em auto; }
pre, code { white-space: pre-wrap; overflow-wrap: anywhere; }
table { width: 100%; border-collapse: collapse; }
th, td { border: 1px solid #999; padding: 0.35em; }
blockquote { margin-left: 0; padding-left: 1em; border-left: 0.25em solid #aaa; }
.chapter-source { margin-top: 2em; font-size: 0.85em; color: #666; }
"""


# ─────────────────────────────────────────────
# EPUB 生成
# ─────────────────────────────────────────────

class Converter:
    def __init__(self, read_list: ReadList, articles: list[CV_Article]):
        self.read_list = read_list
        self.articles = articles
        self._added_epub_paths: set[str] = set()

    def _add_epub_image(self, ebook: epub.EpubBook, img_url: str, epub_path: str, image_data: bytes):
        if epub_path in self._added_epub_paths:
            return
        mime, _ = mime_and_ext(img_url, image_data)
        uid = re.sub(r"[^a-zA-Z0-9_]", "_", os.path.basename(epub_path))
        image_item = epub.EpubImage(
            uid=f"img_{uid}",
            file_name=epub_path,
            media_type=mime,
            content=image_data,
        )
        ebook.add_item(image_item)
        self._added_epub_paths.add(epub_path)
        print(f"  [图片已加入] {os.path.basename(epub_path)}")

    def _add_banner_images(self, ebook: epub.EpubBook) -> dict[str, str]:
        banner_path_by_url: dict[str, str] = {}
        banner_path_by_key: dict[str, str] = {}
        banner_path_by_hash: dict[str, str] = {}

        for article in self.articles:
            if not article.context or not article.banner_url:
                continue

            banner_url = article.banner_url
            dedupe_key = banner_dedupe_key(banner_url)
            if dedupe_key in banner_path_by_key:
                banner_path = banner_path_by_key[dedupe_key]
                banner_path_by_url[banner_url] = banner_path
                print(f"  [banner复用] {os.path.basename(banner_path)}")
                continue

            image_data = download_image(banner_url)
            if not image_data:
                continue

            image_hash = hashlib.sha256(image_data).hexdigest()
            if image_hash in banner_path_by_hash:
                banner_path = banner_path_by_hash[image_hash]
                banner_path_by_key[dedupe_key] = banner_path
                banner_path_by_url[banner_url] = banner_path
                duplicate_name = os.path.basename(epub_image_path(banner_url))
                canonical_name = os.path.basename(banner_path)
                print(f"  [banner重复] {duplicate_name} -> {canonical_name}")
                continue

            banner_path = epub_image_path(banner_url)
            self._add_epub_image(ebook, banner_url, banner_path, image_data)
            banner_path_by_key[dedupe_key] = banner_path
            banner_path_by_hash[image_hash] = banner_path
            banner_path_by_url[banner_url] = banner_path

        return banner_path_by_url

    def convert_epub(self, output_path: str | None = None) -> str:
        self._added_epub_paths.clear()
        ebook = epub.EpubBook()
        book_name = self.read_list.name or f"Bilibili文集{self.read_list.readlist_id}"
        ebook.set_identifier(f"bilibili_rl_{self.read_list.readlist_id}")
        ebook.set_title(book_name)
        ebook.set_language("zh-CN")
        if self.read_list.author:
            ebook.add_author(self.read_list.author)

        if self.read_list.cover_url:
            cover_data = download_image(self.read_list.cover_url)
            if cover_data:
                _, cover_ext = mime_and_ext(self.read_list.cover_url, cover_data)
                ebook.set_cover(f"cover{cover_ext}", cover_data)

        book_style = epub.EpubItem(
            uid="style_book",
            file_name="styles/book.css",
            media_type="text/css",
            content=BOOK_CSS,
        )
        ebook.add_item(book_style)

        banner_path_by_url = self._add_banner_images(ebook)
        chapters = []
        image_url_map: dict[str, str] = {}

        for idx, article in enumerate(self.articles):
            if not article.context:
                continue

            article_title = article.title or f"第{idx+1}章"
            safe_article_title = escape(article_title)
            source_url = f"https://www.bilibili.com/read/cv{article.cv_id}"
            clean_context = sanitize_xhtml_fragment(article.context, base_url=source_url)
            banner_html = make_banner_html(article, banner_path_by_url.get(article.banner_url or ""))
            chapter = epub.EpubHtml(
                title=article_title,
                file_name=f"article{idx}.xhtml",
                lang="zh-CN",
            )
            chapter.content = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN"
  "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>{safe_article_title}</title></head>
<body>
  <h1>{safe_article_title}</h1>
  {banner_html}
  <hr/>
  {clean_context}
  <p class="chapter-source">原文：<a href="{source_url}">{source_url}</a></p>
</body>
</html>""".encode("utf-8")

            chapter.add_item(book_style)
            ebook.add_item(chapter)
            chapters.append(chapter)

            for img_url in article.images:
                image_url_map[img_url] = epub_image_path(img_url)

        for img_url, epub_path in image_url_map.items():
            image_data = download_image(img_url)
            if image_data:
                self._add_epub_image(ebook, img_url, epub_path, image_data)

        ebook.toc = [(epub.Section(book_name), chapters)]
        ebook.add_item(epub.EpubNcx())
        ebook.add_item(epub.EpubNav())
        ebook.spine = ["nav"] + chapters

        if output_path:
            out_fn = os.path.abspath(os.path.expanduser(output_path))
            if not out_fn.lower().endswith(".epub"):
                out_fn += ".epub"
        else:
            out_fn = os.path.abspath(f"{safe_filename(book_name, 'bilibili-readlist')}.epub")

        output_dir = os.path.dirname(out_fn) or os.getcwd()
        os.makedirs(output_dir, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=".epub-build-", suffix=".epub", dir=output_dir)
        os.close(fd)
        try:
            epub.write_epub(temp_path, ebook, {})
            os.replace(temp_path, out_fn)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

        print(f"\n✅ 已生成：{out_fn}")
        return out_fn


def fetch_article(meta: dict) -> CV_Article:
    cv_id = meta["cv_id"]
    print(f"正在抓取 cv{cv_id}…")
    article = CV_Article(cv_id)
    article.fetch_content()
    return article


def fetch_articles(articles_meta: list[dict]) -> list[CV_Article]:
    articles: list[CV_Article] = []
    for meta in articles_meta:
        try:
            articles.append(fetch_article(meta))
        except Exception as e:
            cv_id = meta.get("cv_id", "")
            print(f"  [异常] cv{cv_id}：{e}，保留空章节")
            article = CV_Article(cv_id)
            article.title = f"文章 cv{cv_id}"
            article.context = "<p>（抓取时发生异常）</p>"
            articles.append(article)
    return articles


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="B站文集 → EPUB 转换器")
    parser.add_argument("readlist_id", nargs="?", help="文集 ID，例如 702577")
    parser.add_argument("-c", "--config", help="配置文件路径，例如 config.ini")
    parser.add_argument("-o", "--output", help="输出 EPUB 路径，例如 ./books/文集.epub")
    return parser.parse_args()


# ─────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────

def main() -> int:
    args = parse_args()
    config, config_path = load_config(args.config)
    apply_cookie_config(config, config_path)
    config_readlist_id = config_value(
        config,
        [CONFIG_SECTION, "DEFAULT"],
        ["readlist_id", "read_list_id", "id"],
    )
    readlist_id = str(args.readlist_id or config_readlist_id or "").strip()
    if not readlist_id:
        print("错误：请提供文集 ID，或在配置文件中设置 readlist_id。", file=sys.stderr)
        return 2
    if not re.fullmatch(r"\d+", readlist_id):
        print(f"错误：文集 ID 必须是纯数字，当前值为 {readlist_id!r}。", file=sys.stderr)
        return 2

    print("正在获取文集信息…")
    readlist = ReadList(readlist_id)
    readlist.fetch()

    articles = fetch_articles(readlist.articles_meta)
    c = Converter(readlist, articles)
    c.convert_epub(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
