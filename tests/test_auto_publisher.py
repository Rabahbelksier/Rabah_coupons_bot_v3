import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

for name in ("APP_KEY", "APP_SECRET", "TRACKING_ID", "TELEGRAM_TOKEN"):
    os.environ.setdefault(name, "test")

from services.auto_publisher import (
    AutoPublisher,
    _due_publications,
    _newest_post,
    _publication_keyboard,
    _target_chat_id,
)
from handlers.ai_search import PhotoDeliveryUncertainError
from core.db import claim_auto_publish_delivery, get_auto_publish_report


def _iphone_posts(count=9):
    return [
        {
            "id": index,
            "title": f"Apple iPhone 15 Pro {index}GB",
            "price": str(700 - index * 10),
        }
        for index in range(1, count + 1)
    ]


def test_due_publications_uses_five_post_thresholds():
    due = _due_publications(_iphone_posts(17))

    assert [threshold for _key, threshold, _post in due] == [5, 10, 15]
    assert [post["id"] for _key, _threshold, post in due] == [17, 17, 17]


def test_newest_post_wins_even_when_it_costs_more():
    posts = [
        {
            "id": 1,
            "title": "Apple iPhone 15 Pro 128GB",
            "price": "300",
            "published_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        },
        {
            "id": 2,
            "title": "Apple iPhone 15 Pro 256GB",
            "price": "300",
            "published_at": datetime(2025, 2, 1, tzinfo=timezone.utc),
        },
        {
            "id": 3,
            "title": "Apple iPhone 15 Pro 512GB",
            "price": "450",
            "published_at": datetime(2025, 3, 1, tzinfo=timezone.utc),
        },
    ]

    assert _newest_post(posts)["id"] == 3


def test_due_publications_does_not_merge_different_products():
    posts = _iphone_posts(5) + [
        {
            "id": 20 + index,
            "title": f"Samsung Galaxy S24 {storage}GB",
            "price": str(400 + index),
        }
        for index, storage in enumerate(
            (128, 256, 512, 1024, 2048),
            start=1,
        )
    ]

    due = _due_publications(posts)

    assert len(due) == 2
    assert sorted(threshold for _key, threshold, _post in due) == [5, 5]
    assert due[0][0] != due[1][0]


def test_auto_publish_daily_limit_records_due_threshold_as_skipped():
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.side_effect = [None, (2,), None]

    with patch("core.db.DATABASE_URL", "postgresql://test"), patch(
        "core.db.get_db_connection",
        return_value=connection,
    ):
        delivery_id = claim_auto_publish_delivery("model:apple:iphone:15", 15, 9)

    assert delivery_id is None
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert any("FOR UPDATE" in statement for statement in statements)
    assert any("skipped_daily_limit" in statement for statement in statements)
    assert not any(
        "VALUES (%s, %s, %s, 'claimed')" in statement
        for statement in statements
    )
    connection.commit.assert_called_once_with()


def test_auto_publish_allows_claim_below_daily_limit():
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.side_effect = [(9,), (1,), (42,)]

    with patch("core.db.DATABASE_URL", "postgresql://test"), patch(
        "core.db.get_db_connection",
        return_value=connection,
    ):
        delivery_id = claim_auto_publish_delivery("model:apple:iphone:15", 20, 9)

    assert delivery_id == 42
    assert "VALUES (%s, %s, %s, 'claimed')" in (
        cursor.execute.call_args_list[-1].args[0]
    )
    connection.commit.assert_called_once_with()


def test_auto_publish_uncertain_send_is_not_released_for_retry():
    publisher = AutoPublisher()
    publisher._application = SimpleNamespace(bot=SimpleNamespace())

    with patch(
        "services.auto_publisher.claim_auto_publish_delivery",
        return_value=27,
    ), patch(
        "services.auto_publisher.mark_auto_publish_delivery_sending",
        return_value=True,
    ) as mark_sending, patch(
        "services.auto_publisher.mark_auto_publish_delivery_uncertain",
        return_value=True,
    ) as mark_uncertain, patch(
        "services.auto_publisher.mark_auto_publish_delivery_failed",
    ) as mark_failed, patch(
        "services.auto_publisher.mark_auto_publish_delivery_published",
    ) as mark_published, patch(
        "services.auto_publisher.get_processed_channel_posts_for_auto_publish",
        return_value=_iphone_posts(5),
    ), patch(
        "services.auto_publisher.is_feature_enabled",
        return_value=True,
    ), patch(
        "services.auto_publisher.AUTO_PUBLISH_CHANNEL_LINK",
        "https://t.me/deals_channel",
    ), patch.object(
        publisher,
        "_publish_post",
        new=AsyncMock(
            side_effect=PhotoDeliveryUncertainError(
                "Telegram response was lost"
            )
        ),
    ):
        import asyncio

        asyncio.run(publisher.run_once())

    mark_sending.assert_called_once_with(27)
    mark_uncertain.assert_called_once_with(27)
    mark_failed.assert_not_called()
    mark_published.assert_not_called()


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
    cursor.fetchone.return_value = (1, 2, 3, 4, 10, 5, 6, 1, 2)

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
        "uncertain": 2,
    }
    query = cursor.execute.call_args.args[0]
    assert "published_at::date = CURRENT_DATE - 1" in query
    assert "COUNT(*) FILTER (WHERE status = 'published')" in query
    assert "status IN ('claimed', 'sending')" in query
    assert "COUNT(*) FILTER (WHERE status = 'uncertain')" in query
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


def test_long_caption_failure_does_not_retry_a_successful_photo():
    sent = SimpleNamespace(
        photo=[SimpleNamespace(file_id="telegram-cached-photo-id")],
        message_id=100,
    )
    bot = SimpleNamespace(
        send_photo=AsyncMock(return_value=sent),
        send_message=AsyncMock(side_effect=TimeoutError("reply timed out")),
    )
    publisher = AutoPublisher()
    publisher._application = SimpleNamespace(bot=bot)
    post = {
        "id": 81,
        "content": "عرض طويل " * 300,
        "photo_file_id": "cached-photo",
    }

    with patch(
        "services.auto_publisher.AUTO_PUBLISH_CHANNEL_LINK",
        "https://t.me/deals_channel",
    ), patch(
        "services.auto_publisher._send_post_photo_with_fallback",
        new=AsyncMock(return_value=sent),
    ):
        import asyncio

        result = asyncio.run(publisher._publish_post(post))

    assert result is sent
    bot.send_photo.assert_not_awaited()
    bot.send_message.assert_awaited_once()