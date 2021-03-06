# -*- coding: utf-8 -*-
#
# This file is part of INSPIRE.
# Copyright (C) 2018 CERN.
#
# INSPIRE is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# INSPIRE is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with INSPIRE. If not, see <http://www.gnu.org/licenses/>.
#
# In applying this license, CERN does not waive the privileges and immunities
# granted to it by virtue of its status as an Intergovernmental Organization
# or submit itself to any jurisdiction.

"""ORCID utils."""

from __future__ import absolute_import, division, print_function

import signal
import time
from itertools import chain

from celery.app.task import Task, Context
from celery.exceptions import MaxRetriesExceededError, Retry, TimeLimitExceeded
from elasticsearch_dsl import Q
from flask import current_app
from sqlalchemy import cast, type_coerce
from sqlalchemy.dialects.postgresql import JSONB

from invenio_db import db
from invenio_oauthclient.models import (
    UserIdentity,
    RemoteAccount,
    RemoteToken,
)
from invenio_oauthclient.utils import oauth_link_external_id
from invenio_pidstore.models import PersistentIdentifier
from invenio_records.models import RecordMetadata

from inspire_dojson.utils import get_recid_from_ref
from inspire_utils.record import get_values_for_schema
from inspirehep.modules.search.api import LiteratureSearch
from inspirehep.utils.record_getter import get_db_records


def _split_lists(sequence, chunk_size):
    """Get a list created by splitting the original list every n-th element

    Args:
        sequence (List[Any]): a list to be split
        chunk_size (int): how bit one chunk should be (n)

    Returns:
        List[List[Any]]: the split list
    """
    return [
        sequence[i:i + chunk_size] for i in range(0, len(sequence), chunk_size)
    ]


def get_push_access_tokens(orcids):
    """Get the remote tokens for the given ORCIDs.

    Args:
        orcids(List[str]): ORCIDs to get the tokens for.

    Returns:
        sqlalchemy.util._collections.result: pairs of (ORCID, access_token),
        for ORCIDs having a token. These are similar to named tuples, in that
        the values can be retrieved by index or by attribute, respectively
        ``id`` and ``access_token``.

    """
    return db.session.query(UserIdentity.id, RemoteToken.access_token).filter(
        RemoteToken.id_remote_account == RemoteAccount.id,
        RemoteAccount.user_id == UserIdentity.id_user,
        UserIdentity.id.in_(orcids),
        cast(RemoteAccount.extra_data, JSONB).contains({'allow_push': True}),
    ).all()


def account_setup(remote, token, resp):
    """Perform additional setup after user have been logged in.

    This is a modified version of
    :ref:`invenio_oauthclient.contrib.orcid.account_setup` that stores
    additional metadata.

    :param remote: The remote application.
    :param token: The token value.
    :param resp: The response.
    """
    with db.session.begin_nested():
        # Retrieve ORCID from response.
        orcid = resp.get('orcid')
        full_name = resp.get('name')

        # Set ORCID in extra_data.
        token.remote_account.extra_data = {
            'orcid': orcid,
            'full_name': full_name,
            'allow_push': current_app.config.get('ORCID_ALLOW_PUSH_DEFAULT', False)
        }

        user = token.remote_account.user

        # Create user <-> external id link.
        oauth_link_external_id(user, {'id': orcid, 'method': 'orcid'})


def get_orcids_for_push(record):
    """Obtain the ORCIDs associated to the list of authors in the Literature record.

    The ORCIDs are looked up both in the ``ids`` of the ``authors`` and in the
    Author records that have claimed the paper.

    Args:
        record(dict): metadata from a Literature record

    Returns:
        Iterator[str]: all ORCIDs associated to these authors
    """
    orcids_on_record = []
    author_recids_with_claims = []

    for author in record.get('authors', []):
        orcids_in_author = get_values_for_schema(author.get('ids', []), 'ORCID')
        if orcids_in_author:
            orcids_on_record.extend(orcids_in_author)
        elif author.get('curated_relation') is True and 'record' in author:
            author_recids_with_claims.append(get_recid_from_ref(author['record']))

    author_records = get_db_records(('aut', recid) for recid in author_recids_with_claims)
    all_ids = (author.get('ids', []) for author in author_records)
    orcids_in_authors = chain.from_iterable(get_values_for_schema(ids, 'ORCID') for ids in all_ids)

    return chain(orcids_on_record, orcids_in_authors)


