# Описывает поля, которые можно отдать клиенту о задании и жалобе. Явная форма отделяет видимые
# сведения от служебных полей хранения и обработки.

from pydantic import BaseModel, ConfigDict

from app.Enums.moderation import ReportDecision, ReportReason
from app.Services.Generation.job_snapshots import JobSnapshot


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    operation_key: str
    batch_id: str | None = None
    batch_index: int | None = None
    removed_output_id: str | None = None
    thread_id: str
    prompt: str
    model: str
    aspect: str
    seed: int
    input_id: str | None
    output_id: str | None
    output_preview: str | None = None
    status: str
    error: str | None
    moderated: bool = False
    provider: str
    created_at: float
    started_at: float | None = None
    progress: int | None = None
    run_id: str | None = None
    quality: str = "standard"
    resolution: str = "standard"
    duration_seconds: int = 5
    negative_prompt: str = ""
    guidance_scale: float | None = None


class GenerationReportResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    job_id: str
    reason: ReportReason
    created_at: float
    decision: ReportDecision | None


def job_response(snapshot: JobSnapshot) -> JobResponse:
    """Выбирает открытые клиенту поля и добавляет маленькую обложку."""
    response = JobResponse.model_validate(snapshot.job)
    response.output_preview = snapshot.preview
    return response
