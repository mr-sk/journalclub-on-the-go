from __future__ import annotations

import argparse
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import sync_journalclub as sync


class FakeResponse:
    def __init__(self, body: bytes, content_type: str, status: int = 200):
        self._body = body
        self.headers = {"content-type": content_type}
        self.status = status
        self.ok = 200 <= status < 300

    def body(self) -> bytes:
        return self._body


class FakeRequest:
    def __init__(self, response: FakeResponse):
        self.response = response

    def get(self, _url: str) -> FakeResponse:
        return self.response


class FakeContext:
    def __init__(self, response: FakeResponse | None = None):
        self.request = FakeRequest(response) if response else None


class EmptyArchivePage:
    url = sync.ARCHIVE_URL

    def goto(self, _url: str, **_kwargs):
        return SimpleNamespace(ok=True, status=200)

    def wait_for_timeout(self, _milliseconds: int) -> None:
        pass

    def locator(self, _selector: str):
        return SimpleNamespace(evaluate_all=lambda _script: [])

    def get_by_text(self, _text: str, **_kwargs):
        return SimpleNamespace(count=lambda: 0)


class FakePlaywrightContextManager:
    def __init__(self, context):
        self.playwright = SimpleNamespace(
            chromium=SimpleNamespace(
                launch_persistent_context=lambda *_args, **_kwargs: context
            )
        )

    def __enter__(self):
        return self.playwright

    def __exit__(self, *_args):
        return False


class SyncTests(unittest.TestCase):
    def test_write_json_is_atomic_and_owner_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"

            sync.write_json(path, {"cookies": []})

            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.read_text(), '{\n  "cookies": []\n}\n')
            self.assertEqual(list(path.parent.glob(".*.tmp")), [])

    def test_load_state_rejects_corrupt_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("not json")
            with patch.object(sync, "STATE_FILE", path):
                with self.assertRaisesRegex(RuntimeError, "cannot read sync state"):
                    sync.load_state()

    def test_empty_archive_is_not_up_to_date(self):
        with self.assertRaisesRegex(RuntimeError, "no episode links"):
            sync.episode_links(EmptyArchivePage())

    def test_html_response_is_not_saved_as_audio(self):
        response = FakeResponse(b"<!doctype html><title>Login</title>", "text/html")
        context = FakeContext(response)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "episode"

            with self.assertRaisesRegex(RuntimeError, "non-audio content"):
                sync.download_audio(context, "https://example.test/audio", destination)

            self.assertEqual(list(destination.parent.iterdir()), [])

    def test_valid_mp3_response_is_saved(self):
        response = FakeResponse(b"ID3\x04\x00\x00audio", "audio/mpeg")
        context = FakeContext(response)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "episode"

            result = sync.download_audio(
                context, "https://example.test/audio", destination
            )

            self.assertEqual(result.name, "episode.mp3")
            self.assertEqual(result.read_bytes(), response.body())

    def test_audio_title_period_is_not_treated_as_an_extension(self):
        destination = Path("Predicting fishing vs. not-fishing")

        result = sync.destination_with_extension(destination, ".mp3")

        self.assertEqual(result.name, "Predicting fishing vs. not-fishing.mp3")

    def test_sciencedirect_pdf_candidates_are_derived_from_pii(self):
        candidates = sync.publisher_pdf_candidates(
            "https://www.sciencedirect.com/science/article/pii/S259000562600130X"
        )

        self.assertIn(
            "https://www.sciencedirect.com/science/article/pii/"
            "S259000562600130X/pdfft?isDTMRedir=true&download=true",
            candidates,
        )

    def test_wiley_pdf_candidates_handle_plain_doi_route(self):
        candidates = sync.publisher_pdf_candidates(
            "https://advanced.onlinelibrary.wiley.com/doi/10.1002/aisy.202500833"
        )

        self.assertEqual(
            candidates[0],
            "https://advanced.onlinelibrary.wiley.com/doi/pdfdirect/"
            "10.1002/aisy.202500833",
        )

    def test_incomplete_episode_fails_batch_without_updating_state(self):
        with tempfile.TemporaryDirectory() as directory:
            page = object()
            context = SimpleNamespace(pages=[page], close=Mock())
            args = argparse.Namespace(
                output=Path(directory),
                headless=True,
                papers_only=False,
                latest=7,
            )
            incomplete = sync.EpisodeResult(
                title="Missing audio",
                audio_path=None,
                episode_pdf_path=args.output / "episode.pdf",
                paper_status="shortcut",
            )
            manager = FakePlaywrightContextManager(context)

            with (
                patch.object(sync, "sync_playwright", return_value=manager),
                patch.object(sync, "restore_auth"),
                patch.object(sync, "ensure_login", return_value=["https://episode"]),
                patch.object(sync, "save_auth"),
                patch.object(sync, "load_state", return_value={"synced": []}),
                patch.object(sync, "sync_episode", return_value=incomplete),
                patch.object(sync, "save_state") as save_state,
            ):
                result = sync.run_sync(args)

            self.assertEqual(result, 1)
            save_state.assert_not_called()
            context.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
