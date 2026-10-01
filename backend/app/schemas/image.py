"""会话中登记的 SOP 图片身份及稳定展示链接。"""

from pydantic import BaseModel


class SopImageReference(BaseModel):
    skill_id: str
    sop_name: str
    image_id: str
    oss_key: str
    alt: str
    url: str
