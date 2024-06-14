import aiohttp
from aiohttp.client_exceptions import ServerDisconnectedError

from connectors.logger import logger
from connectors.source import BaseDataSource, ConfigurableFieldValueError
from connectors.utils import CancellableSleeps, iso_utc

WILDCARD = "*"
BLOB = "blob"
PULL_REQUEST_OBJECT = "pullRequest"
REPOSITORY_OBJECT = "repository"
BITBUCKET_CLOUD = "bitbucket_cloud"
ALL_WORKSPACES = "all_workspace"
SELECTED_WORKSPACES = "selected_workspaces"

PING_URL = "https://api.bitbucket.org/2.0/user"
BASE_URL = "https://api.bitbucket.org/2.0/"
PAGELEN = "?pagelen=100"

RETRIES = 3
RETRY_INTERVAL = 2

PULL_REQUEST_SCHEMA = {
    "_id": "id",
    "_timestamp": "updated_on",
    "type": "type",
    "title": "title",
    "description": "description",
}

COMMIT_SCHEMA = {
    "_id": "hash",
    "_timestamp": "date",
    "type": "type",
    "message": "message",
}


class BitBucketClient:
    def __init__(self, configuration) -> None:
        self._sleeps = CancellableSleeps()
        self.configuration = configuration
        self._logger = logger
        self.data_source_type = self.configuration["data_source"]
        self.retry_count = self.configuration["retry_count"]
        self.session = None

    def _get_session(self):
        if self.session:
            return self.session
        if self.data_source_type == BITBUCKET_CLOUD:
            auth = (
                self.configuration["user_name"],
                self.configuration["app_password"],
            )

        basic_auth = aiohttp.BasicAuth(login=auth[0], password=auth[1])
        timeout = aiohttp.ClientTimeout(total=None)  # pyright: ignore
        self.session = aiohttp.ClientSession(
            auth=basic_auth,
            headers={
                "accept": "application/json",
                "content-type": "application/json",
            },
            timeout=timeout,
            raise_for_status=True,
        )
        return self.session

    async def api_call(self, url):
        retry_counter = 0
        while True:
            try:
                async with self._get_session().get(
                    url=url,
                ) as response:
                    yield response
                    break
            except Exception as exception:
                if isinstance(
                    exception,
                    ServerDisconnectedError,
                ):
                    await self.session.close()  # pyright: ignore
                retry_counter += 1
                if retry_counter > self.retry_count:
                    raise exception
                self._logger.warning(
                    f"Retry count: {retry_counter} out of {self.retry_count}. Exception: {exception}"
                )
                await self._sleeps.sleep(RETRY_INTERVAL**retry_counter)

    async def paginated_api_call(self, url):
        while True:
            try:
                async for response in self.api_call(
                    url=url,
                ):
                    json_response = await response.json()
                    next_page_url = json_response.get("next")
                    yield json_response
                    if next_page_url is None:
                        return
                    url = next_page_url
            except Exception as exception:
                self._logger.warning(
                    f"Skipping data from {url}. Exception: {exception}."
                )
                break


