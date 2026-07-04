"""
B站文集 → EPUB 转换器
当前策略：
1. 只请求 CV 页面，不使用 API 兜底。
2. 正文优先解析页面 DOM，失败时解析页面内的 __INITIAL_STATE__。
3. 每章提取 banner 图并写入 EPUB，正文图片也会下载并重写引用。
4. 文章和图片支持并发抓取，缓存命中时不再请求网络。
"""

import os
import re
import json
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import escape
from urllib.parse import urlparse
import sys

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from ebooklib import epub

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0",
    "Referer": "https://www.bilibili.com/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

cookie_fn = "./cookies.txt"
if os.path.isfile(cookie_fn):
    with open(cookie_fn, "r") as f:
        headers["Cookie"] = f.read().strip()

# 正文有效的最低字符数（纯文本，去空格后）
MIN_CONTENT_CHARS = 200
REQUEST_TIMEOUT = 20
DEFAULT_WORKERS = 4
DEFAULT_IMAGE_WORKERS = 8
CACHE_DIR = "./.cache"
IMAGE_CACHE_DIR = os.path.join(CACHE_DIR, "imgs")
IMAGE_EXTS = (".avif", ".webp", ".jpg", ".jpeg", ".png", ".gif")

_thread_local = threading.local()


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(headers)
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


def get_session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = build_session()
        _thread_local.session = session
    return session


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────

def get_image_filename(url: str) -> str:
    path = urlparse(url).path
    basename = os.path.basename(path)
    if "@" in basename:
        base, suffix = basename.split("@", 1)
        suffix_ext = os.path.splitext(suffix)[1].lower()
        if suffix_ext in IMAGE_EXTS:
            safe_suffix = re.sub(r"[^a-zA-Z0-9_.-]", "_", suffix)
            return f"{base}_{safe_suffix}"
        basename = base

    img_id = basename
    if "." not in img_id:
        for ext in IMAGE_EXTS:
            if ext in url:
                img_id += ext
                break
        else:
            img_id += ".jpg"
    return img_id


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
            response = get_session().get(url, timeout=15)
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
        for source in soup.find_all("source"):
            source_type = (source.get("type") or "").lower()
            srcset = source.get("srcset") or ""
            if "image/avif" not in source_type or not srcset:
                continue
            banner_url = normalize_image_url(srcset, strip_transform=False)
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
            resp = get_session().get(url, timeout=REQUEST_TIMEOUT)
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
    img_filename = get_image_filename(url)
    os.makedirs(IMAGE_CACHE_DIR, exist_ok=True)
    fn = os.path.join(IMAGE_CACHE_DIR, img_filename)

    if os.path.exists(fn):
        with open(fn, "rb") as f:
            print(f"  [缓存图片] {img_filename}")
            return f.read()

    try:
        response = get_session().get(url, timeout=REQUEST_TIMEOUT)
        content_type = response.headers.get("content-type", "")
        if "image" not in content_type:
            print(f"  [非图片] {url}（Content-Type: {content_type}）")
            return None
        with open(fn, "wb") as f:
            f.write(response.content)
        return response.content
    except Exception as e:
        print(f"  [图片下载失败] {url}：{e}")
        return None


def mime_and_ext(url: str) -> tuple[str, str]:
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if "@" in url:
        transform_ext = os.path.splitext(url.split("@", 1)[1])[1].lower()
        if transform_ext in IMAGE_EXTS:
            ext = transform_ext
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


def make_banner_html(article: CV_Article) -> str:
    if not article.banner_url:
        return ""
    src = epub_image_path(article.banner_url)
    alt = escape(article.title or "")
    return f'<p><img src="{src}" alt="{alt}" style="max-width:100%;height:auto;"/></p>'


# ─────────────────────────────────────────────
# EPUB 生成
# ─────────────────────────────────────────────

