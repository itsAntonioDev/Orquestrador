"""Testes do adaptador de webhooks do GitHub."""

from __future__ import annotations

from typing import Any

import pytest

from orquestrador.projects import Provider
from orquestrador.webhooks import EventIgnored, InvalidPayload, github

SECRET = "segredo-super-secreto"
SHA = "a" * 40


def push_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ref": "refs/heads/main",
        "after": SHA,
        "deleted": False,
        "repository": {
            "full_name": "acme/api",
            "clone_url": "https://github.com/acme/api.git",
        },
        "sender": {"login": "octocat"},
        "pusher": {"name": "octo"},
        "head_commit": {"message": "corrige bug"},
    }
    payload.update(overrides)
    return payload


def pr_payload(action: str = "opened") -> dict[str, Any]:
    return {
        "action": action,
        "number": 42,
        "repository": {"full_name": "acme/api", "clone_url": "https://github.com/acme/api.git"},
        "sender": {"login": "contribuidor"},
        "pull_request": {
            "number": 42,
            "title": "Nova feature",
            "head": {
                "ref": "feature/x",
                "sha": "b" * 40,
                "repo": {"clone_url": "https://github.com/fork/api.git"},
            },
            "base": {"ref": "main"},
        },
    }


class TestSignature:
    def test_valid_signature(self) -> None:
        body = b'{"hello": "world"}'
        assert github.verify_signature(SECRET, body, github.compute_signature(SECRET, body))

    def test_signature_is_known_hmac_sha256(self) -> None:
        # Example test vector from the GitHub documentation.
        signature = github.compute_signature("It's a Secret to Everybody", b"Hello, World!")
        assert signature == (
            "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"
        )

    @pytest.mark.parametrize(
        "signature",
        [None, "", "sha256=deadbeef", "sha1=abc", github.compute_signature("outro", b"{}")],
    )
    def test_invalid_signatures(self, signature: str | None) -> None:
        assert not github.verify_signature(SECRET, b"{}", signature)

    def test_tampered_body(self) -> None:
        signature = github.compute_signature(SECRET, b'{"a": 1}')
        assert not github.verify_signature(SECRET, b'{"a": 2}', signature)

    def test_empty_secret_never_validates(self) -> None:
        assert not github.verify_signature("", b"{}", github.compute_signature("", b"{}"))

    def test_adapter_reads_header(self) -> None:
        body = b"{}"
        headers = {github.SIGNATURE_HEADER: github.compute_signature(SECRET, body)}
        assert github.ADAPTER.verify(SECRET, body, headers)
        assert not github.ADAPTER.verify(SECRET, body, {})


class TestParsePush:
    def test_branch_push(self) -> None:
        event = github.parse_event("push", push_payload(), "delivery-1")
        assert event.provider == Provider.GITHUB
        assert event.event == "push"
        assert event.repository == "acme/api"
        assert event.branch == "main"
        assert event.tag is None
        assert event.commit == SHA
        assert event.ref == "refs/heads/main"
        assert event.actor == "octocat"
        assert event.message == "corrige bug"
        assert event.clone_url == "https://github.com/acme/api.git"
        assert event.delivery_id == "delivery-1"

    def test_tag_push(self) -> None:
        event = github.parse_event("push", push_payload(ref="refs/tags/v1.2.0"))
        assert event.tag == "v1.2.0"
        assert event.branch is None

    def test_actor_falls_back_to_pusher(self) -> None:
        event = github.parse_event("push", push_payload(sender=None))
        assert event.actor == "octo"

    def test_deleted_branch_is_ignored(self) -> None:
        with pytest.raises(EventIgnored, match="remoção"):
            github.parse_event("push", push_payload(deleted=True))

    @pytest.mark.parametrize("missing", ["ref", "after"])
    def test_missing_fields(self, missing: str) -> None:
        payload = push_payload()
        del payload[missing]
        with pytest.raises(InvalidPayload, match=missing):
            github.parse_event("push", payload)

    def test_missing_repository(self) -> None:
        with pytest.raises(InvalidPayload):
            github.parse_event("push", push_payload(repository={}))


class TestParsePullRequest:
    @pytest.mark.parametrize("action", sorted(github.PULL_REQUEST_ACTIONS))
    def test_relevant_actions(self, action: str) -> None:
        event = github.parse_event("pull_request", pr_payload(action))
        assert event.event == "pull_request"
        assert event.commit == "b" * 40
        assert event.branch == "feature/x"
        assert event.base_branch == "main"
        assert event.pull_request == 42
        assert event.ref == "refs/pull/42/head"
        assert event.clone_url == "https://github.com/fork/api.git"
        assert event.message == "Nova feature"
        assert event.actor == "contribuidor"

    @pytest.mark.parametrize("action", ["closed", "labeled", "edited", None])
    def test_irrelevant_actions_are_ignored(self, action: str | None) -> None:
        payload = pr_payload()
        payload["action"] = action
        with pytest.raises(EventIgnored):
            github.parse_event("pull_request", payload)

    def test_missing_pull_request_object(self) -> None:
        payload = pr_payload()
        del payload["pull_request"]
        with pytest.raises(InvalidPayload):
            github.parse_event("pull_request", payload)

    def test_missing_head_sha(self) -> None:
        payload = pr_payload()
        del payload["pull_request"]["head"]["sha"]
        with pytest.raises(InvalidPayload, match=r"head\.sha"):
            github.parse_event("pull_request", payload)


def test_unsupported_event_is_ignored() -> None:
    with pytest.raises(EventIgnored, match="issues"):
        github.parse_event("issues", {})


def test_repository_name() -> None:
    assert github.repository_name(push_payload()) == "acme/api"
    assert github.repository_name({}) is None
    assert github.repository_name({"repository": "nao-e-dict"}) is None
