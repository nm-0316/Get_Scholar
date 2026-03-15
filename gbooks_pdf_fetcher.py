"""Google Books の表示可能ページをPDFとして保存するツール。"""

from __future__ import annotations

import io

import hashlib
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse


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

# Google Books API v1 エンドポイント
GOOGLE_BOOKS_API_URL = "https://www.googleapis.com/books/v1/volumes"


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


def check_google_books_api(book_id: str) -> dict | None:
    """Google Books API v1 で書籍情報・PDF入手可否を確認する。

    Returns:
        dict with keys:
            'pdf_available': bool
            'epub_available': bool
            'access_info': dict (raw accessInfo from API)
            'title': str
        or None if API call fails.
    """
    try:
        url = f"{GOOGLE_BOOKS_API_URL}/{book_id}"
        resp = requests.get(url, timeout=TIMEOUT)
        if resp.status_code != 200:
            return None
        data = resp.json()
        access_info = data.get("accessInfo", {})
        pdf_info = access_info.get("pdf", {})
        epub_info = access_info.get("epub", {})
        volume_info = data.get("volumeInfo", {})

        result = {
            "title": volume_info.get("title", ""),
            "pdf_available": pdf_info.get("isAvailable", False),
            "pdf_download_link": pdf_info.get("downloadLink"),
            "epub_available": epub_info.get("isAvailable", False),
            "epub_download_link": epub_info.get("downloadLink"),
            "viewability": access_info.get("viewability", "NO_PAGES"),
            "access_info": access_info,
        }
        return result
    except Exception:
        return None


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
    """HTML内に埋め込まれたページID(pg=PA1 等)を抽出。

    抽出できなかった場合は連番の標準的なページIDリストを返す。
    """
    raw_ids = set(re.findall(r'"pid":"([A-Z]{1,3}\d+)"', html))
    fallback_ids = set(re.findall(r'"(PA\d+|PP\d+|PR\d+|PT\d+)"', html))
    page_ids = sorted(raw_ids | fallback_ids, key=lambda x: (x[:2], int(re.sub(r"\D", "", x) or 0)))

    if page_ids:
        return page_ids[:MAX_PAGES]

    # HTML から抽出できない場合は標準的な連番ページIDを生成する
    sequential_ids: list[str] = []
    # 前付け (PP: Preliminary Pages)
    sequential_ids += [f"PP{i}" for i in range(1, 11)]
    # 前付けローマ数字 (PR: PRe-publication pages)
    sequential_ids += [f"PR{i}" for i in range(1, 21)]
    # 本文ページ (PA: PAges)
    sequential_ids += [f"PA{i}" for i in range(1, MAX_PAGES - 30)]
    return sequential_ids[:MAX_PAGES]


def extract_embedded_page_image_urls(page_url: str, html: str) -> dict[str, str]:
    """HTML内に埋め込まれた pid/src マッピングを抽出する。"""
    pattern = re.compile(r'"pid":"([A-Z]{1,3}\d+)"[^\{\}]{0,600}?"src":"([^\"]+)"')
    page_map: dict[str, str] = {}
    for page_id, raw_src in pattern.findall(html):
        src = raw_src.replace("\\u003d", "=").replace("\\u0026", "&").replace("\\/", "/")
        src = src.replace("\\", "")
        if "/books/content" not in src:
            continue
        page_map[page_id] = urljoin(page_url, src)
    return page_map


def looks_like_not_available_image(image_bytes: bytes) -> bool:
    """プレースホルダ画像らしさを簡易判定する。"""
    try:
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        width, height = image.size
        if width <= 10 or height <= 10:
            return True

        # 単色/低色数は「image not available」等のプレースホルダであることが多い。
        palette = image.resize((120, 120)).getcolors(maxcolors=512)
        if palette and len(palette) <= 6:
            return True

        return False
    except Exception:
        return True


