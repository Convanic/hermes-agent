"""Gateway integration contracts for the exact release-approval reply."""
from __future__ import annotations

import asyncio

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.run_inbound import GatewayInboundMixin
from gateway.session import SessionSource
from hermes_cli.kanban_release_approval import ApprovalResult


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        user_id="armin-1",
        thread_id="thread-7",
    )


def test_enabled_exact_reply_is_consumed_with_authenticated_route_context(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "hermes_cli.config_effective.load_user_config_effective",
        lambda: {"kanban": {"release_approval": {
            "enabled": True,
            "promotion_argv": ["/opt/bin/promote wrapper", "--fixed"],
        }}},
    )

    def fake_process(**kwargs):
        captured.update(kwargs)
        return ApprovalResult(
            ok=True,
            classification="success",
            release_id="rel-1",
            dev_result="success",
            test_result="success",
            production_result="success",
            active_release_id="rel-1",
            previous_release_id="rel-0",
            rollback_available=True,
        )

    monkeypatch.setattr(
        "hermes_cli.kanban_release_approval.process_current_board_approval",
        fake_process,
    )
    event = MessageEvent(
        text="freigegeben",
        source=_source(),
        reply_to_message_id="visible-message-9",
    )

    handled, reply = asyncio.run(
        GatewayInboundMixin()._hm_release_approval_intercept(event, event.source)
    )

    assert handled is True
    assert reply is not None
    assert "Release-ID: rel-1" in reply
    assert "Vorgänger: rel-0" in reply
    assert "Rollback verfügbar: ja" in reply
    assert captured["promotion_argv"] == ["/opt/bin/promote wrapper", "--fixed"]
    context = captured["context"]
    assert (context.platform, context.chat_id, context.thread_id) == (
        "telegram", "chat-1", "thread-7",
    )
    assert (context.actor_id, context.reply_to_message_id) == (
        "armin-1", "visible-message-9",
    )


def test_non_exact_or_disabled_reply_is_not_consumed(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config_effective.load_user_config_effective",
        lambda: {"kanban": {"release_approval": {"enabled": False}}},
    )
    source = _source()
    mixin = GatewayInboundMixin()

    assert asyncio.run(mixin._hm_release_approval_intercept(
        MessageEvent(text=" freigegeben", source=source), source,
    )) == (False, None)
    assert asyncio.run(mixin._hm_release_approval_intercept(
        MessageEvent(text="freigegeben", source=source), source,
    )) == (False, None)
