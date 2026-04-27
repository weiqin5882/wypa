#!/usr/bin/env python3
"""
一个用于知识库入库的网页爬虫（纯标准库版本）：
- 抓取网页正文并输出为 txt
- 下载网页中的图片并保存到本地
- 生成 image_manifest.jsonl，记录图片与来源页面映射

相比基础版增强：
- 浏览器化请求头（User-Agent / Accept / Accept-Language / Referer）
- 可配置重试与退避，降低因反爬导致的偶发失败
- 支持 gzip/br/deflate 响应解压（br 需环境支持）
- verbose 模式输出失败原因，便于排查“什么都没爬到”的问题
"""

from __future__ import annotations

import argparse
import gzip
import json
import random
import re
import ssl
import time
import zlib
from collections import deque
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

try:
    import brotli  # type: ignore
except Exception:  # noqa: BLE001
    brotli = None


@dataclass
class PageResult:
    url: str
    title: str
    text_path: Path
    image_paths: list[Path]


@dataclass
class FetchResult:
    ok: bool
    data: bytes
    content_type: str
    final_url: str
    error: str = ""


BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/*,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


class SimpleHTMLExtractor(HTMLParser):
    """提取标题、正文文本、链接和图片。"""

    def __init__(self) -> None:
        super().__init__()
        self.in_title = False
        self.skip_depth = 0
        self._skip_tags = {"script", "style", "noscript"}

        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.links: list[str] = []
        self.images: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {k: (v or "") for k, v in attrs}

        if tag in self._skip_tags:
            self.skip_depth += 1

        if tag == "title":
            self.in_title = True

        if self.skip_depth == 0:
            if tag == "a" and attrs_dict.get("href"):
                self.links.append(attrs_dict["href"])
            if tag == "img" and attrs_dict.get("src"):
                self.images.append(attrs_dict["src"])
            if tag in {"p", "div", "article", "section", "main", "br", "li", "h1", "h2", "h3"}:
                self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if tag in self._skip_tags and self.skip_depth > 0:
            self.skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not data.strip():
            return
        if self.in_title:
            self.title_parts.append(data.strip())
        if self.skip_depth == 0:
            self.text_parts.append(data.strip())


def safe_name(text: str, max_len: int = 80) -> str:
    text = re.sub(r"\s+", "_", text.strip())
    text = re.sub(r"[^\w\u4e00-\u9fff\-_.]", "", text)
    return (text[:max_len] or "untitled").strip("._") or "untitled"


def normalize_url(base_url: str, href: str) -> str:
    return urljoin(base_url, href.split("#")[0])


def same_domain(url1: str, url2: str) -> bool:
    return urlparse(url1).netloc == urlparse(url2).netloc


def decode_http_body(raw: bytes, content_encoding: str) -> bytes:
    enc = (content_encoding or "").lower().strip()
    if enc == "gzip":
        return gzip.decompress(raw)
    if enc == "deflate":
        return zlib.decompress(raw)
    if enc == "br" and brotli is not None:
        return brotli.decompress(raw)
    return raw


def fetch_url(
    url: str,
    timeout: int,
    retries: int,
    backoff: float,
    referer: str | None = None,
) -> FetchResult:
    last_error = "unknown"
    headers = dict(BROWSER_HEADERS)
    if referer:
        headers["Referer"] = referer

    for attempt in range(retries + 1):
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=timeout) as resp:
                content_type = resp.headers.get("Content-Type", "")
                content_encoding = resp.headers.get("Content-Encoding", "")
                raw = resp.read()
                data = decode_http_body(raw, content_encoding)
                final_url = resp.geturl()
                return FetchResult(True, data, content_type, final_url)
        except ssl.SSLError:
            # 一些站点证书链在容器中可能验证失败，退化为不校验证书重试一次
            try:
                insecure_ctx = ssl._create_unverified_context()
                with urlopen(request, timeout=timeout, context=insecure_ctx) as resp:
                    content_type = resp.headers.get("Content-Type", "")
                    content_encoding = resp.headers.get("Content-Encoding", "")
                    raw = resp.read()
                    data = decode_http_body(raw, content_encoding)
                    final_url = resp.geturl()
                    return FetchResult(True, data, content_type, final_url)
            except Exception as e:  # noqa: BLE001
                last_error = f"SSL error: {e}"
        except HTTPError as e:
            last_error = f"HTTP {e.code}"
        except URLError as e:
            last_error = f"URL error: {e.reason}"
        except Exception as e:  # noqa: BLE001
            last_error = f"Exception: {e}"

        if attempt < retries:
            sleep_s = backoff * (2**attempt) + random.uniform(0, 0.5)
            time.sleep(sleep_s)

    return FetchResult(False, b"", "", url, error=last_error)


def decode_html(data: bytes) -> str:
    for enc in ("utf-8", "gb18030", "big5", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def clean_text(raw_text: str) -> str:
    raw_text = re.sub(r"[ \t]+", " ", raw_text)
    raw_text = re.sub(r"\n{3,}", "\n\n", raw_text)
    return raw_text.strip()


def ext_from_content_type(content_type: str, fallback: str = "jpg") -> str:
    c = content_type.lower()
    if "image/" in c:
        ext = c.split("image/")[-1].split(";")[0].strip()
        if ext == "jpeg":
            return "jpg"
        if ext:
            return safe_name(ext, 8)
    return fallback


def crawl(
    start_url: str,
    output_dir: Path,
    max_pages: int,
    max_depth: int,
    delay: float,
    timeout: int,
    retries: int,
    backoff: float,
    verbose: bool,
) -> list[PageResult]:
    pages_dir = output_dir / "pages"
    images_dir = output_dir / "images"
    pages_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    visited: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(start_url, 0)])
    results: list[PageResult] = []
    image_counter = 1

    image_manifest_path = output_dir / "image_manifest.jsonl"
    with image_manifest_path.open("w", encoding="utf-8") as mf:
        while queue and len(results) < max_pages:
            url, depth = queue.popleft()
            if url in visited:
                continue
            visited.add(url)

            fetched = fetch_url(url, timeout=timeout, retries=retries, backoff=backoff)
            if not fetched.ok:
                if verbose:
                    print(f"[SKIP] 页面请求失败: {url} -> {fetched.error}")
                continue

            if "text/html" not in fetched.content_type.lower():
                if verbose:
                    print(f"[SKIP] 非 HTML 内容: {fetched.final_url} ({fetched.content_type})")
                continue

            html = decode_html(fetched.data)
            parser = SimpleHTMLExtractor()
            parser.feed(html)

            title = " ".join(parser.title_parts).strip() or "untitled"
            text = clean_text("\n".join(parser.text_parts))
            if len(text) < 20 and verbose:
                print(f"[WARN] 页面正文很短，可能被反爬或强 JS 渲染: {fetched.final_url}")

            page_id = f"page_{len(results) + 1:04d}"
            text_file = pages_dir / f"{page_id}_{safe_name(title)}.txt"
            text_file.write_text(
                f"URL: {fetched.final_url}\n标题: {title}\n\n{text}\n", encoding="utf-8"
            )

            image_paths: list[Path] = []
            seen_img: set[str] = set()
            for src in parser.images:
                img_url = normalize_url(fetched.final_url, src)
                if img_url in seen_img:
                    continue
                seen_img.add(img_url)

                img_fetched = fetch_url(
                    img_url,
                    timeout=timeout,
                    retries=retries,
                    backoff=backoff,
                    referer=fetched.final_url,
                )
                if not img_fetched.ok:
                    if verbose:
                        print(f"[SKIP] 图片下载失败: {img_url} -> {img_fetched.error}")
                    continue
                if "image/" not in img_fetched.content_type.lower():
                    continue

                ext = ext_from_content_type(img_fetched.content_type)
                img_path = images_dir / f"img_{image_counter:04d}.{ext}"
                image_counter += 1
                img_path.write_bytes(img_fetched.data)
                image_paths.append(img_path)

                mf.write(
                    json.dumps(
                        {
                            "source_page": fetched.final_url,
                            "source_title": title,
                            "image_url": img_url,
                            "local_path": str(img_path.relative_to(output_dir)),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

            results.append(
                PageResult(
                    url=fetched.final_url,
                    title=title,
                    text_path=text_file,
                    image_paths=image_paths,
                )
            )

            if depth < max_depth:
                for href in parser.links:
                    next_url = normalize_url(fetched.final_url, href)
                    if same_domain(start_url, next_url) and next_url not in visited:
                        queue.append((next_url, depth + 1))

            if delay > 0:
                time.sleep(delay + random.uniform(0, 0.8))

    return results


def print_summary(output_dir: Path, results: list[PageResult]) -> None:
    print("=" * 72)
    print("抓取完成，适用于知识库入库的数据已生成：")
    print(f"输出目录: {output_dir}")
    print(f"网页文本数量: {len(results)}")
    print(f"图片清单: {output_dir / 'image_manifest.jsonl'}")
    print("\n每个页面输出：")
    for idx, r in enumerate(results, 1):
        print(f"[{idx}] {r.title}")
        print(f"  - URL: {r.url}")
        print(f"  - 文本文件: {r.text_path}")
        print(f"  - 图片数量: {len(r.image_paths)}")
    if not results:
        print("[提示] 当前结果为 0。建议开启 --verbose 排查：UA/反爬/JS 渲染/网络连通性")
    print("=" * 72)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="网页爬取并导出文本+图片（知识库入库版）")
    p.add_argument("--start-url", required=True, help="起始 URL")
    p.add_argument("--output-dir", default="kb_output", help="输出目录（默认: kb_output）")
    p.add_argument("--max-pages", type=int, default=20, help="最多抓取页面数（默认: 20）")
    p.add_argument("--max-depth", type=int, default=1, help="抓取深度（默认: 1）")
    p.add_argument("--delay", type=float, default=1.2, help="请求间隔秒数（默认: 1.2）")
    p.add_argument("--request-timeout", type=int, default=20, help="单次请求超时秒数（默认: 20）")
    p.add_argument("--retries", type=int, default=2, help="失败重试次数（默认: 2）")
    p.add_argument("--retry-backoff", type=float, default=1.0, help="重试退避基数秒（默认: 1.0）")
    p.add_argument("--verbose", action="store_true", help="打印详细调试日志")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    results = crawl(
        start_url=args.start_url,
        output_dir=output_dir,
        max_pages=max(1, args.max_pages),
        max_depth=max(0, args.max_depth),
        delay=max(args.delay, 0.0),
        timeout=max(5, args.request_timeout),
        retries=max(0, args.retries),
        backoff=max(0.1, args.retry_backoff),
        verbose=args.verbose,
    )
    print_summary(output_dir, results)


if __name__ == "__main__":
    main()