def fetch_page_image(
    session: requests.Session,
    domain: str,
    book_id: str,
    page_id: str,
    embedded_url: str | None = None,
) -> bytes | None:
    if embedded_url:
        resp = session.get(embedded_url, timeout=TIMEOUT)
        if resp.status_code == 200 and "image" in resp.headers.get("Content-Type", "").lower():
            if not looks_like_not_available_image(resp.content):
                return resp.content


    query = urlencode(
        {
            "id": book_id,
            "pg": page_id,
            "img": 1,
            "zoom": 3,
            "hl": "ja",

            "w": 1200,

        }
    )
    content_url = f"https://{domain}/books/content?{query}"

    resp = session.get(content_url, timeout=TIMEOUT)
    if resp.status_code != 200:
        return None

    ctype = resp.headers.get("Content-Type", "").lower()
    if "image" not in ctype:
        return None


    if looks_like_not_available_image(resp.content):
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

    # HTML に埋め込まれたページ画像URLをあらかじめ取得しておく
    embedded_url_map = extract_embedded_page_image_urls(page_url, html)

    domain = urlparse(page_url).netloc
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Referer": page_url})

    images: list[bytes] = []
    consecutive_failures = 0
    for page_id in page_ids:
        embedded_url = embedded_url_map.get(page_id)
        image_bytes = fetch_page_image(session, domain, book_id, page_id, embedded_url)
        if image_bytes:
            images.append(image_bytes)
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            # 連続して取得失敗が続く場合、それ以降は存在しないと判断して打ち切る
            if consecutive_failures >= 5:
                break

    if not images:
        raise DownloadNotAvailableError("表示可能ページの画像を取得できませんでした。")

    output_path = output_dir / f"{book_id}_preview_pages.pdf"
    return save_images_as_pdf(images, output_path)


def main() -> int:
    print("Google Books 取得ツール（合法利用専用）")
    print("1) Google Books API でPDF入手可否を確認")
    print("2) 公式PDFリンクがあればそれを保存")
    print("3) リンクがなければ、表示可能ページ画像をPDF化")

    page_url = input("Google Books URLを入力してください: ").strip()
    if not page_url:
        print("URLが空です。")
        return 1

    try:
        page_url = normalize_google_books_url(page_url)
        book_id = extract_book_id(page_url)

        # --- ステップ1: Google Books API で確認 ---
        print(f"\n書籍ID: {book_id}")
        print("Google Books API で書籍情報を確認中...")
        api_info = check_google_books_api(book_id)
        if api_info:
            print(f"タイトル: {api_info['title']}")
            print(f"  PDF入手可能: {api_info['pdf_available']}")
            print(f"  EPUB入手可能: {api_info['epub_available']}")
            print(f"  閲覧権限: {api_info['viewability']}")

            if api_info["pdf_available"] and api_info["pdf_download_link"]:
                print(f"  PDF直接ダウンロードリンク: {api_info['pdf_download_link']}")
        else:
            print("  Google Books API から情報を取得できませんでした（続行します）。")

        resp = requests.get(page_url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        resp.raise_for_status()

        output_dir = Path("downloads")

        # --- ステップ2: 公式PDFリンクの検索 ---
        pdf_url = find_official_pdf_link(page_url, resp.text)

        # API から得たダウンロードリンクを優先的に試みる
        if not pdf_url and api_info and api_info.get("pdf_download_link"):
            pdf_url = api_info["pdf_download_link"]

        if pdf_url:
            print(f"\n公式PDFリンク候補: {pdf_url}")
            try:
                output = download_pdf_file(pdf_url, output_dir)
                print(f"公式PDFを保存: {output.resolve()}")
                return 0
            except RuntimeError as err:
                print(f"公式PDFのダウンロードに失敗しました: {err}")
                print("表示可能ページをPDF化する方法に切り替えます。")

        # --- ステップ3: 表示可能ページのPDF化 ---
        print("\n表示可能ページをPDF化します...")
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
