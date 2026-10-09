import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import uploader


class _UploadRequest:
    def next_chunk(self):
        return None, {"id": "video-id"}


class _VideosResource:
    def __init__(self):
        self.insert_kwargs = None

    def insert(self, **kwargs):
        self.insert_kwargs = kwargs
        return _UploadRequest()


class _YouTubeClient:
    def __init__(self):
        self.videos_resource = _VideosResource()

    def videos(self):
        return self.videos_resource


class UploadToYouTubeTests(unittest.TestCase):
    def setUp(self):
        self.meta = {
            "title": "Test Short #Shorts",
            "description": "Test description",
            "tags": ["Shorts"],
            "categoryId": "24",
            "privacyStatus": "public",
        }

    def _upload(self, meta):
        youtube = _YouTubeClient()
        with tempfile.NamedTemporaryFile(suffix=".mp4") as video:
            with patch.object(uploader, "MediaFileUpload", return_value=object()):
                response = uploader.upload_to_youtube(youtube, Path(video.name), meta)
        return response, youtube.videos_resource.insert_kwargs

    def test_status_defaults_both_audience_settings_to_false(self):
        response, request = self._upload(self.meta)

        self.assertEqual(response, {"id": "video-id"})
        self.assertEqual(request["part"], "snippet,status")
        self.assertEqual(
            request["body"]["status"],
            {
                "privacyStatus": "public",
                "selfDeclaredMadeForKids": False,
                "containsSyntheticMedia": False,
            },
        )

    def test_scheduled_upload_preserves_publish_time_and_boolean_overrides(self):
        publish_at = "2026-10-10T09:00:00-04:00"
        meta = {
            **self.meta,
            "privacyStatus": "unlisted",
            "publishAt": publish_at,
            "selfDeclaredMadeForKids": True,
            "containsSyntheticMedia": True,
        }

        _, request = self._upload(meta)

        self.assertEqual(
            request["body"]["status"],
            {
                "privacyStatus": "private",
                "selfDeclaredMadeForKids": True,
                "containsSyntheticMedia": True,
                "publishAt": publish_at,
            },
        )

    def test_boolean_overrides_reject_strings(self):
        for key in ("selfDeclaredMadeForKids", "containsSyntheticMedia"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(TypeError, rf"^{key} must be a bool"):
                    self._upload({**self.meta, key: "false"})


class UploadJobTitleTests(unittest.TestCase):
    def _run_job(self, file_name, ai_meta=None):
        youtube = _YouTubeClient()
        config = {
            "drive_queue_folder_id": "drive-folder",
            "privacy_status": "public",
        }
        drive_file = {
            "id": "drive-file-id",
            "name": file_name,
            "size": "1024",
        }

        with (
            patch.object(
                uploader,
                "get_google_clients",
                return_value=(youtube, object()),
            ),
            patch.object(uploader, "get_next_drive_video", return_value=drive_file),
            patch.object(uploader, "download_drive_file"),
            patch.object(
                uploader,
                "generate_metadata_with_groq",
                return_value=ai_meta,
            ) as groq,
            patch.object(
                uploader,
                "generate_metadata_with_gemini",
                return_value=None,
            ) as gemini,
            patch.object(uploader, "mark_as_uploaded"),
            patch.object(uploader, "MediaFileUpload", return_value=object()),
        ):
            result = uploader.upload_job(channel_config=config)

        return result, youtube.videos_resource.insert_kwargs, groq, gemini

    def test_drive_filenames_become_exact_titles_without_extensions(self):
        cases = {
            "BMW M340i Is Underrated.mp4": "BMW M340i Is Underrated",
            "Why Audi Quattro Works.mov": "Why Audi Quattro Works",
            "Mercedes AMG vs BMW M.mp4": "Mercedes AMG vs BMW M",
        }

        for file_name, expected_title in cases.items():
            with self.subTest(file_name=file_name):
                result, request, groq, gemini = self._run_job(file_name)
                snippet = request["body"]["snippet"]

                self.assertEqual(result["title"], expected_title)
                self.assertEqual(snippet["title"], expected_title)
                self.assertEqual(
                    snippet["description"],
                    f"{expected_title}\n\n#Cars #Automotive #Shorts",
                )
                self.assertEqual(snippet["categoryId"], "2")
                self.assertEqual(snippet["tags"], ["Cars", "Automotive", "Shorts"])
                self.assertEqual(
                    request["body"]["status"],
                    {
                        "privacyStatus": "public",
                        "selfDeclaredMadeForKids": False,
                        "containsSyntheticMedia": False,
                    },
                )
                groq.assert_called_once()
                self.assertEqual(
                    groq.call_args.kwargs["video_title"],
                    expected_title,
                )
                gemini.assert_called_once()
                self.assertEqual(
                    gemini.call_args.kwargs["video_title"],
                    expected_title,
                )

    def test_ai_metadata_cannot_override_filename_title_or_automotive_category(self):
        ai_meta = {
            "title": "AI Replaced This Title #Shorts",
            "description": "AI automotive description",
            "tags": ["BMW", "M340i"],
            "categoryId": "24",
        }

        result, request, _, gemini = self._run_job(
            "BMW M340i Is Underrated.mp4",
            ai_meta=ai_meta,
        )
        snippet = request["body"]["snippet"]

        self.assertEqual(result["title"], "BMW M340i Is Underrated")
        self.assertEqual(snippet["title"], "BMW M340i Is Underrated")
        self.assertEqual(snippet["description"], "AI automotive description")
        self.assertEqual(snippet["tags"], ["BMW", "M340i"])
        self.assertEqual(snippet["categoryId"], "2")
        gemini.assert_not_called()

    def test_disabled_ai_uses_filename_based_description_without_ai_calls(self):
        youtube = _YouTubeClient()
        file_name = "Why Audi Quattro Works.mov"
        config = {
            "drive_queue_folder_id": "drive-folder",
            "privacy_status": "public",
            "ai_metadata_enabled": False,
        }
        drive_file = {
            "id": "drive-file-id",
            "name": file_name,
            "size": "1024",
        }

        with (
            patch.object(
                uploader,
                "get_google_clients",
                return_value=(youtube, object()),
            ),
            patch.object(uploader, "get_next_drive_video", return_value=drive_file),
            patch.object(uploader, "download_drive_file"),
            patch.object(uploader, "generate_metadata_with_groq") as groq,
            patch.object(uploader, "generate_metadata_with_gemini") as gemini,
            patch.object(uploader, "mark_as_uploaded"),
            patch.object(uploader, "MediaFileUpload", return_value=object()),
        ):
            uploader.upload_job(channel_config=config)

        snippet = youtube.videos_resource.insert_kwargs["body"]["snippet"]
        self.assertEqual(snippet["title"], "Why Audi Quattro Works")
        self.assertEqual(
            snippet["description"],
            "Why Audi Quattro Works\n\n#Cars #Automotive #Shorts",
        )
        groq.assert_not_called()
        gemini.assert_not_called()

    def test_ai_provider_exceptions_use_filename_based_fallback_metadata(self):
        youtube = _YouTubeClient()
        file_name = "Mercedes AMG vs BMW M.mp4"
        config = {
            "drive_queue_folder_id": "drive-folder",
            "privacy_status": "public",
        }
        drive_file = {
            "id": "drive-file-id",
            "name": file_name,
            "size": "1024",
        }

        with (
            patch.object(
                uploader,
                "get_google_clients",
                return_value=(youtube, object()),
            ),
            patch.object(uploader, "get_next_drive_video", return_value=drive_file),
            patch.object(uploader, "download_drive_file"),
            patch.object(
                uploader,
                "generate_metadata_with_groq",
                side_effect=RuntimeError("Groq unavailable"),
            ),
            patch.object(
                uploader,
                "generate_metadata_with_gemini",
                side_effect=RuntimeError("Gemini unavailable"),
            ),
            patch.object(uploader, "mark_as_uploaded"),
            patch.object(uploader, "MediaFileUpload", return_value=object()),
            self.assertLogs(uploader.log, level="WARNING") as logs,
        ):
            result = uploader.upload_job(channel_config=config)

        snippet = youtube.videos_resource.insert_kwargs["body"]["snippet"]
        self.assertEqual(result["title"], "Mercedes AMG vs BMW M")
        self.assertEqual(snippet["title"], "Mercedes AMG vs BMW M")
        self.assertEqual(
            snippet["description"],
            "Mercedes AMG vs BMW M\n\n#Cars #Automotive #Shorts",
        )
        self.assertEqual(snippet["categoryId"], "2")
        self.assertIn("Groq metadata failed", "\n".join(logs.output))
        self.assertIn("Gemini metadata failed", "\n".join(logs.output))

    def test_invalid_filename_titles_are_logged_and_rejected_before_download(self):
        cases = (
            ("   .mp4", "empty YouTube title"),
            (f"{'A' * 101}.mov", "101 characters; maximum is 100"),
        )
        config = {
            "drive_queue_folder_id": "drive-folder",
            "privacy_status": "public",
        }

        for file_name, expected_error in cases:
            with self.subTest(file_name=file_name):
                drive_file = {
                    "id": "drive-file-id",
                    "name": file_name,
                    "size": "1024",
                }
                with (
                    patch.object(
                        uploader,
                        "get_google_clients",
                        return_value=(_YouTubeClient(), object()),
                    ),
                    patch.object(
                        uploader,
                        "get_next_drive_video",
                        return_value=drive_file,
                    ),
                    patch.object(uploader, "download_drive_file") as download,
                    patch.object(uploader, "upload_to_youtube") as upload,
                    self.assertLogs(uploader.log, level="ERROR") as logs,
                ):
                    with self.assertRaisesRegex(ValueError, expected_error):
                        uploader.upload_job(channel_config=config)

                self.assertIn(repr(file_name), "\n".join(logs.output))
                download.assert_not_called()
                upload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
