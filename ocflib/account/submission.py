"""New account submission.

The functions here are all Celery tasks that submit new accounts for creation.
Account creation always happens on the admin server (supernova), but new
accounts can be submitted from anywhere (e.g. accounts.ocf.berkeley.edu (atool)
or the approve command-line staff script).

A pre-requisite to using functions in this module is configuring Celery with an
appropriate broker and backend URL (probably Redis).

    from celery import Celery
    from ocflib.account.submission import get_tasks

    celery_app = Celery(broker='..', backend='..')
    tasks = get_tasks(celery_app)

    result = tasks.create_account.delay(..)

    # result is now an AsyncResult:
    # https://celery.readthedocs.org/en/latest/reference/celery.result.html#celery.result.AsyncResult
    #
    # You can immediately resolve it with result.wait(timeout=5), or grab
    # result.id and fetch it later.
"""
from collections import namedtuple
from contextlib import contextmanager

from sqlalchemy import Boolean
from sqlalchemy import Column
from sqlalchemy import create_engine
from sqlalchemy import Integer
from sqlalchemy import LargeBinary
from sqlalchemy import String
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.sql import exists

from ocflib.account.creation import create_account as real_create_account
from ocflib.account.creation import NewAccountRequest
from ocflib.account.creation import send_rejected_mail
from ocflib.account.creation import validate_request


Base = declarative_base()


def username_pending(session, request):
    """Returns whether the username is currently pending creation."""
    return session.query(exists().where(
        StoredNewAccountRequest.user_name == request.user_name
    )).scalar()


def user_has_request_pending(session, request):
    """Returns whether the user has an account request pending.
    Checks based on CalNet UID / CalLink OID.
    """
    query = None
    if request.is_group and request.callink_oid != 0:
        query = StoredNewAccountRequest.callink_oid == request.callink_oid
    elif not request.is_group:
        query = StoredNewAccountRequest.calnet_uid == request.calnet_uid
    return (
        query is not None and
        session.query(exists().where(query)).scalar()
    )


class StoredNewAccountRequest(Base):
    """SQLAlchemy object for holding account requests."""

    __tablename__ = 'request'

    def __str__(self):
        # TODO: reasons
        return '{self.user_name} ({type}), because of {reasons}'.format(
            self=self,
            type='group' if self.is_group else 'individual',
            reasons='reasons',
        )

    @classmethod
    def from_request(cls, request):
        """Create a StoredNewAccountRequest from a NewAccountRequest."""
        return cls(
            user_name=request.user_name,
            real_name=request.real_name,
            is_group=request.is_group,
            calnet_uid=request.calnet_uid,
            callink_oid=request.callink_oid,
            email=request.email,
            encrypted_password=request.encrypted_password,
        )

    def to_request(self, handle_warnings=NewAccountRequest.WARNINGS_CREATE):
        """Convert this object to a NewAccountRequest."""
        return NewAccountRequest(**dict(
            {
                field: getattr(self, field)
                for field in NewAccountRequest._fields
                if field in self.__table__.columns._data.keys()
            },
            handle_warnings=handle_warnings,
        ))

    # TODO: enforce these lengths during submission as errors
    id = Column(Integer, primary_key=True)
    user_name = Column(String(255), unique=True, nullable=False)
    real_name = Column(String(255), nullable=False)
    is_group = Column(Boolean, nullable=False)
    calnet_uid = Column(Integer, nullable=True)
    callink_oid = Column(Integer, nullable=True)
    email = Column(String(255), nullable=False)
    encrypted_password = Column(LargeBinary(510), nullable=False)


class NewAccountResponse(namedtuple('NewAccountResponse', [
    'status', 'errors',
])):
    """Response to an account creation request.

    :param status: one of CREATED, FLAGGED, PENDING, REJECTED
        CREATED: account was created successfully
        FLAGGED: account was flagged and not submitted; the response includes a
                 list of warnings. The user can choose to continue, and should
                 send another request with handle_warnings=WARNINGS_SUBMIT.
        PENDING: account was flagged and submitted; staff will manually review
                 it, and the user will receive an email in a few days
        REJECTED: account cannot be created due to a fatal error (e.g. username
                  already taken)
    :param errors: list of errors (or None)
    """
    CREATED = 'created'
    FLAGGED = 'flagged'
    PENDING = 'pending'
    REJECTED = 'rejected'


