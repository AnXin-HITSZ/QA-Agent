from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')
    @field_validator('title',check_fields=False)
    @classmethod
    def nonblank_title(cls,value):
        if not value.strip():raise ValueError('名称不能为空')
        return value.strip()

class Details(Strict):
    amount: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    currency: Literal['CNY'] = 'CNY'
    purpose: str = Field(default='', max_length=1000)
    project: str = Field(default='', max_length=200)
    expense_date: str = Field(default='', max_length=50)
    handler: str = Field(default='', max_length=200)
    policy_ref: str = Field(default='', max_length=500)
    required_materials: list[str] = Field(default_factory=list, max_length=30)

    @field_validator('expense_date')
    @classmethod
    def date(cls,value):
        if value:
            from datetime import date
            if date.fromisoformat(value).isoformat()!=value:raise ValueError('日期须为YYYY-MM-DD')
        return value

    @field_validator('required_materials')
    @classmethod
    def materials(cls, value):
        if any(not x.strip() or len(x)>200 for x in value):
            raise ValueError('材料名称不能为空或超过200字符')
        return list(dict.fromkeys(x.strip() for x in value))

class Create(Strict):
    title: str = Field(min_length=1, max_length=200)
    kind: Literal['reimbursement'] = 'reimbursement'
    details: Details = Field(default_factory=Details)
    request_id: str = Field(min_length=1, max_length=64)

class Edit(Strict):
    revision: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    details: Details
    request_id: str = Field(min_length=1, max_length=64)

class Action(Strict):
    revision: int = Field(ge=1)
    kind: Literal['submit', 'return', 'resubmit', 'approve', 'pay', 'cancel', 'note', 'correct']
    description: str = Field(min_length=1, max_length=4000)
    source_ref: str = Field(default='', max_length=500)
    request_id: str = Field(min_length=1, max_length=64)

class LinkInput(Strict):
    revision: int = Field(ge=1)
    kind: Literal['material', 'task', 'project', 'person']
    title: str = Field(min_length=1, max_length=200)
    # These are labels/references, not arbitrary graph predicates or authority.
    state: Literal['reported', 'provided', 'verified', 'pending', 'done'] = 'reported'
    source_ref: str = Field(default='', max_length=500)
    request_id: str = Field(min_length=1, max_length=64)

    @model_validator(mode='after')
    def allowed_state(self):
        states={'material':{'reported','provided','verified'},'task':{'pending','done'},'project':{'reported'},'person':{'reported'}}
        if self.state not in states[self.kind]:raise ValueError('关联对象状态与类型不符')
        if self.kind=='material' and self.state in ('provided','verified') and not self.source_ref.strip():
            raise ValueError('已提供或已核验材料需填写来源/位置引用')
        return self

class Delete(Strict):
    revision: int = Field(ge=1)
    request_id: str = Field(min_length=1, max_length=64)

class DraftInput(Strict):
    text: str = Field(min_length=1, max_length=6000)
