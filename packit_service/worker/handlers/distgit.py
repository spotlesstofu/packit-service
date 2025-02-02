# Copyright Contributors to the Packit project.
# SPDX-License-Identifier: MIT

"""
This file defines classes for job handlers specific for distgit
"""
import logging
import shutil
from celery import Task
from datetime import datetime
from typing import Optional, Dict

from fasjson_client import Client
from fasjson_client.errors import APIError
from ogr.abstract import PullRequest, PRStatus

from packit.api import PackitAPI
from packit.config import JobConfig, JobType
from packit.config.aliases import get_branches
from packit.config.package_config import PackageConfig
from packit.exceptions import PackitException, PackitDownloadFailedException
from packit.local_project import LocalProject
from packit.utils.repo import RepositoryCache
from packit_service import sentry_integration
from packit_service.config import PackageConfigGetter, ProjectToSync
from packit_service.constants import (
    CONTACTS_URL,
    FASJSON_URL,
    MSG_RETRIGGER,
)
from packit_service.models import (
    ProposeDownstreamTargetStatus,
    ProposeDownstreamTargetModel,
    ProposeDownstreamModel,
    ProposeDownstreamStatus,
)
from packit_service.service.urls import get_propose_downstream_info_url
from packit_service.utils import gather_packit_logs_to_buffer, collect_packit_logs
from packit_service.worker.events import (
    PushPagureEvent,
    ReleaseEvent,
    AbstractIssueCommentEvent,
    CheckRerunReleaseEvent,
    PullRequestCommentPagureEvent,
)
from packit_service.worker.handlers.abstract import (
    JobHandler,
    TaskName,
    configured_as,
    reacts_to,
    run_for_comment,
    run_for_check_rerun,
    RetriableJobHandler,
)
from packit_service.worker.helpers.propose_downstream import ProposeDownstreamJobHelper
from packit_service.worker.reporting import BaseCommitStatus
from packit_service.worker.result import TaskResults

logger = logging.getLogger(__name__)


@configured_as(job_type=JobType.sync_from_downstream)
@reacts_to(event=PushPagureEvent)
class SyncFromDownstream(JobHandler):
    """Sync new specfile changes to upstream after a new git push in the dist-git."""

    task_name = TaskName.sync_from_downstream

    def __init__(
        self,
        package_config: PackageConfig,
        job_config: JobConfig,
        event: dict,
    ):
        super().__init__(
            package_config=package_config,
            job_config=job_config,
            event=event,
        )
        self.dg_repo_name = event.get("repo_name")
        self.dg_branch = event.get("git_ref")
        self._project_to_sync: Optional[ProjectToSync] = None

    @property
    def project_to_sync(self) -> Optional[ProjectToSync]:
        if self._project_to_sync is None:
            if project_to_sync := self.service_config.get_project_to_sync(
                dg_repo_name=self.dg_repo_name, dg_branch=self.dg_branch
            ):
                self._project_to_sync = project_to_sync
        return self._project_to_sync

    def pre_check(self) -> bool:
        return self.project_to_sync is not None

    def run(self) -> TaskResults:
        ogr_project_to_sync = self.service_config.get_project(
            url=f"{self.project_to_sync.forge}/"
            f"{self.project_to_sync.repo_namespace}/{self.project_to_sync.repo_name}"
        )
        upstream_local_project = LocalProject(
            git_project=ogr_project_to_sync,
            working_dir=self.service_config.command_handler_work_dir,
            cache=RepositoryCache(
                cache_path=self.service_config.repository_cache,
                add_new=self.service_config.add_repositories_to_repository_cache,
            )
            if self.service_config.repository_cache
            else None,
        )
        packit_api = PackitAPI(
            self.service_config,
            self.job_config,
            upstream_local_project=upstream_local_project,
        )
        # rev is a commit
        # we use branch on purpose so we get the latest thing
        # TODO: check if rev is HEAD on {branch}, warn then?
        packit_api.sync_from_downstream(
            dist_git_branch=self.dg_branch,
            upstream_branch=self.project_to_sync.branch,
            sync_only_specfile=True,
        )
        return TaskResults(success=True, details={})


class AbortProposeDownstream(Exception):
    """Abort propose-downstream process"""


