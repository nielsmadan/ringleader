from __future__ import annotations

import base64
import copy
import importlib.util
from pathlib import Path

import pytest
from ringleader.portability.capabilities import NativeRoots


@pytest.fixture
def roots(tmp_path):
    return NativeRoots(
        tmp_path / "home",
        tmp_path / "store",
        tmp_path / "cwd",
        tmp_path / "config",
        tmp_path / "cache",
    )


@pytest.fixture
def smoke():
    spec = importlib.util.spec_from_file_location(
        "opencode_smoke", Path("scripts/smoke-portability-opencode.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def full_model_messages():
    image = base64.b64encode(b"fixture-image").decode()
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "[Historical user instruction; origin pi:source-session at instructions]\nPreserve my formatting.",
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "# AGENTS.md instructions: this is my literal input"},
                {
                    "type": "image",
                    "image": f"data:image/png;base64,{image}",
                    "mediaType": "image/png",
                },
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Checking both."},
                {
                    "type": "tool-call",
                    "toolCallId": "a",
                    "toolName": "read",
                    "input": {"file": "a"},
                },
                {
                    "type": "tool-call",
                    "toolCallId": "b",
                    "toolName": "shell",
                    "input": {"command": "cat b"},
                },
            ],
        },
        {
            "role": "tool",
            "content": [
                {
                    "type": "tool-result",
                    "toolCallId": "a",
                    "toolName": "read",
                    "output": {
                        "type": "content",
                        "value": [
                            {"type": "text", "text": "contents"},
                            {"type": "media", "mediaType": "image/png", "data": image},
                        ],
                    },
                },
                {
                    "type": "tool-result",
                    "toolCallId": "b",
                    "toolName": "shell",
                    "output": {"type": "error-text", "value": "failed"},
                },
            ],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "One succeeded."}]},
    ]


def compact_model_messages():
    messages = full_model_messages()
    messages[2]["content"] = [
        messages[2]["content"][0],
        {"type": "text", "text": "[Omitted 2 tool calls and 2 results: read, shell]"},
    ]
    messages.pop(3)
    return messages


def checkpoint_model_messages():
    return [
        {"role": "user", "content": [{"type": "text", "text": "What did we do so far?"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "We read a."}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Read complete."}]},
        {"role": "user", "content": [{"type": "text", "text": "Continue"}]},
    ]


@pytest.mark.parametrize(
    ("case", "builder"),
    [
        ("full", full_model_messages),
        ("compact", compact_model_messages),
        ("native-compaction", checkpoint_model_messages),
    ],
)
def test_native_context_projection_matches_complete_supported_cases(roots, smoke, case, builder):
    smoke.assert_context(builder(), smoke.context_cases(roots)[case])


@pytest.mark.parametrize(
    "mutation",
    [
        "reverse",
        "instructions",
        "user-media",
        "tool-media",
        "error-flag",
        "output",
        "tool-order",
        "role",
        "extra",
        "call-input",
        "call-id",
        "tool-name",
        "media-bytes",
        "media-mime",
    ],
)
def test_context_smoke_detects_native_preservation_failures(roots, smoke, mutation):
    messages = full_model_messages()
    if mutation == "reverse":
        messages.reverse()
    elif mutation == "instructions":
        messages.pop(0)
    elif mutation == "user-media":
        messages[1]["content"].pop()
    elif mutation == "tool-media":
        messages[3]["content"][0]["output"]["value"].pop()
    elif mutation == "error-flag":
        messages[3]["content"][1]["output"]["type"] = "text"
    elif mutation == "output":
        messages[3]["content"][0]["output"]["value"][0]["text"] = "truncated"
    elif mutation == "tool-order":
        messages[3]["content"].reverse()
    elif mutation == "role":
        messages[-1]["role"] = "user"
    elif mutation == "extra":
        messages.append(copy.deepcopy(messages[-1]))
    elif mutation == "call-input":
        messages[2]["content"][1]["input"] = {"file": "different"}
    elif mutation == "call-id":
        messages[3]["content"][0]["toolCallId"] = "wrong-call"
    elif mutation == "tool-name":
        messages[3]["content"][0]["toolName"] = "wrong-tool"
    elif mutation == "media-bytes":
        messages[3]["content"][0]["output"]["value"][1]["data"] = base64.b64encode(
            b"wrong-image"
        ).decode()
    else:
        messages[3]["content"][0]["output"]["value"][1]["mediaType"] = "image/jpeg"
    with pytest.raises(AssertionError):
        smoke.assert_context(messages, smoke.context_cases(roots)["full"])


def test_context_smoke_detects_missing_omission_marker(roots, smoke):
    messages = compact_model_messages()
    messages[2]["content"].pop()
    with pytest.raises(AssertionError):
        smoke.assert_context(messages, smoke.context_cases(roots)["compact"])


@pytest.mark.parametrize("mutation", ["discarded-history", "retained-order"])
def test_context_smoke_checks_checkpoint_exclusion_and_retained_order(roots, smoke, mutation):
    messages = checkpoint_model_messages()
    if mutation == "discarded-history":
        messages.insert(
            0, {"role": "user", "content": [{"type": "text", "text": "Read the full file"}]}
        )
    else:
        messages[1], messages[2] = messages[2], messages[1]
    with pytest.raises(AssertionError):
        smoke.assert_context(messages, smoke.context_cases(roots)["native-compaction"])
