"""Shared state flowing through the LangGraph multi-agent pipeline."""

from typing import Optional
from typing_extensions import TypedDict


class AgentState(TypedDict, total=False):
    # Immutable run inputs
    repo: str
    branch: str
    commit_sha: str
    test_logs: str
    workdir: str

    # Ingest products (Phase 1 memory + repo map)
    error_class: str
    blame: dict                 # {path: weight 0..1}
    incidents: list             # past-incident few-shots
    repo_map: dict

    # Router
    fast_path: Optional[dict]   # {'signature', 'target_files'} when fired
    fast_path_used: bool
    demoted_fast_path: bool     # validator demoted the fast path this attempt

    # Triage / localization
    triage_summary: str
    candidate_files: list       # paths the fixer will see in full
    context: dict               # {path: content} loaded for the fixer
    failure_feedback: str       # validator output fed into the retry loop
    last_fix_diff: str          # diff of the last failed attempt — never repeat it

    # Fixer / critic
    diagnosis: str
    fixes: list                 # [{'filename', 'content'}]
    critic_feedback: str
    critic_rounds: int

    # Budgets and outcome
    attempt: int                # validator attempts used
    llm_calls: int
    passed: bool
    test_output: str
    incident_id: Optional[int]
    pr_url: str
    done: str                   # terminal reason: published | gave_up | error
