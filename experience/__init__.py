"""
Experience —— Agent 的一次已发生经历的结构化记录。

    from experience import ExperienceStore, Experience

🔒 Experience ≠ Memory ≠ Self Model。
   Experience 是**证据**，不是结论 —— 从证据到结论必须经过 Reflection。
"""

from experience.models import (
    SOURCE_ACTION_RESULT, SOURCE_FEEDBACK, SOURCE_OBSERVATION,
    VALID_SOURCES, Experience, new_experience_id,
)
from experience.store import ExperienceError, ExperienceStore

__all__ = ["ExperienceStore", "ExperienceError", "Experience",
           "SOURCE_ACTION_RESULT", "SOURCE_FEEDBACK", "SOURCE_OBSERVATION",
           "VALID_SOURCES", "new_experience_id"]