@configured_as(job_type=JobType.propose_downstream)
@run_for_comment(command="propose-downstream")
@run_for_comment(command="propose-update")  # deprecated
@run_for_check_rerun(prefix="propose-downstream")
@reacts_to(event=ReleaseEvent)
@reacts_to(event=AbstractIssueCommentEvent)
@reacts_to(event=CheckRerunReleaseEvent)
class ProposeDownstreamHandler(RetriableJobHandler):
    topic = "org.fedoraproject.prod.git.receive"
    task_name = TaskName.propose_downstream

    def __init__(
        self,
        package_config: PackageConfig,
        job_config: JobConfig,
        event: dict,
        propose_downstream_run_id: Optional[int] = None,
        celery_task: Task = None,
    ):
        super().__init__(
            package_config=package_config,
            job_config=job_config,
            event=event,
            celery_task=celery_task,
        )
        self._propose_downstream_run_id = propose_downstream_run_id
        self._propose_downstream_helper: Optional[ProposeDownstreamJobHelper] = None

    @property
    def propose_downstream_helper(self) -> ProposeDownstreamJobHelper:
        if not self._propose_downstream_helper:
            self._propose_downstream_helper = ProposeDownstreamJobHelper(
                service_config=self.service_config,
                package_config=self.package_config,
                project=self.project,
                metadata=self.data,
                db_trigger=self.data.db_trigger,
                job_config=self.job_config,
                branches_override=self.data.branches_override,
            )
        return self._propose_downstream_helper

    def sync_branch(
        self, branch: str, model: ProposeDownstreamModel
    ) -> Optional[PullRequest]:
        try:
            downstream_pr = self.api.sync_release(
                dist_git_branch=branch, tag=self.data.tag_name, create_pr=True
            )
        except PackitDownloadFailedException as ex:
            # the archive has not been uploaded to PyPI yet
            # retry for the archive to become available
            logger.info(f"We were not able to download the archive: {ex}")
            # when the task hits max_retries, it raises MaxRetriesExceededError
            # and the error handling code would be never executed
            retries = self.celery_task.retries
            if not self.celery_task.is_last_try():
                # will retry in: 1m and then again in another 2m
                delay = 60 * 2**retries
                logger.info(
                    f"Will retry for the {retries + 1}. time in {delay}s \
                        with propose_downstream_run_id {model.id}."
                )
                # throw=False so that exception is not raised and task
                # is not retried also automatically
                kargs = self.celery_task.task.request.kwargs.copy()
                kargs["propose_downstream_run_id"] = model.id
                # https://docs.celeryq.dev/en/stable/userguide/tasks.html#retrying
                self.celery_task.task.retry(
                    exc=ex, countdown=delay, throw=False, args=(), kwargs=kargs
                )
                raise AbortProposeDownstream()
            raise ex
        finally:
            self.api.up.local_project.git_repo.head.reset(
                "HEAD", index=True, working_tree=True
            )

        return downstream_pr

    def _report_errors_for_each_branch(self, errors: Dict[str, str]) -> None:
        branch_errors = ""
        for branch, err in sorted(
            errors.items(), key=lambda branch_error: branch_error[0]
        ):
            err_without_new_lines = err.replace("\n", " ")
            branch_errors += f"| `{branch}` | `{err_without_new_lines}` |\n"

        msg_retrigger = MSG_RETRIGGER.format(
            job="update",
            command="propose-downstream",
            place="issue",
            packit_comment_command_prefix=self.service_config.comment_command_prefix,
        )
        body_msg = (
            f"Packit failed on creating pull-requests in dist-git:\n\n"
            f"| dist-git branch | error |\n"
            f"| --------------- | ----- |\n"
            f"{branch_errors}\n\n"
            f"{msg_retrigger}\n"
        )

        PackageConfigGetter.create_issue_if_needed(
            project=self.project,
            title=f"Propose downstream failed for release {self.data.tag_name}",
            message=body_msg,
            comment_to_existing=body_msg,
        )

    def _get_or_create_propose_downstream_run(self) -> ProposeDownstreamModel:
        if self._propose_downstream_run_id is not None:
            return ProposeDownstreamModel.get_by_id(self._propose_downstream_run_id)

        propose_downstream_model, _ = ProposeDownstreamModel.create_with_new_run(
            status=ProposeDownstreamStatus.running,
            trigger_model=self.data.db_trigger,
        )

        for branch in self.propose_downstream_helper.branches:
            propose_downstream_target = ProposeDownstreamTargetModel.create(
                status=ProposeDownstreamTargetStatus.queued, branch=branch
            )
            propose_downstream_model.propose_downstream_targets.append(
                propose_downstream_target
            )

        return propose_downstream_model

    def run(self) -> TaskResults:
        """
        Sync the upstream release to dist-git as a pull request.
        """
        # TODO use local project and api from BaseJobHelper when LocalProject refactored
        self.local_project = LocalProject(
            git_project=self.project,
            working_dir=self.service_config.command_handler_work_dir,
            cache=RepositoryCache(
                cache_path=self.service_config.repository_cache,
                add_new=self.service_config.add_repositories_to_repository_cache,
            )
            if self.service_config.repository_cache
            else None,
        )

        self.api = PackitAPI(
            self.service_config,
            self.job_config,
            self.local_project,
        )

        errors = {}
        propose_downstream_model = self._get_or_create_propose_downstream_run()
        branches_to_run = [
            target.branch
            for target in propose_downstream_model.propose_downstream_targets
        ]
        logger.debug(f"Branches to run propose downstream: {branches_to_run}")

        try:
            for model in propose_downstream_model.propose_downstream_targets:
                branch = model.branch
                # skip submitting a branch if we already did that (even if it failed)
                if model.status not in [
                    ProposeDownstreamTargetStatus.running,
                    ProposeDownstreamTargetStatus.retry,
                    ProposeDownstreamTargetStatus.queued,
                ]:
                    logger.debug(
                        f"Skipping propose downstream for branch {branch} "
                        f"that was already processed."
                    )
                    continue
                logger.debug(f"Running propose downstream for {branch}")
                model.set_status(status=ProposeDownstreamTargetStatus.running)
                url = get_propose_downstream_info_url(model.id)
                buffer, handler = gather_packit_logs_to_buffer(
                    logging_level=logging.DEBUG
                )

                try:
                    model.set_start_time(start_time=datetime.utcnow())
                    self.propose_downstream_helper.report_status_to_branch(
                        branch=branch,
                        description="Starting propose downstream...",
                        state=BaseCommitStatus.running,
                        url=url,
                    )
                    downstream_pr = self.sync_branch(
                        branch=branch, model=propose_downstream_model
                    )
                    logger.debug("Downstream PR created successfully.")
                    model.set_downstream_pr_url(downstream_pr_url=downstream_pr.url)
                    model.set_status(status=ProposeDownstreamTargetStatus.submitted)
                    self.propose_downstream_helper.report_status_to_branch(
                        branch=branch,
                        description="Propose downstream finished successfully.",
                        state=BaseCommitStatus.success,
                        url=url,
                    )
                except AbortProposeDownstream:
                    logger.debug(
                        "Propose downstream is being retried because "
                        "we were not able yet to download the archive. "
                    )
                    model.set_status(status=ProposeDownstreamTargetStatus.retry)
                    self.propose_downstream_helper.report_status_to_branch(
                        branch=branch,
                        description="Propose downstream is being retried because "
                        "we were not able yet to download the archive. ",
                        state=BaseCommitStatus.pending,
                        url=url,
                    )
                    return TaskResults(
                        success=True,  # do not create a Sentry issue
                        details={
                            "msg": "Not able to download archive. Task will be retried."
                        },
                    )
                except Exception as ex:
                    logger.debug(f"Propose downstream failed: {ex}")
                    # eat the exception and continue with the execution
                    model.set_status(status=ProposeDownstreamTargetStatus.error)
                    self.propose_downstream_helper.report_status_to_branch(
                        branch=branch,
                        description=f"Propose downstream failed: {ex}",
                        state=BaseCommitStatus.failure,
                        url=url,
                    )
                    errors[branch] = str(ex)
                    sentry_integration.send_to_sentry(ex)
                finally:
                    model.set_finished_time(finished_time=datetime.utcnow())
                    model.set_logs(collect_packit_logs(buffer=buffer, handler=handler))

        finally:
            # remove temporary dist-git clone after we're done here - context:
            # 1. the dist-git repo is cloned on worker, not sandbox
            # 2. it's stored in /tmp, not in the mirrored sandbox PV
            # 3. it's not being cleaned up and it wastes pod's filesystem space
            shutil.rmtree(self.api.dg.local_project.working_dir)

        if errors:
            self._report_errors_for_each_branch(errors)
            propose_downstream_model.set_status(status=ProposeDownstreamStatus.error)
            return TaskResults(
                success=False,
                details={"msg": "Propose downstream failed.", "errors": errors},
            )

        propose_downstream_model.set_status(status=ProposeDownstreamStatus.finished)
        return TaskResults(success=True, details={})


