"""The model layer's invariants: wire-exact reads, additive-API tolerance, access tiers."""

import pytest

from basecradle import ApiObject, Trust, User
from tests.conftest import DASHBOARD_RESPONSE

SELF_FORM = DASHBOARD_RESPONSE["identity"]

# What GET /users/{uuid} returns for someone who trusts you (no self/admin cluster).
DIRECTORY_FORM = {
    "uuid": "019e4b4c-2a17-7c44-9d05-8f3b1e92c6a1",
    "handle": "john",
    "name": "John Doe",
    "kind": "human",
    "trust": {"you_trust": True, "trusts_you": True, "mutual": True},
    "suspended": False,
    "max_timelines": 15,
    "max_participants": 1,
    "max_pending_tasks": 3,
    "about": "Human half of the founding pair.",
    "time_zone": "America/Chicago",
}


class TestWireExactReads:
    def test_fields_mirror_the_json(self):
        user = User(SELF_FORM)
        assert user.uuid == "019e4b4c-3f21-7a90-b5e2-6c1f0a7d3e88"
        assert user.handle == "nova"
        assert user.name == "Nova Digital"
        assert user.kind == "ai"
        assert user.time_zone == "UTC"
        assert user.max_pending_tasks == 3

    def test_null_is_none(self):
        user = User(SELF_FORM)
        assert user.about is None
        assert user.creator is None

    def test_timestamps_stay_raw_iso_strings(self):
        user = User(SELF_FORM)
        assert user.created_at == "2026-01-01T00:00:00.000Z"

    def test_nested_models_wrap_automatically(self):
        user = User(SELF_FORM)
        assert isinstance(user.trust, Trust)
        # SELF_FORM is a self-view: reflexive trust reads all-true.
        assert user.trust.you_trust is True
        assert user.trust.mutual is True


class TestAccessTiers:
    def test_one_user_class_serves_both_forms(self):
        full = User(SELF_FORM)
        slim = User(DIRECTORY_FORM)
        assert full.handle == "nova"
        assert slim.handle == "john"
        # The trusted-peer cluster is present in both of these forms.
        assert slim.about == "Human half of the founding pair."

    def test_absent_field_raises_helpful_attribute_error(self):
        slim = User(DIRECTORY_FORM)  # no self/admin cluster
        with pytest.raises(AttributeError) as exc_info:
            slim.integration_url
        message = str(exc_info.value)
        assert "integration_url" in message
        assert "User" in message
        assert "access-gated" in message

    def test_absent_field_is_not_silently_none(self):
        slim = User(DIRECTORY_FORM)
        assert not hasattr(slim, "visible")


class TestAdditiveApi:
    """The API is additive-only: fields the SDK has never heard of must be readable."""

    def test_unknown_wire_field_is_readable(self):
        user = User({**DIRECTORY_FORM, "pronouns": "it/its"})
        assert user.pronouns == "it/its"

    def test_unknown_nested_field_stays_a_dict(self):
        user = User({**DIRECTORY_FORM, "future_thing": {"a": 1}})
        assert user.future_thing == {"a": 1}


class TestListWrapping:
    """field: list[Model] annotations wrap each element — established for Timeline.participants."""

    def test_list_of_models_wraps_each_element(self):
        class Crew(ApiObject):
            members: list[User]

        crew = Crew({"members": [DIRECTORY_FORM, dict(SELF_FORM)]})

        assert all(isinstance(member, User) for member in crew.members)
        assert [m.handle for m in crew.members] == ["john", "nova"]

    def test_empty_list_stays_empty(self):
        class Crew(ApiObject):
            members: list[User]

        assert Crew({"members": []}).members == []

    def test_unannotated_lists_stay_raw(self):
        user = User({**DIRECTORY_FORM, "tags": ["a", "b"]})
        assert user.tags == ["a", "b"]


class TestClientPropagation:
    """Models built by the client carry it, so resource verbs can call the API."""

    def test_client_reaches_nested_models(self):
        sentinel = object()
        user = User(SELF_FORM, client=sentinel)
        assert user.trust._client is sentinel

    def test_client_reaches_list_elements(self):
        class Crew(ApiObject):
            members: list[User]

        sentinel = object()
        crew = Crew({"members": [DIRECTORY_FORM]}, client=sentinel)
        assert crew.members[0]._client is sentinel

    def test_objects_without_a_client_still_work_for_reads(self):
        user = User(DIRECTORY_FORM)
        assert user.handle == "john"
        assert user._client is None


class TestDunders:
    def test_equality_by_type_and_data(self):
        assert User(DIRECTORY_FORM) == User(DIRECTORY_FORM)
        assert User(DIRECTORY_FORM) != User(SELF_FORM)

    def test_different_types_never_equal(self):
        data = {"you_trust": True, "trusts_you": True, "mutual": True}

        class Imposter(ApiObject):
            pass

        assert Trust(data) != Imposter(data)

    def test_repr_names_type_and_wire_keys_without_values(self):
        user = User(DIRECTORY_FORM)
        assert "User" in repr(user)
        assert "handle" in repr(user)
        # Values stay out of repr — they could be personal.
        assert "John Doe" not in repr(user)

    def test_hashable(self):
        assert len({User(DIRECTORY_FORM), User(DIRECTORY_FORM)}) == 1
