"""User-confirmed memory settings; intentionally excludes geometry, approvals and scripts."""
from pydantic import BaseModel, ConfigDict, Field, model_validator
import re


class BimMemorySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    target_model_path: str = Field(default="", max_length=1024)
    floor_code: str = Field(default="", max_length=64)
    elevation_range: str = Field(default="", max_length=80)
    material_name: str = Field(default="", max_length=256)
    pdf_scale_denominator: float | None = Field(default=None, gt=0, le=100000)

    @model_validator(mode="after")
    def valid_range(self):
        if self.elevation_range:
            number = r"([+-]?(?:\d+(?:\.\d+)?|\.\d+))"
            match = re.fullmatch(rf"\s*{number}\s*(?:m|米)?\s*(?:~|～|至|到)\s*{number}\s*(?:m|米)?\s*",
                                 self.elevation_range.replace("−", "-"), re.IGNORECASE)
            if not match or not 0 < float(match[2]) - float(match[1]) <= 30:
                raise ValueError("标高范围应为底部~顶部（米），层高必须在 0–30 米内")
        return self


class MemoryPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(default="", max_length=2000)
    bim: BimMemorySettings | None = None
