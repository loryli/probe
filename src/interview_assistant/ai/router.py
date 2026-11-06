from fastapi import APIRouter

from interview_assistant.ai.qwen import configuration_status
from interview_assistant.api.dependencies import SettingsDependency

router = APIRouter(prefix="/ai", tags=["ai"])


@router.get("/status", summary="读取本地模型配置状态，不调用模型，不返回密钥")
async def model_status(settings: SettingsDependency) -> dict[str, object]:
    return configuration_status(settings)
