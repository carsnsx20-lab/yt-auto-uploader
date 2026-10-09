import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import uploader


DEFAULT_AUTOMOTIVE_TAGS = [
    "cars",
    "automotive",
    "car culture",
    "car enthusiast",
    "car facts",
    "car shorts",
    "vehicles",
    "auto",
    "The Auto Pit",
]


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
        cases = (
            (
                "BMW M340i Is Underrated.mp4",
                "BMW M340i Is Underrated",
                {"BMW", "BMW cars", "BMW performance", "BMW M340i"},
            ),
            (
                "Why Audi Quattro Works.mov",
                "Why Audi Quattro Works",
                {"Audi", "Audi cars", "Audi performance", "Audi Quattro", "AWD"},
            ),
            (
                "Porsche 911 at the Track.mp4",
                "Porsche 911 at the Track",
                {"Porsche", "Porsche cars"},
            ),
            (
                "Completely Unrelated Subject.mp4",
                "Completely Unrelated Subject",
                set(),
            ),
        )

        for file_name, expected_title, expected_specific_tags in cases:
            with self.subTest(file_name=file_name):
                result, request, groq, gemini = self._run_job(file_name)
                snippet = request["body"]["snippet"]

                self.assertEqual(result["title"], expected_title)
                self.assertEqual(snippet["title"], expected_title)
                self.assertEqual(
                    snippet["description"],
                    (
                        f"{expected_title}\n\n"
                        "Automotive content featuring cars, performance, technology, "
                        "and car culture.\n\n"
                        "Subscribe to The Auto Pit for automotive Shorts.\n\n"
                        "#Shorts #Cars #Automotive"
                    ),
                )
                self.assertEqual(snippet["categoryId"], "2")
                self.assertEqual(
                    snippet["tags"][:len(DEFAULT_AUTOMOTIVE_TAGS)],
                    DEFAULT_AUTOMOTIVE_TAGS,
                )
                self.assertTrue(expected_specific_tags.issubset(snippet["tags"]))
                if not expected_specific_tags:
                    self.assertEqual(snippet["tags"], DEFAULT_AUTOMOTIVE_TAGS)
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
            "tags": ["storytime", "relationship drama"],
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
        self.assertEqual(
            snippet["tags"][:len(DEFAULT_AUTOMOTIVE_TAGS)],
            DEFAULT_AUTOMOTIVE_TAGS,
        )
        self.assertIn("BMW performance", snippet["tags"])
        self.assertNotIn("storytime", snippet["tags"])
        self.assertNotIn("relationship drama", snippet["tags"])
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
            (
                "Why Audi Quattro Works\n\n"
                "Automotive content featuring cars, performance, technology, "
                "and car culture.\n\n"
                "Subscribe to The Auto Pit for automotive Shorts.\n\n"
                "#Shorts #Cars #Automotive"
            ),
        )
        self.assertIn("Audi performance", snippet["tags"])
        self.assertIn("Audi Quattro", snippet["tags"])
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
            (
                "Mercedes AMG vs BMW M\n\n"
                "Automotive content featuring cars, performance, technology, "
                "and car culture.\n\n"
                "Subscribe to The Auto Pit for automotive Shorts.\n\n"
                "#Shorts #Cars #Automotive"
            ),
        )
        self.assertEqual(snippet["categoryId"], "2")
        self.assertIn("Groq metadata failed", "\n".join(logs.output))
        self.assertIn("Gemini metadata failed", "\n".join(logs.output))

    def test_invalid_ai_payload_uses_local_automotive_metadata(self):
        result, request, _, gemini = self._run_job(
            "Porsche 911 Explained.mp4",
            ai_meta=["not", "an", "object"],
        )
        snippet = request["body"]["snippet"]

        self.assertEqual(result["title"], "Porsche 911 Explained")
        self.assertEqual(
            snippet["description"],
            (
                "Porsche 911 Explained\n\n"
                "Automotive content featuring cars, performance, technology, "
                "and car culture.\n\n"
                "Subscribe to The Auto Pit for automotive Shorts.\n\n"
                "#Shorts #Cars #Automotive"
            ),
        )
        self.assertIn("Porsche", snippet["tags"])
        gemini.assert_not_called()

    def test_keyword_mapping_is_configurable_and_deduplicated(self):
        config = {
            "automotive_metadata": {
                "keyword_tags": {
                    "miata": ["Mazda MX-5", "roadster", "cars", "ROADSTER"],
                    "track": ["track cars", "motorsport"],
                },
            },
        }

        tags = uploader.build_video_tags("Track Ready Miata.mp4", config)

        self.assertEqual(
            tags[:len(DEFAULT_AUTOMOTIVE_TAGS)],
            DEFAULT_AUTOMOTIVE_TAGS,
        )
        self.assertIn("Mazda MX-5", tags)
        self.assertIn("roadster", tags)
        self.assertIn("track cars", tags)
        self.assertEqual(
            len(tags),
            len({tag.casefold() for tag in tags}),
        )

    def test_required_brand_keyword_mappings(self):
        cases = {
            "Mercedes AMG vs BMW M": {
                "Mercedes-Benz",
                "Mercedes AMG",
                "BMW",
                "BMW cars",
                "BMW performance",
            },
            "Toyota Reliability Explained": {"Toyota", "Toyota cars"},
            "BMW_M340i": {"BMW", "BMW M340i"},
            "Audi_Quattro": {"Audi", "Audi Quattro", "AWD"},
        }

        for title, expected_tags in cases.items():
            with self.subTest(title=title):
                tags = uploader.build_video_tags(title, {})
                self.assertTrue(expected_tags.issubset(tags))

    def test_ambiguous_unrelated_keywords_do_not_add_automotive_tags(self):
        titles = (
            "Cats vs Dogs",
            "Emergency Call 911",
            "New Music Track",
            "Employee Manual",
            "Electric Guitar Basics",
        )

        for title in titles:
            with self.subTest(title=title):
                self.assertEqual(
                    uploader.build_video_tags(title, {}),
                    DEFAULT_AUTOMOTIVE_TAGS,
                )

    def test_tag_builder_respects_youtube_character_limit_and_tag_cap(self):
        long_tags = [
            f"BMW detailed performance topic number {index}"
            for index in range(50)
        ]
        config = {
            "automotive_metadata": {
                "default_tags": [*DEFAULT_AUTOMOTIVE_TAGS, "CARS"],
                "max_tags": 100,
                "keyword_tags": {
                    "bmw": ["BMW", *long_tags],
                },
            },
        }

        tags = uploader.build_video_tags("BMW Performance.mp4", config)

        self.assertEqual(
            tags[:len(DEFAULT_AUTOMOTIVE_TAGS)],
            DEFAULT_AUTOMOTIVE_TAGS,
        )
        self.assertLessEqual(
            uploader._youtube_tag_character_count(tags),
            uploader.YOUTUBE_TAG_CHARACTER_LIMIT,
        )
        self.assertEqual(
            len(tags),
            len({tag.casefold() for tag in tags}),
        )

    def test_automotive_prompts_contain_no_legacy_story_metadata(self):
        prompts = f"{uploader._VISION_PROMPT}\n{uploader._TEXT_ONLY_PROMPT}".casefold()
        forbidden = (
            "text-message",
            "text message",
            "minecraft",
            "relationship",
            "cheating",
            "betrayal",
            "storytime",
            "story reel",
        )

        self.assertIn("automotive", prompts)
        self.assertNotIn('"title"', prompts)
        self.assertNotIn('"tags"', prompts)
        for term in forbidden:
            with self.subTest(term=term):
                self.assertNotIn(term, prompts)

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