class Converter:
    def __init__(self, read_list: ReadList, articles: list[CV_Article], image_workers: int = DEFAULT_IMAGE_WORKERS):
        self.read_list = read_list
        self.articles = articles
        self.image_workers = max(1, image_workers)

    def _add_epub_image(self, ebook: epub.EpubBook, img_url: str, epub_path: str, image_data: bytes):
        mime, ext = mime_and_ext(img_url)
        uid = re.sub(r"[^a-zA-Z0-9_]", "_", os.path.basename(epub_path))
        image_item = epub.EpubImage(
            uid=f"img_{uid}",
            file_name=epub_path,
            media_type=mime,
            content=image_data,
        )
        ebook.add_item(image_item)
        print(f"  [图片已加入] {os.path.basename(epub_path)}")

    def convert_epub(self):
        ebook = epub.EpubBook()
        ebook.set_identifier(f"bilibili_rl_{self.read_list.readlist_id}")
        ebook.set_title(self.read_list.name)
        ebook.set_language("zh-CN")
        ebook.add_author(self.read_list.author)

        if self.read_list.cover_url:
            cover_data = download_image(self.read_list.cover_url)
            if cover_data:
                ebook.set_cover("cover.jpg", cover_data)

        chapters = []
        image_url_map: dict[str, str] = {}

        for idx, article in enumerate(self.articles):
            if not article.context:
                continue

            banner_html = make_banner_html(article)
            chapter = epub.EpubHtml(
                title=article.title or f"第{idx+1}章",
                file_name=f"article{idx}.xhtml",
                lang="zh-CN",
            )
            chapter.content = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN"
  "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>{article.title}</title></head>
<body>
  <h1>{article.title}</h1>
  {banner_html}
  <hr/>
  {article.context}
</body>
</html>""".encode("utf-8")

            ebook.add_item(chapter)
            chapters.append(chapter)

            if article.banner_url:
                image_url_map[article.banner_url] = epub_image_path(article.banner_url)

            for img_url in article.images:
                image_url_map[img_url] = epub_image_path(img_url)

        with ThreadPoolExecutor(max_workers=self.image_workers) as executor:
            future_map = {
                executor.submit(download_image, img_url): (img_url, epub_path)
                for img_url, epub_path in image_url_map.items()
            }
            for future in as_completed(future_map):
                img_url, epub_path = future_map[future]
                try:
                    image_data = future.result()
                except Exception as e:
                    print(f"  [图片下载失败] {img_url}：{e}")
                    continue
                if image_data:
                    self._add_epub_image(ebook, img_url, epub_path, image_data)

        ebook.toc = [(epub.Section(self.read_list.name), chapters)]
        ebook.add_item(epub.EpubNcx())
        ebook.add_item(epub.EpubNav())
        ebook.spine = ["nav"] + chapters

        out_fn = f"{self.read_list.name}.epub"
        epub.write_epub(out_fn, ebook, {})
        print(f"\n✅ 已生成：{out_fn}")


def fetch_article(meta: dict) -> CV_Article:
    cv_id = meta["cv_id"]
    print(f"正在抓取 cv{cv_id}…")
    article = CV_Article(cv_id)
    article.fetch_content()
    return article


def fetch_articles(articles_meta: list[dict], workers: int) -> list[CV_Article]:
    if workers <= 1:
        return [fetch_article(meta) for meta in articles_meta]

    articles: list[CV_Article | None] = [None] * len(articles_meta)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(fetch_article, meta): idx
            for idx, meta in enumerate(articles_meta)
        }
        for future in as_completed(future_map):
            idx = future_map[future]
            try:
                articles[idx] = future.result()
            except Exception as e:
                cv_id = articles_meta[idx].get("cv_id", "")
                print(f"  [异常] cv{cv_id}：{e}，保留空章节")
                article = CV_Article(cv_id)
                article.title = f"文章 cv{cv_id}"
                article.context = "<p>（抓取时发生异常）</p>"
                articles[idx] = article

    return [article for article in articles if article is not None]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="B站文集 → EPUB 转换器")
    parser.add_argument("readlist_id", nargs="?", default="36436", help="文集 ID，例如 702577")
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"文章并发数，默认 {DEFAULT_WORKERS}；如果触发限流可设为 1",
    )
    parser.add_argument(
        "--image-workers",
        type=int,
        default=DEFAULT_IMAGE_WORKERS,
        help=f"图片并发下载数，默认 {DEFAULT_IMAGE_WORKERS}",
    )
    return parser.parse_args()


# ─────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────

def main():
    args = parse_args()
    readlist_id = str(args.readlist_id)

    print("正在获取文集信息…")
    readlist = ReadList(readlist_id)
    readlist.fetch()

    articles = fetch_articles(readlist.articles_meta, max(1, args.workers))
    c = Converter(readlist, articles, image_workers=max(1, args.image_workers))
    c.convert_epub()


if __name__ == "__main__":
    main()
