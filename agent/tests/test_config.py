"""Environment-backed settings: adapting to a target repo must not need a fork."""

import pytest

import config


def test_defaults_apply_with_nothing_set(monkeypatch):
    """A fresh clone with an empty .env has to run."""
    for name in ("VALIDATOR_IMAGE", "VALIDATOR_TIMEOUT", "PR_BASE_BRANCH",
                 "LLM_MODEL", "TRIAGE_MODEL"):
        monkeypatch.delenv(name, raising=False)
    assert config.validator_image() == "python:3.11-slim"
    assert config.validator_timeout() == 300
    assert config.pr_base_branch() == "main"
    assert config.llm_model() == "poolside/laguna-s-2.1:free"


def test_default_model_costs_nothing(monkeypatch):
    """The out-of-the-box model must not require a funded account.

    "Clone it and watch a run" is the whole onboarding story, and it breaks
    the moment the default needs a credit card.
    """
    monkeypatch.delenv("LLM_MODEL", raising=False)
    assert config.llm_model().endswith(":free")


def test_triage_follows_llm_model_when_unset(monkeypatch):
    """Setting only LLM_MODEL must move every node, not most of them."""
    monkeypatch.delenv("TRIAGE_MODEL", raising=False)
    monkeypatch.setenv("LLM_MODEL", "anthropic/claude-opus-5")
    assert config.triage_model() == "anthropic/claude-opus-5"


def test_fallback_defaults_to_a_free_model(monkeypatch):
    monkeypatch.delenv("FALLBACK_LLM_MODEL", raising=False)
    assert config.fallback_llm_model().endswith(":free")


@pytest.mark.parametrize("off", ["none", "off", "disabled", "false", "0", "NONE"])
def test_fallback_disabled_by_an_explicit_word(monkeypatch, off):
    monkeypatch.setenv("FALLBACK_LLM_MODEL", off)
    assert config.fallback_llm_model() == ""


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_does_not_disable_the_fallback(monkeypatch, blank):
    """A stray empty assignment must not quietly remove the safety net.

    _env reads blank as "use the default" everywhere else, and this setting
    should not be the one exception that fails open.
    """
    monkeypatch.setenv("FALLBACK_LLM_MODEL", blank)
    assert config.fallback_llm_model() == config.DEFAULT_FALLBACK_LLM_MODEL


def test_triage_model_overrides_independently(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "anthropic/claude-opus-5")
    monkeypatch.setenv("TRIAGE_MODEL", "anthropic/claude-haiku-4-5-20251001")
    assert config.triage_model() == "anthropic/claude-haiku-4-5-20251001"
    assert config.llm_model() == "anthropic/claude-opus-5"


@pytest.mark.parametrize("name, getter, value", [
    ("VALIDATOR_IMAGE", config.validator_image, "python:3.12-bookworm"),
    ("PR_BASE_BRANCH", config.pr_base_branch, "develop"),
])
def test_override_is_honoured(monkeypatch, name, getter, value):
    monkeypatch.setenv(name, value)
    assert getter() == value


def test_timeout_override_is_an_int(monkeypatch):
    monkeypatch.setenv("VALIDATOR_TIMEOUT", "900")
    assert config.validator_timeout() == 900


@pytest.mark.parametrize("raw", ["", "   ", "\n"])
def test_blank_falls_back_to_default(monkeypatch, raw):
    """`PR_BASE_BRANCH=` in a .env must not mean "the empty branch".

    Blank and whitespace-only assignments are ordinary in hand-written .env
    files, and an empty base branch would fail the PR call after a fix had
    already gone green.
    """
    monkeypatch.setenv("PR_BASE_BRANCH", raw)
    monkeypatch.setenv("VALIDATOR_IMAGE", raw)
    assert config.pr_base_branch() == "main"
    assert config.validator_image() == "python:3.11-slim"


def test_surrounding_whitespace_is_stripped(monkeypatch):
    """A trailing space or newline in a secret store is invisible and common."""
    monkeypatch.setenv("PR_BASE_BRANCH", "  develop\n")
    assert config.pr_base_branch() == "develop"


@pytest.mark.parametrize("raw", ["not-a-number", "0", "-5", "12.5"])
def test_unusable_timeout_falls_back_rather_than_crashing(monkeypatch, raw):
    """A typo here must not take the server down mid-run.

    Zero or negative would make every validation time out instantly, which
    reads as "the fix failed" - quietly wrong in the direction of discarding
    good fixes.
    """
    monkeypatch.setenv("VALIDATOR_TIMEOUT", raw)
    assert config.validator_timeout() == 300
