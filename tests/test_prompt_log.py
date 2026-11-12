"""可选 Prompt 日志测试。"""

from __future__ import annotations

import json

from llm import prompt_log


def test_disabled_prompt_log_writes_nothing(tmp_path):
    prompt_log.configure(False, tmp_path)
    try:
        prompt_log.record("agent_a", messages="hello")
        assert not list(tmp_path.glob("*.jsonl"))
    finally:
        prompt_log.configure(False, None)


def test_prompt_log_records_system_and_messages(tmp_path):
    prompt_log.configure(True, tmp_path)
    try:
        prompt_log.bind_job("job_1")
        prompt_log.record(
            "agent_a",
            phase="work",
            system="SYSTEM-MARKER",
            messages=[
                {"role": "user", "content": "TASK-MARKER"},
                {"role": "assistant", "content": "OK"},
            ],
            task_id="task_1",
            provider="deepseek",
            model="deepseek-flash",
            purpose="work",
        )
    finally:
        prompt_log.clear_job()
        prompt_log.configure(False, None)

    files = list(tmp_path.glob("prompts-*.jsonl"))
    assert len(files) == 1
    record = json.loads(files[0].read_text("utf-8").splitlines()[0])
    assert record["agent_id"] == "agent_a"
    assert record["job_id"] == "job_1"
    assert record["task_id"] == "task_1"
    assert record["phase"] == "work"
    assert record["system"] == "SYSTEM-MARKER"
    assert record["messages"][0]["content"] == "TASK-MARKER"
    assert record["message_count"] == 2


def test_brain_logs_final_messages_and_system(tmp_path):
    from agents.brain import Brain, BrainSettings
    from llm.base import LLMResponse

    class _Router:
        def generate(self, **kwargs):
            return LLMResponse(
                content="ok", provider="mock", model="mock",
                input_tokens=1, output_tokens=1, total_tokens=2)

    prompt_log.configure(True, tmp_path)
    try:
        brain = Brain(
            router=_Router(),
            settings=BrainSettings(
                agent_id="agent_a", provider="mock", model="mock",
                max_tokens=100),
        )
        brain.generate(
            messages="USER-MARKER", system="SYSTEM-MARKER",
            task_id="task_1", phase="work", call_purpose="work",
            cognitive=False,
        )
    finally:
        prompt_log.configure(False, None)

    record = json.loads(
        next(tmp_path.glob("prompts-*.jsonl")).read_text(
            "utf-8").splitlines()[0])
    assert record["agent_id"] == "agent_a"
    assert record["task_id"] == "task_1"
    assert record["phase"] == "work"
    assert record["system"] == "SYSTEM-MARKER"
    assert record["messages"][0]["content"] == "USER-MARKER"