class BitBucketDataSource(BaseDataSource):
    name = "Bitbucket"
    service_type = "bitbucket"

    def __init__(self, configuration):
        super().__init__(configuration)
        self.repositories = self.configuration["repositories"]
        self.bitbucket_client = BitBucketClient(configuration=configuration)

    @classmethod
    def get_default_configuration(cls):
        return {
            "data_source": {
                "display": "dropdown",
                "label": "Bitbucket data source",
                "options": [
                    {"label": "Bitbucket Cloud", "value": BITBUCKET_CLOUD},
                ],
                "order": 1,
                "type": "str",
                "value": BITBUCKET_CLOUD,
            },
            "user_name": {
                "depends_on": [{"field": "data_source", "value": BITBUCKET_CLOUD}],
                "label": "Bitbucket Cloud Username",
                "order": 2,
                "type": "str",
            },
            "app_password": {
                "depends_on": [{"field": "data_source", "value": BITBUCKET_CLOUD}],
                "label": "Bitbucket Cloud App Password",
                "sensitive": True,
                "order": 3,
                "type": "str",
            },
            "repositories": {
                "display": "textarea",
                "label": "Bitbucket Repository Name",
                "order": 4,
                "tooltip": "Enter full name of repositories eparated by commas (Example: <workspace_name>/<repository1_name>, <workspace_name>/<repository2_name> ...) or type * to fetch all the repositories ",
                "type": "list",
                "requires": True,
            },
            "retry_count": {
                "default_value": 3,
                "display": "numeric",
                "label": "Retries per request",
                "order": 5,
                "required": False,
                "type": "int",
                "ui_restrictions": ["advanced"],
            },
        }

    async def ping(self):
        try:
            await anext(
                self.bitbucket_client.api_call(
                    url=PING_URL,
                )
            )
            self._logger.info("Successfully connected to bitbucket")
        except Exception:
            self._logger.exception("Error while connecting to bitbucket")
            raise

    async def validate_config(self):
        await super().validate_config()
        await self._remote_validation()

    async def _remote_validation(self):
        if self.repositories == [WILDCARD]:
            return
        repository_name_list = []
        async for response in self.bitbucket_client.paginated_api_call(
            url=f"{BASE_URL}user/permissions/repositories"
        ):
            for repository in response.get("values", []):
                repository_name = repository.get("repository", {}).get("full_name")
                repository_name_list.append(repository_name)
        if unavailable_repositories := set(self.repositories) - set(
            repository_name_list
        ):
            msg = f"Repositories '{', '.join(unavailable_repositories)}' are not available. Available repositories are: '{', '.join(repository_name_list)}'"
            raise ConfigurableFieldValueError(msg)

    async def _fetch_workspaces(self):
        async for response in self.bitbucket_client.paginated_api_call(
            url=f"{BASE_URL}workspaces{PAGELEN}"
        ):
            for workspace in response.get("values", []):
                yield workspace["slug"]

    def _prepare_pull_request_doc(self, pull_request, schema):
        return {
            es_fields: pull_request[pull_request_fields]
            for es_fields, pull_request_fields in schema.items()
        }

    async def _fetch_repository_name(self):
        async for workspace_name in self._fetch_workspaces():
            async for response in self.bitbucket_client.paginated_api_call(
                url=f"{BASE_URL}repositories/{workspace_name}{PAGELEN}"
            ):
                for repository_data in response.get("values", []):
                    yield repository_data

    async def _fetch_pull_request(self):
        if self.repositories == [WILDCARD]:
            async for repository_name in self._fetch_repository_name():
                async for response in self.bitbucket_client.paginated_api_call(
                    url=f'{BASE_URL}repositories/{repository_name.get("full_name")}/pullrequests'
                ):
                    for pull_request_data in response.get("values", []):
                        yield self._prepare_pull_request_doc(
                            pull_request_data, PULL_REQUEST_SCHEMA
                        )
        else:
            for repository_name in self.repositories:
                async for response in self.bitbucket_client.paginated_api_call(
                    url=f"{BASE_URL}repositories/{repository_name}/pullrequests"
                ):
                    for pull_request_data in response.get("values", []):
                        yield self._prepare_pull_request_doc(
                            pull_request_data, PULL_REQUEST_SCHEMA
                        )

    def _prepare_commit_doc(self, commit, schema):
        commit_doc = {
            es_fields: commit[commit_fields]
            for es_fields, commit_fields in schema.items()
        }
        commit_doc.update(
            {
                "repository_name": commit.get("repository", {}).get("full_name"),
                "display_name": commit.get("author", {})
                .get("user", {})
                .get("display_name"),
            }
        )
        return commit_doc

    async def _fetch_commits(self):
        if self.repositories == [WILDCARD]:
            async for repository_name in self._fetch_repository_name():
                async for commit_data in self.bitbucket_client.paginated_api_call(
                    url=f"{BASE_URL}repositories/{repository_name}/commits"
                ):
                    for commit in commit_data.get("values", []):
                        yield self._prepare_commit_doc(commit, COMMIT_SCHEMA)

        else:
            for repository_name in self.repositories:
                async for commit_data in self.bitbucket_client.paginated_api_call(
                    url=f"{BASE_URL}repositories/{repository_name}/commits"
                ):
                    for commit in commit_data.get("values", []):
                        yield self._prepare_commit_doc(commit, COMMIT_SCHEMA)

    def _prepare_file_doc(self, file_data):
        return {
            "_id": file_data.get("path"),
            "_timestamp": iso_utc(),
            "type": file_data.get("type"),
        }

    async def _fetch_files_from_folder(self, folder):
        async for response in self.bitbucket_client.paginated_api_call(
            url=folder.get("links", {}).get("self", {}).get("href")
        ):
            for data in response.get("values", []):
                if data.get("type") == "commit_directory":
                    async for file_data in self._fetch_files_from_folder(data):
                        yield file_data
                else:
                    yield self._prepare_file_doc(data)

    async def _fetch_files(self):
        if self.configuration.get("repositories") == [WILDCARD]:
            async for workspace in self._fetch_workspaces():
                async for response in self.bitbucket_client.paginated_api_call(
                    url=f"{BASE_URL}repositories/{workspace}/src",
                ):
                    for data in response.get("values", []):
                        if data.get("type") == "commit_directory":
                            async for file_data in self._fetch_files_from_folder(data):
                                yield file_data
                        else:
                            yield self._prepare_file_doc(data)
        else:
            for repository_name in self.configuration.get("repositories"):
                async for response in self.bitbucket_client.paginated_api_call(
                    url=f"{BASE_URL}repositories/{repository_name}/src",
                ):
                    for data in response.get("values", []):
                        if data.get("type") == "commit_directory":
                            async for file_data in self._fetch_files_from_folder(data):
                                yield file_data
                        else:
                            yield self._prepare_file_doc(data)

    async def get_docs(self, filtering=None):
        async for doc in self._fetch_commits():
            yield doc, None
        # async for doc in self._fetch_pull_request():
        #     yield doc, None
        # async for doc in self._fetch_files():
        #     yield doc, None
