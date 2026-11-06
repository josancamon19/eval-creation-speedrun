from types import SimpleNamespace

import pytest

from generator.training.harbor import (
    byte_encoder,
    checked_reward,
    job_config,
    trajectory_from_mini,
)


class Tokenizer:
    def get_vocab(self):
        return {"Hi": 10, "<|im_end|>": 11}

    def apply_chat_template(self, messages, **kwargs):
        assert all("extra" not in message for message in messages)
        for message in messages:
            for call in message.get("tool_calls") or []:
                assert isinstance(call["function"]["arguments"], dict)
        assert kwargs["tools"][0]["function"]["name"] == "bash"
        return [1, 2, 3]


def recorded_trajectory():
    response = {
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        "choices": [
            {
                "finish_reason": "tool_calls",
                "logprobs": {
                    "content": [
                        {"token": piece, "bytes": list(piece.encode()), "logprob": -0.1}
                        for piece in ["Hi", "<|im_end|>"]
                    ]
                },
            }
        ],
    }
    return {
        "info": {"mini_version": "2.4.6", "exit_status": "Submitted"},
        "messages": [
            {"role": "system", "content": "system", "extra": {}},
            {"role": "user", "content": "fix"},
            {"role": "assistant", "content": "Hi", "extra": {"response": response}},
            {"role": "exit", "content": "done"},
        ],
    }


def test_native_harbor_job_uses_the_installed_mini_swe_agent_and_daytona(tmp_path):
    config = job_config(
        tmp_path, tmp_path / "jobs", "tinker://weights/sampler_weights/1"
    )
    assert "job_name" not in config.model_fields_set
    assert config.environment.type == "daytona" and config.environment.delete
    agent = config.agents[0]
    assert agent.name == "mini-swe-agent" and agent.import_path is None
    assert agent.model_name == "openai/tinker://weights/sampler_weights/1"
    assert agent.kwargs["version"] == "2.4.6"
    assert agent.kwargs["config"] == {"model": {"model_kwargs": {"logprobs": True}}}
    assert agent.kwargs.get("max_tokens") is None
    assert config.n_attempts == 8


def test_training_uses_actual_recorded_tokens_and_keeps_the_full_prompt():
    trajectory = trajectory_from_mini(recorded_trajectory(), Tokenizer())
    assert len(trajectory.transitions) == 1
    transition = trajectory.transitions[0]
    assert transition.ob.to_ints() == [1, 2, 3]
    assert transition.ac.tokens == [10, 11]
    assert transition.ac.logprobs == [-0.1, -0.1]
    assert transition.episode_done


def test_native_mini_connection_accepts_the_tinker_endpoint(tmp_path, monkeypatch):
    from harbor.agents.agent_auth_resolution import resolve_agent_model_connection

    monkeypatch.setenv("TINKER_API_KEY", "test-key")
    config = job_config(
        tmp_path, tmp_path / "jobs", "tinker://weights/sampler_weights/1"
    )
    connection = resolve_agent_model_connection(config.agents[0])
    assert connection.can_configure_base_url


def test_a_prompt_mismatch_or_missing_logprobs_cannot_silently_train():
    data = recorded_trajectory()
    data["messages"][2]["extra"]["response"]["usage"]["prompt_tokens"] = 4
    with pytest.raises(ValueError, match="prompt"):
        trajectory_from_mini(data, Tokenizer())
    data = recorded_trajectory()
    data["messages"][2]["extra"]["response"]["choices"][0]["logprobs"] = None
    with pytest.raises(ValueError, match="logprobs"):
        trajectory_from_mini(data, Tokenizer())


def test_openai_tool_argument_strings_are_parsed_without_mutating_the_record():
    from copy import deepcopy

    data = recorded_trajectory()
    assistant = data["messages"][2]
    assistant["tool_calls"] = [
        {"function": {"name": "bash", "arguments": '{"command":"ls"}'}}
    ]
    data["messages"].insert(
        3, {"role": "tool", "content": "files", "tool_call_id": "call-1"}
    )
    data["messages"].insert(4, deepcopy(assistant))
    before = deepcopy(data)
    trajectory = trajectory_from_mini(data, Tokenizer())
    assert len(trajectory.transitions) == 2
    assert data == before


def test_format_error_responses_still_contribute_the_sampled_action():
    data = recorded_trajectory()
    data["messages"][2]["role"] = "user"
    data["messages"][2]["content"] = "Invalid tool call; try again"
    trajectory = trajectory_from_mini(data, Tokenizer())
    assert trajectory.transitions[0].ac.tokens == [10, 11]


class UnicodeTokenizer(Tokenizer):
    def get_vocab(self):
        alphabet = byte_encoder()
        pieces = {20: b" \xe2\x9c", 21: b"\x85"}
        return super().get_vocab() | {
            "".join(alphabet[b] for b in raw): token for token, raw in pieces.items()
        }

    def apply_chat_template(self, messages, **kwargs):
        prompt = super().apply_chat_template(messages, **kwargs)
        if kwargs["add_generation_prompt"]:
            return prompt
        return [*prompt, 20, 21, 11]

    def decode(self, tokens):
        reverse = {v: k for k, v in byte_encoder().items()}
        vocab = {v: k for k, v in self.get_vocab().items()}
        return bytes(reverse[c] for t in tokens for c in vocab[t]).decode(
            errors="replace"
        )


def unicode_trajectory():
    data = recorded_trajectory()
    response = data["messages"][2]["extra"]["response"]
    response["usage"]["completion_tokens"] = 3
    choice = response["choices"][0]
    choice["message"] = {"role": "assistant", "content": " ✅"}
    choice["logprobs"]["content"] = [
        {"token": text, "bytes": list(text.encode()), "logprob": -0.1}
        for text in [" �", "�", "<|im_end|>"]
    ]
    return data


def test_split_unicode_tokens_are_recovered_from_the_intact_response():
    trajectory = trajectory_from_mini(unicode_trajectory(), UnicodeTokenizer())
    assert trajectory.transitions[0].ac.tokens == [20, 21, 11]
    assert trajectory.transitions[0].ac.logprobs == [-0.1, -0.1, -0.1]


def test_unicode_recovery_rejects_corrupted_text_or_token_boundaries():
    data = unicode_trajectory()
    data["messages"][2]["extra"]["response"]["choices"][0]["message"]["content"] = " �"
    with pytest.raises(ValueError, match="align"):
        trajectory_from_mini(data, UnicodeTokenizer())
    data = unicode_trajectory()
    data["messages"][2]["extra"]["response"]["choices"][0]["logprobs"]["content"][1][
        "bytes"
    ] = [33]
    with pytest.raises(ValueError, match="align"):
        trajectory_from_mini(data, UnicodeTokenizer())


def test_native_zero_scores_are_valid_but_missing_scores_and_errors_fail():
    for reward in (0, 1):
        result = SimpleNamespace(
            exception_info=None,
            verifier_result=SimpleNamespace(rewards={"reward": reward}),
        )
        assert checked_reward(result) == reward
    for reward in (None, float("nan"), -1, 2):
        result = SimpleNamespace(
            exception_info=None,
            verifier_result=SimpleNamespace(rewards={"reward": reward}),
        )
        with pytest.raises(RuntimeError):
            checked_reward(result)
