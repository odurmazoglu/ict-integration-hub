from sqlalchemy.orm import Session

from app.application.workbench.write_authorization_use_cases import (
    CreateWriteAuthorizationUseCase,
    ListWriteAuthorizationsUseCase,
    RevokeWriteAuthorizationUseCase,
)
from app.persistence import (
    SqlAlchemyExecutionSourceInvoiceReader,
    SqlAlchemyReviewRepository,
    SqlAlchemyUnitOfWork,
    SqlAlchemyWriteAuthorizationRepository,
)


def build_create_write_authorization_use_case(*, session: Session) -> CreateWriteAuthorizationUseCase:
    reviews = SqlAlchemyReviewRepository(session)
    return CreateWriteAuthorizationUseCase(
        review_reader=reviews,
        accepted_decision_reader=reviews,
        source_invoice_reader=SqlAlchemyExecutionSourceInvoiceReader(session),
        repository=SqlAlchemyWriteAuthorizationRepository(session),
        unit_of_work=SqlAlchemyUnitOfWork(session),
    )


def build_list_write_authorizations_use_case(*, session: Session) -> ListWriteAuthorizationsUseCase:
    return ListWriteAuthorizationsUseCase(
        repository=SqlAlchemyWriteAuthorizationRepository(session),
        review_reader=SqlAlchemyReviewRepository(session),
    )


def build_revoke_write_authorization_use_case(*, session: Session) -> RevokeWriteAuthorizationUseCase:
    return RevokeWriteAuthorizationUseCase(
        repository=SqlAlchemyWriteAuthorizationRepository(session),
        unit_of_work=SqlAlchemyUnitOfWork(session),
    )
