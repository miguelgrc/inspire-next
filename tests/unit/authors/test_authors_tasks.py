# -*- coding: utf-8 -*-
#
# This file is part of INSPIRE.
# Copyright (C) 2016 CERN.
#
# INSPIRE is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# INSPIRE is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with INSPIRE. If not, see <http://www.gnu.org/licenses/>.
#
# In applying this licence, CERN does not waive the privileges and immunities
# granted to it by virtue of its status as an Intergovernmental Organization
# or submit itself to any jurisdiction.

from __future__ import absolute_import, division, print_function

import pytest
from mock import patch

from invenio_accounts.models import User

from inspirehep.modules.authors.tasks import (
    new_ticket_context,
    update_ticket_context,
    reply_ticket_context,
    curation_ticket_context
)


class MockObj(object):

    def __init__(self, data={}, extra_data={}):
        self._data = data
        self._extra_data = extra_data

    @property
    def data(self):
        return self._data

    @property
    def extra_data(self):
        return self._extra_data


@pytest.fixture()
def data():
    return {
        "control_number": 123,
        "name": {
            "preferred_name": "John Doe"
        },
        "bai": "John.Doe.1"
    }


@pytest.fixture()
def extra_data():
    return {
        "comments": "Foo bar",
        "reason": "Test reason",
        "url": "http://example.com",
        "recid": 123
    }


@pytest.fixture()
def user():
    return User(email="foo@bar.com")


def test_new_ticket_context(data, extra_data, user):
    obj = MockObj(data, extra_data)
    ctx = new_ticket_context(user, obj)

    assert isinstance(ctx['object'], MockObj)
    assert ctx['email'] == 'foo@bar.com'
    assert ctx['subject'] == 'Your suggestion to INSPIRE: author John Doe'
    assert ctx['user_comment'] == 'Foo bar'


def test_update_ticket_context(app, data, extra_data, user):
    config = {
        'AUTHORS_UPDATE_BASE_URL': 'http://inspirehep.net'
    }
    obj = MockObj(data, extra_data)
    with app.app_context():
        with patch.dict(app.config, config):
            expected = {
                'url': 'http://inspirehep.net/record/123',
                'bibedit_url': 'http://inspirehep.net/record/123/edit',
                'email': 'foo@bar.com',
                'user_comment': 'Foo bar'
            }
            ctx = update_ticket_context(user, obj)
            assert ctx == expected


def test_reply_ticket_context(data, extra_data, user):
    obj = MockObj(data, extra_data)
    ctx = reply_ticket_context(user, obj)
    assert isinstance(ctx['object'], MockObj)
    assert isinstance(ctx['user'], User)
    assert ctx['author_name'] == 'John Doe'
    assert ctx['reason'] == 'Test reason'
    assert ctx['record_url'] == 'http://example.com'


def test_curation_ticket_context(data, extra_data, user):
    obj = MockObj(data, extra_data)
    ctx = curation_ticket_context(user, obj)
    assert isinstance(ctx['object'], MockObj)
    assert ctx['recid'] == 123
    assert ctx['user_comment'] == 'Foo bar'
    assert ctx['subject'] == 'Curation needed for author John Doe [John.Doe.1]'
    assert ctx['email'] == 'foo@bar.com'
    assert ctx['record_url'] == 'http://example.com'
