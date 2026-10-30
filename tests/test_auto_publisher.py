import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

for name in ("APP_KEY", "APP_SECRET", "TRACKING_ID", "TELEGRAM_TOKEN"):
    os.environ.setdefault(name, "test")

from services.auto_publisher import (
    AutoPublisher,
    _due_publications,
    _publication_keyboard,
    _target_chat_id,
)
from core.db import get_auto_publish_report


def _iphone_posts():
    return [
        {
            "id": index,
            "title": f"Apple iPhone 15 Pro {index}GB",
            "price": str(700 - index * 10),
        }
        for index in range(1, 10)
    ]


def test_due_publications_uses_shared_identity_and_3_6_9_thresholds():
    due = _due_publications(_iphone_posts())

    assert [threshold for _key, threshold, _post in due] == [3, 6, 9]
    assert [post["id"] for _key, _threshold, post in due] == [9, 9, 9]


def test_due_publications_does_not_merge_different_products():
    posts = _iphone_posts() + [
        {"id": 20, "title": "Samsung Galaxy S24 128GB", "price": "400"},
        {"id": 21, "title": "Samsung Galaxy S24 256GB", "price": "450"},
        {"id": 22, "title": "Samsung Galaxy S24 512GB", "price": "500"},
    ]

    due = _due_publications(posts)

    assert len(due) == 4
    assert sorted(threshold for _key, threshold, _post in due) == [3, 3, 6, 9]


def test_channel_target_and_buttons_are_configured_for_one_row():
    assert _target_chat_id("https://t.me/deals_channel") == "@deals_channel"
    buttons = _publication_keyboard().inline_keyboard[0]

    assert [button.text for button in buttons] == [
        "تطبيق Offers 365",
        "بوت الذكاء الاصطناعي",
    ]
    assert [button.url for button in buttons] == [
        "https://offres365page.up.railway.app/",
        "https://t.me/rabahcoupons1bot",
    ]


def test_auto_publish_report_counts_published_periods_and_lifetime_total():
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (1, 2, 3, 4, 10, 5, 6, 1)

    with patch("core.db.DATABASE_URL", "postgresql://test"), patch(
        "core.db.get_db_connection",
        return_value=connection,
    ):
        report = get_auto_publish_report()

    assert report == {
        "published_today": 1,
        "published_yesterday": 2,
        "published_this_month": 3,
        "published_last_month": 4,
        "published_total": 10,
        "failed_today": 5,
        "claimed": 6,
        "thresholds_today": 1,
    }
    query = cursor.execute.call_args.args[0]
    assert "published_at::date = CURRENT_DATE - 1" in query
    assert "COUNT(*) FILTER (WHERE status = 'published')" in query
    connection.close.assert_called_once_with()


def test_failed_photo_is_not_marked_published_and_can_be_retried():
    post = _iphone_posts()[2]
    bot = SimpleNamespace(send_photo=AsyncMock())
    application = SimpleNamespace(bot=bot)
    publisher = AutoPublisher()
    publisher._application = application

    with patch(
        "handlers.ai_search._load_post_photo",
        new=AsyncMock(return_value=(None, False)),
    ), patch(
        "services.auto_publisher.mark_auto_publish_delivery_failed",
        return_value=True,
    ), patch(
        "services.auto_publisher.claim_auto_publish_delivery",
        return_value=11,
    ), patch(
        "services.auto_publisher.get_processed_channel_posts_for_auto_publish",
        return_value=[post],
    ), patch(
        "services.auto_publisher.is_feature_enabled",
        return_value=True,
    ), patch(
        "services.auto_publisher.AUTO_PUBLISH_CHANNEL_LINK",
        "https://t.me/deals_channel",
    ):
        import asyncio

        async def exercise():
            await publisher._publish_post(post)

        try:
            asyncio.run(exercise())
        except RuntimeError as error:
            assert "no usable image" in str(error)

    bot.send_photo.assert_not_awaited()


def test_auto_publisher_uses_saved_image_link_and_caches_file_id():
    sent = SimpleNamespace(
        photo=[SimpleNamespace(file_id="telegram-cached-photo-id")],
        message_id=99,
    )
    bot = SimpleNamespace(
        send_photo=AsyncMock(return_value=sent),
        send_message=AsyncMock(),
    )
    publisher = AutoPublisher()
    publisher._application = SimpleNamespace(bot=bot)
    post = {
        "id": 80,
        "title": "عرض محفوظ",
        "content": "عرض محفوظ",
        "photo_file_id": None,
        "link_img": "https://img.example/saved.jpg",
    }

    with patch(
        "services.auto_publisher.AUTO_PUBLISH_CHANNEL_LINK",
        "https://t.me/deals_channel",
    ), patch(
        "handlers.ai_search._cache_photo_file_id",
        new=AsyncMock(),
    ) as cache:
        import asyncio

        result = asyncio.run(publisher._publish_post(post))

    assert result is sent
    assert bot.send_photo.await_args.kwargs["photo"] == post["link_img"]
    cache.assert_awaited_once_with(80, "telegram-cached-photo-id")