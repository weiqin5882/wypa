#!/usr/bin/env python3
"""
一个用于知识库入库的网页爬虫（纯标准库版本）：
- 抓取网页正文并输出为 txt
- 下载网页中的图片并保存到本地
- 生成 image_manifest.jsonl，记录图片与来源页面映射

示例：
python spider_kb.py \
  --start-url https://example.com \
  --output-dir ./kb_data \
  --max-pages 20 \
  --max-depth 1
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import deque
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


@dataclass
class PageResult:
    url: str
    title: str
    text_path: Path
    image_paths: list[Path]


USER_AGENT = "Mozilla/5.0 (compatible; KnowledgeBaseCrawler/1.0)"


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


def fetch_url(url: str, timeout: int = 15) -> tuple[bytes, str] | None:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "")
            data = resp.read()
            return data, content_type
    except Exception:
        return None


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

            fetched = fetch_url(url)
            if fetched is None:
                continue
            html_bytes, content_type = fetched
            if "text/html" not in content_type.lower():
                continue

            html = decode_html(html_bytes)
            parser = SimpleHTMLExtractor()
            parser.feed(html)

            title = " ".join(parser.title_parts).strip() or "untitled"
            text = clean_text("\n".join(parser.text_parts))

            page_id = f"page_{len(results) + 1:04d}"
            text_file = pages_dir / f"{page_id}_{safe_name(title)}.txt"
            text_file.write_text(f"URL: {url}\n标题: {title}\n\n{text}\n", encoding="utf-8")

            image_paths: list[Path] = []
            seen_img: set[str] = set()
            for src in parser.images:
                img_url = normalize_url(url, src)
                if img_url in seen_img:
                    continue
                seen_img.add(img_url)

                img_fetched = fetch_url(img_url)
                if img_fetched is None:
                    continue
                img_bytes, img_ct = img_fetched
                if "image/" not in img_ct.lower():
                    continue

                ext = ext_from_content_type(img_ct)
                img_path = images_dir / f"img_{image_counter:04d}.{ext}"
                image_counter += 1
                img_path.write_bytes(img_bytes)
                image_paths.append(img_path)

                mf.write(
                    json.dumps(
                        {
                            "source_page": url,
                            "source_title": title,
                            "image_url": img_url,
                            "local_path": str(img_path.relative_to(output_dir)),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

            results.append(PageResult(url=url, title=title, text_path=text_file, image_paths=image_paths))

            if depth < max_depth:
                for href in parser.links:
                    next_url = normalize_url(url, href)
                    if same_domain(start_url, next_url) and next_url not in visited:
                        queue.append((next_url, depth + 1))

            if delay > 0:
                time.sleep(delay)

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
    print("=" * 72)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="网页爬取并导出文本+图片（知识库入库版）")
    p.add_argument("--start-url", required=True, help="起始 URL")
    p.add_argument("--output-dir", default="kb_output", help="输出目录（默认: kb_output）")
    p.add_argument("--max-pages", type=int, default=20, help="最多抓取页面数（默认: 20）")
    p.add_argument("--max-depth", type=int, default=1, help="抓取深度（默认: 1）")
    p.add_argument("--delay", type=float, default=0.5, help="请求间隔秒数（默认: 0.5）")
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
    )
    print_summary(output_dir, results)


if __name__ == "__main__":
    main()
