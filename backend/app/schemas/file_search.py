"""Explicit literal semantics; phrase is a single literal term, never a DSL string."""
from typing import Literal
from pydantic import BaseModel, Field, model_validator


class FileSearchRequest(BaseModel):
    mode: Literal["literal", "semantic"] = "literal"
    query: str = Field(default="", max_length=500)
    literal_terms: list[str] = Field(min_length=1, max_length=8)
    literal_operator: Literal["all", "any"] = "all"
    fields: list[Literal["name", "path", "body"]] = Field(default_factory=lambda: ["name", "body"], min_length=1)
    prefix: str = Field(default="", max_length=1000)
    page_size: int = Field(default=20, ge=1, le=50)
    candidate_limit: int | None = Field(default=None, ge=1, le=200)

    @model_validator(mode="after")
    def validate_request(self):
        if any(not t or len(t) > 200 for t in self.literal_terms):
            raise ValueError("字面值须为 1—200 个字符，不做隐式空白清理")
        self.literal_terms = list(dict.fromkeys(self.literal_terms))
        self.fields = list(dict.fromkeys(self.fields))
        self.query = self.query.strip()
        if self.mode == "semantic" and not self.query:
            raise ValueError("语义文件发现必须提供完整主题 query")
        self.prefix = self.prefix.strip().lstrip("/")
        if self.prefix and not self.prefix.endswith("/"):
            self.prefix += "/"
        return self
