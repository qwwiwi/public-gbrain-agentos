"""Offline tests for media marker formatting (no Telegram calls).

Run: .venv/bin/python -m unittest test_media_info -v
"""

import unittest
from datetime import datetime

from telethon.tl.types import (
    Document,
    DocumentAttributeAnimated,
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeSticker,
    DocumentAttributeVideo,
    InputStickerSetEmpty,
    MessageMediaContact,
    MessageMediaDocument,
    MessageMediaGeo,
    MessageMediaPhoto,
    MessageMediaPoll,
    Photo,
    PhotoSize,
    Poll,
    PollAnswer,
    PollResults,
    TextWithEntities,
    GeoPoint,
)

from media_info import (
    describe_media,
    format_duration,
    format_size,
    media_marker,
    message_text_with_marker,
)


def _doc(attrs, mime="application/octet-stream", size=1000):
    return MessageMediaDocument(
        document=Document(
            id=1, access_hash=1, file_reference=b"", date=datetime(2026, 1, 1),
            mime_type=mime, size=size, dc_id=2, attributes=attrs,
        )
    )


class FormatTests(unittest.TestCase):
    def test_size(self):
        self.assertEqual(format_size(512), "512 B")
        self.assertEqual(format_size(340 * 1024), "340 KB")
        self.assertEqual(format_size(int(1.2 * 1024 * 1024)), "1.2 MB")
        self.assertEqual(format_size(None), "?")

    def test_duration(self):
        self.assertEqual(format_duration(10), "0:10")
        self.assertEqual(format_duration(95), "1:35")
        self.assertEqual(format_duration(3725), "1:02:05")


class MarkerTests(unittest.TestCase):
    def test_pdf(self):
        m = _doc([DocumentAttributeFilename("report.pdf")], "application/pdf", int(1.2 * 1024 * 1024))
        info = describe_media(m)
        self.assertEqual(info.kind, "document")
        self.assertEqual(info.file_name, "report.pdf")
        self.assertEqual(media_marker(info), "[файл: report.pdf, 1.2 MB]")

    def test_voice(self):
        m = _doc([DocumentAttributeAudio(duration=10, voice=True)], "audio/ogg")
        self.assertEqual(media_marker(describe_media(m)), "[голосовое 0:10]")
        self.assertEqual(describe_media(m).duration, 10)

    def test_video_note(self):
        m = _doc([DocumentAttributeVideo(duration=12.0, w=384, h=384, round_message=True)], "video/mp4")
        self.assertEqual(media_marker(describe_media(m)), "[кружок 0:12]")

    def test_video(self):
        m = _doc([DocumentAttributeVideo(duration=35.4, w=1280, h=720)], "video/mp4")
        self.assertEqual(media_marker(describe_media(m)), "[видео 0:35]")

    def test_gif(self):
        m = _doc([DocumentAttributeAnimated(), DocumentAttributeVideo(duration=2, w=1, h=1)], "video/mp4")
        self.assertEqual(media_marker(describe_media(m)), "[GIF]")

    def test_sticker(self):
        m = _doc([DocumentAttributeSticker(alt="", stickerset=InputStickerSetEmpty())], "image/webp")
        self.assertEqual(media_marker(describe_media(m)), "[стикер]")

    def test_audio(self):
        m = _doc([DocumentAttributeAudio(duration=61, title="Song", performer="Band")], "audio/mpeg")
        self.assertEqual(media_marker(describe_media(m)), "[аудио: Band – Song, 1:01]")

    def test_photo(self):
        photo = Photo(id=1, access_hash=1, file_reference=b"", date=datetime(2026, 1, 1),
                      sizes=[PhotoSize(type="x", w=800, h=600, size=54321)], dc_id=2)
        info = describe_media(MessageMediaPhoto(photo=photo))
        self.assertEqual(media_marker(info), "[фото]")
        self.assertEqual(info.size, 54321)

    def test_poll(self):
        poll = Poll(id=1, question=TextWithEntities(text="Когда эфир?", entities=[]),
                    answers=[PollAnswer(text=TextWithEntities(text="Сегодня", entities=[]), option=b"0")], hash=0)
        m = MessageMediaPoll(poll=poll, results=PollResults())
        self.assertEqual(media_marker(describe_media(m)), "[опрос: Когда эфир?]")

    def test_geo_and_contact(self):
        self.assertEqual(media_marker(describe_media(MessageMediaGeo(geo=GeoPoint(long=1.0, lat=2.0, access_hash=0)))), "[геолокация]")
        c = MessageMediaContact(phone_number="", first_name="Иван", last_name="Петров", vcard="", user_id=0)
        self.assertEqual(media_marker(describe_media(c)), "[контакт: Иван Петров]")


class LineTextTests(unittest.TestCase):
    def test_caption_appended(self):
        m = _doc([DocumentAttributeFilename("a.pdf")], size=2048)
        self.assertEqual(message_text_with_marker("Смотри договор", m), "[файл: a.pdf, 2 KB] Смотри договор")

    def test_media_without_caption(self):
        m = _doc([DocumentAttributeAudio(duration=10, voice=True)])
        self.assertEqual(message_text_with_marker("", m), "[голосовое 0:10]")

    def test_plain_text_unchanged(self):
        self.assertEqual(message_text_with_marker("привет", None), "привет")
        self.assertIsNone(message_text_with_marker(None, None))


if __name__ == "__main__":
    unittest.main()