@configured_as(job_type=JobType.koji_build)
@run_for_comment(command="koji-build")
@reacts_to(event=PushPagureEvent)
@reacts_to(event=PullRequestCommentPagureEvent)
class DownstreamKojiBuildHandler(RetriableJobHandler):
    """
    This handler can submit a build in Koji from a dist-git.
    """

    topic = "org.fedoraproject.prod.git.receive"
    task_name = TaskName.downstream_koji_build

    def __init__(
        self,
        package_config: PackageConfig,
        job_config: JobConfig,
        event: dict,
        celery_task: Optional[Task] = None,
    ):
        super().__init__(
            package_config=package_config,
            job_config=job_config,
            event=event,
            celery_task=celery_task,
        )
        self.dg_branch = event.get("git_ref")
        self._pull_request: Optional[PullRequest] = None
        self._packit_api = None

    @property
    def pull_request(self):
        if not self._pull_request and self.data.event_dict["committer"] == "pagure":
            logger.debug(
                f"Getting pull request with head commit {self.data.commit_sha}"
                f"for repo {self.project.namespace}/{self.project.repo}"
            )
            prs = [
                pr
                for pr in self.project.get_pr_list(status=PRStatus.all)
                if pr.head_commit == self.data.commit_sha
            ]
            if prs:
                self._pull_request = prs[0]
        return self._pull_request

    @property
    def packit_api(self):
        if not self._packit_api:
            self._packit_api = PackitAPI(
                self.service_config,
                self.job_config,
                downstream_local_project=self.local_project,
            )
        return self._packit_api

    def get_pr_author(self):
        """Get the login of the author of the PR (if there is any corresponding PR)."""
        return self.pull_request.author if self.pull_request else None

    def is_packager(self, user):
        """Check that the given FAS user
        is a packager

        Args:
            user (str) FAS user account name
        Returns:
            true if a packager false otherwise
        """
        self.packit_api.init_kerberos_ticket()
        client = Client(FASJSON_URL)
        try:
            groups = client.list_user_groups(username=user)
        except APIError:
            logger.debug(f"Unable to get groups for user {user}.")
            return False
        return "packager" in [group["groupname"] for group in groups.result]

    def pre_check(self) -> bool:
        if self.data.event_type in (PushPagureEvent.__name__,):
            if self.data.git_ref not in (
                configured_branches := get_branches(
                    *self.job_config.dist_git_branches,
                    default="main",
                    with_aliases=True,
                )
            ):
                logger.info(
                    f"Skipping build on '{self.data.git_ref}'. "
                    f"Koji build configured only for '{configured_branches}'."
                )
                return False

            if self.data.event_dict["committer"] == "pagure":
                pr_author = self.get_pr_author()
                logger.debug(f"PR author: {pr_author}")
                if pr_author not in self.job_config.allowed_pr_authors:
                    logger.info(
                        f"Push event {self.data.identifier} with corresponding PR created by"
                        f" {pr_author} that is not allowed in project "
                        f"configuration: {self.job_config.allowed_pr_authors}."
                    )
                    return False
            else:
                committer = self.data.event_dict["committer"]
                logger.debug(f"Committer: {committer}")
                if committer not in self.job_config.allowed_committers:
                    logger.info(
                        f"Push event {self.data.identifier} done by "
                        f"{committer} that is not allowed in project "
                        f"configuration: {self.job_config.allowed_committers}."
                    )
                    return False
        elif self.data.event_type in (PullRequestCommentPagureEvent.__name__,):
            commenter = self.data.actor
            logger.debug(
                f"Triggering downstream koji build through comment by: {commenter}"
            )
            if not self.is_packager(commenter):
                logger.info(
                    f"koji-build retrigger comment event on PR identifier {self.data.pr_id} "
                    f"done by {commenter} which is not a packager."
                )
                return False

        return True

    def run(self) -> TaskResults:
        self.local_project = LocalProject(
            git_project=self.project,
            working_dir=self.service_config.command_handler_work_dir,
            cache=RepositoryCache(
                cache_path=self.service_config.repository_cache,
                add_new=self.service_config.add_repositories_to_repository_cache,
            )
            if self.service_config.repository_cache
            else None,
        )
        branch = (
            self.project.get_pr(self.data.pr_id).target_branch
            if self.data.event_type in (PullRequestCommentPagureEvent.__name__,)
            else self.dg_branch
        )
        try:
            self.packit_api.build(
                dist_git_branch=branch,
                scratch=self.job_config.scratch,
                nowait=True,
                from_upstream=False,
            )
        except PackitException as ex:
            if not self.job_config.issue_repository:
                logger.debug(
                    "No issue repository configured. "
                    "User will not be notified about the failure."
                )
                raise ex

            if self.celery_task and not self.celery_task.is_last_try():
                logger.debug(
                    "Celery task will be retried. User will not be notified about the failure."
                )
                raise ex

            logger.debug(
                f"Issue repository configured. We will create "
                f"a new issue in {self.job_config.issue_repository}"
                "or update the existing one."
            )

            issue_repo = self.service_config.get_project(
                url=self.job_config.issue_repository
            )
            body = f"Koji build on `{branch}` branch failed:\n" "```\n" f"{ex}\n" "```"
            PackageConfigGetter.create_issue_if_needed(
                project=issue_repo,
                title="Fedora Koji build failed to be triggered",
                message=body
                + f"\n\n*Get in [touch with us]({CONTACTS_URL}) if you need some help.*",
                comment_to_existing=body,
            )
            raise ex
        return TaskResults(success=True, details={})