def get_literature_recids_for_orcid(orcid):
    """Return the Literature recids that were claimed by an ORCiD.

    We record the fact that the Author record X has claimed the Literature
    record Y by storing in Y an author object with a ``$ref`` pointing to X
    and the key ``curated_relation`` set to ``True``. Therefore this method
    first searches the DB for the Author records for the one containing the
    given ORCiD, and then uses its recid to search in ES for the Literature
    records that satisfy the above property.

    Args:
        orcid (str): the ORCiD.

    Return:
        list(int): the recids of the Literature records that were claimed
        by that ORCiD.

    """
    orcid_object = '[{"schema": "ORCID", "value": "%s"}]' % orcid
    # this first query is written in a way that can use the index on (json -> ids)
    author_rec_uuid = db.session.query(RecordMetadata.id)\
        .filter(type_coerce(RecordMetadata.json, JSONB)['ids'].contains(orcid_object)).one().id
    author_recid = db.session.query(PersistentIdentifier.pid_value).filter(
        PersistentIdentifier.object_type == 'rec',
        PersistentIdentifier.object_uuid == author_rec_uuid,
        PersistentIdentifier.pid_type == 'aut',
    ).one().pid_value

    query = Q('match', authors__curated_relation=True) & Q('match', authors__recid=author_recid)
    search_by_curated_author = LiteratureSearch().query('nested', path='authors', query=query)\
                                                 .params(_source=['control_number'], size=9999)

    return [el['control_number'] for el in search_by_curated_author]


def log_service_response(logger, response, extra_message=None):
    msg = '{} {} {} completed with HTTP-status={}'.format(
        response.request.method, response.request.url, extra_message, response.status_code)
    if response.ok:
        logger.info(msg)
    else:
        logger.error(msg + ' and response={}'.format(response))


def apply_celery_task_with_retry(task_func, args=None, kwargs=None,
                                 max_retries=5, countdown=10, time_limit=None):
    """
    When executing a (bind=True) task synchronously (with `mytask.apply()` or
    just calling it as a function `mytask()`) the `self.retry()` does not work,
    but the original exception is raised (without any retry) so you "lose"
    the exception management logic written in the task code.

    This function overcome such limitation.
    Example:

    # Celery task:
    @shared_task(bind=True)
    def normalize_name_task(self, first_name, last_name, nick_name=''):
        try:
            result = ... network call ...
        except RequestException as exc:
                exception = None
            raise self.retry(max_retries=3, countdown=5, exc=exception)
        return result

    # Call the task sync with retry:
    result = apply_celery_task_with_retry(
        normalize_name_task, args=('John', 'Doe'), kwargs=dict(nick_name='Dee'),
        max_retries=2, countdown=5*60, time_limit=2*60*60
    )

    Note: it assumes that @shared_task is the first (the one on top) decorator
    for the Celery task.

    Args:
        task_func: Celery task function to be run.
        args: the positional arguments to pass on to the task.
        kwargs: the keyword arguments to pass on to the task.
        max_retries: maximum number of retries before raising MaxRetriesExceededError.
        countdown: hard time limit for each attempt. If the last attempt
            It can be a callable, eg:
                backoff = lambda retry_count: 2 ** (retry_count + 1)
                apply_celery_task_with_retry(..., countdown=backoff)
        time_limit: hard time limit for each single attempt in seconds. If the
            last attempt fails because of the time limit, raises TimeLimitExceeded.

    Returns: what the task_func returns.
    """
    def handler(signum, frame):
        raise TimeLimitExceeded

    args = args or tuple()
    kwargs = kwargs or dict()

    retry_mixin = RetryMixin()
    retry_mixin.request.retries = 0
    time_limit_exceeded = False
    # Get the actual function decorated by @shared_task(bind=True).
    unbound_func = task_func.__wrapped__.__func__
    for _ in range(max_retries + 1):
        try:
            if time_limit:
                signal.signal(signal.SIGALRM, handler)
                signal.setitimer(signal.ITIMER_REAL, time_limit)
            result = unbound_func(retry_mixin, *args, **kwargs)
        except (Retry, TimeLimitExceeded) as exc:
            if time_limit:
                signal.alarm(0)  # Disable the alarm.
            if isinstance(exc, TimeLimitExceeded):
                time_limit_exceeded = True
            else:
                time_limit_exceeded = False
            sleep_time = countdown
            if callable(countdown):
                sleep_time = countdown(retry_mixin.request.retries)
            time.sleep(sleep_time)
            retry_mixin.request.retries += 1
            continue
        if time_limit:
            signal.alarm(0)  # Disable the alarm.
        return result
    exception = retry_mixin.exc
    exception = exception or MaxRetriesExceededError
    if time_limit_exceeded:
        exception = TimeLimitExceeded
    raise exception


class RetryMixin(Task):
    def __init__(self, *args, **kwargs):
        self.exc = None
        self._request = Context()

    @property
    def request(self):
        return self._request

    def retry(self, *args, **kwargs):
        self.exc = kwargs.get('exc')
        return Retry