def get_tasks(celery_app, credentials=None):
    """Return Celery tasks instantiated against the provided instance."""
    # mysql, for stored account requests
    Session = None

    def get_session():
        nonlocal Session
        if Session is None:
            Session = sessionmaker(
                bind=create_engine(credentials.mysql_uri),
            )
        return Session()

    # convenience function for dispatching Celery events
    def dispatch_event(event_type, **kwargs):
        with celery_app.events.default_dispatcher() as disp:
            disp.send(type=event_type, **kwargs)

    @celery_app.task
    def create_account(request):
        # status reporting
        status = []

        def _report_status(line):
            """Update task status by adding the given line."""
            status.append(line)
            create_account.update_state(
                state='PROGRESS',
                meta={'status': status},
            )

        @contextmanager
        def report_status(start, stop, task):
            _report_status(start + ' ' + task)
            yield
            _report_status(stop + ' ' + task)

        # actual account creation
        with report_status('Validating', 'Validated', 'account request'):
            errors, warnings = validate_request(
                request, credentials, get_session())

        if errors:
            # Fatal errors; cannot be bypassed, even with staff approval
            return NewAccountResponse(
                status=NewAccountResponse.REJECTED,
                errors=(errors + warnings),
            )
        elif warnings:
            # Non-fatal errors; the frontend can choose to create the account
            # anyway, submit the account for staff approval, or get a response
            # with a list of warnings for further inspection.
            if request.handle_warnings == NewAccountRequest.WARNINGS_SUBMIT:
                with report_status('Submitting', 'Submitted', 'account for staff approval'):
                    stored_request = StoredNewAccountRequest.from_request(
                        request)

                    session = get_session()
                    session.add(stored_request)  # TODO: error handling
                    session.commit()

                    dispatch_event(
                        'ocflib.account_submitted',
                        request=dict(request.to_dict(), reasons=warnings),
                    )
                    return NewAccountResponse(
                        status=NewAccountResponse.PENDING,
                        errors=warnings,
                    )
            elif request.handle_warnings == NewAccountRequest.WARNINGS_WARN:
                return NewAccountResponse(
                    status=NewAccountResponse.FLAGGED,
                    errors=warnings,
                )

        real_create_account(request, credentials, report_status)
        dispatch_event('ocflib.account_created', request=request.to_dict())
        return NewAccountResponse(
            status=NewAccountResponse.CREATED,
            errors=[],
        )

    @celery_app.task
    def get_pending_requests():
        return get_session().query(StoredNewAccountRequest).all()

    def get_remove_row_by_user_name(user_name):
        """Fetch stored request, then remove it."""
        session = get_session()
        request_row = session.query(StoredNewAccountRequest).filter(
            StoredNewAccountRequest.user_name == user_name
        ).first()
        session.delete(request_row)
        session.commit()
        return request_row.to_request()

    @celery_app.task
    def approve_request(user_name):
        request = get_remove_row_by_user_name(user_name)
        create_account.delay(request)
        dispatch_event('ocflib.account_approved', request=request.to_dict())

    @celery_app.task
    def reject_request(user_name):
        request = get_remove_row_by_user_name(user_name)
        reason = 'TODO: come up with a reason'
        send_rejected_mail(request, reason)
        dispatch_event('ocflib.account_rejected', request=request.to_dict())

    return _AccountSubmissionTasks(
        create_account=create_account,
        get_pending_requests=get_pending_requests,
        approve_request=approve_request,
        reject_request=reject_request,
    )

_AccountSubmissionTasks = namedtuple('AccountSubmissionTasks', [
    'create_account',
    'get_pending_requests',
    'approve_request',
    'reject_request',
])

AccountCreationCredentials = namedtuple('AccountCreationCredentials', [
    'encryption_key', 'mysql_uri', 'kerberos_keytab', 'kerberos_principal',
])
