#!/usr/bin/env python3
"""Sync personal JournalClub.io episodes to iCloud Drive for offline iPhone use."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
import plistlib
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlparse

from playwright.sync_api import BrowserContext, Page, sync_playwright


BASE_URL = "https://journalclub.io"
ARCHIVE_URL = f"{BASE_URL}/episodes"
LOGIN_URL = f"{BASE_URL}/login"
ROOT = Path(__file__).resolve().parent
PROFILE = ROOT / ".journalclub-browser"
STATE_FILE = ROOT / ".journalclub-state.json"
AUTH_FILE = ROOT / ".journalclub-auth.json"
LOCK_FILE = ROOT / ".journalclub-sync.lock"
DEFAULT_OUTPUT = (
    Path.home()
    / "Library/Mobile Documents/com~apple~CloudDocs/Journal Club"
)
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".mp4", ".wav", ".aac", ".ogg", ".flac"}


@dataclass(frozen=True)
class EpisodeResult:
    title: str
    audio_path: Path | None
    episode_pdf_path: Path | None
    paper_status: str

    @property
    def required_complete(self) -> bool:
        return self.audio_path is not None and self.episode_pdf_path is not None


def safe_name(value: str, maximum: int = 140) -> str:
    value = re.sub(r"[/:\\]", " - ", value)
    value = re.sub(r"[\x00-\x1f]", "", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value or "Untitled")[:maximum].rstrip()


def write_json(path: Path, payload: dict, mode: int = 0o600) -> None:
    write_bytes(
        path,
        (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode(),
        mode,
    )


def write_bytes(path: Path, body: bytes, mode: int = 0o644) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            path.chmod(mode)
        except OSError:
            pass  # iCloud Drive rejects chmod on synced files; not critical
    finally:
        temporary.unlink(missing_ok=True)


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"synced": []}
    try:
        state = json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError) as error:
        raise RuntimeError(f"cannot read sync state: {error}") from error
    if not isinstance(state, dict) or not isinstance(state.get("synced", []), list):
        raise RuntimeError("sync state must contain a 'synced' list")
    if not all(isinstance(url, str) for url in state.get("synced", [])):
        raise RuntimeError("sync state contains a non-string episode URL")
    return state


def save_state(state: dict) -> None:
    write_json(STATE_FILE, state)


def restore_auth(context: BrowserContext) -> None:
    if not AUTH_FILE.exists():
        return
    try:
        auth = json.loads(AUTH_FILE.read_text())
        if not isinstance(auth, dict):
            raise RuntimeError("saved Journal Club login is not a JSON object")
        cookies = auth.get("cookies", [])
        if cookies:
            context.add_cookies(cookies)
    except (json.JSONDecodeError, OSError) as error:
        raise RuntimeError(f"cannot read saved Journal Club login: {error}") from error


def save_auth(context: BrowserContext) -> None:
    write_json(AUTH_FILE, context.storage_state())


@contextmanager
def exclusive_run():
    LOCK_FILE.touch(mode=0o600, exist_ok=True)
    LOCK_FILE.chmod(0o600)
    with LOCK_FILE.open("r+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("another Journal Club sync is already running") from error
        yield


def episode_links(page: Page) -> tuple[list[str], bool]:
    response = page.goto(ARCHIVE_URL, wait_until="domcontentloaded")
    if response is None or not response.ok:
        status = response.status if response else "no response"
        raise RuntimeError(f"Journal Club archive returned HTTP {status}")
    page.wait_for_timeout(1_000)
    links = page.locator('a[href*="/episodes/"]').evaluate_all(
        "els => els.map(a => a.href)"
    )
    result: list[str] = []
    for link in links:
        clean = link.split("#", 1)[0]
        if clean.startswith(f"{BASE_URL}/episodes/") and clean not in result:
            result.append(clean)
    path = urlparse(page.url).path.rstrip("/")
    signed_out = path == "/login" or page.get_by_text(
        "Signup to View", exact=True
    ).count() > 0
    if not result and not signed_out:
        raise RuntimeError(
            "Journal Club archive returned no episode links; refusing to report up to date"
        )
    return result, signed_out


def ensure_login(page: Page, headless: bool = False) -> list[str]:
    links, signed_out = episode_links(page)
    if not signed_out:
        return links

    if headless:
        raise RuntimeError(
            "Journal Club login expired. Run ./sync.sh interactively on the Mac Studio."
        )

    print("\nSign in to Journal Club in the browser window.")
    print("When you can see your account or episode archive, return here.")
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    input("Press Return after signing in: ")
    links, signed_out = episode_links(page)
    if signed_out:
        raise RuntimeError(
            "The archive still shows 'Signup to View'. "
            "Please confirm the browser shows you as signed in and run again."
        )
    return links


def first_href(page: Page, patterns: tuple[str, ...]) -> str | None:
    anchors = page.locator("a[href]")
    for index in range(anchors.count()):
        anchor = anchors.nth(index)
        text = (anchor.inner_text() or "").strip().lower()
        href = anchor.get_attribute("href") or ""
        combined = f"{text} {href.lower()}"
        if any(pattern in combined for pattern in patterns):
            return urljoin(page.url, href)
    return None


def extension_from_url(url: str, content_type: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in AUDIO_EXTENSIONS:
        return suffix
    content_type = content_type.lower()
    if "audio/mp4" in content_type:
        return ".m4a"
    if "mp4" in content_type:
        return ".mp4"
    if "aac" in content_type:
        return ".aac"
    if "wav" in content_type:
        return ".wav"
    if "ogg" in content_type:
        return ".ogg"
    if "flac" in content_type:
        return ".flac"
    return ".mp3"


def destination_with_extension(destination: Path, extension: str) -> Path:
    return destination.parent / f"{destination.name}{extension}"


def is_audio(body: bytes, content_type: str) -> bool:
    if not body:
        return False
    prefix = body[:256].lstrip().lower()
    if prefix.startswith((b"<!doctype html", b"<html", b"<?xml")):
        return False
    media_type = content_type.lower().split(";", 1)[0].strip()
    if media_type.startswith("audio/") or media_type == "video/mp4":
        return True
    return (
        body.startswith(b"ID3")
        or (len(body) > 1 and body[0] == 0xFF and body[1] & 0xE0 == 0xE0)
        or (len(body) > 12 and body[4:8] == b"ftyp")
        or (len(body) > 12 and body.startswith(b"RIFF") and body[8:12] == b"WAVE")
        or body.startswith(b"OggS")
        or body.startswith(b"fLaC")
    )


def download_audio(context: BrowserContext, url: str, destination: Path) -> Path:
    response = context.request.get(url)
    if not response.ok:
        raise RuntimeError(f"audio download returned HTTP {response.status}")
    content_type = response.headers.get("content-type", "")
    body = response.body()
    if not is_audio(body, content_type):
        raise RuntimeError(
            f"audio download returned non-audio content ({content_type or 'unknown type'})"
        )
    extension = extension_from_url(url, content_type)
    path = destination_with_extension(destination, extension)
    write_bytes(path, body)
    return path


def write_webloc(path: Path, url: str) -> None:
    write_bytes(path, plistlib.dumps({"URL": url}))


def is_pdf(body: bytes, content_type: str) -> bool:
    return body.startswith(b"%PDF-") or "application/pdf" in content_type.lower()


def file_starts_with(path: Path, signatures: tuple[bytes, ...]) -> bool:
    try:
        with path.open("rb") as handle:
            prefix = handle.read(256)
    except OSError:
        return False
    return any(prefix.startswith(signature) for signature in signatures)


def is_audio_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return is_audio(handle.read(256), "")
    except OSError:
        return False


def try_pdf_url(
    context: BrowserContext, url: str, referer: str | None = None
) -> bytes | None:
    try:
        headers = {
            "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.1"
        }
        if referer:
            headers["Referer"] = referer
        response = context.request.get(
            url,
            headers=headers,
            timeout=45_000,
        )
        if not response.ok:
            return None
        body = response.body()
        if is_pdf(body, response.headers.get("content-type", "")):
            return body
    except Exception:
        return None
    return None


def doi_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.netloc.lower() not in {"doi.org", "dx.doi.org"}:
        return None
    doi = unquote(parsed.path.lstrip("/"))
    return doi if doi.startswith("10.") else None


def crossref_pdf_candidates(context: BrowserContext, doi_url: str) -> list[str]:
    doi = doi_from_url(doi_url)
    if not doi:
        return []
    try:
        response = context.request.get(
            f"https://api.crossref.org/works/{quote(doi, safe='')}",
            headers={
                "Accept": "application/json",
                "User-Agent": "journalclub-sync/1.0",
            },
            timeout=30_000,
        )
        if not response.ok:
            return []
        links = response.json().get("message", {}).get("link", [])
    except Exception:
        return []
    return [
        link["URL"]
        for link in links
        if isinstance(link, dict)
        and isinstance(link.get("URL"), str)
        and "pdf" in str(link.get("content-type", "")).lower()
    ]


def publisher_pdf_candidates(url: str) -> list[str]:
    parsed = urlparse(url)
    clean_url = url.split("?", 1)[0].rstrip("/")
    candidates: list[str] = []

    pii_match = re.search(r"/(?:pii|retrieve/pii)/(S[0-9A-Z]+)", clean_url, re.I)
    if pii_match:
        article_url = (
            f"https://www.sciencedirect.com/science/article/pii/{pii_match.group(1)}"
        )
        candidates.extend(
            (
                f"{article_url}/pdfft?isDTMRedir=true&download=true",
                f"{article_url}/pdfft?download=true",
            )
        )

    if parsed.netloc.lower().endswith("mdpi.com"):
        article_url = re.sub(r"/(html|notes|pdf(?:-vor)?)$", "", clean_url)
        candidates.extend((f"{article_url}/pdf", f"{article_url}/pdf-vor"))

    if "onlinelibrary.wiley.com" in parsed.netloc.lower():
        doi_match = re.search(r"/doi/(?:abs/|full/)?(10\..+)$", clean_url, re.I)
        if doi_match:
            origin = f"{parsed.scheme}://{parsed.netloc}"
            candidates.extend(
                (
                    f"{origin}/doi/pdfdirect/{doi_match.group(1)}",
                    f"{origin}/doi/pdf/{doi_match.group(1)}",
                )
            )

    return candidates


def download_paper(
    context: BrowserContext, page: Page, doi_url: str, destination: Path
) -> bool:
    # Some DOI registrants honor PDF content negotiation directly.
    body = try_pdf_url(context, doi_url)
    if body:
        write_bytes(destination, body)
        return True

    candidates = crossref_pdf_candidates(context, doi_url)
    publisher_error: Exception | None = None
    try:
        page.goto(doi_url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(1_500)

        # Common open-access publisher routes that are not always advertised in
        # page metadata (or are injected only after client-side challenges).
        candidates.extend(publisher_pdf_candidates(page.url))

        for selector in (
            'meta[name="citation_pdf_url"]',
            'meta[name="wkhealth_pdf_url"]',
            'meta[property="og:pdf"]',
        ):
            locator = page.locator(selector)
            for index in range(locator.count()):
                value = locator.nth(index).get_attribute("content")
                if value:
                    candidates.append(urljoin(page.url, value))

        anchors = page.locator("a[href]")
        for index in range(min(anchors.count(), 500)):
            anchor = anchors.nth(index)
            href = anchor.get_attribute("href") or ""
            text = (anchor.inner_text() or "").strip().lower()
            lowered = href.lower().split("?", 1)[0]
            if lowered.endswith(".pdf") or any(
                phrase in text
                for phrase in ("download pdf", "view pdf", "full text pdf", "pdf (")
            ):
                candidates.append(urljoin(page.url, href))
    except Exception as error:
        publisher_error = error

    for candidate in dict.fromkeys(candidates):
        body = try_pdf_url(context, candidate, referer=page.url)
        if body:
            write_bytes(destination, body)
            return True
        # A few publishers reject API-style requests but allow the same URL in
        # the authenticated browser tab after their cookie/challenge flow.
        try:
            response = page.goto(candidate, wait_until="commit", timeout=60_000)
            if response:
                browser_body = response.body()
                if is_pdf(browser_body, response.headers.get("content-type", "")):
                    write_bytes(destination, browser_body)
                    return True
        except Exception:
            pass
    if publisher_error:
        print(f"  paper detail: publisher page error: {publisher_error}", file=sys.stderr)
    return False


def sync_episode(
    context: BrowserContext,
    page: Page,
    url: str,
    output: Path,
    papers_only: bool = False,
) -> EpisodeResult:
    response = page.goto(url, wait_until="networkidle")
    if response is None or not response.ok:
        status = response.status if response else "no response"
        raise RuntimeError(f"episode page returned HTTP {status}")
    if urlparse(page.url).path.rstrip("/") == "/login":
        raise RuntimeError("Journal Club login expired while loading an episode")
    title = safe_name(page.locator("h1").first.inner_text())
    folder = output / title
    folder.mkdir(parents=True, exist_ok=True)
    print(f"\n{title}")

    audio_path: Path | None = None
    pdf_path: Path | None = None
    if not papers_only:
        audio = first_href(
            page,
            (
                "download the audio",
                "download audio",
                ".mp3",
                ".m4a",
                ".aac",
                ".wav",
            ),
        )
        audio_path = next(
            (
                path
                for path in folder.iterdir()
                if path.is_file()
                and path.stem == title
                and path.suffix.lower() in AUDIO_EXTENSIONS
                and is_audio_file(path)
            ),
            None,
        )
        if audio_path:
            print(f"  audio: {audio_path.name}")
        elif audio:
            audio_path = download_audio(context, audio, folder / title)
            print(f"  audio: {audio_path.name}")
        else:
            print("  audio: ERROR no downloadable link found")

        # Chromium's print output gives the iPhone a readable, offline copy.
        pdf_path = folder / f"{title} - Episode.pdf"
        if not file_starts_with(pdf_path, (b"%PDF-",)):
            pdf_body = page.pdf(format="Letter", print_background=True)
            if not is_pdf(pdf_body, ""):
                raise RuntimeError("episode print output was not a PDF")
            write_bytes(pdf_path, pdf_body)
        print(f"  reading: {pdf_path.name}")

    doi = first_href(page, ("doi.org/", "download the pdf", "want the paper"))
    paper_status = "missing"
    if doi:
        write_webloc(folder / "Original Paper.webloc", doi)
        paper_path = folder / f"{title} - Original Paper.pdf"
        if file_starts_with(paper_path, (b"%PDF-",)) or download_paper(
            context, page, doi, paper_path
        ):
            print(f"  paper: {paper_path.name}")
            paper_status = "pdf"
        else:
            print(
                "  paper: publisher blocked automated PDF retrieval; "
                "saved Original Paper.webloc"
            )
            paper_status = "shortcut"
    else:
        print("  paper: no DOI link found")

    return EpisodeResult(title, audio_path, pdf_path, paper_status)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync Journal Club audio and reading copies to iCloud Drive."
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT, help="destination folder"
    )
    parser.add_argument(
        "--papers-only",
        action="store_true",
        help="revisit previously synced episodes and fetch original-paper PDFs",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="run without a browser window (requires a previously saved login)",
    )
    parser.add_argument(
        "--latest",
        type=int,
        default=7,
        help="maximum new episodes per run (default: 7; use 0 for all)",
    )
    return parser.parse_args()


def run_sync(args: argparse.Namespace) -> int:
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    state = load_state()
    synced = set(state.get("synced", []))

    with sync_playwright() as playwright:
        launch_options = {
            "headless": args.headless,
            "accept_downloads": True,
        }
        if shutil.which("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
            launch_options["channel"] = "chrome"
        context = playwright.chromium.launch_persistent_context(
            str(PROFILE), **launch_options
        )
        restore_auth(context)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            links = ensure_login(page, headless=args.headless)
            save_auth(context)
            if args.papers_only:
                pending = [link for link in links if link in synced]
            else:
                pending = [link for link in links if link not in synced]
            if args.latest > 0 and not args.papers_only:
                pending = pending[: args.latest]
            if not pending:
                print(f"Already up to date. Files are in {output}")
                return 0

            print(f"Syncing {len(pending)} episode(s) to {output}")
            failures: list[str] = []
            for link in pending:
                try:
                    result = sync_episode(
                        context, page, link, output, papers_only=args.papers_only
                    )
                except Exception as error:  # continue so one unusual page is not fatal
                    failure = f"{link}: {error}"
                    failures.append(failure)
                    print(f"  ERROR skipped: {failure}", file=sys.stderr)
                    continue
                if not args.papers_only and not result.required_complete:
                    failure = f"{result.title}: required audio or episode PDF missing"
                    failures.append(failure)
                    print(f"  ERROR: {failure}", file=sys.stderr)
                    continue
                if not args.papers_only:
                    synced.add(link)
                    state["synced"] = sorted(synced)
                    save_state(state)
        finally:
            context.close()

    if failures:
        print(f"\nSync completed with {len(failures)} failure(s):", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("\nDone. The files will appear in the iPhone Files app under iCloud Drive.")
    return 0


def main() -> int:
    args = parse_args()
    try:
        with exclusive_run():
            return run_sync(args)
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
