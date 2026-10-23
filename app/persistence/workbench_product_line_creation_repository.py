from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.application.workbench.exceptions import (
    ProductRemediationDataIntegrityError,
    ProductRemediationError,
    ProductRemediationRaceError,
)
from app.application.workbench.product_line_creation import (
    NewProductIntent,
    NewProductKind,
    ProductLineCreation,
    ProductLineCreationStatus,
)
from app.models.workbench_product_line_creation import WorkbenchProductLineCreation

SAFE_PRODUCT_LINE_CREATION_PERSISTENCE_ERROR = "Product line creation persistence operation failed."


class SqlAlchemyProductLineCreationRepository:
    """PR D: durable creation state per review line (insert once, compare-and-swap after)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def find(
        self, *, review_id: str, company_id: int, review_version: int, line_number: str
    ) -> ProductLineCreation | None:
        try:
            record = self._find(
                review_id=review_id, company_id=company_id, review_version=review_version, line_number=line_number
            )
        except SQLAlchemyError as exc:
            raise ProductRemediationError(SAFE_PRODUCT_LINE_CREATION_PERSISTENCE_ERROR) from exc
        return _creation(record) if record is not None else None

    def insert_attempt(self, creation: ProductLineCreation) -> ProductLineCreation:
        if (
            not isinstance(creation, ProductLineCreation)
            or creation.status is not ProductLineCreationStatus.CREATE_ATTEMPTED
        ):
            raise ProductRemediationDataIntegrityError("Only a create_attempted product line creation can be inserted.")
        record = _model(creation)
        try:
            with self._session.begin_nested():
                self._session.add(record)
                self._session.flush()
                self._session.refresh(record)
        except IntegrityError as exc:
            raise ProductRemediationRaceError(
                "Another request is already creating a product for this review line."
            ) from exc
        except SQLAlchemyError as exc:
            raise ProductRemediationError(SAFE_PRODUCT_LINE_CREATION_PERSISTENCE_ERROR) from exc
        return _creation(record)

    def advance(
        self,
        creation: ProductLineCreation,
        *,
        expected_status: ProductLineCreationStatus,
        new_status: ProductLineCreationStatus,
        product_template_id: int | None = None,
        product_id: int | None = None,
        supplierinfo_id: int | None = None,
    ) -> ProductLineCreation:
        try:
            record = self._find(
                review_id=creation.review_id,
                company_id=creation.company_id,
                review_version=creation.review_version,
                line_number=creation.line_number,
            )
            if record is None:
                raise ProductRemediationDataIntegrityError("The product line creation to advance no longer exists.")
            if str(record.status) != expected_status.value:
                raise ProductRemediationDataIntegrityError(
                    "The product line creation status changed unexpectedly; refusing to advance."
                )
            record.status = new_status.value
            if product_template_id is not None:
                record.product_template_id = product_template_id
            if product_id is not None:
                record.product_id = product_id
            if supplierinfo_id is not None:
                record.supplierinfo_id = supplierinfo_id
            self._session.flush()
            self._session.refresh(record)
            return _creation(record)
        except ProductRemediationError:
            raise
        except SQLAlchemyError as exc:
            raise ProductRemediationError(SAFE_PRODUCT_LINE_CREATION_PERSISTENCE_ERROR) from exc

    def discard_attempt(self, creation: ProductLineCreation) -> None:
        model = WorkbenchProductLineCreation
        try:
            result = self._session.execute(
                delete(model)
                .where(
                    model.review_id == creation.review_id,
                    model.company_id == creation.company_id,
                    model.review_version == creation.review_version,
                    model.line_number == creation.line_number,
                    model.status == ProductLineCreationStatus.CREATE_ATTEMPTED.value,
                    model.product_template_id.is_(None),
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise ProductRemediationDataIntegrityError(
                    "The product line creation is no longer an unresolved attempt; refusing to discard it."
                )
            self._session.flush()
        except ProductRemediationError:
            raise
        except SQLAlchemyError as exc:
            raise ProductRemediationError(SAFE_PRODUCT_LINE_CREATION_PERSISTENCE_ERROR) from exc

    def _find(
        self, *, review_id: str, company_id: int, review_version: int, line_number: str
    ) -> WorkbenchProductLineCreation | None:
        model = WorkbenchProductLineCreation
        return self._session.scalar(
            select(model).where(
                model.review_id == review_id,
                model.company_id == company_id,
                model.review_version == review_version,
                model.line_number == line_number,
            )
        )


def _model(creation: ProductLineCreation) -> WorkbenchProductLineCreation:
    intent = creation.intent
    return WorkbenchProductLineCreation(
        review_id=creation.review_id,
        company_id=creation.company_id,
        review_version=creation.review_version,
        line_number=creation.line_number,
        status=creation.status.value,
        supplier_partner_id=creation.supplier_partner_id,
        seller_item_code=creation.seller_item_code,
        product_name=intent.product_name,
        product_kind=intent.kind.value,
        categ_id=intent.categ_id,
        uom_id=intent.uom_id,
        internal_reference=intent.internal_reference,
        sale_ok=intent.sale_ok,
        intent_digest=intent.digest(),
        supplier_product_name=creation.supplier_product_name,
        approved_by=creation.approved_by,
        create_attempted_at=creation.create_attempted_at,
        product_template_id=creation.product_template_id,
        product_id=creation.product_id,
        supplierinfo_id=creation.supplierinfo_id,
    )


def _creation(record: WorkbenchProductLineCreation) -> ProductLineCreation:
    try:
        intent = NewProductIntent(
            product_name=str(record.product_name),
            kind=NewProductKind(str(record.product_kind)),
            categ_id=int(record.categ_id),
            uom_id=int(record.uom_id),
            internal_reference=record.internal_reference,
            sale_ok=record.sale_ok,
        )
        if intent.digest() != record.intent_digest:
            raise ProductRemediationDataIntegrityError("A product line creation intent no longer matches its digest.")
        return ProductLineCreation(
            review_id=str(record.review_id),
            company_id=int(record.company_id),
            review_version=int(record.review_version),
            line_number=str(record.line_number),
            status=ProductLineCreationStatus(str(record.status)),
            supplier_partner_id=int(record.supplier_partner_id),
            seller_item_code=str(record.seller_item_code),
            intent=intent,
            approved_by=str(record.approved_by),
            create_attempted_at=record.create_attempted_at,
            supplier_product_name=record.supplier_product_name,
            product_template_id=record.product_template_id,
            product_id=record.product_id,
            supplierinfo_id=record.supplierinfo_id,
        )
    except ProductRemediationDataIntegrityError:
        raise
    except (ValueError, TypeError, ProductRemediationError) as exc:
        raise ProductRemediationDataIntegrityError("A product line creation row could not be read.") from exc


__all__ = ["SqlAlchemyProductLineCreationRepository"]
