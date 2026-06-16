from __future__ import annotations

from .formatter import NewsroomFormatter
from .models import DraftArticle, QuarantineItem, RawDocument, ValidationResult, WriterInput
from .validator import NewsroomValidator
from .writer import NewsroomWriter


MAX_REPAIR_ATTEMPTS = 3


class NewsroomRepairService:
    def __init__(self, *, writer: NewsroomWriter | None = None, formatter: NewsroomFormatter | None = None, validator: NewsroomValidator | None = None) -> None:
        self.writer = writer or NewsroomWriter()
        self.formatter = formatter or NewsroomFormatter()
        self.validator = validator or NewsroomValidator()

    def attempt(
        self,
        *,
        draft: DraftArticle,
        validation: ValidationResult,
        writer_input: WriterInput,
        raw_documents: list[RawDocument],
        quarantine_items: list[QuarantineItem],
    ) -> tuple[DraftArticle, ValidationResult]:
        current_draft = draft
        current_validation = validation
        for attempt_number in range(draft.attempt_number + 1, MAX_REPAIR_ATTEMPTS + 1):
            if current_validation.passed:
                return current_draft, current_validation
            current_draft = self.writer.draft(writer_input, attempt_number=attempt_number, compact=True)
            current_draft = self.formatter.format(current_draft, writer_input)
            current_validation = self.validator.validate(current_draft, writer_input, raw_documents, quarantine_items)
            if current_validation.passed:
                return current_draft, current_validation
        return current_draft, current_validation