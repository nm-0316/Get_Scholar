"""Google Books の表示可能ページをPDFとして保存するツール。"""

from __future__ import annotations

import io
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT = 20
MAX_PAGES = 300


class DownloadNotAvailableError(RuntimeError):
    """取得可能データが見つからない場合の例外。"""


def normalize_google_books_url(url: str) -> str:
    parsed = urlparse(url)
    if "books.google." not in parsed.netloc:
        raise ValueError("Google Books のURLを指定してください。")
    return url


def extract_book_id(page_url: str) -> str:
    book_id = parse_qs(urlparse(page_url).query).get("id", [""])[0]
    if not book_id:
        raise ValueError("URLから書籍ID(id=...)を抽出できませんでした。")
    return book_id


def find_official_pdf_link(page_url: str, html: str) -> str | None:
    """ページ上の公式PDFダウンロードリンクを返す。なければ None。"""
    soup = BeautifulSoup(html, "html.parser")

    for a_tag in soup.select("a[href]"):
        href = a_tag.get("href", "")
        text = (a_tag.get_text(" ", strip=True) or "").lower()
        if not href:
            continue

        candidate = requests.compat.urljoin(page_url, href)
        netloc = urlparse(candidate).netloc.lower()
        is_google_domain = "google." in netloc
        looks_like_pdf = ".pdf" in candidate.lower() or "download" in candidate.lower()
        looks_like_download_text = "pdf" in text or "download" in text or "ダウンロード" in text

        if is_google_domain and (looks_like_pdf or looks_like_download_text):
            return candidate

    regex = re.compile(r"https://books\.google\.[^\"'\s>]+", re.IGNORECASE)
    for match in regex.findall(html):
        if "download" in match.lower() and "pdf" in match.lower():
            return match

    return None


def download_pdf_file(url: str, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)

    with requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, stream=True) as resp:
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "").lower()
        if "pdf" not in content_type and "application/octet-stream" not in content_type:
            raise RuntimeError(
                f"PDF以外のレスポンスを受信しました: Content-Type={content_type or 'unknown'}"
            )

        filename = "google_books_download.pdf"
        disposition = resp.headers.get("Content-Disposition", "")
        match = re.search(r'filename="?([^\";]+)"?', disposition)
        if match:
            filename = match.group(1)

        out_path = output_dir / filename
        with out_path.open("wb") as file:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    file.write(chunk)

    return out_path


def extract_visible_page_ids(html: str) -> list[str]:
    """HTML内に埋め込まれたページID(pg=PA1 等)を抽出。"""
    raw_ids = set(re.findall(r'"pid":"([A-Z]{1,3}\d+)"', html))
    fallback_ids = set(re.findall(r'"(PA\d+|PP\d+|PR\d+|PT\d+)"', html))
    page_ids = sorted(raw_ids | fallback_ids, key=lambda x: (x[:2], int(re.sub(r"\D", "", x) or 0)))
    return page_ids[:MAX_PAGES]


def fetch_page_image(session: requests.Session, domain: str, book_id: str, page_id: str) -> bytes | None:
    query = urlencode(
        {
            "id": book_id,
            "pg": page_id,
            "img": 1,
            "zoom": 3,
            "hl": "ja",
        }
    )
    content_url = f"https://{domain}/books/content?{query}"

    resp = session.get(content_url, timeout=TIMEOUT)
    if resp.status_code != 200:
        return None

    ctype = resp.headers.get("Content-Type", "").lower()
    if "image" not in ctype:
        return None

    return resp.content


def save_images_as_pdf(images: list[bytes], output_path: Path) -> Path:
    pil_images = []
    for image_bytes in images:
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        pil_images.append(image)

    if not pil_images:
        raise DownloadNotAvailableError("表示可能ページ画像を取得できませんでした。")

    first, rest = pil_images[0], pil_images[1:]
    first.save(output_path, format="PDF", save_all=True, append_images=rest)
    return output_path


def download_visible_pages_as_pdf(page_url: str, html: str, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)

    book_id = extract_book_id(page_url)
    page_ids = extract_visible_page_ids(html)
    if not page_ids:
        raise DownloadNotAvailableError("表示ページ情報を抽出できませんでした。")

    domain = urlparse(page_url).netloc
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Referer": page_url})

    images: list[bytes] = []
    for page_id in page_ids:
        image_bytes = fetch_page_image(session, domain, book_id, page_id)
        if image_bytes:
            images.append(image_bytes)

    if not images:
        raise DownloadNotAvailableError("表示可能ページの画像を取得できませんでした。")

    output_path = output_dir / f"{book_id}_preview_pages.pdf"
    return save_images_as_pdf(images, output_path)


def main() -> int:
    print("Google Books 取得ツール（合法利用専用）")
    print("1) 公式PDFリンクがあればそれを保存")
    print("2) リンクがなければ、表示可能ページ画像をPDF化")

    page_url = input("Google Books URLを入力してください: ").strip()
    if not page_url:
        print("URLが空です。")
        return 1

    try:
        page_url = normalize_google_books_url(page_url)
        resp = requests.get(page_url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        resp.raise_for_status()

        pdf_url = find_official_pdf_link(page_url, resp.text)
        output_dir = Path("downloads")

        if pdf_url:
            print(f"公式PDFリンク候補: {pdf_url}")
            output = download_pdf_file(pdf_url, output_dir)
            print(f"公式PDFを保存: {output.resolve()}")
        else:
            print("公式PDFリンクが見つからないため、表示可能ページをPDF化します。")
            output = download_visible_pages_as_pdf(page_url, resp.text, output_dir)
            print(f"プレビューPDFを保存: {output.resolve()}")

        return 0

    except requests.HTTPError as error:
        print(f"HTTPエラー: {error}")
        return 3
    except Exception as error:  # noqa: BLE001
        print(f"エラー: {error}")
        return 99


if __name__ == "__main__":
    sys.exit(main())
