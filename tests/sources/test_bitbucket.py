from contextlib import asynccontextmanager
from copy import copy
from unittest import mock
from unittest.mock import AsyncMock, patch

import pytest

from connectors.sources.bitbucket import (
    BITBUCKET_CLOUD,
    COMMIT_SCHEMA,
    PULL_REQUEST_SCHEMA,
    BitBucketClient,
    BitBucketDataSource,
    ConfigurableFieldValueError,
)
from tests.commons import AsyncIterator
from tests.sources.support import create_source


class JSONAsyncMock(AsyncMock):
    def __init__(self, json, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._json = json

    async def json(self):
        return self._json


RESPONSE_WORKSPACE = {
    "values": [
        {
            "type": "workspace",
            "uuid": "{939688f8-0096-4c0a-8e97-660852b2393d}",
            "name": "ConnectorTrail",
            "slug": "connectortrail",
            "created_on": "2024-03-05T10:16:45.934329+00:00",
        }
    ],
    "pagelen": 10,
    "size": 1,
    "page": 1,
}

RESPONSE_PULL_REQUEST = {
    "values": [
        {
            "type": "pullrequest",
            "id": 1,
            "title": "asdfsg",
            "description": "hello",
            "created_on": "2024-03-14T05:58:36.030910+00:00",
            "updated_on": "2024-03-14T05:58:36.623556+00:00",
        }
    ],
    "pagelen": 10,
    "size": 1,
    "page": 1,
}

RESPONSE_COMMIT = {
    "values": [
        {
            "type": "commit",
            "hash": "6a1246f7f887775fbc6f897c3d0f6d8950f43b49",
            "date": "2024-03-14T05:57:56+00:00",
            "author": {
                "user": {
                    "display_name": "Soham Mandaviya",
                },
            },
            "message": "asdfsg",
            "repository": {"full_name": "connectortrail/repo1"},
        },
    ],
    "pagelen": 30,
    "size": 1,
    "page": 1,
    "next": "link to next page",
}

EXPECTED_WORKSPACE = "connectortrail"


EXPECTED_PULL_REQUEST = {
    "_id": 1,
    "_timestamp": "2024-03-14T05:58:36.623556+00:00",
    "type": "pullrequest",
    "title": "asdfsg",
    "description": "hello",
}

EXPECTED_COMMIT = {
    "_id": "6a1246f7f887775fbc6f897c3d0f6d8950f43b49",
    "_timestamp": "2024-03-14T05:57:56+00:00",
    "type": "commit",
    "message": "asdfsg",
    "display_name": "Soham Mandaviya",
    "repository_name": "connectortrail/repo1",
}

RESPONSE_REPOSITORY_NAME = {
    "values": [
        {"repository": {"full_name": "connectortrail/repo1"}},
        {"repository": {"full_name": "connectortrail/repo2"}},
    ]
}

RESPONSE_REPOSITORY = {
    "values": [
        {
            "uuid": "{ABC-123-X4Y}",
            "full_name": "connectortrail/repo1",
            "name": "Repo1",
            "slug": "repo1",
            "workspace": {
                "name": "ConnectorTrail",
                "slug": "connectortrail",
            },
            "project": {
                "name": "Connector1",
            },
            "created_on": "2024-03-05T10:17:27.127667+00:00",
            "updated_on": "2024-03-14T10:15:54.620297+00:00",
            "is_private": "true",
        },
    ],
    "pagelen": 10,
    "size": 1,
    "page": 1,
    "next": "link to next page",
}

EXPECTED_REPOSITORY_NAME = ["connectortrail/repo1", "connectortrail/repo2"]
EXPECTED_REPOSITORY_NAME1 = "connectortrail/repo1"

RESPONSE_FILE_INSIDE_FOLDER = {
    "values": [
        {
            "path": "om",
            "type": "commit_directory",
            "links": {
                "self": {
                    "href": "https://api.bitbucket.org/2.0/repositories/connectortrail/repo1/src/80b5f9c29d93e0afbbe519a7ba736cc854afee2d/om/"
                }
            },
        }
    ]
}

EXPECTED_FILE_INSIDE_FOLDER = {
    "path": "om/first.py",
    "type": "commit_file",
    "attributes": [],
    "escaped_path": "om/first.py",
    "size": 20,
    "mimetype": "text/x-python",
    "links": {
        "self": {
            "href": "https://api.bitbucket.org/2.0/repositories/connectortrail/repo1/src/80b5f9c29d93e0afbbe519a7ba736cc854afee2d/om/first.py"
        }
    },
}


@asynccontextmanager
async def create_bitbucket_source():
    async with create_source(
        BitBucketDataSource,
        data_source=BITBUCKET_CLOUD,
        user_name="username",
        app_password="password",
        repositories="*",
    ) as source:
        yield source


@pytest.mark.asyncio
@patch("aiohttp.ClientSession.get")
async def test_ping_successful_connection(mock_get):
    mock_get.return_value.__aenter__.return_value.status = 200
    async with create_bitbucket_source() as source:
        with patch.object(
            BitBucketClient, "api_call", return_value=mock_get.return_value.status
        ):
            await source.ping()


@pytest.mark.asyncio
@patch("aiohttp.ClientSession.get")
async def test_ping_unsuccessful_connection(mock_get):
    """Test ping method en connection is unsuccessful"""
    async with create_bitbucket_source() as source:
        with patch.object(
            BitBucketClient,
            "api_call",
            side_effect=Exception("Connection unsuccessful"),
        ):
            with pytest.raises(Exception):
                await source.ping()


@pytest.mark.asyncio
async def test_remote_validation_when_repository_names_are_not_correct_so_raise_exception():
    async with create_bitbucket_source() as source:
        source.repositories = ["connectortrail/repo1", "trial1/demo1"]
        async_response = AsyncMock()
        async_response.__aenter__ = AsyncMock(
            return_value=JSONAsyncMock(RESPONSE_REPOSITORY_NAME)
        )
        with mock.patch("aiohttp.ClientSession.get", return_value=async_response):
            with pytest.raises(
                ConfigurableFieldValueError,
                match="Repositories 'trial1/demo1' are not available. Available repositories are: 'connectortrail/repo1, connectortrail/repo2'",
            ):
                await source._remote_validation()


@pytest.mark.asyncio
async def test_remote_validation_when_repository_names_are_correct():
    async with create_bitbucket_source() as source:
        source.repositories = ["connectortrail/repo1", "connectortrail/repo2"]
        async_response = AsyncMock()
        async_response.__aenter__ = AsyncMock(
            return_value=JSONAsyncMock(RESPONSE_REPOSITORY_NAME)
        )
        with mock.patch("aiohttp.ClientSession.get", return_value=async_response):
            await source._remote_validation()


@pytest.mark.asyncio
async def test_remote_validation_when_input_is_wildcard():
    async with create_bitbucket_source() as source:
        source.repositories = ["*"]
        assert None is await source._remote_validation()


@pytest.mark.asyncio
async def test_get_session():
    async with create_bitbucket_source() as source:
        first_session = source.bitbucket_client._get_session()
        second_session = source.bitbucket_client._get_session()
        assert first_session is second_session


@pytest.mark.asyncio
async def test_fetch_workspaces():
    async with create_bitbucket_source() as source:
        with mock.patch.object(
            BitBucketClient,
            "paginated_api_call",
            return_value=AsyncIterator([RESPONSE_WORKSPACE]),
        ):
            async for workspace in source._fetch_workspaces():
                assert workspace == EXPECTED_WORKSPACE


@pytest.mark.asyncio
@patch.object(
    BitBucketDataSource,
    "_fetch_repository_name",
    return_value=AsyncIterator([RESPONSE_REPOSITORY.get("values")[0]]),
)
async def test_fetch_pull_requests_when_wildcard(repository_patch):
    async with create_bitbucket_source() as source:
        with mock.patch.object(
            BitBucketClient,
            "paginated_api_call",
            return_value=AsyncIterator([RESPONSE_PULL_REQUEST]),
        ):
            async for response in source._fetch_pull_request():
                assert response == EXPECTED_PULL_REQUEST


@pytest.mark.asyncio
async def test_fetch_pull_requests_when_not_wildcard():
    async with create_bitbucket_source() as source:
        source.configuration.get_field("repositories").value = [
            "practice-bitbucket-connectors/repo1"
        ]
        with mock.patch.object(
            BitBucketClient,
            "paginated_api_call",
            return_value=AsyncMock(return_value=AsyncIterator([RESPONSE_PULL_REQUEST])),
        ):
            async for response in source._fetch_pull_request():
                assert response == EXPECTED_PULL_REQUEST


@pytest.mark.asyncio
async def test_prepare_pull_request_doc():
    async with create_bitbucket_source() as source:
        output = source._prepare_pull_request_doc(
            RESPONSE_PULL_REQUEST.get("values")[0], PULL_REQUEST_SCHEMA
        )
        assert EXPECTED_PULL_REQUEST == output


@pytest.mark.asyncio
async def test_prepare_commit_doc():
    async with create_bitbucket_source() as source:
        output = source._prepare_commit_doc(
            RESPONSE_COMMIT.get("values")[0], COMMIT_SCHEMA
        )
        assert EXPECTED_COMMIT == output


@pytest.mark.asyncio
@patch.object(
    BitBucketDataSource,
    "_fetch_repository_name",
    return_value=AsyncIterator([EXPECTED_REPOSITORY_NAME1]),
)
async def test_fetch_commits_when_wildcard(workspace_patch):
    async with create_bitbucket_source() as source:
        with mock.patch.object(
            BitBucketClient,
            "paginated_api_call",
            return_value=AsyncIterator([RESPONSE_COMMIT]),
        ):
            async for response in source._fetch_commits():
                assert response == EXPECTED_COMMIT


@pytest.mark.asyncio
async def test_fetch_commits_when_not_wildcard():
    async with create_bitbucket_source() as source:
        source.configuration.get_field("repositories").value = [
            "practice-bitbucket-connectors/repo1"
        ]
        with mock.patch.object(
            BitBucketClient,
            "paginated_api_call",
            return_value=AsyncMock(return_value=AsyncIterator([RESPONSE_COMMIT])),
        ):
            async for response in source._fetch_commits():
                assert response == EXPECTED_COMMIT


@pytest.mark.asyncio
@patch.object(
    BitBucketDataSource,
    "_fetch_workspaces",
    return_value=AsyncIterator([EXPECTED_WORKSPACE]),
)
async def test_fetch_repository_name(self):
    async with create_bitbucket_source() as source:
        with mock.patch.object(
            BitBucketClient,
            "paginated_api_call",
            return_value=AsyncIterator([RESPONSE_REPOSITORY]),
        ):
            async for response in source._fetch_repository_name():
                assert response.get("full_name") == EXPECTED_REPOSITORY_NAME1


@pytest.mark.asyncio
@patch.object(
    BitBucketDataSource,
    "_fetch_workspaces",
    return_value=AsyncIterator([EXPECTED_WORKSPACE]),
)
@patch.object(
    BitBucketDataSource,
    "_fetch_files_from_folder",
    return_value=AsyncIterator([EXPECTED_FILE_INSIDE_FOLDER]),
)
async def test_fetch_files_inside_a_folder_when_repository_input_is_wildcard(
    workspace_patch, files_inside_folder_patch
):
    async with create_bitbucket_source() as source:
        with mock.patch.object(
            BitBucketClient,
            "paginated_api_call",
            return_value=AsyncIterator([RESPONSE_FILE_INSIDE_FOLDER]),
        ):
            async for file in source._fetch_files():
                assert file == EXPECTED_FILE_INSIDE_FOLDER


@pytest.mark.asyncio
@patch.object(
    BitBucketDataSource,
    "_fetch_workspaces",
    return_value=AsyncIterator([EXPECTED_WORKSPACE]),
)
@patch.object(
    BitBucketDataSource,
    "_fetch_files_from_folder",
    return_value=AsyncIterator([EXPECTED_FILE_INSIDE_FOLDER]),
)
async def test_fetch_files_inside_a_folder_when_repository_input_is_not_wildcard(
    workspace_patch, files_inside_folder_patch
):
    async with create_bitbucket_source() as source:
        source.configuration.get_field("repositories").value = ["connectortrail/repo1"]
        with mock.patch.object(
            BitBucketClient,
            "paginated_api_call",
            return_value=AsyncIterator([RESPONSE_FILE_INSIDE_FOLDER]),
        ):
            async for file in source._fetch_files():
                assert file == EXPECTED_FILE_INSIDE_FOLDER


RESPONSE_FOLDER = {
    "path": "om",
    "type": "commit_directory",
    "links": {
        "self": {
            "href": "https://api.bitbucket.org/2.0/repositories/connectortrail/repo1/src/80b5f9c29d93e0afbbe519a7ba736cc854afee2d/om/"
        }
    },
}


@pytest.mark.asyncio
async def test_fetch_files_from_folder_when_file_is_inside_more_than_one_folder():
    async with create_bitbucket_source() as source:
        with mock.patch.object(
            BitBucketClient,
            "paginated_api_call",
            return_value=AsyncIterator([RESPONSE_FILE_INSIDE_FOLDER]),
        ):
            async for file in source._fetch_files_from_folder(RESPONSE_FOLDER):
                assert file == EXPECTED_FILE_INSIDE_FOLDER


RESPONSE_FILE = {
    "values": [
        {
            "path": "ContextManagerExample.py",
            "type": "commit_file",
            "attributes": [],
            "escaped_path": "ContextManagerExample.py",
            "size": 546,
            "mimetype": "text/x-python",
            "links": {
                "self": {
                    "href": "https://api.bitbucket.org/2.0/repositories/connectortrail/repo1/src/80b5f9c29d93e0afbbe519a7ba736cc854afee2d/ContextManagerExample.py"
                }
            },
        },
    ]
}

EXPECTED_FILE = {
    "_id": "-9172225332661435081",
    "_timestamp": "2024-03-27T13:06:46.636917+00:00",
    "filename": "ContextManagerExample.py",
    "type": "commit_file",
    "file_type": "text/x-python",
    "file_size": 546,
    "path": "ContextManagerExample.py",
}


# @pytest.mark.asyncio
# async def test_fetch_files_from_folder_when_file_is_inside_one_folder():
#     async with create_bitbucket_source() as source:
#         with mock.patch.object(
#             BitBucketClient,
#             "paginated_api_call",
#             return_value=AsyncIterator([RESPONSE_FILE]),
#         ):
#             async for file in source._fetch_files_from_folder(RESPONSE_FOLDER):
#                 assert file == EXPECTED_FILE


@pytest.mark.asyncio
@mock.patch.object(
    BitBucketDataSource,
    "_fetch_commits",
    return_value=AsyncIterator([[copy(EXPECTED_COMMIT), None]]),
)
async def test_get_docs(commit_patch):
    async with create_bitbucket_source() as source:
        expected_responses = [[EXPECTED_COMMIT, None]]
        documents = []
        async for item, _ in source.get_docs():
            documents.append(item)
        assert documents == expected_responses
