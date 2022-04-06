# Copyright Contributors to the Packit project.
# SPDX-License-Identifier: MIT

import logging
from typing import Optional, List, Set

from ogr.abstract import GitProject

from packit.config import JobType, PackageConfig, JobConfig
from packit.config.aliases import get_branches
from packit_service.config import ServiceConfig
from packit_service.models import AbstractTriggerDbType
from packit_service.trigger_mapping import are_job_types_same
from packit_service.worker.events import EventData
from packit_service.worker.helpers.job_helper import BaseJobHelper
from packit_service.worker.reporting import BaseCommitStatus

logger = logging.getLogger(__name__)


class ProposeDownstreamJobHelper(BaseJobHelper):
    job_type = JobType.propose_downstream
    status_name: str = "propose-downstream"

    def __init__(
        self,
        service_config: ServiceConfig,
        package_config: PackageConfig,
        project: GitProject,
        metadata: EventData,
        db_trigger: AbstractTriggerDbType,
        job_config: JobConfig,
        branches_override: Optional[Set[str]] = None,
    ):
        super().__init__(
            service_config=service_config,
            package_config=package_config,
            project=project,
            metadata=metadata,
            db_trigger=db_trigger,
            job_config=job_config,
        )
        self.branches_override = branches_override
        self.msg_retrigger: str = ""
        self._check_names: Optional[List[str]] = None
        self._default_distgit_branch: Optional[str] = None
        self._job: Optional[JobConfig] = None

    @classmethod
    def get_check_cls(cls, branch: str = None, identifier: Optional[str] = None) -> str:
        chroot_str = f":{branch}" if branch else ""
        optional_suffix = f":{identifier}" if identifier else ""
        return f"{cls.status_name}{chroot_str}{optional_suffix}"

    def get_check(self, branch: str = None) -> str:
        return self.get_check_cls(branch, identifier=self.job_config.identifier)

    def report_status_to_all(
        self,
        description: str,
        state: BaseCommitStatus,
        url: str = "",
        markdown_content: str = None,
    ) -> None:
        if self.job_type:
            self._report(
                description=description,
                state=state,
                url=url,
                check_names=self.check_names,
                markdown_content=markdown_content,
            )

    def report_status_to_branch(
        self,
        branch: str,
        description: str,
        state: BaseCommitStatus,
        url: str = "",
        markdown_content: str = None,
    ):
        if self.job and branch in self.branches:
            cs = self.get_check(branch)
            self._report(
                description=description,
                state=state,
                url=url,
                check_names=cs,
                markdown_content=markdown_content,
            )

    @property
    def check_names(self):
        if not self._check_names:
            self._check_names = [self.get_check(branch) for branch in self.branches]
        return self._check_names

    @property
    def default_dg_branch(self):
        if not self._default_distgit_branch:
            self._default_distgit_branch = (
                self.api.dg.local_project.git_project.default_branch
            )
        return self._default_distgit_branch

    @property
    def branches(self) -> Set[str]:
        """
        Return all valid branches from config.
        """
        branches = get_branches(
            *self.job.metadata.dist_git_branches, default=self.default_dg_branch
        )
        if self.branches_override:
            logger.debug(f"Branches override: {self.branches_override}")
            branches = branches & self.branches_override

        return branches

    @property
    def job(self) -> Optional[JobConfig]:
        """
        Check if there is JobConfig for propose downstream defined
        :return: JobConfig or None
        """
        if not self.job_type:
            return None
        if not self._job:
            for job in [self.job_config] + self.package_config.jobs:
                if are_job_types_same(job.type, self.job_type) and (
                    self.db_trigger
                    and self.db_trigger.job_config_trigger_type == job.trigger
                ):
                    self._job = job
                    break
        return self._job
