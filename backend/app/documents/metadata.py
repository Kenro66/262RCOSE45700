"""Issue #45：按 PDF 的 SHA-256 查找文件级适用条件；空值不表示不适用。"""
from datetime import date
import json
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict

class PolicyMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    country: Literal["KR", "CN"] | None = None
    direction: Literal["INBOUND", "OUTBOUND", "BOTH"] | None = None
    effective_date: date | None = None
    applicable_persons: list[Literal[
        "GENERAL_TRAVELER", "RESIDENT_TRAVELER", "NON_RESIDENT_TRAVELER",
        "FREQUENT_TRAVELER", "CREW", "MINOR",
    ]] | None = None

# 五份政策 PDF 的哈希及四项条件保存在同目录的登记文件中。
_REGISTRY = Path(__file__).with_name("policy_registry.json")

def lookup_metadata(sha256: str) -> PolicyMetadata:
    records = json.loads(_REGISTRY.read_text(encoding="utf-8"))
    for record in records:
        if record["sha256"] == sha256:
            return PolicyMetadata.model_validate(record["metadata"])
    # 未匹配时四项均为空，表示未登记或未确认。
    return PolicyMetadata()
